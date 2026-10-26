# -*- coding: utf-8 -*-
"""语音对话（按下说话）的单元测试。

**不碰真麦克风、不依赖识别模型**：这里验的是时序与边界——打断与录音的先后、
连按语义、跨线程回来的陈旧结果作废、裁剪的余量、重采样的比例。真实识别质量
由冒烟脚本（用真录音跑）负责，测试套件只保证"逻辑没错位"。

Qt 组件全部走 offscreen，与 ``tests/test_pet_sprite.py`` 同口径。
"""

from __future__ import annotations

import array
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from PySide6.QtMultimedia import QAudioFormat  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pet.asr.recorder import (  # noqa: E402
    HEAD_MARGIN_SECONDS,
    TAIL_MARGIN_SECONDS,
    SpeechRecorder,
    _resample,
)
from pet.asr.sensevoice import normalize_text  # noqa: E402
from pet.voice_talk import (  # noqa: E402
    DEFAULT_SILENCE_SECONDS,
    EMPTY_RESULT_HINT,
    VoiceTalkController,
)

app = QApplication.instance() or QApplication([])


# ---------------------------------------------------------------- 替身
class FakeHost:
    """记录调用的宿主替身（三个钩子全实现，顺序也记下来）。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.delivered: list[str] = []
        self.notices: list[str] = []

    def voice_talk_interrupt(self) -> None:
        self.calls.append(("interrupt",))

    def voice_talk_deliver(self, text: str) -> bool:
        self.calls.append(("deliver", text))
        self.delivered.append(text)
        return True

    def voice_talk_notice(self, text: str) -> None:
        self.calls.append(("notice", text))
        self.notices.append(text)


class FakeConfig:
    def __init__(self, silence=DEFAULT_SILENCE_SECONDS) -> None:
        self._silence = silence

    def get(self, key, default=None):
        if key == "voice_talk_silence_seconds":
            return self._silence
        return default


def _controller(host=None, silence=DEFAULT_SILENCE_SECONDS):
    host = host or FakeHost()
    return host, VoiceTalkController(host, FakeConfig(silence))


@pytest.fixture
def started_recorder(monkeypatch):
    """把录音器的 start/stop 换成记录器（不打开任何设备）。"""
    def install(ctrl, ok=True, reason=""):
        seq = []

        def fake_start():
            seq.append("start")
            return ok, reason

        monkeypatch.setattr(ctrl._recorder, "start", fake_start)
        monkeypatch.setattr(ctrl._recorder, "set_silence_seconds", lambda v: None)
        return seq

    return install


# ---------------------------------------------------------------- 时序
class TestToggleOrder:
    def test_idle_interrupts_then_records(self, started_recorder):
        """空闲按下：**先掐声音，再开录音**——顺序反了就会把她自己的朗读录进去。"""
        host, ctrl = _controller()
        seq = started_recorder(ctrl)
        # 借 host 的 calls 记录 interrupt 的位置
        monkeypatch_order = []
        host.voice_talk_interrupt = lambda: monkeypatch_order.append("interrupt")
        ctrl._host = host
        ctrl.toggle()
        monkeypatch_order.append("done")
        assert seq == ["start"]
        assert monkeypatch_order[0] == "interrupt", "打断必须发生在录音之前"

    def test_recording_press_stops_instead_of_restarting(self, monkeypatch):
        """方案 B：录音中再按一次 = 收尾，不重新开录、也不打断朗读。"""
        host, ctrl = _controller()
        stopped = []
        monkeypatch.setattr(ctrl._recorder, "stop", lambda: stopped.append("stop"))
        ctrl._recorder._active = True
        ctrl.toggle()
        assert stopped == ["stop"]
        assert host.calls == [], "收尾不该触发打断或提示"

    def test_busy_press_is_ignored_with_hint(self, started_recorder):
        """识别中按键：不录音，只提示（此时麦克风已交还，重开会抢 CPU）。"""
        host, ctrl = _controller()
        seq = started_recorder(ctrl)
        ctrl._busy = True
        ctrl.toggle()
        assert seq == [], "识别中不该开始录音"
        assert host.notices and "识别" in host.notices[0]

    def test_start_failure_reports_reason(self, started_recorder):
        host, ctrl = _controller()
        started_recorder(ctrl, ok=False, reason="没有找到麦克风")
        ctrl.toggle()
        assert host.notices == ["没有找到麦克风"]

    def test_shutdown_blocks_further_presses(self, started_recorder):
        host, ctrl = _controller()
        seq = started_recorder(ctrl)
        ctrl.shutdown()
        ctrl.toggle()
        assert seq == []


# ---------------------------------------------------------------- 识别结果
class TestRecognitionFlow:
    def test_empty_audio_hints_without_alarm(self):
        host, ctrl = _controller()
        ctrl._on_audio(None)
        assert host.notices == ["没听到你说话"]

    def test_empty_text_hints(self):
        host, ctrl = _controller()
        ctrl._on_recognition_done(0, "   ")
        assert host.notices == [EMPTY_RESULT_HINT]
        assert host.delivered == []

    def test_result_is_delivered_and_busy_clears(self):
        host, ctrl = _controller()
        ctrl._busy = True
        ctrl._on_recognition_done(ctrl._generation, "今天天气不错")
        assert host.delivered == ["今天天气不错"]
        assert ctrl.busy is False

    def test_stale_generation_is_dropped(self):
        """用户连按两次 / 退出后，上一句的识别结果不许再投递。"""
        host, ctrl = _controller()
        ctrl._generation = 7
        ctrl._on_recognition_done(6, "上一句的结果")
        assert host.delivered == []

    def test_result_after_shutdown_is_dropped(self):
        host, ctrl = _controller()
        ctrl.shutdown()
        ctrl._on_recognition_done(ctrl._generation, "退出后才回来")
        assert host.delivered == []

    def test_missing_host_hook_does_not_raise(self):
        class Bare:
            pass

        ctrl = VoiceTalkController(Bare(), FakeConfig())
        # 没有任何钩子：只记日志，不抛
        ctrl._on_audio(None)
        ctrl._on_recognition_done(ctrl._generation, "一句话")
        assert ctrl.busy is False


# ---------------------------------------------------------------- 参数收紧
class TestSilenceSeconds:
    def test_default(self):
        _, ctrl = _controller()
        assert ctrl._silence_seconds() == DEFAULT_SILENCE_SECONDS == 1.0

    @pytest.mark.parametrize("raw,expected", [(99, 3.0), (0.01, 0.4), (1.5, 1.5)])
    def test_clamped(self, raw, expected):
        _, ctrl = _controller(silence=raw)
        assert ctrl._silence_seconds() == expected

    def test_dirty_value_falls_back(self):
        _, ctrl = _controller(silence="不是数字")
        assert ctrl._silence_seconds() == DEFAULT_SILENCE_SECONDS


# ---------------------------------------------------------------- 录音器
class TestRecorderClip:
    def _rec(self):
        rec = SpeechRecorder()
        fmt = QAudioFormat()
        fmt.setSampleRate(16000)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.Int16)
        rec._actual_format = fmt
        return rec

    def test_no_voice_returns_empty(self):
        rec = self._rec()
        rec._chunks = [0.0] * 16000
        rec._total = 16000
        assert rec._clip() == []

    def test_margins_are_applied(self):
        rec = self._rec()
        rec._chunks = [0.1] * 48000
        rec._total = 48000
        rec._first_voice = 16000
        rec._last_voice = 32000
        clipped = rec._clip()
        expected = (32000 - 16000) + int(HEAD_MARGIN_SECONDS * 16000) \
            + int(TAIL_MARGIN_SECONDS * 16000)
        assert len(clipped) == expected

    def test_clip_does_not_run_past_end(self):
        """末帧语音贴着录音末尾时，尾部余量不许越界。"""
        rec = self._rec()
        rec._chunks = [0.1] * 20000
        rec._total = 20000
        rec._first_voice = 100
        rec._last_voice = 20000
        assert len(rec._clip()) == 20000

    def test_decode_resamples_48k(self):
        rec = self._rec()
        fmt = QAudioFormat()
        fmt.setSampleRate(48000)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.Int16)
        rec._actual_format = fmt
        pcm = array.array("h", [1000] * 4800)
        out = rec._decode(pcm.tobytes())
        assert abs(len(out) - 1600) <= 1

    def test_decode_downmixes_stereo(self):
        rec = self._rec()
        fmt = QAudioFormat()
        fmt.setSampleRate(16000)
        fmt.setChannelCount(2)
        fmt.setSampleFormat(QAudioFormat.Int16)
        rec._actual_format = fmt
        # 左右声道分别为 +0.5 / +0.1 → 降混应为 +0.3
        pcm = array.array("h", [16384, 3277] * 100)
        out = rec._decode(pcm.tobytes())
        assert len(out) == 100
        assert out[0] == pytest.approx(0.3, abs=1e-3)


class TestResample:
    def test_identity_when_rates_equal(self):
        src = [0.1, 0.2, 0.3]
        assert _resample(src, 16000, 16000) is src

    def test_empty_input(self):
        assert _resample([], 48000, 16000) == []

    def test_length_ratio(self):
        src = [0.0] * 4800
        assert abs(len(_resample(src, 48000, 16000)) - 1600) <= 1

    def test_keeps_extremes(self):
        src = [0.0, 1.0, 0.0, -1.0] * 300
        out = _resample(src, 48000, 16000)
        assert max(out) <= 1.0 and min(out) >= -1.0


# ---------------------------------------------------------------- 识别文本
class TestNormalizeText:
    def test_collapses_whitespace(self):
        assert normalize_text("  你好   世界 \n") == "你好 世界"

    def test_keeps_punctuation(self):
        """标点不动：改字等于替用户说话，而标点同时是提示词里的断句。"""
        assert normalize_text("你好，世界。") == "你好，世界。"

    def test_none(self):
        assert normalize_text(None) == ""


# ---------------------------------------------------------------- 打断边界
class TestReaderInterrupt:
    def _reader(self):
        from pet.chat_reply_voice import ChatReplyReader

        return ChatReplyReader(
            speak=lambda s: True,
            channel_idle=lambda: True,
            enabled=lambda: True,
            schedule=lambda ms, fn: None,
        )

    def test_interrupt_clears_queue_keeps_subscription(self):
        """跟 stop 的分工：interrupt 只清这一轮，不能退订——退了就再也不念了。"""
        reader = self._reader()
        reader._subscribed = True
        reader._queue.extend(["第一句", "第二句"])
        reader.interrupt()
        assert list(reader._queue) == []
        assert reader.subscribed is True, "interrupt 不许退订"

    def test_stop_does_unsubscribe(self):
        reader = self._reader()
        reader._subscribed = True
        reader._queue.extend(["第一句"])
        reader.stop()
        assert list(reader._queue) == []
        assert reader.subscribed is False
