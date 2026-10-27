# -*- coding: utf-8 -*-
"""百炼 CosyVoice provider 的合同与解析守卫（2026-10-26 接入「龙呼呼」童声时补）。

守三件事：
① provider 合同（id / 可用性 / flavor 不含凭据 / plan 带 mp3）；
② 响应解析——非流式只给 OSS URL，``data`` 为空是**正常形态**，不能当失败；
③ 错误响应要说成人话（百炼的 ``{"code","message"}``），不能把整段 JSON 抛给用户。
"""

from __future__ import annotations

import base64
import json

import pytest

from pet.tts import bailian


def test_provider_contract_basics() -> None:
    provider = bailian.PROVIDER
    assert provider.id == "bailian"
    assert provider.availability({"api_key": ""}) == bailian.AVAILABILITY_CODE
    assert provider.availability({"api_key": "sk-x"}) == ""


def test_flavor_carries_content_params_only() -> None:
    values = {"instruct": "你说话的情感是 happy。",
              "model": "cosyvoice-v2", "api_key": "sk-secret"}
    flavor = bailian.PROVIDER.flavor(values)
    # v2 的龙呼呼是 longhuhu（不带 _v3），音色由模型推导
    assert "longhuhu" in flavor and "happy" in flavor and "cosyvoice-v2" in flavor
    assert "sk-secret" not in flavor, "凭据绝不许进缓存键（flavor）"


def test_plan_defaults_to_mp3_and_longhuhu() -> None:
    plan = bailian.PROVIDER.plan({"api_key": "sk-x"})
    assert plan["ext"] == "mp3"
    assert plan["model"] == bailian.DEFAULT_MODEL == "cosyvoice-v3-flash"
    assert plan["voice"] == bailian.DEFAULT_VOICE == "longhuhu_v3"
    assert plan["instruct"] == ""


def test_only_longhuhu_models_are_offered() -> None:
    """龙呼呼只有这两款模型提供（2026-10-27 官方音色列表核对），下拉里别再多给。"""
    offered = [value for value, _label in bailian.MODEL_OPTIONS]
    assert offered == ["cosyvoice-v3-flash", "cosyvoice-v2"]
    assert dict(bailian.MODEL_VOICE) == {
        "cosyvoice-v3-flash": "longhuhu_v3",
        "cosyvoice-v2": "longhuhu",
    }


@pytest.mark.parametrize(
    "model,expected",
    [
        ("cosyvoice-v3-flash", "longhuhu_v3"),
        ("cosyvoice-v2", "longhuhu"),
        ("  CosyVoice-V2  ", "longhuhu"),  # 大小写/空白都归一
    ],
)
def test_voice_is_derived_from_model(model, expected) -> None:
    """音色 ID 不能手选：给了模型就推导出对应那一代的龙呼呼。"""
    assert bailian.PROVIDER.plan({"api_key": "sk-x", "model": model})["voice"] == expected


def test_stale_voice_value_does_not_win() -> None:
    """历史配置里存着不匹配的音色 ID 时以模型为准（否则服务端报音色不存在）。"""
    plan = bailian.PROVIDER.plan({
        "api_key": "sk-x", "model": "cosyvoice-v2", "voice": "longhuhu_v3"})
    assert plan["voice"] == "longhuhu"


@pytest.mark.parametrize("stale", ["cosyvoice-v3.5-flash", "cosyvoice-v3.5-plus",
                                   "cosyvoice-v3-plus", "", None, "随便写的"])
def test_retired_or_unknown_model_falls_back(stale) -> None:
    """已下线的 v3.5 系（没有龙呼呼音色）与手改错值都回落到默认模型，不至于失声。"""
    plan = bailian.PROVIDER.plan({"api_key": "sk-x", "model": stale})
    assert plan["model"] == "cosyvoice-v3-flash"
    assert plan["voice"] == "longhuhu_v3"


def test_voice_is_not_a_configurable_field() -> None:
    """音色不是设置项（只有一副嗓子）——字段里不该再有 voice 行。"""
    assert "voice" not in {spec.name for spec in bailian.PROVIDER.fields}


def test_empty_instruct_is_not_sent(monkeypatch, tmp_path) -> None:
    """没选情绪时请求体里不能出现 instruction（空串会被服务端当非法指令）。"""
    sent: list[dict] = []

    def fake_synth(body, key):
        sent.append(body)
        return b"ID3audio"

    monkeypatch.setattr(bailian, "_synthesize", fake_synth)
    plan = bailian.PROVIDER.plan({"api_key": "sk-x", "model": "cosyvoice-v2"})
    out = tmp_path / "a.mp3"
    bailian.PROVIDER.synth("你好呀", plan, out)
    assert "instruction" not in sent[0]["input"]
    assert out.read_bytes() == b"ID3audio"


@pytest.mark.parametrize("model", ["cosyvoice-v3-flash", "cosyvoice-v2"])
def test_instruct_is_sent_on_both_models(monkeypatch, tmp_path, model) -> None:
    """保留下来的两款都支持 Instruct，选了就要发出去。"""
    sent: list[dict] = []

    monkeypatch.setattr(bailian, "_synthesize", lambda body, key: (sent.append(body), b"x")[1])
    plan = bailian.PROVIDER.plan({
        "api_key": "sk-x", "model": model, "instruct": "你说话的情感是 happy。"})
    bailian.PROVIDER.synth("你好呀", plan, tmp_path / "b.mp3")
    assert sent[0]["model"] == model
    assert sent[0]["input"]["instruction"] == "你说话的情感是 happy。"


def test_missing_key_raises_unavailable() -> None:
    plan = bailian.PROVIDER.plan({"api_key": ""})
    with pytest.raises(bailian.TtsUnavailable) as exc:
        bailian.PROVIDER.synth("你好", plan, None)
    assert exc.value.code == bailian.AVAILABILITY_CODE


def test_error_message_prefers_message_and_code() -> None:
    raw = json.dumps({"code": "InvalidApiKey", "message": "Invalid API-key provided."}).encode()
    assert bailian._error_message(raw) == "Invalid API-key provided.（InvalidApiKey）"


def test_parse_url_only_response(monkeypatch) -> None:
    """非流式的真实形态：``audio.data`` 为空字符串，只有 ``url``。"""
    body = json.dumps({"output": {"finish_reason": "stop",
                                  "audio": {"data": "", "url": "http://example.com/a.mp3"}}}).encode()

    class _Resp:
        status = 200

        def read(self, *_a):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(bailian.urllib.request, "urlopen", lambda *a, **k: _Resp())
    monkeypatch.setattr(bailian, "_download", lambda url: b"ID3downloaded:" + url.encode())
    assert bailian._synthesize({"model": "m", "input": {}}, "sk-x") == b"ID3downloaded:http://example.com/a.mp3"


def test_parse_inline_base64_response(monkeypatch) -> None:
    """流式/内联形态：``audio.data`` 直接给 base64。"""
    body = json.dumps({"output": {"audio": {"data": base64.b64encode(b"ID3inline").decode()}}}).encode()

    class _Resp:
        status = 200

        def read(self, *_a):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(bailian.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert bailian._synthesize({"model": "m", "input": {}}, "sk-x") == b"ID3inline"
