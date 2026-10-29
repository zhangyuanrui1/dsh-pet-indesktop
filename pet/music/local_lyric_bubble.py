# -*- coding: utf-8 -*-
"""本地点歌的歌词气泡：歌在唱时，桌宠头顶冒「歌名 + 当前行」。

与外部播放器链路（``lyric_controller``，联网取词 + SMTC 采样）完全独立：

- 取词源是歌文件夹里的**本地 .lrc**（``local_library`` 扫描出来的），零网络请求；
- 进度直接问 ``LocalMusicPlayer.position()``——自己的播放器，进度是精确值，
  不需要 lyric_controller 那套「估算 + 校准 + 提前量」；
- 气泡礼仪逐条对齐 ``lyric_controller``：歌词是**最低优先级**的被动内容，
  告警/审批/交互气泡在展示时让路，点击台词等别人写进气泡的内容不覆盖
  （判据同款：副标题不是我们写的歌名 → 被抢占 → 让路 5 秒）。

为什么是「每拍重送续期」而不是「显示一次挂到句尾」：``show_text`` 的自动隐藏
定时器只有一次机会，句子可能比它长；每拍重送既续命又让换句在 500ms 内生效。
``reserve=False`` 刻意保住：歌词刷新不许动聊天/提醒的气泡预约。

无词歌（没有 .lrc）**不冒泡**——光秃秃飘个歌名只会挡视线，面板里本来就看得到。
"""
from __future__ import annotations

import logging
import time

from PySide6.QtCore import QObject, QTimer

from . import local_library, lyric as music_lyric
from .local_player import shared_player
from .songbook_dialog import _lyric_line_at

log = logging.getLogger("dsh-pet-standalone")

#: 歌词气泡的轮询/续期间隔（毫秒）。与压低轮询同拍，一个 500ms 定时器成本可忽略。
POLL_MS = 500
#: 被别人的气泡抢占后让路多久（秒）。
LYRIC_YIELD_SECONDS = 5.0


class LocalLyricBubble(QObject):
    """把本地点歌的当前歌词句送进宿主气泡（每宿主一个，随用随装）。"""

    def __init__(self, host, *, player=None) -> None:
        super().__init__()
        # host 不是 QObject（窗口/壳是，但测试替身不是），不挂父子；
        # 生命周期由宿主上的属性引用兜住（_local_lyric_bubble）。
        self._host = host
        # player 可注入（测试）；生产用共享单例——歌是全局的，歌词跟着歌走。
        self._player = player if player is not None else shared_player()

        self._track_key: tuple[str, str] | None = None   # (歌名, 音频路径)
        self._lyrics: list = []                          # parse_lrc 输出
        self._title_line = ""                            # 副标题（歌名[·版本]）
        self._written: tuple[str, str] = ("", "")        # 我们最后一次写入气泡的内容
        self._yield_until = 0.0
        self._width_locked = False                       # 首句定宽，位置不再跳

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

    # ---------------------------------------------------------------- 主循环

    def _on_tick(self) -> None:
        """每拍：还在放且当前歌有词 → 把当前句送进气泡；否则自然收场。"""
        shower = getattr(self._host, "show_bubble", None)
        if not callable(shower):
            return
        visible = getattr(self._host, "isVisible", None)
        if callable(visible) and not visible():
            return
        # 开关每拍读：设置页改完立刻生效，不用重启（读一个配置项，成本可忽略）。
        config = getattr(self._host, "_config", None)
        if config is not None and not bool(
                config.get("local_music_bubble_lyrics", True)):
            self._forget_track()
            return

        player = self._player
        song = player.current_song
        if song is None or not player.is_playing():
            self._forget_track()
            return

        path = getattr(player, "current_path", "") or ""
        self._ensure_track(song, path)
        if not self._lyrics:
            return  # 无词歌：不冒泡（面板里看歌名就够了）
        line = _lyric_line_at(self._lyrics, player.position())
        if not line:
            return
        if self._yield_active():
            return

        try:
            # 时长给足一拍有余；续期靠每拍重送（歌词是最低优先级内容，
            # 不许动聊天/提醒的气泡预约 → reserve=False）。
            shower(line, duration_ms=POLL_MS * 3, subtitle=self._title_line,
                   title_first=True, width_locked=self._width_locked,
                   reserve=False)
        except Exception:
            log.debug("本地点歌歌词气泡显示失败", exc_info=True)
            return
        self._written = (self._title_line, line)
        # 首句显示完就把宽度定下来，同首歌后续不再改宽（位置不跳）。
        self._width_locked = True

    # ---------------------------------------------------------------- 曲目与歌词

    def _ensure_track(self, song: local_library.Song, path: str) -> None:
        """换歌/换版本 → 重置状态并解析新的 .lrc（每首歌只解析一次）。"""
        key = (song.name, path)
        if key == self._track_key:
            return
        self._forget_track()
        self._track_key = key
        variants = song.variants()
        self._title_line = (song.name if len(variants) <= 1
                            else f"{song.name}（{local_library.variant_label(path)}）")
        if not song.lrc_path:
            return
        try:
            with open(song.lrc_path, encoding="utf-8", errors="replace") as fh:
                self._lyrics = music_lyric.parse_lrc(fh.read())
        except OSError as exc:
            log.warning("本地歌词读取失败 %s（%s）", song.lrc_path, exc)
            self._lyrics = []

    def _forget_track(self) -> None:
        """停了/没歌：清每首歌的状态。气泡靠 duration 自然过期，不主动砸。"""
        self._track_key = None
        self._lyrics = []
        self._title_line = ""
        self._width_locked = False

    # ---------------------------------------------------------------- 气泡礼仪
    # 与 lyric_controller 同款三件套：告警/审批/交互让路、被抢占识别、让路续期。

    def _bubble_blocked(self) -> bool:
        """告警/审批/交互气泡占用期间，歌词让路（优先级最低）。"""
        if getattr(self._host, "_alert_current", None) is not None:
            return True
        if getattr(self._host, "_sticky_bubble_active", False):
            return True
        bubble = getattr(self._host, "_speech_bubble", None)
        if bubble is not None and getattr(bubble, "_interactive_active", False):
            return True
        return False

    def _bubble_taken_by_other(self) -> bool:
        """气泡里现在显示的是不是**别人**的内容（点击台词 / 被动弹窗）。

        歌词每拍重发，若不识别这种情况就会把刚弹出的台词瞬间盖掉。判据：
        气泡可见，且「(副标题, 正文)」不等于**我们最后一次写入的内容**——
        比副标题比对更准：换歌瞬间气泡里挂的还是上一首歌的标题（也是我们
        写的），按歌名比对会把换曲误判成被抢占、白白让路 5 秒。
        """
        bubble = getattr(self._host, "_speech_bubble", None)
        if bubble is None:
            return False
        try:
            if not bubble.isVisible():
                return False
            label = getattr(bubble, "_subtitle_label", None)
            current = label.text() if label is not None else ""
            body = getattr(bubble, "_raw_text", "")
        except Exception:
            return False
        return (current, body) != self._written

    def _yield_active(self) -> bool:
        """别的气泡正在占用：让路并续期，返回 True 表示本次不要显示歌词。

        续期而非一次性跳过——否则让路期结束的瞬间弹窗还在，歌词又盖上去了。
        """
        now = time.monotonic()
        if self._bubble_taken_by_other():
            self._yield_until = now + LYRIC_YIELD_SECONDS
            return True
        if self._bubble_blocked():
            self._yield_until = now + LYRIC_YIELD_SECONDS
            return True
        return now < self._yield_until
