# -*- coding: utf-8 -*-
"""本地音乐播放引擎：独立的 QMediaPlayer，与语音通道（报时/台词）物理分离。

为什么**不**复用语音通道（``voice/chime_service``）：那是"一次一段、新覆盖旧"
的短语音通道，塞一首三分钟的歌进去，一次点击台词就会把歌顶掉，反过来歌也会
占住通道让报时排不上。两路各用各的播放器，冲突只剩"同时出声"一件——那用
压低（ducking）解决。

压低的语义（用户拍板的方案 A）：

- 宿主把"语音通道是否正在出声"以 callable 的形式挂进来（``attach_voice_activity``）；
- 内部 500ms 轮询一次：语音在响 → 音乐音量压到**配置音量的 20%**，语音停 → 恢复；
- 轮询而不是挂信号，是刻意与 ``chime_service`` 解耦——它的信号面是给合成结果
  用的（``synthesized``），没有"开始出声"的信号；为音乐加一个就会动到语音通道
  的接口面。500ms 轮询一个纯属性读取，开销可以忽略。

唱歌动画**不用本模块管**：``window_alerts.check_music_sing`` 走 pycaw 音频峰值
检测，本地播放器的声音本来就从默认输出设备出去，动画链路自动点亮——经典窗口
与 overlay 两套架构都不用接一行线。

播放器进程内单例（``shared_player``）：音乐一次只能放一首，跨窗口共享同一个
实例才不会出现"两个宠各放各的"。
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from . import local_library

log = logging.getLogger("dsh-pet-standalone")

#: 语音响起时音乐压低到的比例（相对配置音量）。
DUCK_RATIO = 0.2
#: 轮询语音活动状态的间隔（毫秒）。
DUCK_POLL_MS = 500


class LocalMusicPlayer(QObject):
    """单曲播放 + 音量压低。所有公开方法线程口径：Qt 主线程。"""

    track_changed = Signal(str, str)      # 歌名, 音频路径
    state_changed = Signal(bool)          # True=播放中
    position_changed = Signal(int, int)   # 当前 ms, 总长 ms
    playback_failed = Signal(str, str)    # 歌名, 原因

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._audio = QAudioOutput()
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio)
        self._config_volume = 80          # 配置音量（0~100），压低在其上打折
        self._prefer_cover = True         # 点歌默认版本（宿主每次打开菜单时推送）
        self._voice_active_fn = None      # 宿主挂的"语音是否正在出声"
        self._ducked = False
        self._playlist: list[local_library.Song] = []
        self._current: local_library.Song | None = None
        self._current_path = ""           # 正在播的音频文件（歌词气泡标版本用）

        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self._on_position)
        self._player.playbackStateChanged.connect(self._on_state)
        # 用 errorOccurred(带错误串) 而不是 errorChanged(无参)：后者在 PySide6 里
        # 不带参数，接一个收参的槽会当场 TypeError。
        self._player.errorOccurred.connect(self._on_error)
        self._player.mediaStatusChanged.connect(self._on_media_status)

        self._duck_timer = QTimer(self)
        self._duck_timer.setInterval(DUCK_POLL_MS)
        self._duck_timer.timeout.connect(self._apply_volume)
        self._duck_timer.start()

    # ------------------------------------------------------------ 播放控制

    def play_song(self, song: local_library.Song, *, prefer_cover: bool | None = None) -> None:
        """点歌入口：按偏好挑版本并起播（正在放的歌重复点 = 从头再放）。

        ``prefer_cover`` 不传时沿用上次的选择；进程启动后的默认值由宿主
        （菜单/面板，能读到配置的一方）经 :meth:`set_prefer_cover` 推过来，
        本模块**不自己读配置**——它没有配置视图，猜只会猜错。
        """
        prefer = self._prefer_cover if prefer_cover is None else bool(prefer_cover)
        self._prefer_cover = prefer
        path = local_library.choose_variant(song, prefer)
        self.play_path(song, path)

    def play_path(self, song: local_library.Song, path: str) -> None:
        log.info("点歌播放：%s ← %s", song.name, path)
        self._current = song
        self._current_path = path
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()
        self.track_changed.emit(song.name, path)

    def toggle_play_pause(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        elif self._player.source().isValid():
            self._player.play()

    def stop(self) -> None:
        self._player.stop()

    def seek(self, position_ms: int) -> None:
        self._player.setPosition(max(0, position_ms))

    def skip(self, offset: int) -> None:
        """歌单内切歌：+1 下一首 / -1 上一首（循环）。没有歌单就忽略。"""
        if not self._playlist or self._current is None:
            return
        names = [s.name for s in self._playlist]
        try:
            index = names.index(self._current.name)
        except ValueError:
            index = -1
        nxt = self._playlist[(index + offset) % len(self._playlist)]
        self.play_song(nxt)

    def set_playlist(self, songs: list[local_library.Song]) -> None:
        self._playlist = list(songs)

    def can_skip(self) -> bool:
        """歌单里能不能切歌（菜单置灰判据；skip 自身也有同款守卫）。"""
        return bool(self._playlist) and self._current is not None

    # ------------------------------------------------------------ 音量与压低

    def set_prefer_cover(self, prefer: bool) -> None:
        """宿主推送「点歌默认播翻唱版」。每次菜单/面板打开时重推（配置随时可改）。"""
        self._prefer_cover = bool(prefer)

    def set_volume(self, value: int) -> None:
        """设置配置音量（0~100）。压低比例叠加在其上，不写回配置。"""
        self._config_volume = max(0, min(100, int(value)))
        self._apply_volume()

    def attach_voice_activity(self, fn) -> None:
        """挂"语音通道是否正在出声"的探针（callable -> bool）。None 解除。"""
        self._voice_active_fn = fn

    def _apply_volume(self) -> None:
        voice = False
        if self._voice_active_fn is not None:
            try:
                voice = bool(self._voice_active_fn())
            except Exception:  # noqa: BLE001 —— 探针坏了不许拖垮音乐
                log.exception("音乐压低探针异常，本次按无语音处理")
                voice = False
        if voice != self._ducked:
            self._ducked = voice
            log.info("音乐音量%s（语音%s）",
                     "压低至 20%" if voice else "恢复", "响起" if voice else "结束")
        factor = DUCK_RATIO if self._ducked else 1.0
        self._audio.setVolume(self._config_volume / 100.0 * factor)

    # ------------------------------------------------------------ 内部信号转发

    def _on_position(self, *_position_ms) -> None:
        self.position_changed.emit(
            self._player.position(), self._player.duration())

    def _on_state(self, state) -> None:
        self.state_changed.emit(
            state == QMediaPlayer.PlaybackState.PlayingState)

    def _on_error(self, error, error_string: str = "") -> None:
        if error != QMediaPlayer.Error.NoError:
            name = self._current.name if self._current else "?"
            reason = error_string or self._player.errorString() or str(error)
            log.error("本地音乐播放失败：%s（%s）", name, reason)
            self.playback_failed.emit(name, reason)

    def _on_media_status(self, status) -> None:
        # InvalidMedia 也要上报：损坏文件不一定走 errorChanged（实测 Win 上
        # 不自洽的 flac 直接置 InvalidMedia 而无错误串）。
        if status == QMediaPlayer.MediaStatus.InvalidMedia:
            name = self._current.name if self._current else "?"
            self.playback_failed.emit(name, "音频无法解码（文件损坏或格式不支持）")

    # ------------------------------------------------------------ 状态查询

    @property
    def current_song(self) -> local_library.Song | None:
        return self._current

    @property
    def current_path(self) -> str:
        return self._current_path

    def is_playing(self) -> bool:
        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def position(self) -> int:
        return max(int(self._player.position() or 0), 0)

    def duration(self) -> int:
        return max(int(self._player.duration() or 0), 0)


_shared: LocalMusicPlayer | None = None


def shared_player() -> LocalMusicPlayer:
    """进程内单例。首次调用在 Qt 主线程发生（菜单/面板/宿主接线都在主线程）。"""
    global _shared
    if _shared is None:
        _shared = LocalMusicPlayer()
    return _shared
