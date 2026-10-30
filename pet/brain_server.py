# -*- coding: utf-8 -*-
"""桌宠本地 HTTP「大脑」（方案 B 的 Phase 2）。

让桌宠的「人格 + 记忆 + 会话 + 语音」通过 127.0.0.1 暴露给 AstrBot 插件：
QQ 收到的消息经插件 POST 到 /v1/chat，由本服务用桌宠同一套 PromptBuilder /
SessionStore / ChatMemory / OpenAICompatibleProvider 生成回复，并落盘到与桌面
完全相同的 sessions/<character_id>/ 目录——所以 QQ 聊出的记忆，桌面大肥鱼能召回。

设计要点：
- 复用而非重写：直接 import pet.chat.* 的纯逻辑层（不依赖 Qt 事件循环）。
- 与桌宠共享 config_dir（%APPDATA%/dsh-pet-standalone）+ instance_id="" → 同一套
  sessions/memory 文件；多进程写盘已由 memory.py 的跨进程 QLockFile 锁保护。
- 仅绑回环地址 + X-Pet-Token 鉴权，外部不可达。

启动（必须用桌宠的 venv，里面有 PySide6 / keyring 等依赖）：
    D:/dshjingyu/dsh-pet-indesktop/.venv/Scripts/python.exe pet/brain_server.py
环境变量（可选）：
    PET_BRAIN_HOST       默认 127.0.0.1
    PET_BRAIN_PORT       默认 8765
    PET_BRAIN_TOKEN      鉴权 token；不填则首次启动随机生成并打印（需抄到 AstrBot 插件）
    PET_BRAIN_CHARACTER  默认 dafeiyu（大肥鱼，决定与桌面共享记忆的角色）
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import secrets
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# ---- 把项目根加进 sys.path，保证 `import pet` 可用 ----
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("pet-brain-server")

# ---- 配置（环境变量优先）----
HOST = os.environ.get("PET_BRAIN_HOST", "127.0.0.1")
PORT = int(os.environ.get("PET_BRAIN_PORT", "8765"))
CHARACTER_ID = os.environ.get("PET_BRAIN_CHARACTER", "dafeiyu")
TOKEN = os.environ.get("PET_BRAIN_TOKEN", "") or ""
# 请求体上限：带 base64 图片时体积较大，但也要防滥用
MAX_BODY_BYTES = int(os.environ.get("PET_BRAIN_MAX_BODY", str(24 * 1024 * 1024)))

# conversation_key → session_id 的稳定命名空间（固定值，保证同一 QQ 会话恒定）
_SESSION_NS = uuid.UUID("d5e6f7a8-1234-5678-9abc-def012345678")

# 每会话串行锁：同一 QQ 会话不并发生成（不同会话并行无妨）
_session_locks: dict[str, threading.Lock] = {}
_session_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _session_locks_guard:
        lock = _session_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _session_locks[key] = lock
        return lock


def _load_runtime():
    """加载桌宠配置 + 探明角色 manifest / 记忆路径。返回 dict。"""
    from pet.config import Config

    cfg = Config()  # 默认指向 %APPDATA%/dsh-pet-standalone/config.json
    # 角色 manifest 目录探测（用于加载大肥鱼 persona；找不到就退回全局人设）
    candidates = [
        ROOT / "assets" / "characters",
        Path(str(cfg.dir)) / "characters",
    ]
    characters_root = None
    for c in candidates:
        if (c / CHARACTER_ID / "manifest.json").is_file():
            characters_root = c
            break
    return {"cfg": cfg, "characters_root": characters_root}


_RUNTIME = None


def runtime():
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = _load_runtime()
    return _RUNTIME


def _active_provider():
    """返回带 api_key 补全的 active ProviderConfig（及其所属 ChatSettings）。"""
    from pet.chat.models import SecretStore

    cfg = runtime()["cfg"]
    settings = cfg.chat_settings()
    provider = settings.active_config  # 已挂 web_search
    if not provider.api_key:
        provider.api_key = SecretStore().get(provider.api_key_ref)
    return settings, provider


def synth_audio(text: str):
    """用桌宠当前 TTS 后端合成语音；失败返回 None（不影响文字回复）。"""
    try:
        import pet.tts  # noqa: F401 —— 触发所有 provider 注册
        from pet.tts import get as get_tts
        from pet.tts_secrets import get as get_secret

        cfg = runtime()["cfg"]
        backend = str(cfg.data.get("voice_chime_tts_backend", "edge")).strip().lower()
        provider = get_tts(backend)
        if provider is None:
            log.warning("TTS 后端未注册：%s", backend)
            return None
        values = provider.values(cfg.data)
        # 注入钥匙串里的 secret（api_key 类字段不进 config.json）
        for spec in provider.fields:
            if getattr(spec, "kind", None) == "secret" and getattr(spec, "secret_ref", None):
                values[spec.name] = get_secret(spec.secret_ref)
        plan = provider.plan(values)
        cache_dir = Path(str(cfg.dir)) / "brain_tts_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        out_path = cache_dir / f"{uuid.uuid4().hex}.{plan.get('ext', 'mp3')}"
        provider.synth(text, plan, out_path)
        if out_path.is_file():
            return str(out_path)
        return None
    except Exception:
        log.exception("TTS 合成失败（已忽略，仅返回文字）")
        return None


# ---- 分条节奏 ----
# 「一次回 1~4 条、一条一句、反问与「欸对了」独占一条、单独一行「……」= 两秒沉默、
# 连续敷衍时压回一条」这些**人设规则已写进 assets/characters/dafeiyu/manifest.json**，
# 桌面端与 QQ 共用同一份，这里不再追加渠道提示，避免两份规则打架。

# 切超长单行：只在句末标点处断，避免把一句话从中间劈开
_SENT_END = "。！？…!?～~"
_LINE_PREFIX = re.compile(r"^\s*(?:[-—*•·>]+|\d+[.、)）]|[（(]\d+[）)])\s*")
# 整行被一对引号包住时才去掉（「还行」→ 还行）；句中引号是她的语气，不能碰，
# 否则「又「还行」」会被削成「又「还行」
_WRAP_PAIRS = (("「", "」"), ("『", "』"), ("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'"))


def _strip_wrap_quotes(s: str) -> str:
    s = s.strip()
    changed = True
    while changed and len(s) >= 2:
        changed = False
        for a, b in _WRAP_PAIRS:
            if s.startswith(a) and s.endswith(b):
                s = s[len(a): -len(b)].strip()
                changed = True
                break
    return s

_BARE_PREFIX = re.compile(r"^(?:你|他|我)\s*[:：]\s*")
# 这些单字本身就是一句完整的反应，不该被并进上一条
_STANDALONE = set("唔欸哼嗯哦啊诶喂哈好不看在谁别对呀咦唉")
# 整行只有省略号 =「她停了一下再说」，转成沉默，不真的发出去
_PAUSE_LINE = re.compile(r"^[…⋯\.．·]{2,6}$")
_TAIL_PERIOD = re.compile(r"(?<![。])。$")
# 跳话题标记：这两个必须独占一条
_TOPIC_MARKS = ("欸对了", "诶对了")
# 敷衍词：连续出现说明他在应付，这时不该连发
_DULL_WORDS = {
    "嗯", "哦", "嗯嗯", "哦哦", "哦嗯", "随便", "都行", "行", "行吧", "好", "好哦",
    "不知道", "无所谓", "是吗", "呵", "呃", "额", "嗯哼", "随你",
}


def _strip_tail_period(s: str) -> str:
    """句末不打句号（微信感）；「。。」这种刻意的保留。"""
    return _TAIL_PERIOD.sub("", s).strip()


def _split_before_topic(line: str) -> list:
    """「欸对了」必须独占一条：无论出现在句首还是句中，都把后面的新话题推到下一条。"""
    for mark in _TOPIC_MARKS:
        i = line.find(mark)
        if i < 0:
            continue
        if i == 0:
            rest = line[len(mark):].strip(" ，,。、,.!！~～")
            if len(rest) >= 2:
                return [mark, rest]
            return [line]
        head = line[:i].strip(" ，,。、")
        tail = line[i:].strip()
        if head and len(tail) >= 3:
            # 「算了不问了欸对了楼下那只猫」→ 还得把 tail 里的「欸对了」再摘出来
            rest = tail[len(mark):].strip(" ，,。、,.!！~～")
            return [head, mark, rest] if len(rest) >= 2 else [head, tail]
    return [line]


def _split_at_question(line: str) -> list:
    """反问单独一条：问号后面还接了正经内容，就断开。

    「又改？产品经理拿你充电吧」->「又改？」+「产品经理拿你充电吧」
    「你不睡？」-> 不动（问号在末尾）
    """
    for m in re.finditer(r"[？?]+", line):
        if len(line) - m.end() >= 3:
            head = line[: m.end()].strip()
            tail = line[m.end():].strip()
            if head and tail:
                return [head, tail]
            break
    return [line]


def _split_long(line: str, limit: int = 22) -> list:
    """单行太长（模型没换行、一口气写了三句）时，按句末标点切成多条。"""
    if len(line) <= limit:
        return [line]
    out, buf = [], ""
    for ch in line:
        buf += ch
        if ch in _SENT_END and len(buf) >= 6:
            out.append(buf.strip())
            buf = ""
        elif len(buf) >= limit:
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    # 尾巴只剩一两个字（例如"…吧"）就并回上一条，避免出现"吧"这种孤儿气泡
    if len(out) >= 2 and len(out[-1]) <= 2:
        out[-2] += out[-1]
        out.pop()
    return out or [line]


def _is_dull_text(raw: str) -> bool:
    body = re.sub(r"[\s，。,.!！?？~～…]", "", raw or "")
    return bool(body) and len(body) <= 3 and body in _DULL_WORDS


def _dull_streak(messages: list, current_text: str = "") -> int:
    """他连续敷衍了几次（只回「嗯」「哦」「随便」）。决定还要不要连发。

    必须把**当前这句**也算进去：之前只看历史，导致「嗯」之后再说「随便」时
    只数到 1 次，永远压不回一条。
    """
    if current_text and not _is_dull_text(current_text):
        return 0  # 这次说了正经话，不再算敷衍
    streak = 1 if current_text else 0
    for m in reversed(messages or []):
        if (getattr(m, "role", "") or "") != "user":
            continue
        if _is_dull_text(getattr(m, "content", "") or ""):
            streak += 1
        else:
            break
    return streak


def _split_replies(text: str, max_parts: int = 4) -> list:
    """把模型输出拆成"一条一条要发出去的消息"。

    返回 [{"text": 消息, "pause": 发这条之前额外静默几秒}, ...]。
    pause 由"整行只有省略号"产生 —— 用来演出赌气时那种 4 秒的停顿。

    容错链：去项目符号/序号/引号/"你："前缀 →「欸对了」独占 → 反问处断开
    → 单行过长二次切 → 单字碎片合并 → 超条数收敛 → 句末去句号。
    """
    raw = (text or "").replace("\r", "").strip()
    if not raw:
        return []

    # 1) 逐行清洗，整行省略号标记成停顿
    seq = []  # [("text", s) | ("pause", s)]
    for ln in raw.split("\n"):
        s = _LINE_PREFIX.sub("", ln.strip())
        s = _BARE_PREFIX.sub("", s)
        s = _strip_wrap_quotes(s)
        if not s:
            continue
        if _PAUSE_LINE.match(s):
            seq.append(("pause", s))
            continue
        for part in _split_before_topic(s):
            for piece in _split_at_question(part):
                for sub in _split_long(piece):
                    if sub:
                        seq.append(("text", sub))
    if not seq:
        seq = [("text", raw)]

    # 2) 单字碎片（"吧"这类）并到上一条；语气词（唔/欸/哼）保留独立
    merged = []
    for kind, s in seq:
        if (
            kind == "text" and merged and merged[-1][0] == "text"
            and len(s) == 1 and s not in _STANDALONE
            and len(merged[-1][1]) + 1 <= 25
        ):
            merged[-1] = ("text", merged[-1][1] + s)
        else:
            merged.append((kind, s))

    # 3) 超条数收敛
    idx = [i for i, (k, _) in enumerate(merged) if k == "text"]
    if len(idx) > max_parts:
        if max_parts <= 1:
            # 免打扰：压回一条，取第一句；太短就把第二句并上（补个逗号，别黏成"唔行吧"）
            first = merged[idx[0]][1]
            if len(first) <= 6 and len(idx) > 1:
                sep = "" if first[-1:] in "…，,。！？?～" else "，"
                first = first + sep + merged[idx[1]][1]
            # 压回一条也必须短：太长就只留第一个分句（「那我不吵你了」而不是一整段）
            if len(first) > 22:
                first = re.split(r"[，,。！？?…～]", first)[0].strip() or first[:22]
            merged = [("text", first)]
        else:
            tail = "".join(merged[i][1] for i in idx[max_parts - 1:])
            merged = [merged[i] for i in idx[: max_parts - 1]] + [("text", tail)]

    # 4) 组装气泡：把停顿折进"下一条之前等多久"
    bubbles, pending = [], 0.0
    for kind, s in merged:
        if kind == "pause":
            pending += random.uniform(1.9, 2.1)  # 「……」= 约 2 秒沉默
            continue
        bubbles.append({"text": _strip_tail_period(s), "pause": round(pending, 2)})
        pending = 0.0
    return [b for b in bubbles if b["text"]]


def _attach_images(messages: list, images: list) -> list:
    """把图片挂到最后一条 user 消息上（OpenAI 多模态 content 数组）。

    images 元素可以是 http(s) URL、data URI，或裸 base64（自动补 jpeg 前缀）。
    实测 deepseek-v4-flash 已支持视觉（prompt_tokens 会包含图片），故无需换模型。
    """
    urls = []
    for raw in images[:4]:  # 一轮最多 4 张，避免请求体过大
        s = str(raw or "").strip()
        if not s:
            continue
        if s.startswith("http://") or s.startswith("https://") or s.startswith("data:"):
            urls.append(s)
        else:
            urls.append("data:image/jpeg;base64," + s)
    if not urls:
        return messages

    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") != "user":
            continue
        txt = messages[i].get("content")
        if not isinstance(txt, str):
            txt = str(txt)
        parts = [{"type": "text", "text": txt}]
        for u in urls:
            parts.append({"type": "image_url", "image_url": {"url": u}})
        messages[i]["content"] = parts
        log.info("已挂载 %d 张图片到 user 消息（多模态）", len(urls))
        break
    return messages


def generate(text: str, conversation_key: str, want_audio: bool = False, images=None):
    """核心：生成回复 + 落盘 + 记忆提炼 + 可选语音。返回 (reply, audio_path, session_id)。

    images: 图片列表，元素可为 http(s) URL、data URI 或裸 base64。
    """
    from pet.chat.models import ChatMessage
    from pet.chat.prompt import PromptBuilder
    from pet.chat.session_store import SessionStore
    from pet.chat.memory import memory_path, ChatMemory, memory_payload, render_injection
    from pet.chat.memory_extract import schedule_extraction
    from pet.chat.providers import OpenAICompatibleProvider, StreamEvent

    images = [str(x).strip() for x in (images or []) if str(x).strip()][:4]
    if not (text and text.strip()) and not images:
        raise ValueError("text 与 images 不能同时为空")
    text = (text or "").strip() or "（发来一张图片）"

    rt = runtime()
    cfg = rt["cfg"]
    characters_root = rt["characters_root"]
    # 关键：用 _active_provider() 拿到已补全 api_key 的 provider，
    # 否则 settings.active_config 的 api_key 为空 -> LLM 401。
    settings, ac = _active_provider()
    config_dir = str(cfg.dir)

    store = SessionStore(config_dir, "")  # 与桌宠主进程同 instance_id（""）
    session_id = str(uuid.uuid5(_SESSION_NS, conversation_key))
    character_id = CHARACTER_ID

    session = store.load(session_id, character_id)
    if session is None:
        session = store.create(character_id, ac.provider_id,
                               settings.default_system_prompt)
        # 关键：ChatSession.create() 内部用 uuid4().hex 生成 session_id，必须换成
        # 由 conversation_key 派生的稳定 id。否则每次请求都会新建一个会话文件，
        # 历史永远不累积（表现为"她记不住上一句"、敷衍检测永不触发、记忆提炼拿不到上下文）。
        session.session_id = session_id
        store.save(session)  # 先落盘空会话，append_messages 才能读到

    mem_path = memory_path(config_dir, "", character_id)
    memory_enabled = bool(cfg.data.get("chat_memory_enabled", True))
    mp = memory_payload(ChatMemory(mem_path), memory_enabled)

    # 记忆注入：始终把已存的全部事实/偏好（render_injection，按重要度排序、≤1200 字）
    # 注入 system。QQ 与桌面不共享"当前会话上下文"，这里用完整记忆列表补偿，保证 QQ
    # 从第一条消息起就"知道她"；记忆仍只在 memory.json 一份（与桌面大肥鱼共享）。
    memory_text = ""
    if memory_enabled and mp.entries:
        try:
            memory_text = render_injection(mp.entries)
        except Exception:
            log.exception("记忆渲染失败（已忽略）")
    if not memory_text:
        memory_text = mp.text or ""

    builder = PromptBuilder(characters_root)
    # 他连续敷衍几次？连续敷衍时"分条"是特权，不是义务，要压回一条。
    # 注意：必须在 append_messages 之前取，这里 session.messages 还是纯历史。
    dull_streak = _dull_streak(session.messages, current_text=text)
    messages = builder.build_messages(
        settings, character_id, session.messages, text, memory_text=memory_text,
    )
    messages = _attach_images(messages, images)

    cancel = threading.Event()
    provider_inst = OpenAICompatibleProvider()
    log.info("LLM 请求：model=%s base=%s api_key长度=%d", ac.model, ac.base_url, len(ac.api_key or ''))
    parts = []
    for chunk in provider_inst.stream(messages, ac, cancel):
        if isinstance(chunk, StreamEvent):
            continue
        parts.append(chunk)
    reply = "".join(parts).strip()
    if not reply:
        raise RuntimeError("模型未返回任何内容")

    user_msg = ChatMessage("user", f"{text}（含图片）" if images else text)
    assistant_msg = ChatMessage("assistant", reply)
    updated, _ = store.append_messages(session, [user_msg, assistant_msg])
    if updated is not None:
        session = updated

    # 记忆提炼（后台线程，静默；失败只写日志，不影响回复）
    try:
        schedule_extraction(
            config=cfg.data, memory_path=mem_path,
            user_text=text, reply_text=reply,
            provider=provider_inst, provider_config=ac,
        )
    except Exception:
        log.exception("记忆提炼调度失败（已忽略）")

    audio_path = synth_audio(reply) if want_audio else None
    max_bubbles = 1 if dull_streak >= 2 else 4
    if dull_streak >= 2:
        log.info("他连续敷衍 %d 次 → 本轮压回一条", dull_streak)
    bubbles = _split_replies(reply, max_parts=max_bubbles)
    log.info(
        "分条结果：%d 条 字数=%s 停顿=%s",
        len(bubbles), [len(b["text"]) for b in bubbles],
        [b["pause"] for b in bubbles],
    )
    return reply, audio_path, session_id, bubbles


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PetBrain/1.0"

    def log_message(self, fmt, *args):
        log.info("[http] " + fmt, *args)

    def _auth_ok(self) -> bool:
        if not TOKEN:
            return True  # 未配置 token 时放行（仅绑回环，开发期便利）
        return self.headers.get("X-Pet-Token", "") == TOKEN

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:  # 含 base64 图片时请求体会变大
            log.warning("请求体过大：%d 字节（上限 %d）", length, MAX_BODY_BYTES)
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/v1/health":
            if not self._auth_ok():
                self._send_json({"error": "unauthorized"}, 401)
                return
            rt = runtime()
            settings, provider = _active_provider()
            self._send_json({
                "status": "ok",
                "role": CHARACTER_ID,
                "model": provider.model,
                "base_url": provider.base_url,
                "tts_backend": rt["cfg"].data.get("voice_chime_tts_backend", "edge"),
                "character_id": CHARACTER_ID,
                "has_character_manifest": rt["characters_root"] is not None,
                "version": "1.0",
            })
            return
        if parsed.path == "/v1/memory":
            if not self._auth_ok():
                self._send_json({"error": "unauthorized"}, 401)
                return
            self._handle_memory()
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if not self._auth_ok():
            self._send_json({"error": "unauthorized"}, 401)
            return
        if parsed.path == "/v1/chat":
            self._handle_chat()
        elif parsed.path == "/v1/tts":
            self._handle_tts()
        elif parsed.path == "/v1/memory":
            self._handle_memory()
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_chat(self):
        data = self._read_json()
        text = str(data.get("text", "")).strip()
        conversation_key = str(
            data.get("conversation_key") or data.get("session_key") or "default"
        ).strip()
        want_audio = bool(data.get("want_audio", False))
        images = data.get("images") or []
        if isinstance(images, str):
            images = [images]
        if not isinstance(images, list):
            images = []
        if not text and not images:
            self._send_json({"error": "text 与 images 至少提供一个"}, 400)
            return
        lock = _lock_for(conversation_key)
        with lock:
            t0 = time.monotonic()
            try:
                reply, audio_path, session_id, bubbles = generate(
                    text, conversation_key, want_audio, images=images,
                )
                elapsed = int((time.monotonic() - t0) * 1000)
                self._send_json({
                    "reply": reply,
                    "replies": [b["text"] for b in bubbles],  # 兼容只认 replies 的旧插件
                    "bubbles": bubbles,                       # 带停顿信息，新插件用它
                    "audio_path": audio_path,
                    "session_id": session_id,
                    "character_id": CHARACTER_ID,
                    "elapsed_ms": elapsed,
                })
            except Exception as exc:
                log.exception("生成失败")
                self._send_json({"error": str(exc)}, 200)

    def _handle_tts(self):
        data = self._read_json()
        text = str(data.get("text", "")).strip()
        if not text:
            self._send_json({"error": "text 必填"}, 400)
            return
        audio_path = synth_audio(text)
        if audio_path:
            self._send_json({"audio_path": audio_path})
        else:
            self._send_json({"error": "TTS 合成失败"}, 200)

    def _handle_memory(self):
        from collections import Counter

        from pet.chat.memory import ChatMemory, memory_path, render_injection

        rt = runtime()
        cfg = rt["cfg"]
        memory_enabled = bool(cfg.data.get("chat_memory_enabled", True))
        mem_path = memory_path(str(cfg.dir), "", CHARACTER_ID)
        entries = ChatMemory(mem_path).load() if memory_enabled else []
        layers = dict(Counter(e.get("layer", "?") for e in entries))
        # render_injection 覆盖全部层（user/fact/lesson/topic），不再只取 user 快照；
        # 自动提炼产出的是 fact 层，旧 injection_text 只看 user 层会误显示为空。
        mem_text = render_injection(entries) if entries else ""
        self._send_json({
            "character_id": CHARACTER_ID,
            "memory_enabled": memory_enabled,
            "entry_count": len(entries),
            "layers": layers,
            "memory_text": mem_text,
        })


class _BrainServer(ThreadingHTTPServer):
    """监听器：Windows 上禁用端口复用。

    ThreadingHTTPServer 默认 allow_reuse_address=1，Windows 下会设置 SO_REUSEADDR，
    使得**两个进程可以同时监听同一端口**：旧窗口没关时，新进程也能"启动成功"，
    但请求被随机分给旧进程——表现为"改了代码却没生效"（例如图片参数被旧逻辑丢掉）。
    这里在 Windows 上禁用复用，重复启动会直接报端口占用，问题立刻可见。
    """

    daemon_threads = True
    allow_reuse_address = os.name != "nt"


def main():
    global TOKEN
    import logging.handlers

    handlers = [logging.StreamHandler()]  # 控制台（bat 窗口里能看见）
    try:
        # 同时落盘，便于事后排查（例如确认「已挂载 N 张图片」是否真的执行）
        handlers.append(
            logging.handlers.RotatingFileHandler(
                ROOT / "brain_server.log", maxBytes=2 * 1024 * 1024,
                backupCount=2, encoding="utf-8",
            )
        )
    except OSError:
        pass  # 写不了日志文件不影响服务
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    # 预加载配置（把角色 manifest / TTS 后端等打进启动日志；失败立即可见）
    try:
        rt = runtime()
        cfg = rt["cfg"]
        settings, provider = _active_provider()
    except Exception:
        log.exception("加载桌宠配置失败（请确认用桌宠的 .venv 运行，且 config.json 可读）")
        return

    if not TOKEN:
        TOKEN = secrets.token_hex(16)
        log.warning("未设置 PET_BRAIN_TOKEN，已生成随机 token（仅本次会话有效）：%s", TOKEN)
        log.warning("请在 AstrBot 插件里配置同样的 PET_BRAIN_TOKEN；或固定用环境变量。")

    log.info("桌宠大脑服务器 启动：%s:%d", HOST, PORT)
    log.info("config_dir = %s", cfg.dir)
    log.info("character_id = %s（记忆共享角色）", CHARACTER_ID)
    log.info("active model = %s @ %s", provider.model, provider.base_url)
    log.info("角色 manifest = %s", rt["characters_root"] or "（未找到，使用全局人设）")
    log.info("TTS 后端 = %s", cfg.data.get("voice_chime_tts_backend", "edge"))

    try:
        server = _BrainServer((HOST, PORT), _Handler)
    except OSError as exc:
        log.error("无法绑定端口 %d：%s", PORT, exc)
        log.error("→ 已有一个大脑服务在跑（旧的控制台窗口没关？）。")
        log.error("→ 请先关闭旧的 run_brain_server 窗口，再重新启动本窗口。")
        return
    log.info("按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("正在停止…")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
