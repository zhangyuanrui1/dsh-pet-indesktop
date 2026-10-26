# -*- coding: utf-8 -*-
"""阿里云百炼 CosyVoice provider —— 童声「龙呼呼」等系统音色 + Instruct 情绪控制。

**为什么加它**（2026-10-26 机主需求）：Edge / 豆包的中文音色都不是想要的甜娃童声；
百炼 CosyVoice 的系统音色里有 ``longhuhu_v3``（龙呼呼，6~10 岁天真烂漫女童），
是同类里最接近「小女童讲话」的官方音色，且支持 Instruct 情绪指令，不用本地部署、
不用克隆参考音频——本机无独显，本地 CosyVoice2 纯 CPU 推理每句要十几秒，不可用。

接口（2026-10-26 按官方《非实时语音合成 CosyVoice HTTP API》实现）：

```
POST https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer
Authorization: Bearer <API Key，存系统钥匙串>
body: {"model": "cosyvoice-v2",
       "input": {"text": ..., "voice": "longhuhu_v3",
                 "format": "mp3", "sample_rate": 24000,
                 "instruction": "你说话的情感是 happy。"}}
```

非流式响应**不给音频字节**，只给一个 24 小时有效的 OSS 预签名 URL（``output.audio.url``），
所以 ``synth()`` 是「请求 → 拿 URL → 再下载」两步；两步都失败才算失败。

约定（与 ``pet/tts/base.py`` 的红线一致）：
- 模块顶层只用标准库，不 import Qt、不 import 任何第三方包；
- API Key 走 ``kind="secret"`` + ``secret_ref``，只进系统钥匙串，不进 config.json；
- ``flavor()`` 只放影响音频内容的参数（音色 / 情绪指令 / 模型），凭据不进缓存键。
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

from .base import TtsField, TtsProvider, TtsUnavailable, register

PROVIDER_ID = "bailian"
LABEL = "百炼 CosyVoice（需 API Key，有童声）"
ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer"
SECRET_REF = "tts/bailian_api_key"
AVAILABILITY_CODE = "bailian_key_missing"
MISSING_MESSAGE = (
    "百炼的 API Key 没配：登录阿里云百炼控制台（bailian.console.aliyun.com）→ API-KEY 管理"
    "里创建，填进设置页即可（只存系统钥匙串，不写进配置文件）。"
)
TIMEOUT_S = 60
DEFAULT_MODEL = "cosyvoice-v3.5-flash"
DEFAULT_VOICE = "longhuhu"
SAMPLE_RATE = 24000
#: 语速用整数百分比配置（共享的 number 控件只吃整数），发请求换算成官方的倍数。
#: 100 = 正常（对应 API 的 rate=1.0），50~200 对齐官方取值 [0.5, 2.0]。
DEFAULT_RATE_PERCENT = 100

#: 模型。**音色 ID 分两代，不能混用**（官方口径：一个模型的音色不能给另一个模型用）：
#: - v3.5 系（v3.5-flash / v3.5-plus）：音色不带 _v3 后缀（龙呼呼=``longhuhu``），
#:   **不支持 Instruct 指令**，价格更低（v3.5-flash 0.8 元/万字符）；
#: - v2 / v3 系：音色带 _v3 后缀（龙呼呼=``longhuhu_v3``），支持 Instruct。
#: 默认 v3.5-flash——2026-10-26 机主账号开通的免费额度只有 v3.5 系两款。
MODEL_OPTIONS = (
    ("cosyvoice-v3.5-flash", "cosyvoice-v3.5-flash（默认，便宜）"),
    ("cosyvoice-v3.5-plus", "cosyvoice-v3.5-plus（质量更高）"),
    ("cosyvoice-v2", "cosyvoice-v2（支持情绪指令）"),
    ("cosyvoice-v3-flash", "cosyvoice-v3-flash（支持情绪指令）"),
    ("cosyvoice-v3-plus", "cosyvoice-v3-plus（支持情绪指令，更贵）"),
)

#: 系统音色（名称/参数取自官方《CosyVoice 音色列表》）。同一副嗓子两代各有 ID：
#: 不带后缀的给 v3.5 系模型用，带 ``_v3`` 的给 v2/v3 系模型用——选错模型会报
#: 音色不存在，这里把两代都列出来并在标签里标明代际。
VOICE_OPTIONS = (
    ("longhuhu", "龙呼呼·女童（v3.5 模型用）"),
    ("longhuhu_v3", "龙呼呼·女童（v2/v3 模型用）"),
    ("longpaopao", "龙泡泡·飞天泡泡音（v3.5 模型用）"),
    ("longpaopao_v3", "龙泡泡·飞天泡泡音（v2/v3 模型用）"),
    ("longjielidou_v3", "龙杰力豆·阳光顽皮男（v2/v3 模型用）"),
    ("longshanshan", "龙闪闪·戏剧化童声（v3.5 模型用）"),
    ("longshanshan_v3", "龙闪闪·戏剧化童声（v2/v3 模型用）"),
    ("longniuniu", "龙牛牛·阳光男童声（v3.5 模型用）"),
    ("longniuniu_v3", "龙牛牛·阳光男童声（v2/v3 模型用）"),
    ("longanyang", "龙安洋·阳光大男孩（全模型）"),
    ("longanhuan", "龙安欢·欢脱元气女（全模型）"),
)

#: Instruct 预设（``none`` 表示不发 instruction）。格式必须严格照官方要求：
#: 「你说话的情感是 X。」／「你说话的角色是 Y，你说话的情感是 X。」，结尾句号不能省。
#: **只有 v2/v3 系音色支持**；v3.5 系模型会被自动忽略（见 ``_effective_instruct``）。
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
            "voice", "voice_chime_bailian_voice", "音色",
            "童声首选龙呼呼；官方音色名可手填。",
            kind="select", default=DEFAULT_VOICE, options=VOICE_OPTIONS, allow_custom=True,
        ),
        TtsField(
            "instruct", "voice_chime_bailian_instruct", "情绪 / 角色",
            "换情绪/角色；v3.5 模型不支持。",
            kind="select", default="", options=INSTRUCT_OPTIONS, allow_custom=True,
        ),
        TtsField(
            "rate", "voice_chime_bailian_rate", "语速",
            "100 正常；80 慢读，150 加快。",
            kind="number", default=DEFAULT_RATE_PERCENT, minimum=50, maximum=200, suffix=" %",
        ),
        TtsField(
            "model", "voice_chime_bailian_model", "模型",
            "换模型要配对的音色。",
            kind="select", default=DEFAULT_MODEL, options=MODEL_OPTIONS,
            allow_custom=True, advanced=True,
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
            "voice": clean_voice(values.get("voice")),
            "instruct": _effective_instruct(model, values.get("instruct")),
            "rate": clean_rate(values.get("rate")),
            "model": model,
            "ext": "mp3",
        }

    def flavor(self, values: dict) -> str:
        # 只放影响音频内容的参数（密钥不进缓存键）；与 plan() 同口径——v3.5 系
        # 不吃 instruct，参数不同也是同一段音频，缓存键必须一致
        model = clean_model(values.get("model"))
        return "|".join((
            self.id,
            model,
            clean_voice(values.get("voice")),
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
        body = {
            "model": str(plan.get("model") or DEFAULT_MODEL),
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

        try:
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
def clean_voice(value) -> str:
    text = str(value or "").strip()
    return text[:64] if text else DEFAULT_VOICE


def clean_model(value) -> str:
    text = str(value or "").strip()
    return text[:64] if text else DEFAULT_MODEL


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


def _effective_instruct(model: str, value) -> str:
    """v3.5 系模型不吃 Instruct 指令——直接丢弃，别发给服务端（发了可能报错）。"""
    if str(model or "").strip().startswith("cosyvoice-v3.5"):
        return ""
    return clean_instruct(value)


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
