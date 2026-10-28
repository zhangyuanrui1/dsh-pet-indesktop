# -*- coding: utf-8 -*-
"""台词语音库面板的回归（快照 / 无用文件清理 / 手动生成 / 对话框）。

产品契约：
- 点击本来就说库存音频（``AppShell.speak_self_talk`` → ``find_audio`` → ``play_file``），
  不是每次调接口；本面板只是让它**看得见、管得动**；
- 快照按「当前台词名单」判定：有产物=已配音，没产物=待生成；
- **无用文件** = 台词库里没有任何现存台词引用的音频（改词 / 换音色留下的），
  清理只删这些，且删后可重新生成 → 没有数据风险；
- 「生成缺失语音」是**手动授权**：开关默认关着也必须能跑（``respect_switch=False``），
  否则点按钮毫无反应，用户只会以为坏了；自动触发（保存设置 / 启动）仍受开关约束。

纪律：不打网络（假 provider 写结构自洽的 wav），Qt 用例不弹真对话框。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pet import tts
from pet.config import Config
from pet.voice import self_talk as stv

# --------------------------------------------------------------- 替身


def _wav(payload: bytes = b"\x00\x00" * 16) -> bytes:
    """结构自洽的最小 wav（查找侧会核对 RIFF 头声明的长度）。"""
    fmt = (
        b"fmt " + (16).to_bytes(4, "little") + b"\x01\x00\x01\x00"
        + (16000).to_bytes(4, "little") + (32000).to_bytes(4, "little")
        + b"\x02\x00\x10\x00"
    )
    data = b"data" + len(payload).to_bytes(4, "little") + payload
    body = b"WAVE" + fmt + data
    return b"RIFF" + (len(body) - 4).to_bytes(4, "little") + body


class _FakeProvider(tts.TtsProvider):
    """假合成后端：write_bytes 一段 wav，不联网。"""

    id = "vlfake"
    label = "假合成后端（语音库测试）"
    fields = (tts.TtsField(name="voice", key="vl_fake_voice", label="音色", default="v1"),)
    is_fallback = False

    def __init__(self) -> None:
        self.calls: list[str] = []

    def plan(self, values):
        return {"ext": "wav"}

    def flavor(self, values):
        return f"vlfake|{values.get('voice') or ''}"

    def availability(self, values):
        return ""

    def preflight(self, values):
        return values, ""

    def synth(self, text, plan, out_path):
        self.calls.append(text)
        Path(out_path).write_bytes(_wav())


@pytest.fixture
def fake_provider():
    provider = _FakeProvider()
    tts.register(provider)
    try:
        yield provider
    finally:
        tts.unregister(provider.id)


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _cfg(tmp_path, *, switch: bool = False, backend: str = "") -> Config:
    """两条自言自语 + 一条点击绑定（其中一条与全局重复，名单要去重）。

    ``backend`` 指定合成后端：合成类用例必须钉住假后端，否则会落到默认的 edge
    真去联网（测试不许打网络）。
    """
    cfg = Config(base=tmp_path)
    cfg.set("self_talk_texts", ["好女孩……", "再陪你一会儿。"])
    cfg.set_click_talk_bindings("shenshen", {"click-1": ["绑定的专属台词。", "好女孩……"]})
    cfg.set("self_talk_voice_precache_enabled", switch)
    if backend:
        cfg.set("voice_chime_tts_backend", backend)
        cfg.set("voice_chime_tts_fallback", False)
    return cfg


def _library(cfg) -> Path:
    return Path(cfg.dir) / stv.CACHE_DIR_NAME


def _write_classic(cfg, text: str, blob: bytes | None = None) -> Path:
    """按经典文本键写一个产物（等价于外部脚本/在线合成留下的文件）。"""
    path = stv.cache_path(cfg.dir, text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob if blob is not None else _wav())
    return path


# --------------------------------------------------------------- 快照


def test_snapshot_marks_ready_and_missing(tmp_path, monkeypatch):
    """有产物的算已配音，没产物的算待生成；名单按文本去重（3 条不是 4 条）。"""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(stv, "_attempts", lambda _cfg: ())
    _write_classic(cfg, "好女孩……")

    snap = stv.library_snapshot(cfg)

    assert [e["text"] for e in snap["entries"]] == [
        "好女孩……", "再陪你一会儿。", "绑定的专属台词。"]
    assert snap["ready"] == 1
    assert snap["missing"] == 2
    ready = next(e for e in snap["entries"] if e["text"] == "好女孩……")
    assert ready["path"] is not None and ready["size"] > 0


def test_snapshot_flags_orphans_only_for_unreferenced_files(tmp_path, monkeypatch):
    """改词/换音色留下的文件才算无用；现存台词的产物绝不能被当成无用。"""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(stv, "_attempts", lambda _cfg: ())
    kept = _write_classic(cfg, "好女孩……")
    stale = _library(cfg) / "0123456789abcdef.mp3"       # 谁都不引用的旧文件
    stale.write_bytes(b"ID3" + b"\x00" * 100)
    temp = _library(cfg) / f"{stv.TEMP_PREFIX}inflight.wav"  # 在飞的临时文件不算库里条目
    temp.write_bytes(b"x")

    snap = stv.library_snapshot(cfg)

    assert snap["orphan_paths"] == [stale]
    assert kept not in snap["orphan_paths"]
    assert temp not in snap["orphan_paths"]
    assert snap["total_bytes"] >= kept.stat().st_size + stale.stat().st_size


def test_snapshot_on_missing_dir_is_empty_not_failure(tmp_path, monkeypatch):
    """台词库还没建（一句都没配过音）：空快照，不抛异常。"""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(stv, "_attempts", lambda _cfg: ())

    snap = stv.library_snapshot(cfg)

    assert snap["ready"] == 0 and snap["missing"] == 3
    assert snap["total_bytes"] == 0 and snap["orphan_paths"] == []


# --------------------------------------------------------------- 清理


def test_delete_orphans_removes_only_unreferenced(tmp_path, monkeypatch):
    """只删无用文件；现存台词的产物必须原样保留（删了就得重新合成）。"""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(stv, "_attempts", lambda _cfg: ())
    kept = _write_classic(cfg, "好女孩……")
    stale = _library(cfg) / "deadbeefdeadbeef.mp3"
    stale.write_bytes(b"ID3" + b"\x00" * 64)
    snap = stv.library_snapshot(cfg)

    removed, freed = stv.delete_orphans(snap)

    assert (removed, freed) == (1, 67)   # b"ID3" + 64 字节
    assert not stale.exists()
    assert kept.exists(), "被引用的音频不许删"
    assert stv.library_snapshot(cfg)["orphan_paths"] == []


def test_delete_orphans_survives_locked_file(tmp_path, monkeypatch):
    """单个文件删不掉（被占用）跳过，不影响其他删除。"""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(stv, "_attempts", lambda _cfg: ())
    first = _library(cfg) / "aaaaaaaaaaaaaaaa.mp3"
    second = _library(cfg) / "bbbbbbbbbbbbbbbb.mp3"
    _library(cfg).mkdir(parents=True, exist_ok=True)
    first.write_bytes(b"1")
    second.write_bytes(b"2")
    real_unlink = Path.unlink

    def guarded(self, *args, **kwargs):
        if self.name == first.name:
            raise OSError("locked")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", guarded)
    removed, freed = stv.delete_orphans(stv.library_snapshot(cfg))

    assert (removed, freed) == (1, 1)
    assert first.exists() and not second.exists()


# --------------------------------------------------------------- 手动生成


def test_manual_precache_runs_with_switch_off(tmp_path, fake_provider):
    """手动「生成缺失语音」：开关默认关着也要真的合成（点按钮即授权）。"""
    cfg = _cfg(tmp_path, switch=False, backend="vlfake")
    stats = stv.run_precache(cfg, respect_switch=False)

    assert stats["made"] == 3, "三句台词都该合成（重复的那句只算一次）"
    assert stats["failed"] == 0
    assert set(fake_provider.calls) == {"好女孩……", "再陪你一会儿。", "绑定的专属台词。"}


def test_automatic_precache_still_respects_switch(tmp_path, fake_provider):
    """自动触发（保存设置 / 启动）仍受开关约束：关着一个请求都不发。"""
    cfg = _cfg(tmp_path, switch=False, backend="vlfake")
    stats = stv.run_precache(cfg)

    assert stats["made"] == 0
    assert stats["reason"] == "switched-off"
    assert fake_provider.calls == []


def test_start_precache_manual_bypasses_switch(tmp_path, fake_provider, monkeypatch):
    """后台入口也一样：手动模式能起线程，自动模式开关关着直接返回 False。"""
    cfg = _cfg(tmp_path, switch=False, backend="vlfake")
    assert stv.start_precache(cfg) is False
    assert stv.start_precache(cfg, respect_switch=False) is True


def test_generation_progress_callback_reports_each_line(tmp_path, fake_provider):
    """进度回调按句上报，且回调抛异常不影响合成。"""
    cfg = _cfg(tmp_path, switch=False, backend="vlfake")
    seen: list[tuple[int, int]] = []

    def progress(done, total):
        seen.append((done, total))

    stv.run_precache(cfg, respect_switch=False, progress=progress)
    assert seen == [(1, 3), (2, 3), (3, 3)]

    seen.clear()

    def boom(done, total):
        raise RuntimeError("ui gone")

    stats = stv.run_precache(cfg, respect_switch=False, progress=boom)
    assert seen == [] and stats["made"] == 0, "已全部有产物时不再合成，也不该被回调异常打断"


# --------------------------------------------------------------- 对话框


def test_dialog_lists_lines_with_status(tmp_path, app, monkeypatch):
    """面板：逐条列出台词与状态，待生成时生成按钮可用。"""
    from pet.voice_library_dialog import VoiceLibraryDialog

    cfg = _cfg(tmp_path)
    monkeypatch.setattr(stv, "_attempts", lambda _cfg: ())
    _write_classic(cfg, "好女孩……")

    dialog = VoiceLibraryDialog(cfg)
    try:
        assert dialog.list.count() == 3
        texts = [dialog.list.item(i).text() for i in range(dialog.list.count())]
        assert any(text.startswith("[已配音]") for text in texts)
        assert sum(text.startswith("[待生成]") for text in texts) == 2
        assert dialog.gen_btn.isEnabled(), "还有待生成的台词，生成按钮必须可点"
        assert not dialog.clean_btn.isEnabled(), "没有无用文件时清理按钮禁用"
    finally:
        dialog.deleteLater()


def test_dialog_cleans_orphans_after_confirmation(tmp_path, app, monkeypatch):
    """面板清理：确认后删无用文件、刷新统计。"""
    from pet.voice_library_dialog import VoiceLibraryDialog, QMessageBox

    cfg = _cfg(tmp_path)
    monkeypatch.setattr(stv, "_attempts", lambda _cfg: ())
    _write_classic(cfg, "好女孩……")
    stale = _library(cfg) / "ffffffffffffffff.mp3"
    stale.write_bytes(b"ID3" + b"\x00" * 32)
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)

    dialog = VoiceLibraryDialog(cfg)
    try:
        assert dialog.clean_btn.isEnabled()
        dialog._clean_orphans()
        assert not stale.exists()
        assert "已清理 1 个文件" in dialog.summary.text()
    finally:
        dialog.deleteLater()


def test_dialog_cleanup_cancelled_deletes_nothing(tmp_path, app, monkeypatch):
    """点「否」什么都不删。"""
    from pet.voice_library_dialog import VoiceLibraryDialog, QMessageBox

    cfg = _cfg(tmp_path)
    monkeypatch.setattr(stv, "_attempts", lambda _cfg: ())
    stale = _library(cfg) / "cccccccccccccccc.mp3"
    _library(cfg).mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"ID3" + b"\x00" * 8)
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.No)

    dialog = VoiceLibraryDialog(cfg)
    try:
        dialog._clean_orphans()
        assert stale.exists()
    finally:
        dialog.deleteLater()
