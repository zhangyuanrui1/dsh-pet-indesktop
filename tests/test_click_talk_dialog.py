# -*- coding: utf-8 -*-
"""点击动画台词绑定对话框的素材发现回归测试。

_discover_click_names 必须走 catalog.resolve_character_video_dir（外部 DLC
目录优先），并同时识别 webm/gif——不能只认内置目录（否则 DLC 角色在绑定
对话框里看到的是别的角色的动画名，绑定写到不存在的键上静默失效）。
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from pet import catalog
from pet.click_talk_dialog import (
    ClickTalkBindingsDialog,
    _discover_all_names,
    _discover_click_names,
)


@pytest.fixture
def app():
    """对话框用例的 QApplication（模块级单例，offscreen 平台）。"""
    return QApplication.instance() or QApplication([])


def test_discovers_from_external_dlc_dir(tmp_path, monkeypatch):
    """外部 DLC 角色：从外部目录读点击动画名，webm/gif 都识别。"""
    click_dir = tmp_path / "mydlc" / "videos" / "click"
    click_dir.mkdir(parents=True)
    (click_dir / "点击-开心.webm").write_bytes(b"")
    (click_dir / "点击-生气.gif").write_bytes(b"")
    monkeypatch.setattr(catalog, "external_character_dirs", lambda: [tmp_path])
    names = _discover_click_names("mydlc")
    assert names == ["点击-开心", "点击-生气"]


def test_missing_dir_falls_back_to_catalog_constant(tmp_path, monkeypatch):
    """目录完全不存在时回退 catalog.CLICKS（保持旧兜底行为）。"""
    monkeypatch.setattr(catalog, "external_character_dirs", lambda: [tmp_path])
    names = _discover_click_names("no-such-character")
    assert names == list(catalog.CLICKS)


def test_builtin_character_discovers_real_names():
    """内置角色（shenshen）：读到真实素材名而非过期常量——
    守住「catalog.CLICKS 字面量与磁盘真实文件名已漂移」的回归。"""
    names = _discover_click_names(catalog.DEFAULT_CHARACTER)
    click_dir = catalog.resolve_character_video_dir(catalog.DEFAULT_CHARACTER) / "click"
    expected = sorted(
        {p.stem for p in click_dir.glob("*.webm")} | {p.stem for p in click_dir.glob("*.gif")}
    )
    assert names == expected
    assert names, "内置角色必须有点击动画"


# ---------------------------------------------------------------- 2026-10-28：任意动画动态添加
class _StubConfig:
    """最小配置替身：只实现对话框用到的四面。"""

    def __init__(self, bindings=None, character="shenshen"):
        self._bindings = {k: list(v) for k, v in (bindings or {}).items()}
        self._character = character
        self.saved = None

    def get(self, key, default=None):
        return self._character if key == "character" else default

    def click_talk_bindings(self, character_id):
        return dict(self._bindings)

    def set_click_talk_bindings(self, character_id, bindings):
        self._bindings = {k: list(v) for k, v in bindings.items()}
        self.saved = (character_id, dict(self._bindings))


def test_discovers_all_names_across_subdirs(tmp_path, monkeypatch):
    """全部动画发现：递归扫素材目录（含 random/events 等子目录），webm/gif 都算。"""
    videos = tmp_path / "mydlc" / "videos"
    (videos / "click").mkdir(parents=True)
    (videos / "random").mkdir()
    (videos / "events" / "balance").mkdir(parents=True)
    (videos / "click" / "点击-开心.webm").write_bytes(b"")
    (videos / "random" / "吃西瓜.gif").write_bytes(b"")
    (videos / "random" / "忽略.txt").write_bytes(b"")
    (videos / "events" / "balance" / "余额-档位1.webm").write_bytes(b"")
    monkeypatch.setattr(catalog, "external_character_dirs", lambda: [tmp_path])
    assert _discover_all_names("mydlc") == ["余额-档位1", "吃西瓜", "点击-开心"]


def test_discover_all_names_falls_back_to_catalog(tmp_path, monkeypatch):
    """素材目录完全缺失时回退 catalog.ANIM_FILES 全量映射。"""
    monkeypatch.setattr(catalog, "external_character_dirs", lambda: [tmp_path])
    names = _discover_all_names("no-such-character")
    assert names == sorted(catalog.ANIM_FILES)


def test_dialog_lists_bound_extras_and_marks_removable(app):
    """既有绑定里的非点击动画自动出现在列表尾部，且只有它们可移除。"""
    config = _StubConfig({"click1": ["你好"], "吃西瓜": ["好甜"], "空绑定": []})
    dialog = ClickTalkBindingsDialog(config, click_names=["click1"])
    try:
        assert dialog.entries == ["click1", "吃西瓜"]   # 空绑定不入列
        dialog.list.setCurrentRow(0)
        assert not dialog.remove_btn.isEnabled()        # 点击动画不可移除
        dialog.list.setCurrentRow(1)
        assert dialog.remove_btn.isEnabled()
        assert dialog.text_edit.toPlainText() == "好甜"
    finally:
        dialog.deleteLater()


def test_dialog_add_animation_and_save(app):
    """添加动画 → 绑台词 → 保存：进绑定表，运行时点击池即可取到。"""
    config = _StubConfig()
    dialog = ClickTalkBindingsDialog(config, click_names=["click1"])
    try:
        assert "吃西瓜" in dialog.all_names             # 候选池 = 全量动画
        dialog._add_animation("吃西瓜")
        assert dialog.entries == ["click1", "吃西瓜"]
        dialog.text_edit.setPlainText("好甜\n真甜")
        dialog._save()
        character, bindings = config.saved
        assert character == "shenshen"
        assert bindings == {"吃西瓜": ["好甜", "真甜"]}
    finally:
        dialog.deleteLater()


def test_dialog_drops_empty_extra_on_save(app):
    """加了动画但没绑台词：保存时连同列表条目一起丢弃（不落幽灵键）。"""
    config = _StubConfig()
    dialog = ClickTalkBindingsDialog(config, click_names=["click1"])
    try:
        dialog._add_animation("吃西瓜")
        dialog.text_edit.setPlainText("   ")            # 只有空白 = 没台词
        dialog._save()
        assert config.saved[1] == {}
        assert dialog.entries == ["click1"]
    finally:
        dialog.deleteLater()


def test_dialog_remove_extra_clears_binding(app):
    """移除动态添加项：列表条目与绑定一起消失；点击动画移除是 no-op。"""
    config = _StubConfig({"吃西瓜": ["好甜"]})
    dialog = ClickTalkBindingsDialog(config, click_names=["click1"])
    try:
        dialog.list.setCurrentRow(0)                    # click1：移除按钮禁用
        dialog._remove_selected()
        assert dialog.entries == ["click1", "吃西瓜"]
        dialog.list.setCurrentRow(1)                    # 吃西瓜：可移除
        dialog._remove_selected()
        assert dialog.entries == ["click1"]
        dialog._save()
        assert config.saved[1] == {}
    finally:
        dialog.deleteLater()


def test_dialog_add_animation_is_idempotent(app):
    """重复添加同一动画是 no-op；点击动画始终保持在列表头部。"""
    config = _StubConfig()
    dialog = ClickTalkBindingsDialog(config, click_names=["click1"])
    try:
        assert "吃西瓜" in dialog._remaining_candidates()
        dialog._add_animation("吃西瓜")
        dialog._add_animation("吃西瓜")                 # 重复添加 no-op
        assert dialog.entries.count("吃西瓜") == 1
        assert dialog.entries[0] == "click1"
        assert "吃西瓜" not in dialog._remaining_candidates()
    finally:
        dialog.deleteLater()


# ---------------------------------------------------------------- config 层：绑定动画名单
def test_config_bound_animations_filters_empty_and_dedups(tmp_path):
    """只有绑了非空台词的动画进名单；保序去重。"""
    from pet.config import Config

    config = Config(base=tmp_path)
    config.set_click_talk_bindings("shenshen", {
        "吃西瓜": ["好甜"],
        "空绑定": [],
        "非列表": "不是列表",
        "涮火锅": ["咕嘟咕嘟"],
    })
    assert config.click_talk_bound_animations("shenshen") == ["吃西瓜", "涮火锅"]


def test_config_bound_animations_empty_when_no_bindings(tmp_path):
    from pet.config import Config

    config = Config(base=tmp_path)
    assert config.click_talk_bound_animations("shenshen") == []
