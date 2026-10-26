# -*- coding: utf-8 -*-
"""「语音识别模型」那一行：状态 + 下载按钮 + 进度条 + 取消。

放在设置 → 语音 页的下方（语音输入组）。它**不是持久化设置项**，所以不用
``SettingRow``——与角色包页 ``_InfoRow`` 同一条理由（每个 ``SettingRow`` 都必须
归属某个功能域，否则会被当成漏登记的配置项）。

三个设计要点：

1. **下载跑在 worker 线程**（``threading.Thread(daemon=True)``），界面只在主线程动。
   进度与阶段文案一律经 Qt 信号回主线程，绝不在 worker 里碰控件。
2. **状态从磁盘算，不从内存记**：设置页是独立进程，关掉就没了；而模型在
   ``resources/asr_models/`` 里。每次显示都用体积判一遍（毫秒级，不算 239 MB 的
   sha256），下载完成后再做一次完整校验。
3. **取消要能在几百毫秒内停下**：``CancelToken`` 由下载核心在每块数据之间查，
   UI 只负责置位；已下的 ``.part`` 保留，下次点"继续下载"接着下。
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..asr import fetch, model_dir

logger = logging.getLogger(__name__)

#: 按钮文案（就绪/可下载/下载中/可继续）
_LABEL_DOWNLOAD = "下载模型（{size}）"
_LABEL_READY = "已安装"
_LABEL_RETRY = "重试"
_LABEL_RESUME = "继续下载"


class AsrModelRow(QWidget):
    """一行：标题 + 状态说明 + 下载/取消按钮 + 进度条。

    ``installed`` 在下载并通过校验后发出，给页面刷新其它提示用（当前没有订阅者
    也不影响：语音入口每次按热键时都会现场重查 ``availability()``）。
    """

    installed = Signal()

    #: worker 线程 → 主线程（AutoConnection 跨线程自动排队，不能直接改控件）
    _progress = Signal(int, int)
    _note = Signal(str)
    _finished = Signal(bool, str)

    def __init__(self, parent: QWidget | None = None, *, directory=None) -> None:
        super().__init__(parent)
        self._dir = directory
        self._token: fetch.CancelToken | None = None
        self._busy = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        title = QLabel("语音识别模型", self)
        title.setObjectName("settingLabel")
        title.setWordWrap(True)
        layout.addWidget(title)

        self._hint = QLabel("", self)
        self._hint.setObjectName("settingHint")
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)
        self._button = QPushButton(self)
        self._button.clicked.connect(self._on_button_clicked)
        controls.addWidget(self._button)
        self._cancel_button = QPushButton("取消", self)
        self._cancel_button.setToolTip("停下当前下载。已下的部分会保留，下次接着下。")
        self._cancel_button.clicked.connect(self._on_cancel_clicked)
        self._cancel_button.hide()
        controls.addWidget(self._cancel_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self._bar = QProgressBar(self)
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(True)
        self._bar.hide()
        layout.addWidget(self._bar)

        self._progress.connect(self._on_progress)
        self._note.connect(self._on_note)
        self._finished.connect(self._on_finished)
        self.refresh()

    # ------------------------------------------------------------ 对外
    def target_dir(self):
        """模型目录（``DSH_PET_ASR_MODEL_DIR`` 优先，否则随包 resources/asr_models）。"""
        return self._dir or model_dir()

    def refresh(self) -> None:
        """按磁盘现状刷新状态（不打断正在进行的下载）。"""
        if self._busy:
            return
        states = fetch.quick_states(self.target_dir())
        gaps = [name for name, (ok, _) in states.items() if not ok]
        size_text = fetch.human_size(fetch.TOTAL_BYTES)
        if not gaps:
            self._set_idle(_LABEL_READY, enabled=False,
                           hint=f"已安装（{size_text}）。语音对话可以直接用：按热键说话，"
                                "说完停顿 1 秒自动发出去。")
            return
        reason = "、".join(gaps)
        self._set_idle(_LABEL_DOWNLOAD.format(size=size_text), enabled=True,
                       hint=f"未安装（缺 {reason}，共 {size_text}）。"
                            "下载后就能对着麦克风说话，识别在本机完成、不上传。")

    # ------------------------------------------------------------ 内部：状态
    def _set_idle(self, text: str, *, enabled: bool, hint: str) -> None:
        self._button.setText(text)
        self._button.setEnabled(enabled)
        self._hint.setText(hint)
        self._cancel_button.hide()
        self._bar.hide()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._button.setEnabled(not busy)
        self._cancel_button.setVisible(busy)
        self._bar.setVisible(busy)

    # ------------------------------------------------------------ 内部：动作
    def _on_button_clicked(self) -> None:
        if self._busy:
            return
        target = self.target_dir()
        self._token = fetch.CancelToken()
        token = self._token
        self._set_busy(True)
        self._bar.setValue(0)
        self._bar.setFormat("准备中…")
        self._hint.setText("正在准备下载…")
        threading.Thread(target=lambda: self._run_download(target, token),
                         daemon=True, name="asr-model-dl").start()

    def _run_download(self, target, token: fetch.CancelToken) -> None:
        """下载体（在 worker 线程跑；测试直接同步调它）。

        所有对界面的影响都经信号回到主线程——这里碰控件就是跨线程改 QWidget。
        """
        try:
            fetch.fetch_models(
                target,
                progress=lambda got, total: self._safe_emit(self._progress, got, total),
                notify=lambda text: self._safe_emit(self._note, text),
                cancel=token,
            )
        except fetch.FetchCancelled:
            self._safe_emit(self._finished, False, "已取消（已下的部分保留，下次接着下）")
        except fetch.FetchError as exc:
            self._safe_emit(self._finished, False, f"失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - 任何异常都要变成人话显示
            logger.exception("语音识别模型下载失败")
            self._safe_emit(self._finished, False, f"失败：{type(exc).__name__}: {exc}")
        else:
            self._safe_emit(self._finished, True, "已安装并通过校验")

    def _on_cancel_clicked(self) -> None:
        token = self._token
        if token is None:
            return
        token.cancel()
        self._cancel_button.setEnabled(False)
        self._hint.setText("正在停下…")

    # ------------------------------------------------------------ 内部：worker → 主线程
    def _safe_emit(self, signal, *args) -> None:
        """控件可能已随设置页关闭被删：没人收就丢掉（与角色包页同一纪律）。"""
        try:
            signal.emit(*args)
        except RuntimeError:
            pass

    def _on_progress(self, got: int, total: int) -> None:
        if not self._busy:
            return
        if total <= 0:
            self._bar.setFormat(f"{fetch.human_size(got)}…")
            return
        percent = min(99, got * 100 // total)
        self._bar.setValue(percent)
        self._bar.setFormat(
            f"{percent}%  {fetch.human_size(got)} / {fetch.human_size(total)}")

    def _on_note(self, text: str) -> None:
        if self._busy:
            # 阶段文案走说明行（进度条只显示字节），两者不抢同一处
            self._hint.setText(text)

    def _on_finished(self, ok: bool, message: str) -> None:
        self._set_busy(False)
        self._cancel_button.setEnabled(True)
        self._token = None
        if ok:
            self.refresh()
            self.installed.emit()
            return
        # 失败/取消：按钮变成"重试"或"继续下载"，已下的 .part 还在
        cancelled = message.startswith("已取消")
        self._button.setText(_LABEL_RESUME if cancelled else _LABEL_RETRY)
        self._button.setEnabled(True)
        self._hint.setText(message)
        self._bar.hide()


__all__ = ["AsrModelRow"]
