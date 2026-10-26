# -*- coding: utf-8 -*-
"""「语音识别模型」这一行的状态机回归：未装 / 已装 / 下载中 / 取消 / 失败。

为什么值得单独锁：这一行是**唯一**能让用户把 229 MB 模型装上的界面入口，而它的
状态全从磁盘算（设置页关掉就没了）。四个状态之间走错一步，用户看到的就是
「点了没反应」「明明装好了还让我下载」或者「取消之后要重下 150 MB」。

下载本身不在这里测（``tests/test_asr_fetch.py`` 用本地 HTTP 服务测），这里把
``fetch.fetch_models`` 换成同步假实现，只验界面怎么随结果变化。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pet.asr import fetch  # noqa: E402
from pet.voice.asr_model_row import AsrModelRow  # noqa: E402


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _fake_sizes(monkeypatch) -> None:
    """把三个文件的期望体积改小，测试里才造得出"已就绪"。"""
    for name in fetch.MODEL_FILES:
        monkeypatch.setitem(fetch.EXPECTED_SIZE, name, 4)


def _install_files(root: Path) -> None:
    for name in fetch.MODEL_FILES:
        (root / name).write_bytes(b"abcd")


def _row(tmp_path) -> AsrModelRow:
    _qapp()
    return AsrModelRow(directory=tmp_path)


def _run_sync(row: AsrModelRow) -> None:
    """同步跑一次下载体（不启线程，免得与 pytest 抢时序）。

    ``_run_download`` 里的 emit 在同线程是直连，所以状态在返回前就已经更新完——
    这也正是把这些用例写成同步的原因：状态机该怎么走，不该由线程调度决定。
    """
    row._token = fetch.CancelToken()   # noqa: SLF001
    row._set_busy(True)                # noqa: SLF001
    row._run_download(row.target_dir(), row._token)  # noqa: SLF001


def _button_text(row: AsrModelRow) -> str:
    return row._button.text()  # noqa: SLF001 - 测试直接读控件状态


def test_missing_shows_download_button(tmp_path, monkeypatch):
    _fake_sizes(monkeypatch)
    row = _row(tmp_path)
    assert "下载模型" in _button_text(row)
    assert row._button.isEnabled()  # noqa: SLF001
    assert "未安装" in row._hint.text()  # noqa: SLF001
    assert row._bar.isHidden()  # noqa: SLF001


def test_ready_shows_installed_and_disables_button(tmp_path, monkeypatch):
    _fake_sizes(monkeypatch)
    _install_files(tmp_path)
    row = _row(tmp_path)
    assert _button_text(row) == "已安装"
    assert not row._button.isEnabled()  # noqa: SLF001
    assert "已安装" in row._hint.text()  # noqa: SLF001


def test_download_success_switches_to_installed(tmp_path, monkeypatch):
    _fake_sizes(monkeypatch)
    row = _row(tmp_path)
    installed = []
    row.installed.connect(lambda: installed.append(True))

    def fake_fetch(target, *, progress=None, notify=None, cancel=None, **_kw):
        if notify:
            notify("下载识别模型包（约 155 MB）")
        if progress:
            progress(50, 100)
            progress(100, 100)
        _install_files(Path(target))
        return list(fetch.MODEL_FILES)

    monkeypatch.setattr(fetch, "fetch_models", fake_fetch)

    _run_sync(row)

    assert _button_text(row) == "已安装"
    assert row._bar.isHidden()  # noqa: SLF001
    assert installed == [True]


def test_progress_updates_bar_and_hint(tmp_path, monkeypatch):
    _fake_sizes(monkeypatch)
    row = _row(tmp_path)
    row._set_busy(True)  # noqa: SLF001

    row._on_progress(70 * 1024 * 1024, 155 * 1024 * 1024)  # noqa: SLF001
    assert row._bar.value() == 45  # noqa: SLF001
    assert "70.0 MB" in row._bar.format()  # noqa: SLF001

    row._on_note("下载断句模型（silero_vad.onnx）")  # noqa: SLF001
    assert "silero_vad.onnx" in row._hint.text()  # noqa: SLF001


def test_cancel_leaves_resume_button(tmp_path, monkeypatch):
    _fake_sizes(monkeypatch)
    row = _row(tmp_path)

    def fake_fetch(target, *, progress=None, notify=None, cancel=None, **_kw):
        if cancel:
            cancel.cancel()
        raise fetch.FetchCancelled("已取消")

    monkeypatch.setattr(fetch, "fetch_models", fake_fetch)

    _run_sync(row)

    assert _button_text(row) == "继续下载"
    assert row._button.isEnabled()  # noqa: SLF001
    assert "已取消" in row._hint.text()  # noqa: SLF001
    assert row._bar.isHidden()  # noqa: SLF001


def test_failure_offers_retry(tmp_path, monkeypatch):
    _fake_sizes(monkeypatch)
    row = _row(tmp_path)

    def fake_fetch(target, *, progress=None, notify=None, cancel=None, **_kw):
        raise fetch.FetchError("重试 6 次仍未下完（已下 12.0 MB）")

    monkeypatch.setattr(fetch, "fetch_models", fake_fetch)

    _run_sync(row)

    assert _button_text(row) == "重试"
    assert "失败" in row._hint.text()  # noqa: SLF001


def test_cancel_button_marks_token(tmp_path, monkeypatch):
    _fake_sizes(monkeypatch)
    row = _row(tmp_path)
    row._token = fetch.CancelToken()   # noqa: SLF001
    row._set_busy(True)                # noqa: SLF001
    token = row._token                 # noqa: SLF001

    row._on_cancel_clicked()  # noqa: SLF001

    assert token.cancelled
    assert not row._cancel_button.isEnabled()  # noqa: SLF001
    assert "正在停下" in row._hint.text()  # noqa: SLF001


def test_busy_state_shows_cancel_and_hides_it_after(tmp_path, monkeypatch):
    _fake_sizes(monkeypatch)
    row = _row(tmp_path)
    assert row._cancel_button.isHidden()  # noqa: SLF001

    row._set_busy(True)  # noqa: SLF001
    assert not row._cancel_button.isHidden()  # noqa: SLF001
    assert not row._button.isEnabled()  # noqa: SLF001

    row._on_finished(True, "已安装并通过校验")  # noqa: SLF001
    assert row._cancel_button.isHidden()  # noqa: SLF001


def test_refresh_ignored_while_busy(tmp_path, monkeypatch):
    """下载中刷一次状态不能把"下载中"打回"未安装"（否则按钮会中途变可点）。"""
    _fake_sizes(monkeypatch)
    row = _row(tmp_path)
    row._set_busy(True)  # noqa: SLF001

    row.refresh()

    assert not row._button.isEnabled()  # noqa: SLF001
    assert not row._bar.isHidden()  # noqa: SLF001
