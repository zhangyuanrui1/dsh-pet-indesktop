# -*- coding: utf-8 -*-
"""本地歌库扫描：把音乐目录解析成「点歌」用的歌单（纯逻辑，零 Qt）。

目录约定（实测 ``D:\\dshjingyu\\music``，桌宠唱歌任务的产物）：

    music/
    ├── 晴天/
    │   ├── 晴天-周杰伦.mp3        ← 原曲（mp3 / flac 都认）
    │   ├── 晴天-周杰伦.lrc         ← 歌词（带时间轴，缺了不致命）
    │   └── 晴天-龙呼呼版.mp3       ← 翻唱版（可有多个：…版1 / …版2）
    └── 稻香-周杰伦/ …

判别只看**文件内容特征**，不看文件夹名：文件夹名与内部文件名并不一致
（``芊芊-阿月月/`` 里面叫 ``阿YueYue``），靠外层名字猜一定猜错。规则：

- 含 ``COVER_MARK``（"龙呼呼版"）的音频 → 翻唱；其余音频 → 原曲（至多一首，
  多首时取名字最短的那个并在 log 里说明，正常歌库不会出现）；
- ``.lrc`` → 歌词；一首歌最多带一份，多份取与原曲同名优先，再退字母序；
- 文件夹里没有任何音频 → 整个文件夹跳过（不是歌，比如只放歌词的草稿夹）。

扫描在后台线程跑（面板/菜单都不该被几百个 stat 卡住），所以本模块**零 Qt、
零磁盘阻塞副作用**——就是 ``os.scandir`` + 纯函数，单测不需要任何 Qt 夹具。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger("dsh-pet-standalone")

#: 翻唱版的文件名标记（桌宠唱歌任务产物统一带这个后缀）。
COVER_MARK = "龙呼呼版"

#: 认作音频的扩展名（小写）。flac 走 Windows 系统解码（Media Foundation，Win10+ 自带）。
AUDIO_EXTS = {".mp3", ".flac", ".wav", ".m4a", ".ogg"}

#: 歌词扩展名。
LYRIC_EXT = ".lrc"

#: 点歌子菜单最多直接列多少首：超过就提示去面板（右键菜单不该无限长）。
MENU_MAX_SONGS = 30


@dataclass(frozen=True)
class Song:
    """一首歌：文件夹 + 原曲 + 翻唱列表 + 歌词。

    ``name`` 取**文件夹名**（无文件夹的散装音频取文件名主干）——它是用户在
    歌单里看到的、也是点歌菜单里点的那个名字。
    """

    name: str
    original_path: str
    cover_paths: tuple[str, ...] = ()
    lrc_path: str = ""
    folder: str = ""

    def has_cover(self) -> bool:
        return bool(self.cover_paths)

    def variants(self) -> tuple[str, ...]:
        """全部可播版本：原曲永远在，翻唱按字母序排在后面。"""
        return (self.original_path, *sorted(self.cover_paths))


def _audio_files(names: list[str]) -> list[str]:
    return [n for n in names if os.path.splitext(n)[1].lower() in AUDIO_EXTS]


def _split_original_covers(audio: list[str]) -> tuple[str, list[str]]:
    """把一个文件夹里的音频分成（原曲, 翻唱列表）。

    翻唱按字母序稳定排序（"版1"/"版2" 的顺序才可预期）；原曲在多首时取名字
    最短的一个——剩下的按翻唱处理会让"另一版"出现在版本菜单里，好过整个丢掉。
    """
    covers = sorted(n for n in audio if COVER_MARK in n)
    originals = sorted(n for n in audio if COVER_MARK not in n)
    if not originals:
        # 只有翻唱没有原曲：第一份翻唱顶原曲位（保证 variants() 永不为空）。
        return covers.pop(0), covers
    original = min(originals, key=len)
    extra = [n for n in originals if n != original]
    return original, covers + extra


def _pick_lrc(lyrics: list[str], original: str) -> str:
    """一首歌多份 .lrc 时的取舍：与原曲同名优先，其次字母序第一个。"""
    if not lyrics:
        return ""
    stem = os.path.splitext(original)[0]
    same = sorted(n for n in lyrics if os.path.splitext(n)[0] == stem)
    return same[0] if same else sorted(lyrics)[0]


def _scan_folder(dirpath: str, dirname: str) -> Song | None:
    try:
        names = os.listdir(dirpath)
    except OSError as exc:
        log.warning("歌库扫描：跳过读不了的文件夹 %s（%s）", dirpath, exc)
        return None
    audio = _audio_files(names)
    if not audio:
        return None
    original, covers = _split_original_covers(audio)
    lrc = _pick_lrc(
        [n for n in names if n.lower().endswith(LYRIC_EXT)], original
    )
    return Song(
        name=dirname,
        original_path=os.path.join(dirpath, original),
        cover_paths=tuple(os.path.join(dirpath, n) for n in covers),
        lrc_path=os.path.join(dirpath, lrc) if lrc else "",
        folder=dirpath,
    )


def discover_songs(root: str) -> list[Song]:
    """扫描音乐目录，返回按歌名排序的歌单。

    每个含音频的子文件夹算一首歌；根目录下**散装**音频文件也算（用户直接把
    mp3 丢根里不该被无视）。目录不存在 / 不可读 → 空表（调用方据此禁用入口），
    绝不抛——这是菜单和面板都要直接吃的函数。
    """
    root = os.fspath(root or "")
    if not root or not os.path.isdir(root):
        return []
    songs: list[Song] = []
    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        log.warning("歌库扫描失败：%s（%s）", root, exc)
        return []
    loose: list[str] = []
    for entry in entries:
        if entry.is_dir():
            song = _scan_folder(entry.path, entry.name)
            if song is not None:
                songs.append(song)
        elif os.path.splitext(entry.name)[1].lower() in AUDIO_EXTS:
            loose.append(entry.name)
    for name in sorted(loose):
        audio = [name]
        original, covers = _split_original_covers(audio)
        lrc = ""
        sibling = os.path.join(root, os.path.splitext(name)[0] + LYRIC_EXT)
        if os.path.isfile(sibling):
            lrc = os.path.basename(sibling)
        songs.append(Song(
            name=os.path.splitext(name)[0],
            original_path=os.path.join(root, original),
            cover_paths=tuple(os.path.join(root, n) for n in covers),
            lrc_path=os.path.join(root, lrc) if lrc else "",
            folder="",
        ))
    songs.sort(key=lambda s: s.name)
    return songs


def choose_variant(song: Song, prefer_cover: bool) -> str:
    """按偏好挑一版：翻唱优先时没翻唱就回落原曲（永远有得播）。"""
    if prefer_cover and song.cover_paths:
        return sorted(song.cover_paths)[0]
    return song.original_path


def variant_label(path: str) -> str:
    """版本显示名：翻唱标出来，原曲标"原版"。"""
    return "龙呼呼版" if COVER_MARK in os.path.basename(path) else "原版"
