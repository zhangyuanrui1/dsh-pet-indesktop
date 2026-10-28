# -*- coding: utf-8 -*-
"""歌单面板：本地音乐目录的歌单、点播、进度与本地 LRC 歌词。

**数据从哪来**：``local_library.discover_songs`` 扫描配置的音乐目录（每首歌一个
文件夹：原曲 + 翻唱 + .lrc）。扫描在后台线程跑——190MB / 几百个文件的 stat
不该卡 GUI 线程；结果经 150ms 轮询取回（与 ``voice_library_dialog`` 同一套模式）。

**歌词从哪来**：只读歌文件夹里的本地 ``.lrc``，**绝不联网**——联网取词是外部
播放器链路（``lyric_controller``）的事，这里的歌自带歌词文件，不需要猜。

**播放器是谁**：进程级共享的 ``LocalMusicPlayer``（独立 QMediaPlayer，不挤语音
通道）。音量 / 默认版本在打开时从配置同步进播放器；语音压低探针在 AppShell
创建语音服务时已接好，与本面板无关。
"""
from __future__ import annotations

import logging
import os
import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QVBoxLayout,
)

from . import local_library, lyric as music_lyric
from .local_player import shared_player

logger = logging.getLogger(__name__)

#: 扫描结果轮询间隔（毫秒）。
SCAN_POLL_MS = 150
#: 歌词刷新间隔（毫秒）。歌词行以秒为粒度，250ms 足够跟手。
LYRIC_POLL_MS = 250


def _lyric_line_at(lines, position_ms: int) -> str:
    """当前播放位置对应的歌词行（含它后面一句作下一行提示）。

    ``lines`` 是 ``parse_lrc`` 的输出（已按时间排序）。位置落在第一句之前
    返回空串（前奏），落在行间空档（间奏空行）也返回空——间奏就该没词。
    """
    current = ""
    upcoming = ""
    for line in lines:
        if line.at * 1000 <= position_ms:
            current = line.text
        else:
            upcoming = line.text
            break
    return current if current else upcoming


class SongbookDialog(QDialog):
    """歌单 + 版本选择 + 播放控制 + 本地歌词。"""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.songs: list[local_library.Song] = []
        self._lyrics: list = []          # parse_lrc 输出，当前歌有 .lrc 时填充
        self._updating_slider = False    # 拖动进度条时回填会打架，先关掉

        self.setWindowTitle("点歌 · 本地歌单")
        self.resize(560, 420)
        self.player = shared_player()

        root_layout = QVBoxLayout(self)

        self.song_list = QListWidget(self)
        self.song_list.itemDoubleClicked.connect(lambda _item: self._play_default())
        self.song_list.currentItemChanged.connect(self._on_song_selected)
        root_layout.addWidget(QLabel("双击一首歌开始播（或选中后点播放）：", self))
        root_layout.addWidget(self.song_list, stretch=3)

        # 版本行：版本下拉 + 播放 / 暂停 / 停止 / 上一首 / 下一首
        version_row = QHBoxLayout()
        self.version_combo = QComboBox(self)
        self.version_combo.setToolTip("同一首歌的不同版本（原曲 / 龙呼呼翻唱），切换即换源播")
        self._loading_variants = False    # 重填下拉时会触发 currentIndexChanged，别误当用户切版本
        self.version_combo.currentIndexChanged.connect(self._on_version_changed)
        version_row.addWidget(self.version_combo, stretch=1)
        self.prev_btn = QPushButton("上一首", self)
        self.play_btn = QPushButton("播放", self)
        self.stop_btn = QPushButton("停止", self)
        self.next_btn = QPushButton("下一首", self)
        for btn, slot in (
            (self.prev_btn, lambda: self.player.skip(-1)),
            (self.play_btn, self._toggle_play),
            (self.stop_btn, self.player.stop),
            (self.next_btn, lambda: self.player.skip(+1)),
        ):
            btn.clicked.connect(slot)
            version_row.addWidget(btn)
        root_layout.addLayout(version_row)

        # 进度行
        self.position_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.position_slider.setRange(0, 0)
        self.position_slider.sliderPressed.connect(self._hold_slider)
        self.position_slider.sliderReleased.connect(self._release_slider)
        self.position_slider.sliderMoved.connect(self._seek_preview)
        root_layout.addWidget(self.position_slider)
        self.time_label = QLabel("00:00 / 00:00", self)
        root_layout.addWidget(self.time_label)

        # 歌词区：当前一句大字 + 原始两行备查
        self.lyric_label = QLabel("", self)
        self.lyric_label.setWordWrap(True)
        self.lyric_label.setStyleSheet("font-size: 15px; font-weight: 500;")
        self.lyric_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root_layout.addWidget(self.lyric_label, stretch=1)

        # 音量行
        volume_row = QHBoxLayout()
        volume_row.addWidget(QLabel("音乐音量", self))
        self.volume_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(int(config.get("local_music_volume", 80)))
        self.volume_slider.valueChanged.connect(self._on_volume_changed)
        self.volume_slider.sliderReleased.connect(self._save_volume)
        volume_row.addWidget(self.volume_slider, stretch=1)
        root_layout.addLayout(volume_row)

        self.status_label = QLabel("", self)
        root_layout.addWidget(self.status_label)

        # 播放器信号 → 面板状态
        self.player.track_changed.connect(self._on_track_changed)
        self.player.state_changed.connect(self._on_state_changed)
        self.player.position_changed.connect(self._on_position_changed)
        self.player.playback_failed.connect(self._on_play_failed)

        # 歌词行刷新定时器（播放中才真的刷）
        self._lyric_timer = QTimer(self)
        self._lyric_timer.setInterval(LYRIC_POLL_MS)
        self._lyric_timer.timeout.connect(self._refresh_lyric)
        self._lyric_timer.start()

        # 打开时把配置同步进共享播放器（音量 / 默认版本）
        self.player.set_volume(int(config.get("local_music_volume", 80)))
        self.player.set_prefer_cover(bool(config.get("local_music_prefer_cover", True)))

        self._start_scan()

    # ------------------------------------------------------------ 扫描

    def _start_scan(self) -> None:
        root = str(self.config.get("local_music_dir") or "").strip()
        if not root:
            self.status_label.setText(
                "还没有配置音乐目录。到 设置 → 音乐 → 本地点歌 里选择歌曲文件夹。")
            return
        self.status_label.setText(f"正在扫描 {root} …")
        result: dict = {}
        thread = threading.Thread(
            target=self._scan_worker, args=(root, result), daemon=True)
        thread.start()
        self._scan_poll = QTimer(self)
        self._scan_poll.setInterval(SCAN_POLL_MS)
        self._scan_poll.timeout.connect(lambda: self._poll_scan(result, thread))
        self._scan_poll.start()

    @staticmethod
    def _scan_worker(root: str, result: dict) -> None:
        result["songs"] = local_library.discover_songs(root)

    def _poll_scan(self, result: dict, thread: threading.Thread) -> None:
        if thread.is_alive():
            return
        self._scan_poll.stop()
        self.songs = result.get("songs") or []
        self.player.set_playlist(self.songs)
        self._reload_song_list()
        if not self.songs:
            self.status_label.setText("这个目录下没有找到歌曲（每首歌一个子文件夹）。")
        else:
            covers = sum(1 for s in self.songs if s.has_cover())
            self.status_label.setText(
                f"共 {len(self.songs)} 首，其中 {covers} 首有龙呼呼翻唱版。")

    def _reload_song_list(self) -> None:
        self.song_list.clear()
        for song in self.songs:
            mark = "　♪有翻唱" if song.has_cover() else ""
            item = QListWidgetItem(song.name + mark)
            item.setData(Qt.ItemDataRole.UserRole, song.name)
            item.setToolTip(f"原曲：{song.original_path}")
            self.song_list.addItem(item)
        self.version_combo.clear()
        self._lyrics = []

    # ------------------------------------------------------------ 播放

    def _selected_song(self) -> local_library.Song | None:
        item = self.song_list.currentItem()
        if item is None:
            return None
        name = item.data(Qt.ItemDataRole.UserRole)
        for song in self.songs:
            if song.name == name:
                return song
        return None

    def _play_default(self) -> None:
        song = self._selected_song()
        if song is not None:
            self.player.play_song(song)

    def _toggle_play(self) -> None:
        self.player.toggle_play_pause()

    def _play_variant(self) -> None:
        song = self._selected_song()
        if song is None:
            return
        path = self.version_combo.currentData()
        if path:
            self.player.play_path(song, path)

    def _on_song_selected(self, _item=None, _previous=None) -> None:
        """选中歌变化 → 重填版本下拉（默认选中配置偏好的那版）。"""
        song = self._selected_song()
        self.version_combo.clear()
        self._lyrics = []
        if song is None:
            return
        prefer_cover = bool(self.config.get("local_music_prefer_cover", True))
        variants = song.variants()
        default_path = local_library.choose_variant(song, prefer_cover)
        self._loading_variants = True
        try:
            for index, path in enumerate(variants):
                label = local_library.variant_label(path)
                if sum(1 for p in variants
                       if local_library.variant_label(p) == label) > 1:
                    # 同名版本（版1/版2）：带上文件名区分。
                    label = f"{label} · {os.path.basename(path)}"
                self.version_combo.addItem(label, path)
                if path == default_path:
                    self.version_combo.setCurrentIndex(index)
        finally:
            self._loading_variants = False
        self.version_combo.setVisible(len(variants) > 1)
        if song.lrc_path:
            try:
                with open(song.lrc_path, encoding="utf-8", errors="replace") as fh:
                    self._lyrics = music_lyric.parse_lrc(fh.read())
            except OSError as exc:
                logger.warning("歌词读取失败 %s（%s）", song.lrc_path, exc)
                self._lyrics = []
        self.lyric_label.setText("")

    def _on_version_changed(self, _index: int) -> None:
        """用户手动切版本（下拉重填不算）→ 立刻换源播这一版。"""
        if self._loading_variants:
            return
        self._play_variant()

    # ------------------------------------------------------------ 播放器回填

    def _on_track_changed(self, name: str, path: str) -> None:
        self.status_label.setText(f"正在播放：{name}（{local_library.variant_label(path)}）")
        # 歌单里选中项跟随正在播的歌
        for row in range(self.song_list.count()):
            if self.song_list.item(row).data(Qt.ItemDataRole.UserRole) == name:
                self.song_list.setCurrentRow(row)
                break

    def _on_state_changed(self, playing: bool) -> None:
        self.play_btn.setText("暂停" if playing else "播放")

    def _on_position_changed(self, position_ms: int, duration_ms: int) -> None:
        if not self._updating_slider:
            self.position_slider.setRange(0, max(duration_ms, 0))
            self.position_slider.setValue(max(position_ms, 0))
        self.time_label.setText(
            f"{self._fmt(position_ms)} / {self._fmt(duration_ms)}")

    def _on_play_failed(self, name: str, reason: str) -> None:
        self.status_label.setText(f"《{name}》播放失败：{reason}")

    def _refresh_lyric(self) -> None:
        if not self._lyrics or not self.player.is_playing():
            return
        line = _lyric_line_at(self._lyrics, self.player.position())
        if line and line != self.lyric_label.text():
            self.lyric_label.setText(line)

    # ------------------------------------------------------------ 进度条 / 音量

    def _hold_slider(self) -> None:
        self._updating_slider = True

    def _release_slider(self) -> None:
        self._updating_slider = False
        self.player.seek(self.position_slider.value())

    def _seek_preview(self, value: int) -> None:
        self.time_label.setText(f"{self._fmt(value)} / {self._fmt(self.position_slider.maximum())}")

    def _on_volume_changed(self, value: int) -> None:
        self.player.set_volume(value)

    def _save_volume(self) -> None:
        self.config.set("local_music_volume", int(self.volume_slider.value()))
        self.config.save()

    @staticmethod
    def _fmt(ms: int) -> str:
        seconds = max(int(ms or 0), 0) // 1000
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    # ------------------------------------------------------------ 生命周期

    def closeEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        # 关面板不停歌：音乐是挂着的，面板只是遥控器。
        super().closeEvent(event)
