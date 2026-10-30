"""桌宠大脑桥接插件（方案 B · Phase 3）。

接管策略（v2）：
- 私聊：默认**全量接管**（private_always=true）。这样每一句都走桌宠大脑，
  人设「一次 1~2 句、不写小作文」和共享记忆才真正生效。
- 群聊：仅命中触发词（大肥鱼/桌宠）时接管，避免在群里刷屏。
- 白名单：allowed_user_ids 非空时，只有这些 QQ 号能触发，防止陌生人
  私聊小号时套出「关于用户的记忆」。
- 回复形态：大脑返回 replies[]（每行一条短句）。私聊**逐条发送**，条间随机停
  0.7~1.4 秒，像真人连着发微信；群聊合并成一条避免刷屏。
  注意：逐条发送时绝不能再用 set_result，否则 pipeline 会重复发一遍。
- 图片/非文本：插件把图片转 base64 交给大脑做多模态理解；取图失败才用短句兜底。

命中后把文本转发到本机桌宠大脑 HTTP 服务（pet/brain_server.py，
默认 127.0.0.1:8765），拿到回复后用 plain_result + stop_event 接管，
不再走 AstrBot 自带的 LLM。未接管的消息正常放行。

依赖：aiohttp（AstrBot 自带）。
"""
import asyncio
import random
import re

import aiohttp

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.event_message_type import EventMessageType

# 只用于判断「剥离后是否还剩实质内容」：空白 + 中英文标点
_PUNCT_ONLY = re.compile(r"[\s，,。.！!？?～~、:：；;·…—\-]")

# 图片消息的短句回应（大脑暂无视觉，不装看得见）
_IMAGE_REPLIES = (
    "欸？图？……我这儿打不开，你说说是什么。",
    "图片我看不见诶，形容一下？",
    "唔，图我瞅不着。……你直接说嘛。",
)


_MAX_IMAGES = 3                          # 一轮最多带 3 张图
_MAX_IMAGE_B64 = 6 * 1024 * 1024         # 单张 base64 上限（约 4.5MB 原图）


def _as_bool(v, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on", "y")


class PetBrainBridge(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config

    # ---- 配置项（AstrBot 后台「插件 → 桌宠大脑桥接 → 配置」里可改）----
    @property
    def brain_url(self) -> str:
        return (self.conf.get("brain_url") or "http://127.0.0.1:8765").rstrip("/")

    @property
    def brain_token(self) -> str:
        return str(self.conf.get("brain_token") or "")

    @property
    def triggers(self) -> list:
        raw = str(self.conf.get("triggers") or "大肥鱼,桌宠")
        return [t.strip() for t in raw.split(",") if t.strip()]

    @property
    def timeout(self) -> int:
        try:
            return int(self.conf.get("timeout") or 60)
        except (TypeError, ValueError):
            return 60

    @property
    def private_always(self) -> bool:
        """私聊是否全量接管（不依赖触发词）。"""
        return _as_bool(self.conf.get("private_always"), default=True)

    @property
    def allowed_user_ids(self) -> set:
        raw = str(self.conf.get("allowed_user_ids") or "").strip()
        return {x.strip() for x in raw.split(",") if x.strip()}

    @property
    def bubble_gap(self) -> tuple:
        """多条气泡之间的停顿区间（秒）。像真人打字，不是被一刀切成几条。"""
        def _f(key, default):
            try:
                return float(self.conf.get(key) or default)
            except (TypeError, ValueError):
                return default

        lo = _f("bubble_gap_min", 0.7)
        hi = _f("bubble_gap_max", 1.4)
        if hi < lo:
            lo, hi = hi, lo
        return max(0.0, lo), max(0.0, hi)

    async def _ask_brain(self, text: str, conversation_key: str, images=None) -> dict | None:
        url = f"{self.brain_url}/v1/chat"
        headers = {
            "Content-Type": "application/json",
            "X-Pet-Token": self.brain_token,
        }
        payload = {
            "text": text,
            "conversation_key": conversation_key,
            "want_audio": False,
        }
        if images:
            payload["images"] = images
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, headers=headers, json=payload,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error("[pet_brain] /v1/chat HTTP %s: %s", resp.status, body[:200])
                        return None
                    return await resp.json()
        except asyncio.TimeoutError:
            logger.error("[pet_brain] 请求大脑服务超时")
            return None
        except Exception as e:  # noqa: BLE001
            logger.error("[pet_brain] 请求大脑服务失败: %r", e)
            return None

    async def _send_replies(self, event: AstrMessageEvent, bubbles: list, is_private: bool) -> int:
        """按心跳式节奏发送多条气泡。

        bubbles: [{"text": ..., "pause": 0.0}, ...]，pause 表示这一条之前额外静默的秒数
        （「……」独占一行时由大脑折算成约 2 秒，连写两行就是 4 秒，用来演赌气/犹豫）。

        - 私聊多条：逐条 event.send()，条间随机停顿（0.7~1.4s）+ pause，像真人连发。
          注意绝不能再用 set_result，否则 pipeline 会把同一条再发一遍。
        - 群聊：合并成一条，避免刷屏。
        - 单条：走 set_result（最稳，也少一次平台调用）。
        """
        items = []
        for b in bubbles or []:
            if isinstance(b, dict):
                text = str(b.get("text") or "").strip()
                try:
                    pause = max(0.0, float(b.get("pause") or 0))
                except (TypeError, ValueError):
                    pause = 0.0
            else:
                text, pause = str(b or "").strip(), 0.0
            if text:
                items.append((text, pause))
        if not items:
            return 0

        if len(items) == 1:
            text, pause = items[0]
            if pause > 0:
                await asyncio.sleep(pause)  # 先沉默再开口（「……」开头的那种）
            event.set_result(event.plain_result(text))
            event.stop_event()
            return 1

        if not is_private:
            event.set_result(event.plain_result("".join(t for t, _ in items)))
            event.stop_event()
            return 1

        lo, hi = self.bubble_gap
        sent = 0
        for i, (text, pause) in enumerate(items):
            gap = (0.0 if i == 0 else random.uniform(lo, hi)) + pause
            if gap > 0:
                await asyncio.sleep(gap)
            try:
                await event.send(MessageChain(chain=[Plain(text)]))
                sent += 1
            except Exception as e:  # noqa: BLE001
                logger.error("[pet_brain] 第 %d 条发送失败: %r", i + 1, e)

        if sent == 0:
            # 一条都没发出去，退回单条集中发送，至少别让她彻底沉默
            event.set_result(event.plain_result("".join(t for t, _ in items)))
        event.stop_event()
        return sent

    def _strip_trigger(self, text: str) -> str:
        """去掉「大肥鱼」「桌宠」这类称呼，避免她把自己名字听进去。

        注意保留语义：
        - 「大肥鱼」/「大肥鱼？」（纯呼唤）→ 转成「在吗」
        - 「？？」（纯标点，不含称呼）→ 原样透传，让她的反应自然
        - 「大肥鱼，你在干嘛」→「你在干嘛」
        """
        out = text
        for t in self.triggers:
            out = out.replace(t, "")
        out = out.strip()
        if _PUNCT_ONLY.sub("", out):
            # 还剩实质内容
            if any(t in text for t in self.triggers):
                return out.lstrip(" \t\r\n，,。.！!～~、:：；;") or "在吗"
            return out
        # 剥离后只剩标点/空白
        if any(t in text for t in self.triggers):
            return "在吗"
        return out or text

    @staticmethod
    def _guess_mime(seg) -> str:
        src = str(
            getattr(seg, "path", "") or getattr(seg, "file", "") or getattr(seg, "url", "") or ""
        )
        src = src.split("?")[0].lower()
        for ext, mime in (
            (".png", "image/png"),
            (".gif", "image/gif"),
            (".webp", "image/webp"),
            (".bmp", "image/bmp"),
        ):
            if src.endswith(ext):
                return mime
        return "image/jpeg"

    async def _extract_images(self, event: AstrMessageEvent) -> list:
        """把消息里的图片转成 data URI 列表，交给大脑做多模态理解。"""
        try:
            segs = event.get_messages() or []
        except Exception:  # noqa: BLE001
            return []
        out = []
        for s in segs:
            if "image" not in type(s).__name__.lower():
                continue
            if len(out) >= _MAX_IMAGES:
                break
            try:
                b64 = await s.convert_to_base64()
            except Exception as e:  # noqa: BLE001
                logger.warning("[pet_brain] 图片转 base64 失败: %r", e)
                continue
            if not b64:
                continue
            if len(b64) > _MAX_IMAGE_B64:
                logger.warning("[pet_brain] 图片过大已跳过（%d 字符）", len(b64))
                continue
            out.append(f"data:{self._guess_mime(s)};base64,{b64}")
        return out

    def _has_media(self, event: AstrMessageEvent) -> bool:
        """消息链里是否有图片/表情/语音/视频/文件等非文本内容。"""
        try:
            segs = event.get_messages() or []
        except Exception:  # noqa: BLE001
            return False
        kinds = ("image", "face", "record", "video", "file")
        return any(
            any(k in type(s).__name__.lower() for k in kinds) for s in segs
        )

    def _reply_non_text(self, event: AstrMessageEvent, is_private: bool) -> None:
        """图片/表情等非文本消息：由「她」用短句回应，不走 AstrBot 通用人格。"""
        if not (is_private and self.private_always):
            return
        if not self._has_media(event):
            return  # 真没内容（撤回了/空消息），不插话
        event.set_result(event.plain_result(random.choice(_IMAGE_REPLIES)))
        event.stop_event()
        logger.info("[pet_brain] 非文本消息，用短句回应（大脑暂无视觉）")

    @filter.event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()

        if text.startswith("/"):
            return  # 命令类消息交给 AstrBot 自己的命令系统

        # 白名单：防止陌生人私聊小号时把「关于用户的记忆」套出去
        allow = self.allowed_user_ids
        if allow:
            try:
                sender_id = event.get_sender_id()
            except Exception:  # noqa: BLE001
                sender_id = ""
            if sender_id not in allow:
                return

        try:
            is_private = event.is_private_chat()
        except Exception:  # noqa: BLE001
            is_private = False

        images = await self._extract_images(event)

        if not text and not images:
            if self._has_media(event):
                # 有媒体但没取到图（转换失败 / 表情 / 语音等）：短句兜底
                self._reply_non_text(event, is_private)
            return

        triggered = any(t in text for t in self.triggers)
        if not triggered and not (is_private and self.private_always):
            return  # 群聊且未点名：放行给 AstrBot 默认处理

        clean = self._strip_trigger(text) if text else "（发来一张图片）"
        # unified_msg_origin 形如 小鱼鱼:FriendMessage:12345 / 小鱼鱼:GroupMessage:xxx
        # 用作大脑服务的会话 key，保证同一 QQ 会话共享记忆
        conversation_key = event.unified_msg_origin
        logger.info(
            "[pet_brain] 接管(%s)，转大脑：%s %r（图 %d 张）",
            "私聊" if is_private else "群聊", conversation_key, clean[:40], len(images),
        )

        data = await self._ask_brain(clean, conversation_key, images)
        bubbles = []
        if data:
            bubbles = data.get("bubbles") or []
            if not bubbles:  # 旧版大脑只给 replies
                bubbles = [{"text": str(x).strip()} for x in (data.get("replies") or []) if str(x).strip()]
            if not bubbles:
                one = str(data.get("reply") or "").strip()
                bubbles = [{"text": one}] if one else []
        if not bubbles:
            # 大脑没返回（服务没开 / 出错）。这里**不放行**给 AstrBot 默认链路，
            # 否则会突然变回「通用助手」写小作文，人格断裂。改为回一句短提示，
            # 让人一眼看出是大脑服务没开。
            logger.warning("[pet_brain] 大脑无回复，回退到短提示（请检查 run_brain_server.bat）")
            event.set_result(event.plain_result("唔…我这边卡了一下，你再说一遍？"))
            event.stop_event()
            return

        n = await self._send_replies(event, bubbles, is_private)
        logger.info(
            "[pet_brain] 已用大脑回复（%d 条 / 共 %d 字），接管完成",
            n, sum(len(b.get("text", "")) for b in bubbles if isinstance(b, dict)),
        )
