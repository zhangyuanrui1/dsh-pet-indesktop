# -*- coding: utf-8 -*-
"""台词语音库面板：看得见、试听得了、管得动。

**为什么有它**（2026-10-28 机主需求）：点击动画台词都很短、反复播放，希望直接播
库存音频而不是每次调接口。这件事在**运行时本来就成立**——点击走的
``AppShell.speak_self_talk`` 先查本地库（``pet/voice/self_talk.py`` 的
``find_audio``），命中就 ``play_file``，零延迟、不联网；没命中才合成一次并落盘，
之后同样命中。真正缺的是"看得见与管得动"：

* 库里有哪些台词、哪些还没配音、占多少磁盘——以前完全不可见；
* 改词 / 换音色后旧文件没人引用却一直留着——以前没有清理入口。

于是本面板做三件事：**列清单**（逐条台词 + 状态 + 体积）、**手动一键生成**
（缺哪句补哪句，不受默认关闭的预缓存开关限制）、**清理无用语音**（删掉没有任何
现存台词引用的文件，删前确认、删后可重新生成）。

**不在本面板里做的事**：不碰 ``voice_chime_cache``（报时/回复朗读共用的即时合成
缓存，按需自动复用，删了只是下次重合成，没有管理价值）。本面板只管台词库。
"""
from __future__ import annotations

import logging
import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from .voice import self_talk

logger = logging.getLogger(__name__)

#: 生成时轮询进度的间隔（毫秒）。合成一句要秒级，200ms 足够跟手又不费 CPU。
PROGRESS_POLL_MS = 200


def _human_size(num_bytes: int) -> str:
    """字节数 → 便于阅读的 KB/MB。"""
    size = float(num_bytes or 0)
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{max(size, 0) / 1024:.0f} KB"


class VoiceLibraryDialog(QDialog):
    """台词库清单 + 手动生成 + 清理无用语音。"""

    def __init__(self, config, *, configs=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.configs = configs
        self.snapshot: dict = {}
        # 后台生成的状态（线程只写这几个标量，Qt 侧轮询读取）
        self._progress = {"running": False, "done": 0, "total": 0, "stats": None}
        self._poll = QTimer(self)
        self._poll.setInterval(PROGRESS_POLL_MS)
        self._poll.timeout.connect(self._on_poll)

        self.setWindowTitle("台词语音库")
        self.resize(680, 500)
        self.setMinimumSize(600, 420)

        layout = QVBoxLayout(self)

        hint = QLabel(
            "点击时先说库存音频：命中就直接播（零延迟、断网可用），没有才联网合成一次并入库。\n"
            "「生成缺失语音」把还没配音的台词一次补齐；改动台词或更换音色后，旧音频会变成"
            "无用文件，可用「清理无用语音」删掉（删了不丢数据，下次要用再合）。",
            self,
        )
        hint.setWordWrap(True)
        hint.setObjectName("voiceLibraryHint")
        layout.addWidget(hint)

        self.summary = QLabel("", self)
        self.summary.setObjectName("voiceLibrarySummary")
        layout.addWidget(self.summary)

        self.list = QListWidget(self)
        self.list.setObjectName("voiceLibraryList")
        self.list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.list.currentRowChanged.connect(lambda _row: self._sync_buttons())
        layout.addWidget(self.list, 1)

        row = QHBoxLayout()
        self.gen_btn = QPushButton("生成缺失语音", self)
        self.gen_btn.setObjectName("voiceLibraryGenerateButton")
        self.gen_btn.clicked.connect(self._start_generation)
        self.preview_btn = QPushButton("试听", self)
        self.preview_btn.setObjectName("voiceLibraryPreviewButton")
        self.preview_btn.clicked.connect(self._preview_selected)
        self.clean_btn = QPushButton("清理无用语音", self)
        self.clean_btn.setObjectName("voiceLibraryCleanButton")
        self.clean_btn.clicked.connect(self._clean_orphans)
        self.close_btn = QPushButton("关闭", self)
        self.close_btn.setObjectName("voiceLibraryCloseButton")
        self.close_btn.clicked.connect(self.accept)
        for button in (self.gen_btn, self.preview_btn, self.clean_btn):
            row.addWidget(button)
        row.addStretch(1)
        row.addWidget(self.close_btn)
        layout.addLayout(row)

        self.refresh()

    # ---------------------------------------------------------------- 刷新
    def refresh(self) -> None:
        """重取快照并重绘列表（打开面板、生成结束、清理之后都走这里）。"""
        try:
            self.snapshot = self_talk.library_snapshot(self.config, configs=self.configs)
        except Exception:  # noqa: BLE001 —— 面板打不开比数据不准更糟
            logger.exception("读取台词语音库失败")
            self.snapshot = {}
        entries = list(self.snapshot.get("entries") or [])
        current = self.list.currentRow()

        self.list.clear()
        for entry in entries:
            ready = entry.get("path") is not None
            alt = entry.get("fallback_path")
            if ready:
                status = "已配音"
            elif alt is not None:
                # 库里躺着的是备用后端（edge）的旧音频：能兜底，但不是当前音色，
                # 标成"已配音"会让用户以为点下去就是选的那把嗓子。
                status = "待生成（现为备用音色）"
            else:
                status = "待生成"
            size = f" · {_human_size(entry.get('size') or 0)}" if ready else ""
            item = QListWidgetItem(f"[{status}] {entry['source']}：{entry['text']}{size}")
            tip = f"来源：{entry['source']}\n台词：{entry['text']}"
            if alt is not None and not ready:
                tip += ("\n现有文件是备用后端（edge）合成的旧音频，音色与当前设置不同；"
                        "点「生成缺失语音」会用当前音色重新合成。")
            item.setToolTip(tip)
            if ready:
                item.setData(Qt.ItemDataRole.UserRole, str(entry["path"]))
            else:
                item.setForeground(Qt.GlobalColor.gray)
            self.list.addItem(item)

        total = len(entries)
        ready_count = int(self.snapshot.get("ready") or 0)
        orphan_count = len(self.snapshot.get("orphan_paths") or [])
        self.summary.setText(
            f"台词 {total} 条：已配音 {ready_count} 条，待生成 {total - ready_count} 条 ｜ "
            f"台词库占用 {_human_size(self.snapshot.get('total_bytes') or 0)}"
            + (f"（其中无用文件 {orphan_count} 个，"
               f"{_human_size(self.snapshot.get('orphan_bytes') or 0)}）" if orphan_count else "")
        )
        if entries:
            self.list.setCurrentRow(min(max(current, 0), len(entries) - 1))
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        """按状态决定按钮可用性；生成中禁用生成按钮（防重复点击起两轮）。"""
        running = bool(self._progress["running"])
        missing = int(self.snapshot.get("missing") or 0)
        orphans = len(self.snapshot.get("orphan_paths") or [])
        self.gen_btn.setEnabled(not running and missing > 0)
        self.gen_btn.setText("生成中…" if running else "生成缺失语音")
        self.clean_btn.setEnabled(not running and orphans > 0)
        self.preview_btn.setEnabled(not running and self._selected_path() is not None)

    def _selected_path(self):
        item = self.list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    # ---------------------------------------------------------------- 动作
    def _preview_selected(self) -> None:
        """试听选中台词（走与点击音效同一个播放池，不占报时音频通道）。"""
        path = self._selected_path()
        if not path:
            return
        try:
            from .click_sound import play_sound

            play_sound(path)
        except Exception:
            logger.exception("试听台词失败：%s", path)

    def _start_generation(self) -> None:
        """后台补齐缺失台词（不受默认关闭的预缓存开关限制：点按钮即授权）。"""
        if self._progress["running"]:
            return
        self._progress.update(running=True, done=0, total=0, stats=None)
        self._sync_buttons()
        self.summary.setText("正在生成台词语音…")
        config, configs = self.config, self.configs
        state = self._progress

        def work() -> None:
            def on_progress(done: int, total: int) -> None:
                state["done"], state["total"] = done, total

            try:
                state["stats"] = self_talk.run_precache(
                    config, configs=configs, respect_switch=False, progress=on_progress)
            except Exception:
                logger.exception("生成台词语音失败")
                state["stats"] = {"reason": "error"}
            finally:
                state["running"] = False

        threading.Thread(target=work, name="voice-library-generate", daemon=True).start()
        self._poll.start()

    def _on_poll(self) -> None:
        """轮询后台生成进度；结束后停表、刷新、按结果给一句结论。"""
        state = self._progress
        if state["running"]:
            total = int(state["total"])
            done = int(state["done"])
            if total:
                self.summary.setText(f"正在生成台词语音… {done}/{total}")
            return
        self._poll.stop()
        stats = state.get("stats") or {}
        self.refresh()
        made = int(stats.get("made") or 0)
        failed = int(stats.get("failed") or 0)
        reason = str(stats.get("reason") or "")
        if reason == "no-backend":
            self.summary.setText(
                "没有可用的语音后端：先在「语音报时」里点一次试听确认能出声，再回来生成。")
        elif reason == "error":
            self.summary.setText("生成失败，详情见日志。")
        else:
            self.summary.setText(
                f"本次生成 {made} 条" + (f"，失败 {failed} 条（详见日志）" if failed else "，全部完成。"))

    def _clean_orphans(self) -> None:
        """删掉没有任何现存台词引用的音频（改词 / 换音色留下的旧文件）。"""
        orphans = list(self.snapshot.get("orphan_paths") or [])
        if not orphans:
            return
        answer = QMessageBox.question(
            self,
            "清理无用语音",
            f"将删除 {len(orphans)} 个没有任何台词引用的音频文件，"
            f"释放约 {_human_size(self.snapshot.get('orphan_bytes') or 0)}。\n\n"
            "这些文件对应的是改动前的旧台词或旧音色的产物；删掉不影响现在的播放"
            "（真的再需要时会重新合成）。继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed, freed = self_talk.delete_orphans(self.snapshot)
        self.refresh()
        failed = len(orphans) - removed
        self.summary.setText(
            f"已清理 {removed} 个文件，释放 {_human_size(freed)}"
            + (f"；{failed} 个删除失败（可能正被占用）" if failed else "。"))
