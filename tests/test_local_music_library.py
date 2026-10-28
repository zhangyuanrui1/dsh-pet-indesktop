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
