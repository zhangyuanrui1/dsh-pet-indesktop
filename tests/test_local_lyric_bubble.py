# -*- coding: utf-8 -*-
"""本地点歌歌词气泡（``music/local_lyric_bubble``）。

重点验证三件与"歌词是最低优先级内容"有关的事：唱歌时正常上气泡、
别人占气泡时让路、以及无词歌不冒泡。零网络请求——取词源是本地 .lrc 文件。
"""
import pytest

from PySide6.QtWidgets import QApplication

from pet.music import local_library
from pet.music.local_lyric_bubble import LocalLyricBubble


@pytest.fixture(scope="module")
def app():
    from PySide6.QtCore import QCoreApplication

    return QCoreApplication.instance() or QCoreApplication([])


# ---------------------------------------------------------------- 替身

class FakePlayer:
    """duck-type LocalMusicPlayer 的只读面。"""

    def __init__(self, song=None, path="", *, playing=False, position_ms=0):
        self._song = song
        self._path = path
        self._playing = playing
        self._position_ms = position_ms

    @property
    def current_song(self):
        return self._song

    @property
    def current_path(self):
        return self._path

    def is_playing(self):
        return self._playing

    def position(self):
        return self._position_ms


class FakeBubble:
    def __init__(self):
        self.subtitle = ""
        self.body = ""
        self.visible = False
        self.interactive = False

    def isVisible(self):  # noqa: N802 (Qt 命名)
        return self.visible

    @property
    def _interactive_active(self):
        return self.interactive

    @property
    def _raw_text(self):
        return self.body

    class _Label:
        """模仿 QLabel 的只读面：控制器读的是 ``text()`` 方法（QLabel 同款）。"""

        def __init__(self, bubble):
            self._bubble = bubble

        def text(self):
            return self._bubble.subtitle

    @property
    def _subtitle_label(self):
        return self._Label(self)


class FakeHost:
    def __init__(self):
        self.shown = []
        self.visible = True
        self.alert_current = None
        self.sticky_active = False
        self._speech_bubble = FakeBubble()

    def isVisible(self):  # noqa: N802 (Qt 命名)
        return self.visible

    @property
    def _alert_current(self):
        return self.alert_current

    @property
    def _sticky_bubble_active(self):
        return self.sticky_active

    def show_bubble(self, text, duration_ms=3200, subtitle=None, *,
                    title_first=False, width_locked=False, reserve=True,
                    **_ignored):
        self.shown.append({
            "text": text,
            "subtitle": subtitle or "",
            "duration_ms": duration_ms,
            "title_first": title_first,
            "width_locked": width_locked,
            "reserve": reserve,
        })
        # 写进气泡（供被抢占识别用）
        self._speech_bubble.body = text
        self._speech_bubble.subtitle = subtitle or ""
        self._speech_bubble.visible = True


def _make_song(tmp_path, *, name="晴天", with_lyrics=True, covers=0):
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    original = folder / f"{name}.mp3"
    original.write_bytes(b"\x00" * 8)
    lrc_path = ""
    if with_lyrics:
        lrc = folder / f"{name}.lrc"
        lrc.write_text("[00:01.00]第一句歌词\n[00:30.00]第二句歌词\n",
                       encoding="utf-8")
        lrc_path = str(lrc)
    cover_paths = []
    for i in range(covers):
        cover = folder / f"{name}-龙呼呼版{i or ''}.mp3"
        cover.write_bytes(b"\x00" * 8)
        cover_paths.append(str(cover))
    return local_library.Song(
        name=name, original_path=str(original),
        cover_paths=tuple(cover_paths), lrc_path=lrc_path,
        folder=str(folder),
    )


def _controller(host, player):
    return LocalLyricBubble(host, player=player)


# ---------------------------------------------------------------- 用例

class TestShowsLyrics:
    def test_playing_song_with_lyrics_shows_line(self, tmp_path, app):
        song = _make_song(tmp_path)
        player = FakePlayer(song, song.original_path, playing=True,
                            position_ms=2000)
        host = FakeHost()
        _controller(host, player)._on_tick()
        assert len(host.shown) == 1
        call = host.shown[0]
        assert call["text"] == "第一句歌词"
        assert call["subtitle"] == "晴天"
        assert call["title_first"] is True
        assert call["reserve"] is False, "歌词刷新不许动聊天/提醒的气泡预约"

    def test_line_advances_with_position(self, tmp_path, app):
        song = _make_song(tmp_path)
        host = FakeHost()
        player = FakePlayer(song, song.original_path, playing=True,
                            position_ms=31000)
        _controller(host, player)._on_tick()
        assert host.shown[0]["text"] == "第二句歌词"

    def test_multi_variant_title_carries_version(self, tmp_path, app):
        song = _make_song(tmp_path, covers=1)
        player = FakePlayer(song, song.cover_paths[0], playing=True,
                            position_ms=2000)
        host = FakeHost()
        _controller(host, player)._on_tick()
        assert "龙呼呼版" in host.shown[0]["subtitle"]

    def test_width_locked_from_second_tick(self, tmp_path, app):
        song = _make_song(tmp_path)
        player = FakePlayer(song, song.original_path, playing=True,
                            position_ms=2000)
        host = FakeHost()
        ctrl = _controller(host, player)
        ctrl._on_tick()
        ctrl._on_tick()
        assert host.shown[0]["width_locked"] is False
        assert host.shown[1]["width_locked"] is True

    def test_track_change_resets_width_lock(self, tmp_path, app):
        song_a = _make_song(tmp_path, name="晴天")
        song_b = _make_song(tmp_path, name="稻香")
        host = FakeHost()
        player = FakePlayer(song_a, song_a.original_path, playing=True,
                            position_ms=2000)
        ctrl = _controller(host, player)
        ctrl._on_tick()
        ctrl._on_tick()
        assert host.shown[-1]["width_locked"] is True
        player._song = song_b
        player._path = song_b.original_path
        ctrl._on_tick()
        assert host.shown[-1]["width_locked"] is False, "换歌要重新定宽"


class TestYields:
    def test_interactive_bubble_blocks(self, tmp_path, app):
        song = _make_song(tmp_path)
        host = FakeHost()
        host._speech_bubble.interactive = True
        player = FakePlayer(song, song.original_path, playing=True,
                            position_ms=2000)
        _controller(host, player)._on_tick()
        assert host.shown == [], "审批/交互气泡在展示时歌词必须让路"

    def test_foreign_bubble_content_yields(self, tmp_path, app):
        song = _make_song(tmp_path)
        host = FakeHost()
        player = FakePlayer(song, song.original_path, playing=True,
                            position_ms=2000)
        ctrl = _controller(host, player)
        ctrl._on_tick()
        assert host.shown, "前提：第一拍正常显示"
        # 别的内容（点击台词）写进气泡：无副标题、正文不是我们的歌词
        host._speech_bubble.subtitle = ""
        host._speech_bubble.body = "哼哼，本鱼鱼生气了"
        before = len(host.shown)
        ctrl._on_tick()
        assert len(host.shown) == before, "被抢占后要让路，不许把台词瞬间盖掉"

    def test_yields_then_recovers(self, tmp_path, app):
        song = _make_song(tmp_path)
        host = FakeHost()
        player = FakePlayer(song, song.original_path, playing=True,
                            position_ms=2000)
        ctrl = _controller(host, player)
        ctrl._on_tick()
        assert host.shown, "前提：第一拍正常显示"
        # 让路期内（气泡被别人的台词占着）不显示——续期而不是一次性跳过
        host._speech_bubble.subtitle = ""
        host._speech_bubble.body = "别人的台词"
        ctrl._on_tick()
        shown_before = len(host.shown)
        ctrl._on_tick()
        assert len(host.shown) == shown_before
        # 时间越过让路期 → 恢复显示
        ctrl._yield_until -= 6.0
        ctrl._on_tick()
        assert host.shown[-1]["text"] == "第一句歌词"

    def test_sticky_bubble_blocks(self, tmp_path, app):
        song = _make_song(tmp_path)
        host = FakeHost()
        host.sticky_active = True
        player = FakePlayer(song, song.original_path, playing=True,
                            position_ms=2000)
        _controller(host, player)._on_tick()
        assert host.shown == []


class TestConfigSwitch:
    """``local_music_bubble_lyrics`` 开关：关了就不冒泡，开了恢复。"""

    def _host_with_config(self, enabled: bool) -> FakeHost:
        host = FakeHost()

        class _Cfg:
            def get(self, key, default=None):
                if key == "local_music_bubble_lyrics":
                    return enabled
                return default

        host._config = _Cfg()
        return host

    def test_switch_off_shows_nothing(self, tmp_path, app):
        song = _make_song(tmp_path)
        host = self._host_with_config(enabled=False)
        player = FakePlayer(song, song.original_path, playing=True,
                            position_ms=2000)
        _controller(host, player)._on_tick()
        assert host.shown == [], "开关关着时不许冒泡"

    def test_switch_recheck_every_tick(self, tmp_path, app):
        """开关每拍重读：设置页改完立刻生效（开→关→开），不用重启。"""
        song = _make_song(tmp_path)
        host = FakeHost()
        state = {"on": True}

        class _Cfg:
            def get(self, key, default=None):
                if key == "local_music_bubble_lyrics":
                    return state["on"]
                return default

        host._config = _Cfg()
        player = FakePlayer(song, song.original_path, playing=True,
                            position_ms=2000)
        ctrl = _controller(host, player)
        ctrl._on_tick()
        assert len(host.shown) == 1
        state["on"] = False
        ctrl._on_tick()
        assert len(host.shown) == 1, "关掉后下一拍就该停"
        state["on"] = True
        ctrl._on_tick()
        assert len(host.shown) == 2, "重新打开后恢复显示"

    def test_host_without_config_defaults_to_on(self, tmp_path, app):
        song = _make_song(tmp_path)
        host = FakeHost()  # 没有 _config：按开处理
        player = FakePlayer(song, song.original_path, playing=True,
                            position_ms=2000)
        _controller(host, player)._on_tick()
        assert len(host.shown) == 1


class TestBoundaries:
    def test_not_playing_shows_nothing(self, tmp_path, app):
        song = _make_song(tmp_path)
        host = FakeHost()
        player = FakePlayer(song, song.original_path, playing=False)
        _controller(host, player)._on_tick()
        assert host.shown == []

    def test_song_without_lrc_shows_nothing(self, tmp_path, app):
        song = _make_song(tmp_path, with_lyrics=False)
        host = FakeHost()
        player = FakePlayer(song, song.original_path, playing=True,
                            position_ms=2000)
        _controller(host, player)._on_tick()
        assert host.shown == [], "无词歌不冒泡——光秃秃飘个歌名只会挡视线"

    def test_before_first_line_still_shows_upcoming(self, tmp_path, app):
        """前奏期（第一句之前）沿用面板同款语义：显示即将唱到的那句。"""
        song = _make_song(tmp_path)
        host = FakeHost()
        player = FakePlayer(song, song.original_path, playing=True,
                            position_ms=0)
        _controller(host, player)._on_tick()
        assert host.shown[0]["text"] == "第一句歌词"

    def test_host_without_show_bubble_is_noop(self, tmp_path, app):
        song = _make_song(tmp_path)
        player = FakePlayer(song, song.original_path, playing=True,
                            position_ms=2000)
        ctrl = LocalLyricBubble(object(), player=player)  # 无 show_bubble 的宿主
        ctrl._on_tick()  # 不抛即为过
