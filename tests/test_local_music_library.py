# -*- coding: utf-8 -*-
"""本地点歌：扫描层与版本选择的纯逻辑测试（零 Qt）。

用例按真实歌库（``D:\\dshjingyu\\music``）的形状造临时目录：每首歌一个文件夹，
内含原曲 / 翻唱 / .lrc；重点钉住几条容易回归的规则——

- 判别只看**文件名内容**（"龙呼呼版"标记），不看文件夹名（真实歌库里
  ``芊芊-阿月月/`` 里面叫 ``阿YueYue``）；
- 没有原曲只有翻唱的文件夹也要能出歌（第一份翻唱顶原曲位）；
- 目录不存在 / 没有音频 → 空歌单，绝不抛（菜单和面板直接吃这个返回值）。
"""
from __future__ import annotations

import os

import pytest

from pet.music import local_library as ll


# --------------------------------------------------------------- 造歌库夹具

def _make_song(root, name, *, original="晴天-周杰伦.mp3", covers=(), lrc="晴天-周杰伦.lrc"):
    folder = root / name
    folder.mkdir(parents=True)
    if original:
        (folder / original).write_bytes(b"\x00" * 16)
    for cover in covers:
        (folder / cover).write_bytes(b"\x00" * 16)
    if lrc:
        (folder / lrc).write_text("[00:01.00]测试歌词", encoding="utf-8")
    return folder


@pytest.fixture
def lib(tmp_path):
    return tmp_path / "music"


# --------------------------------------------------------------- 扫描与判别

class TestDiscoverSongs:
    def test_standard_folder(self, lib):
        _make_song(lib, "晴天", covers=("晴天-龙呼呼版.mp3",))
        songs = ll.discover_songs(str(lib))
        assert [s.name for s in songs] == ["晴天"]
        song = songs[0]
        assert song.original_path.endswith("晴天-周杰伦.mp3")
        assert len(song.cover_paths) == 1
        assert song.cover_paths[0].endswith("晴天-龙呼呼版.mp3")
        assert song.lrc_path.endswith("晴天-周杰伦.lrc")

    def test_folder_name_is_display_name_not_inner_file(self, lib):
        # 真实歌库：文件夹「芊芊-阿月月」里面叫「芊芊-阿YueYue.mp3」。
        _make_song(lib, "芊芊-阿月月", original="芊芊-阿YueYue.mp3",
                   lrc="芊芊-阿YueYue.lrc")
        songs = ll.discover_songs(str(lib))
        assert songs[0].name == "芊芊-阿月月"
        assert songs[0].original_path.endswith("芊芊-阿YueYue.mp3")

    def test_flac_original_recognized(self, lib):
        _make_song(lib, "客官不可以", original="客官不可以-徐良.flac",
                   covers=("客官不可以-龙呼呼版.mp3",))
        songs = ll.discover_songs(str(lib))
        assert songs[0].original_path.endswith(".flac")

    def test_multiple_covers_sorted(self, lib):
        _make_song(lib, "有点甜", covers=(
            "有点甜-龙呼呼版2.mp3", "有点甜-龙呼呼版1.mp3"))
        songs = ll.discover_songs(str(lib))
        assert [os.path.basename(p) for p in songs[0].cover_paths] == [
            "有点甜-龙呼呼版1.mp3", "有点甜-龙呼呼版2.mp3"]

    def test_only_cover_still_a_song(self, lib):
        # 没有原曲：第一份翻唱顶原曲位，variants() 永不为空。
        _make_song(lib, "翻唱only", original="", covers=("歌-龙呼呼版.mp3",))
        songs = ll.discover_songs(str(lib))
        assert songs[0].original_path.endswith("歌-龙呼呼版.mp3")
        assert songs[0].has_cover() is False  # 顶原曲位的那份不再算翻唱
        assert songs[0].variants() == (songs[0].original_path,)

    def test_lrc_prefers_name_matching_original(self, lib):
        _make_song(lib, "如果爱忘了", original="如果爱忘了.mp3",
                   lrc="如果爱忘了+(Live).lrc")
        extra = lib / "如果爱忘了" / "如果爱忘了.lrc"
        extra.write_text("[00:01.00]另一份", encoding="utf-8")
        songs = ll.discover_songs(str(lib))
        assert songs[0].lrc_path.endswith("如果爱忘了.lrc")

    def test_folder_without_audio_skipped(self, lib):
        _make_song(lib, "真歌")
        draft = lib / "歌词草稿"
        draft.mkdir()
        (draft / "词.txt").write_text("不是歌", encoding="utf-8")
        songs = ll.discover_songs(str(lib))
        assert [s.name for s in songs] == ["真歌"]

    def test_loose_audio_at_root_counts(self, lib):
        lib.mkdir()
        (lib / "散装.mp3").write_bytes(b"\x00" * 16)
        (lib / "散装.lrc").write_text("[00:01.00]词", encoding="utf-8")
        songs = ll.discover_songs(str(lib))
        assert len(songs) == 1
        assert songs[0].name == "散装"
        assert songs[0].folder == ""

    def test_sorted_by_name(self, lib):
        # 按名字的 Unicode 码点排序：晴(6674) < 爱(7231) < 稻(7A3B)。
        _make_song(lib, "晴天")
        _make_song(lib, "稻香")
        _make_song(lib, "爱你")
        assert [s.name for s in ll.discover_songs(str(lib))] == ["晴天", "爱你", "稻香"]

    @pytest.mark.parametrize("bad", ["", None, "不存在的目录", "config.json"])
    def test_bad_root_returns_empty(self, tmp_path, bad):
        if bad == "config.json":
            real = tmp_path / "config.json"
            real.write_text("{}", encoding="utf-8")
            bad = str(real)
        elif bad == "不存在的目录":
            bad = str(tmp_path / "nope")
        assert ll.discover_songs(bad) == []


# --------------------------------------------------------------- 版本选择

class TestVariants:
    def test_prefer_cover_falls_back_to_original(self, lib):
        _make_song(lib, "晴天", covers=("晴天-龙呼呼版.mp3",))
        _make_song(lib, "幻听", original="幻听-许嵩.mp3", lrc="幻听-许嵩.lrc")
        by_name = {s.name: s for s in ll.discover_songs(str(lib))}
        assert ll.choose_variant(by_name["晴天"], True).endswith("龙呼呼版.mp3")
        assert ll.choose_variant(by_name["晴天"], False).endswith("晴天-周杰伦.mp3")
        assert ll.choose_variant(by_name["幻听"], True).endswith("幻听-许嵩.mp3")

    def test_variant_label(self):
        assert ll.variant_label("D:/m/晴天-龙呼呼版.mp3") == "龙呼呼版"
        assert ll.variant_label("D:/m/晴天-周杰伦.mp3") == "原版"


# --------------------------------------------------------------- 点歌菜单的播放控制
# 「让人家歇一会儿嘛 / 给主人换一首 / 人家想再听刚才那首 / 人家今天不唱了」四件套
# 复用外部播放器那组的文案，目标换成**本地播放器**。用例钉住：文案在场、
# 按播放器状态置灰、回调真的打到 skip/stop/toggle 上。


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class _FakeCfg:
    def __init__(self, mapping):
        self._d = dict(mapping)

    def get(self, key, default=None):
        return self._d.get(key, default)


class _FakePet:
    def __init__(self, root):
        self.cfg = _FakeCfg({"local_music_dir": str(root)})


class _FakeLocalPlayer:
    """duck-type LocalMusicPlayer 的控制面。

    刻意**不**用真的共享播放器：那会构造 QMediaPlayer / QAudioOutput，
    测试进程退出时 Qt 多媒体对象收尾不稳（实测段错误 EXIT=127）。菜单
    回调只碰这几个方法，替身记录调用就够了。
    """

    def __init__(self):
        self.current_song = None
        self.current_path = ""
        self.playlist: list = []
        self.calls: list = []

    def can_skip(self):
        return bool(self.playlist) and self.current_song is not None

    def set_prefer_cover(self, prefer):
        self.calls.append(("prefer_cover", prefer))

    def set_volume(self, value):
        self.calls.append(("volume", value))

    def set_playlist(self, songs):
        self.playlist = list(songs)

    def play_song(self, song, *, prefer_cover=None):
        self.calls.append(("play_song", song.name))

    def toggle_play_pause(self):
        self.calls.append("toggle")

    def stop(self):
        self.calls.append("stop")

    def skip(self, offset):
        self.calls.append(("skip", offset))


@pytest.fixture
def menu_env(qapp, tmp_path, monkeypatch):
    """造歌库 + 假共享播放器（见 _FakeLocalPlayer：真构造会崩进程）。"""
    from pet.context_menus import shared as menu_shared
    from pet.music import local_player as player_mod

    for name, covers in (("晴天", ("晴天-龙呼呼版.mp3",)), ("稻香", ())):
        _make_song(tmp_path, name, covers=covers)
    player = _FakeLocalPlayer()
    monkeypatch.setattr(menu_shared, "_shared_local_player", lambda: player)
    monkeypatch.setattr(player_mod, "shared_player", lambda: player)
    yield tmp_path, player


def _build_request_menu(pet):
    """构造点歌子菜单。父 QMenu 一并返回——没人引用时 C++ 对象会被回收，
    子菜单跟着陪葬（shiboken "Internal C++ object already deleted"）。"""
    from PySide6.QtWidgets import QMenu

    from pet.context_menus.shared import add_music_request

    menu = QMenu()
    return menu, add_music_request(menu, pet)


def _items(submenu):
    return [(a.text(), a.isEnabled()) for a in submenu.actions() if a.text()]


def _find(submenu, needle):
    for a in submenu.actions():
        if needle in a.text():
            return a
    return None


class TestRequestMenuControls:
    PAUSE = "让人家歇一会儿嘛"
    NEXT = "给主人换一首"
    PREV = "人家想再听刚才那首"
    STOP = "人家今天不唱了"

    def test_four_controls_present_with_local_labels(self, menu_env):
        root, _player = menu_env
        _menu, submenu = _build_request_menu(_FakePet(root))
        texts = [t for t, _enabled in _items(submenu)]
        for needle in (self.PAUSE, self.NEXT, self.PREV, self.STOP):
            assert any(needle in t for t in texts), texts
        # 歌单也在：控制组之后是歌名列表
        assert any("晴天" in t for t in texts)

    def test_disabled_without_current_song(self, menu_env):
        root, player = menu_env
        assert player.current_song is None, "前提：还没放过任何歌"
        _menu, submenu = _build_request_menu(_FakePet(root))
        for needle in (self.PAUSE, self.NEXT, self.PREV, self.STOP):
            action = _find(submenu, needle)
            assert action is not None and not action.isEnabled(), needle

    def test_enabled_once_a_song_is_loaded(self, menu_env):
        root, player = menu_env
        songs = ll.discover_songs(str(root))
        player.set_playlist(songs)
        player.current_song = songs[0]
        _menu, submenu = _build_request_menu(_FakePet(root))
        for needle in (self.PAUSE, self.NEXT, self.PREV, self.STOP):
            action = _find(submenu, needle)
            assert action is not None and action.isEnabled(), needle

    def test_next_action_skips_forward(self, menu_env):
        root, player = menu_env
        songs = ll.discover_songs(str(root))
        player.set_playlist(songs)
        player.current_song = songs[0]
        _menu, submenu = _build_request_menu(_FakePet(root))
        player.calls.clear()  # 建造器同步配置也走播放器，只看本次点击的调用
        _find(submenu, self.NEXT).trigger()
        assert player.calls == [("skip", +1)]

    def test_stop_action_stops_player(self, menu_env):
        root, player = menu_env
        songs = ll.discover_songs(str(root))
        player.set_playlist(songs)
        player.current_song = songs[0]
        _menu, submenu = _build_request_menu(_FakePet(root))
        player.calls.clear()  # 建造器同步配置也走播放器，只看本次点击的调用
        _find(submenu, self.STOP).trigger()
        assert player.calls == ["stop"]

    def test_toggle_action_toggles_playback(self, menu_env):
        root, player = menu_env
        songs = ll.discover_songs(str(root))
        player.set_playlist(songs)
        player.current_song = songs[0]
        _menu, submenu = _build_request_menu(_FakePet(root))
        player.calls.clear()  # 建造器同步配置也走播放器，只看本次点击的调用
        _find(submenu, self.PAUSE).trigger()
        assert player.calls == ["toggle"]

    def test_sing_still_plays_via_shared_player(self, menu_env):
        """点歌回调走同一个替身播放器（起了歌，控制组才有得控）。"""
        root, player = menu_env
        songs = ll.discover_songs(str(root))
        player.set_playlist(songs)
        _menu, submenu = _build_request_menu(_FakePet(root))
        _find(submenu, "晴天").trigger()
        assert ("play_song", "晴天") in player.calls
