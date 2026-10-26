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
    values = {"voice": "longhuhu_v3", "instruct": "你说话的情感是 happy。",
              "model": "cosyvoice-v2", "api_key": "sk-secret"}
    flavor = bailian.PROVIDER.flavor(values)
    assert "longhuhu_v3" in flavor and "happy" in flavor and "cosyvoice-v2" in flavor
    assert "sk-secret" not in flavor, "凭据绝不许进缓存键（flavor）"


def test_plan_defaults_to_mp3_and_longhuhu() -> None:
    plan = bailian.PROVIDER.plan({"api_key": "sk-x"})
    assert plan["ext"] == "mp3"
    assert plan["voice"] == bailian.DEFAULT_VOICE == "longhuhu"
    assert plan["model"] == bailian.DEFAULT_MODEL
    assert plan["instruct"] == ""


def test_empty_instruct_is_not_sent(monkeypatch, tmp_path) -> None:
    """没选情绪时请求体里不能出现 instruction（空串会被服务端当非法指令）。"""
    sent: list[dict] = []

    def fake_synth(body, key):
        sent.append(body)
        return b"ID3audio"

    monkeypatch.setattr(bailian, "_synthesize", fake_synth)
    plan = bailian.PROVIDER.plan({"api_key": "sk-x", "voice": "longhuhu_v3"})
    out = tmp_path / "a.mp3"
    bailian.PROVIDER.synth("你好呀", plan, out)
    assert "instruction" not in sent[0]["input"]
    assert out.read_bytes() == b"ID3audio"


def test_instruct_is_sent_when_chosen(monkeypatch, tmp_path) -> None:
    sent: list[dict] = []

    monkeypatch.setattr(bailian, "_synthesize", lambda body, key: (sent.append(body), b"x")[1])
    plan = bailian.PROVIDER.plan({
        "api_key": "sk-x", "model": "cosyvoice-v2", "instruct": "你说话的情感是 happy。"})
    bailian.PROVIDER.synth("你好呀", plan, tmp_path / "b.mp3")
    assert sent[0]["input"]["instruction"] == "你说话的情感是 happy。"


def test_v35_models_drop_instruct() -> None:
    """v3.5 系模型不支持 Instruct：选了情绪也要丢弃，不发也不进缓存键。"""
    values = {"api_key": "sk-x", "model": "cosyvoice-v3.5-flash",
              "voice": "longhuhu", "instruct": "你说话的情感是 happy。"}
    plan = bailian.PROVIDER.plan(values)
    assert plan["instruct"] == ""
    assert "happy" not in bailian.PROVIDER.flavor(values)


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
