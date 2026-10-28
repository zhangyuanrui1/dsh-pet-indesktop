# -*- coding: utf-8 -*-
"""阿里云百炼 provider —— 童声「龙呼呼」系 + Instruct 情绪控制 + 两个新音色。

**为什么加它**（2026-10-26 机主需求）：Edge / 豆包的中文音色都不是想要的甜娃童声；
百炼 CosyVoice 的系统音色里有龙呼呼（6~10 岁天真烂漫女童），是同类里最接近
「小女童讲话」的官方音色，且支持 Instruct 情绪指令，不用本地部署、不用克隆参考
音频——本机无独显，本地 CosyVoice2 纯 CPU 推理每句要十几秒，不可用。

**模型与音色**（音色 ID 与模型代际绑定，不能混用）：
- ``cosyvoice-v3-flash`` → ``longhuhu_v3``／``cosyvoice-v2`` → ``longhuhu``：
  龙呼呼这一副嗓子（2026-10-27 机主按官方《CosyVoice 音色列表》核对）；
- ``qwen-audio-3.0-tts-flash`` → ``longpaopao_v3.6``：「龙泡泡」软糯可爱音
  （2026-10-28 机主指定），**走的是同一个** CosyVoice HTTP 接口（实测通过）；
- ``qwen3-tts-flash-realtime`` → ``Bunny``：「萌小姬」（2026-10-28 机主指定），
  **只有 WebSocket 实时接口**（官方文档明确 realtime 系不走 HTTP），合成路径在
  ``_synthesize_realtime``——连 wss → session.update 配音色 → 投文本 → 收 PCM
  增量 → 本地拼 WAV。

所以**音色不是可选项，而是由模型推导**，避免选出一个模型没有的音色，请求直接
被服务端驳回。

接口（CosyVoice / Qwen-Audio-TTS 非实时，2026-10-26 按官方 HTTP API 实现）：

```
POST https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer
Authorization: Bearer <API Key，存系统钥匙串>
body: {"model": "cosyvoice-v3-flash",
       "input": {"text": ..., "voice": "longhuhu_v3",
                 "format": "mp3", "sample_rate": 24000,
                 "instruction": "你说话的情感是 happy。"}}
```

非流式响应**不给音频字节**，只给一个 24 小时有效的 OSS 预签名 URL（``output.audio.url``），
所以 ``synth()`` 是「请求 → 拿 URL → 再下载」两步；两步都失败才算失败。

约定（与 ``pet/tts/base.py`` 的红线一致）：
- 模块顶层只用标准库，不 import Qt、不 import 任何第三方包（``websockets`` 在
  ``synth()`` 的实时分支里惰性导入）；
- API Key 走 ``kind="secret"`` + ``secret_ref``，只进系统钥匙串，不进 config.json；
- ``flavor()`` 只放影响音频内容的参数（音色 / 情绪指令 / 模型），凭据不进缓存键。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import urllib.error
import urllib.request
import wave

from .base import TtsField, TtsProvider, TtsUnavailable, register

PROVIDER_ID = "bailian"
LABEL = "百炼语音（需 API Key，有童声）"
ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer"
SECRET_REF = "tts/bailian_api_key"
AVAILABILITY_CODE = "bailian_key_missing"
MISSING_MESSAGE = (
    "百炼的 API Key 没配：登录阿里云百炼控制台（bailian.console.aliyun.com）→ API-KEY 管理"
    "里创建，填进设置页即可（只存系统钥匙串，不写进配置文件）。"
)
TIMEOUT_S = 60
DEFAULT_MODEL = "cosyvoice-v3-flash"
SAMPLE_RATE = 24000
#: 语速用整数百分比配置（共享的 number 控件只吃整数），发请求换算成官方的倍数。
#: 100 = 正常（对应 API 的 rate=1.0），50~200 对齐官方取值 [0.5, 2.0]。
DEFAULT_RATE_PERCENT = 100

#: 模型 → 音色 ID。**音色 ID 与模型代际绑定，不能混用**（一个模型的音色不能给
#: 另一个模型用，选错服务端直接报「音色不存在」）：
#: - ``cosyvoice-v3-flash`` → ``longhuhu_v3``（v3 代命名，带 _v3 后缀）
#: - ``cosyvoice-v2`` → ``longhuhu``（v2 代命名，不带后缀）
#: - ``qwen-audio-3.0-tts-flash`` → ``longpaopao_v3.6``（龙泡泡，v3.6 代命名）
#: - ``qwen3-tts-flash-realtime`` → ``Bunny``（萌小姬，Qwen3-TTS 音色命名）
#: 音色**不是可选项而是推导项**：下拉里只剩这几个模型，改模型音色 ID 自动跟着换。
MODEL_VOICE = (
    ("cosyvoice-v3-flash", "longhuhu_v3"),
    ("cosyvoice-v2", "longhuhu"),
    ("qwen-audio-3.0-tts-flash", "longpaopao_v3.6"),
    ("qwen3-tts-flash-realtime", "Bunny"),
)
DEFAULT_VOICE = dict(MODEL_VOICE)[DEFAULT_MODEL]
_MODEL_IDS = tuple(model for model, _voice in MODEL_VOICE)

MODEL_OPTIONS = (
    ("cosyvoice-v3-flash", "cosyvoice-v3-flash · 龙呼呼（默认）"),
    ("cosyvoice-v2", "cosyvoice-v2 · 龙呼呼（上一代，音色略有差异）"),
    ("qwen-audio-3.0-tts-flash", "qwen-audio-3.0-tts-flash · 龙泡泡（软糯可爱音）"),
    ("qwen3-tts-flash-realtime", "qwen3-tts-flash-realtime · 萌小姬（实时）"),
)

#: 只有 WebSocket 实时接口的模型（官方文档：realtime 系不走 HTTP SpeechSynthesizer，
#: 也不支持 Instruct 情绪指令与 rate 语速参数——设置页据此隐藏对应行）。
REALTIME_MODEL = "qwen3-tts-flash-realtime"
REALTIME_WS_URL = (
    "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3-tts-flash-realtime"
)
REALTIME_SAMPLE_RATE = 24000

#: Instruct 预设（``none`` 表示不发 instruction）。格式必须严格照官方要求：
#: 「你说话的情感是 X。」／「你说话的角色是 Y，你说话的情感是 X。」，结尾句号不能省。
#: CosyVoice 两款与 qwen-audio-3.0-tts-flash 都支持（后者实测也吃这套中文指令）；
#: realtime 版不支持，选它时本行在设置页隐藏、请求里也不会带（见 _effective_instruct）。
INSTRUCT_OPTIONS = (
    ("", "默认（不指定情绪）"),
    ("你说话的情感是 neutral。", "平静 neutral"),
    ("你说话的情感是 happy。", "开心 happy"),
    ("你说话的情感是 surprised。", "惊讶 surprised"),
    ("你说话的情感是 sad。", "难过 sad"),
    ("你说话的情感是 angry。", "生气 angry"),
    ("你说话的情感是 fearful。", "害怕 fearful"),
    ("你说话的角色是可爱孩童，你说话的情感是 happy。", "角色：可爱孩童 · 开心"),
    ("你说话的角色是小猪佩奇，你说话的情感是 happy。", "角色：小猪佩奇 · 开心"),
    ("你说话的角色是傲娇公主，你说话的情感是 neutral。", "角色：傲娇公主"),
)


class BailianTtsProvider(TtsProvider):
    id = PROVIDER_ID
    label = LABEL
    is_fallback = False
    availability_code = AVAILABILITY_CODE
    unavailable_message = MISSING_MESSAGE
    fields = (
        TtsField(
            "instruct", "voice_chime_bailian_instruct", "情绪 / 角色",
            "换情绪，留空即默认。",
            kind="select", default="", options=INSTRUCT_OPTIONS, allow_custom=True,
            # realtime 版不支持情绪指令：选它时隐藏本行（请求里同样不会带）。
            hidden_when=("model", REALTIME_MODEL),
        ),
        TtsField(
            "rate", "voice_chime_bailian_rate", "语速",
            "100 正常；80 慢读，150 加快。",
            kind="number", default=DEFAULT_RATE_PERCENT, minimum=50, maximum=200, suffix=" %",
            # realtime 实时接口没有语速参数：选它时隐藏本行（合成路径也不会用）。
            hidden_when=("model", REALTIME_MODEL),
        ),
        TtsField(
            "model", "voice_chime_bailian_model", "模型",
            "换模型音色自动跟着换。",
            kind="select", default=DEFAULT_MODEL, options=MODEL_OPTIONS,
            allow_custom=False, advanced=True,
        ),
        TtsField(
            "api_key", "voice_chime_bailian_api_key", "API Key",
            "去阿里云百炼控制台创建。",
            kind="secret", default="", secret_ref=SECRET_REF,
        ),
    )

    # ------------------------------------------------------------ 纯层
    def plan(self, values: dict) -> dict:
        model = clean_model(values.get("model"))
        return {
            "provider": self.id,
            "api_key": str(values.get("api_key") or "").strip(),
            "voice": voice_for(model),
            "instruct": _effective_instruct(model, values.get("instruct")),
            "rate": clean_rate(values.get("rate")),
            "model": model,
            # realtime 走 WebSocket 收 PCM，本地拼 WAV；其余仍是 mp3
            "ext": "wav" if model == REALTIME_MODEL else "mp3",
        }

    def flavor(self, values: dict) -> str:
        # 只放影响音频内容的参数（密钥不进缓存键）；音色由模型推导，所以只放推导
        # 结果——配置里那个已经作废的 voice 值不该影响缓存命中
        model = clean_model(values.get("model"))
        return "|".join((
            self.id,
            model,
            voice_for(model),
            _effective_instruct(model, values.get("instruct")),
            f"rate={clean_rate(values.get('rate')):.2f}",
        ))

    def availability(self, values: dict) -> str:
        return "" if str(values.get("api_key") or "").strip() else AVAILABILITY_CODE

    # ------------------------------------------------------------ IO 层
    def synth(self, text: str, plan: dict, out_path) -> None:
        key = str(plan.get("api_key") or "").strip()
        if not key:
            raise TtsUnavailable(AVAILABILITY_CODE)
        model = str(plan.get("model") or DEFAULT_MODEL)
        try:
            if model == REALTIME_MODEL:
                audio = _synthesize_realtime(
                    str(text or ""), key, str(plan.get("voice") or DEFAULT_VOICE))
            else:
                body = {
                    "model": model,
                    "input": {
                        "text": str(text or ""),
                        "voice": str(plan.get("voice") or DEFAULT_VOICE),
                        "format": "mp3",
                        "sample_rate": SAMPLE_RATE,
                    },
                }
                instruct = str(plan.get("instruct") or "").strip()
                if instruct:
                    body["input"]["instruction"] = instruct
                rate = float(plan.get("rate") or 1.0)
                if abs(rate - 1.0) > 1e-6:
                    body["input"]["rate"] = rate
                audio = _synthesize(body, key)
        except TtsUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 —— 统一转成可读错误
            raise TtsUnavailable(AVAILABILITY_CODE, f"百炼合成失败：{exc}") from exc
        if not audio:
            raise TtsUnavailable(AVAILABILITY_CODE, "百炼返回空音频")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_name(out_path.name + ".part")
        tmp.write_bytes(audio)
        tmp.replace(out_path)


# ---------------------------------------------------------------- 清洗
def clean_model(value) -> str:
    """模型归一：只认 MODEL_VOICE 里登记的几款，其余（含已下线的 v3.5 系）一律回落默认。

    回落而不是报错，是因为历史配置里可能还写着 ``cosyvoice-v3.5-flash``——那个
    模型没有龙呼呼音色（音色 ID 也不同），留着只会让每句话都合成失败。
    """
    text = str(value or "").strip().lower()
    return text if text in _MODEL_IDS else DEFAULT_MODEL


def voice_for(model) -> str:
    """按模型推导音色 ID——音色不可单独选，避免出「模型没有这个音色」的组合。"""
    return dict(MODEL_VOICE)[clean_model(model)]


def _effective_instruct(model, raw) -> str:
    """realtime 版不支持情绪指令：选它时指令一律置空（不发、也不进缓存键）。"""
    if clean_model(model) == REALTIME_MODEL:
        return ""
    return clean_instruct(raw)


def clean_instruct(value) -> str:
    """Instruct 指令：单行、限长（官方要求整句中文指令，超长无意义）。"""
    text = " ".join(str(value or "").split())
    return text[:200]


def clean_rate(value) -> float:
    """语速：整数百分比（50~200，100=正常）换算成官方倍数（0.5~2.0）。

    兼容直接填倍数的历史/手改配置：值 ≤ 10 时按倍数理解（1.0 = 正常）。
    """
    try:
        raw = float(value)
    except (TypeError, ValueError):
        raw = DEFAULT_RATE_PERCENT
    if raw <= 10:  # 已经是倍数口径
        raw *= 100
    return max(0.5, min(2.0, raw / 100.0))


# ---------------------------------------------------------------- HTTP
def _synthesize(body: dict, api_key: str) -> bytes:
    """请求合成 → 拿 OSS URL → 下载音频字节。任一步失败抛异常。"""
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:  # 401/403 是 Key 问题，其余按服务端错误报
        detail = _error_message(exc.read(4096))
        if exc.code in (401, 403):
            raise TtsUnavailable(AVAILABILITY_CODE, f"认证失败（HTTP {exc.code}）：{detail}") from exc
        raise TtsUnavailable(AVAILABILITY_CODE, f"请求失败（HTTP {exc.code}）：{detail}") from exc
    except urllib.error.URLError as exc:
        raise TtsUnavailable(AVAILABILITY_CODE, f"网络连接失败：{exc.reason}") from exc

    data = _loads(raw)
    if data is None:
        raise TtsUnavailable(AVAILABILITY_CODE, "百炼返回的不是 JSON")
    output = data.get("output") if isinstance(data, dict) else None
    audio = output.get("audio") if isinstance(output, dict) else None
    if not isinstance(audio, dict):
        raise TtsUnavailable(AVAILABILITY_CODE, _error_message(raw) or "百炼没有返回音频")

    inline = str(audio.get("data") or "").strip()
    if inline:
        try:
            return base64.b64decode(inline)
        except Exception:  # noqa: BLE001 —— 坏的 base64 当失败处理，不写空缓存
            raise TtsUnavailable(AVAILABILITY_CODE, "百炼返回的音频数据无法解码")

    url = str(audio.get("url") or "").strip()
    if not url:
        raise TtsUnavailable(AVAILABILITY_CODE, "百炼没有返回音频地址")
    return _download(url)


def _download(url: str) -> bytes:
    """下载合成结果（OSS 预签名 URL，24 小时内有效）。"""
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as resp:
            return resp.read()
    except urllib.error.URLError as exc:
        raise TtsUnavailable(AVAILABILITY_CODE, f"下载音频失败：{exc.reason}") from exc


# ---------------------------------------------------------------- 实时（WebSocket）
def _synthesize_realtime(text: str, api_key: str, voice: str) -> bytes:
    """qwen3-tts-flash-realtime 只有 WebSocket 实时接口：连接 → 配音色 → 投文本
    → 收 PCM 增量 → 本地拼 WAV 字节返回。

    ``websockets`` 在这里惰性导入（模块顶层只许标准库，见 base.py 红线）；它是
    requirements.txt 声明过的运行时依赖，打包脚本会 ``--collect-all`` 收进产物。
    """
    try:
        from websockets.asyncio.client import connect
    except Exception as exc:  # noqa: BLE001 —— 统一转成「不可用」信号
        raise TtsUnavailable(
            AVAILABILITY_CODE, f"实时合成依赖 websockets 库：{exc}") from exc
    try:
        return asyncio.run(_realtime_collect(connect, text, api_key, voice))
    except TtsUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TtsUnavailable(AVAILABILITY_CODE, f"实时合成失败：{exc}") from exc


async def _realtime_collect(connect, text: str, api_key: str, voice: str) -> bytes:
    """一次实时合成的完整事件流（ServerCommit 模式，报时文本一次投完）。"""
    pcm = bytearray()
    async with connect(
        REALTIME_WS_URL,
        additional_headers={
            "Authorization": f"Bearer {api_key}",
            "user-agent": "dsh-pet-desktop",
        },
    ) as ws:
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {"voice": voice, "response_format": "pcm",
                        "sample_rate": REALTIME_SAMPLE_RATE},
        }))
        await ws.send(json.dumps({"type": "input_text_buffer.append", "text": text}))
        await ws.send(json.dumps({"type": "input_text_buffer.commit"}))

        loop = asyncio.get_running_loop()
        deadline = loop.time() + TIMEOUT_S
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RuntimeError(f"实时合成超时（>{TIMEOUT_S}s）")
            raw = await asyncio.wait_for(ws.recv(), remaining)
            event = _loads(raw if isinstance(raw, bytes) else str(raw).encode("utf-8"))
            if not isinstance(event, dict):
                continue
            etype = str(event.get("type") or "")
            if etype == "response.audio.delta":
                pcm += base64.b64decode(str(event.get("delta") or ""))
            elif etype == "error":
                raise RuntimeError(_realtime_error_message(event))
            elif etype in ("response.done", "session.finished"):
                break
        try:
            await ws.send(json.dumps({"type": "session.finish"}))
        except Exception:  # noqa: BLE001 —— 收尾失败不影响已收到的音频
            pass
    if not pcm:
        raise RuntimeError("实时接口没有返回音频")
    return _pcm_to_wav(bytes(pcm))


def _realtime_error_message(event: dict) -> str:
    """实时接口的 error 事件（``{"type":"error","error":{"code","message"}}``）说成人话。"""
    err = event.get("error") if isinstance(event.get("error"), dict) else {}
    message = str(err.get("message") or "").strip()
    code = str(err.get("code") or "").strip()
    if not message:
        message = " ".join(str(event).split())[:300]
    return f"{message}（{code}）" if code else message


def _pcm_to_wav(pcm: bytes) -> bytes:
    """16-bit 单声道 PCM 裸流包上 WAV 头（realtime 接口返回的是无头 PCM）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(REALTIME_SAMPLE_RATE)
        handle.writeframes(pcm)
    return buf.getvalue()


def _loads(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def _error_message(raw: bytes) -> str:
    """从百炼的错误响应里抽出人话（``{"code":…,"message":…}``）。"""
    data = _loads(raw)
    if isinstance(data, dict):
        message = str(data.get("message") or "").strip()
        code = str(data.get("code") or "").strip()
        if message:
            return f"{message}（{code}）" if code else message
        if code:
            return code
    text = " ".join(str(raw or b"").decode("utf-8", "replace").split()) if raw else ""
    return text[:300] or "百炼请求失败"


PROVIDER = register(BailianTtsProvider())
