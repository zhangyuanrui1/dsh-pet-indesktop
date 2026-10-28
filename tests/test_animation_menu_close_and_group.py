# -*- coding: utf-8 -*-
"""#221：动画菜单两项改进——点完动作收起整条菜单链 + 随机动作前缀分组。

真机现状（2026-10-07，当前部署构建）：右键 → 播放动画 → 随机动作 → 点一个
动作，三级 QMenu 全部留在屏上；随机动作 95 项单列 236×1439px，顶满全屏。

两条断言都走真实用户路径，不 mock 菜单：
- 收起：真实 Qt 子菜单链（hover 让 Qt 自己弹出子菜单）+ 真实鼠标按下/抬起；
- 分组：真实建造器（``populate_context_menu``）产出的菜单树，外加本仓
  shenshen 素材包的真实随机动作清单。

覆盖：
1. 收起真值表：动作项（默认收起）/ 动作项（``anim_menu_stay_open``=连播）/
   设置项（速率·大小）/ 开关项（拖动物理）/ 子菜单项；
2. 分组结构：家族拆分、不丢项、单桶不超过原清单、短清单与单家族不拆；
3. 分组后叶子仍触发同一动作；legacy / modern 两条路径结构一致；
4. 分组里点动作同样收起（层级变深不改变收起语义）。
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QMenu

from pet import catalog
from pet.context_menu import populate_context_menu

app = QApplication.instance() or QApplication([])

ANIMATION_CATEGORIES = ("待机", "转向", "移动", "点击回应", "随机动作")
# 分组后单个子菜单最多允许的条目数：实机 95 项单列 1439px（约 15px/项）顶满
# 全屏；60 项约 900px，在 1440p（可用约 1400px）与 1080p（可用约 1000px）
# 都在一屏内，还给屏幕边缘留出余量。
MAX_SUBMENU_ITEMS = 60


class _FakeConfig(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)

    def set(self, key, value, *args, **kwargs):
        dict.__setitem__(self, key, value)


class _FakePet:
    """窗口路径的最小 pet 面（面名与 registry / shared 的读取点一一对应）。"""

    def __init__(self, acts=None, **values):
        self.cfg = _FakeConfig(values)
        self.idles = ["待机呼吸休闲"]
        self.turns = ["东张西望"]
        self.moves = ["漂浮踏步"]
        self.clicks = ["点击回应-开心跃动"]
        self.acts = list(acts if acts is not None else ["吃西瓜", "吃饺子"])
        self.playback_speed = 1.0
        self.scale = catalog.DEFAULT_SCALE
        self.drag_physics = False
        self.no_move = False
        self.played = []
        self.speed_calls = []
        self.drag_calls = []

    def switch_clip(self, name):
        self.played.append(name)

    def trigger_move(self, name):
        self.played.append(name)
        return True

    def set_playback_speed(self, value):
        self.speed_calls.append(value)
        self.playback_speed = value

    def set_drag_physics(self, enabled):
        self.drag_calls.append(enabled)
        self.drag_physics = enabled

    def __getattr__(self, _name):
        return None


def _build_menu(pet) -> QMenu:
    menu = QMenu()
    populate_context_menu(menu, pet)
    return menu


def _find(container, label: str):
    for action in container.actions():
        if action.text() == label:
            return action
    raise AssertionError(
        f"菜单缺项 {label}；实际 = {[a.text() for a in container.actions()]}")


def _find_deep(container, label: str):
    """递归查找（modern 的开关类条目收在「桌宠控制」等分组子菜单里）。"""
    match = _find_deep_or_none(container, label)
    assert match is not None, f"菜单树里找不到 {label}"
    return match


def _find_deep_or_none(container, label: str):
    for action in container.actions():
        if action.text() == label:
            return action
        submenu = action.menu()
        if submenu is not None:
            found = _find_deep_or_none(submenu, label)
            if found is not None:
                return found
    return None


def _pump(predicate, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    app.processEvents()
    return bool(predicate())


def _hover(menu: QMenu, action) -> None:
    pos = menu.actionGeometry(action).center()
    event = QMouseEvent(
        QMouseEvent.Type.MouseMove, pos, menu.mapToGlobal(pos),
        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(menu, event)
    app.processEvents()


def _click(menu: QMenu, action) -> None:
    for event_type in (QMouseEvent.Type.MouseButtonPress,
                       QMouseEvent.Type.MouseButtonRelease):
        pos = menu.actionGeometry(action).center()
        event = QMouseEvent(
            event_type, pos, menu.mapToGlobal(pos),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(menu, event)
        app.processEvents()


def _open_chain(root: QMenu, labels) -> QMenu:
    """hover 展开 Qt 自己管理的子菜单链（真实 popup，不是手工堆叠）。"""
    root.popup(QPoint(40, 40))
    assert _pump(root.isVisible), "offscreen 下根菜单必须真的可见（否则测不到点击路径）"
    menu = root
    for label in labels:
        action = _find(menu, label)
        _hover(menu, action)
        submenu = action.menu()
        assert submenu is not None, f"{label} 不是子菜单"
        assert _pump(submenu.isVisible), f"{label} 子菜单未被 Qt 展开"
        menu = submenu
    return menu


def _close_all(*menus) -> None:
    for menu in menus:
        if menu is not None:
            menu.close()
    app.processEvents()


# ---------------------------------------------------------------- 收起真值表
def test_clicking_random_action_closes_whole_menu_chain():
    """默认（anim_menu_stay_open 未开）：点随机动作后整条菜单链消失，动作照播。"""
    pet = _FakePet()
    root = _build_menu(pet)
    play = _find(root, "播放动画").menu()
    try:
        acts = _open_chain(root, ("播放动画", "随机动作"))
        assert pet.played == [], "展开菜单不得触发动作"
        _click(acts, _find(acts, "吃西瓜"))
        assert _pump(lambda: not root.isVisible())
        assert not play.isVisible() and not acts.isVisible(), "三级菜单必须整条收起"
        assert pet.played == ["吃西瓜"]
    finally:
        _close_all(play, root)


def test_stay_open_preference_keeps_chain_visible():
    """连播出路：anim_menu_stay_open=True 时退回旧行为（点完动作菜单留在屏上）。"""
    pet = _FakePet(anim_menu_stay_open=True)
    root = _build_menu(pet)
    play = _find(root, "播放动画").menu()
    try:
        acts = _open_chain(root, ("播放动画", "随机动作"))
        leaf = _find(acts, "吃西瓜")
        _click(acts, leaf)
        app.processEvents()
        assert pet.played == ["吃西瓜"], "连播模式下动作必须立即触发"
        assert root.isVisible() and play.isVisible() and acts.isVisible(), (
            "连播模式必须保留旧行为（菜单不收起）")
    finally:
        _close_all(root)


def test_every_animation_leaf_carries_close_on_trigger():
    """五个动画分类的叶子（含分组后的家族叶子）都挂 closeOnTrigger。"""
    pet = _FakePet(acts=[f"吃{m}号" for m in range(12)] + [
        "工作状态-忙碌点按", "工作状态-思考冒泡", "工作状态-雀跃庆祝"])
    root = _build_menu(pet)
    play = _find(root, "播放动画").menu()
    try:
        for label in ANIMATION_CATEGORIES:
            submenu = _find(play, label).menu()
            submenu.aboutToShow.emit()
            leaves = [a for a in submenu.actions() if a.menu() is None]
            if not leaves:                     # 分组后动作下沉一层
                families = [a.menu() for a in submenu.actions() if a.menu() is not None]
                assert families, f"{label} 既没有动作也没有家族子菜单"
                for family in families:
                    family.aboutToShow.emit()
                    leaves.extend(a for a in family.actions() if a.menu() is None)
            assert leaves, f"{label} 无叶子动作"
            for action in leaves:
                assert action.property("closeOnTrigger") is True, action.text()
    finally:
        _close_all(play, root)


def test_setting_and_toggle_entries_do_not_close():
    """设置类（速率 / 大小）、开关类（拖动物理）与子菜单项一律不受收起影响。"""
    pet = _FakePet()
    root = _build_menu(pet)
    speed = _find(root, "播放速率").menu()
    size = _find(root, "大小").menu()
    try:
        speed.aboutToShow.emit()
        assert not bool(_find(speed, "1.5x").property("closeOnTrigger"))
        assert not bool(_find(size, f"{int(round(catalog.CANVAS_W * catalog.SCALE_STEPS[0]))}px"
                              ).property("closeOnTrigger"))
        assert not bool(_find_deep(root, "拖动物理").property("closeOnTrigger"))
        # 真实点击设置项：菜单留在屏上（可连续调档）
        _open_chain(root, ("播放速率",))
        _click(speed, _find(speed, "1.5x"))
        assert pet.speed_calls == [1.5]
        assert root.isVisible() and speed.isVisible(), "设置项不得收起菜单"
    finally:
        _close_all(size, speed, root)


def test_stay_open_preference_does_not_leak_to_setting_entries():
    """连播开关只作用动画项：打开它，速率/大小/开关类仍保持原语义。"""
    pet = _FakePet(anim_menu_stay_open=True)
    root = _build_menu(pet)
    speed = _find(root, "播放速率").menu()
    try:
        speed.aboutToShow.emit()
        assert not bool(_find(speed, "1.5x").property("closeOnTrigger"))
        assert not bool(_find_deep(root, "拖动物理").property("closeOnTrigger"))
    finally:
        _close_all(speed, root)


def test_stay_open_holds_inside_family_submenus():
    """连播开关必须覆盖家族子菜单：家族菜单是构建期建的，交互规则要递归跟上。

    家族子菜单若漏装停留交互过滤器，连播模式下点族内动作会走 Qt 原生关闭路径，
    菜单照样收起——这正是"只改一级、深层漏装"的典型回归。
    """
    pet = _FakePet(
        acts=[f"吃{m}号" for m in range(26)] + ["写代码", "下五子棋"],
        anim_menu_stay_open=True,
    )
    root = _build_menu(pet)
    try:
        family = _open_chain(root, ("播放动画", "随机动作", "吃"))
        _click(family, _find(family, "吃3号"))
        app.processEvents()
        assert pet.played == ["吃3号"]
        assert root.isVisible() and family.isVisible(), "连播模式在家族子菜单里同样保持展开"
    finally:
        _close_all(root)


def test_clicking_grouped_leaf_plays_same_animation_and_closes():
    """层级变深（随机动作 → 家族 → 叶子）不改变触发语义与收起行为。"""
    pet = _FakePet(acts=[f"吃{m}号" for m in range(26)] + ["写代码", "下五子棋"])
    root = _build_menu(pet)
    try:
        acts = _open_chain(root, ("播放动画", "随机动作", "吃"))
        assert pet.played == []
        _click(acts, _find(acts, "吃3号"))
        assert _pump(lambda: not root.isVisible())
        assert not acts.isVisible(), "四级菜单链必须整条收起"
        assert pet.played == ["吃3号"]
    finally:
        _close_all(root)


# ---------------------------------------------------------------- 分组结构
def _family_members(submenu: QMenu) -> dict[str, list[str]]:
    """{家族标签: 成员}；未分组时返回 {家族标签算作父菜单自身}。"""
    submenu.aboutToShow.emit()
    families: dict[str, list[str]] = {}
    for action in submenu.actions():
        if action.isSeparator():
            continue
        child = action.menu()
        if child is None:
            families.setdefault("", []).append(action.text())
            continue
        child.aboutToShow.emit()
        families[action.text()] = [
            a.text() for a in child.actions() if not a.isSeparator()]
    return families


def test_grouping_splits_prefix_families_without_losing_actions():
    """95 项随机动作按前缀家族分组：不丢项、不变序、单桶回到一屏内。"""
    pet = _FakePet(acts=_synthetic_group_acts())
    root = _build_menu(pet)
    try:
        acts = _find(_find(root, "播放动画").menu(), "随机动作").menu()
        families = _family_members(acts)
        assert "" not in families, "长清单必须分组，不得保留一整个平铺清单"
        assert set(families) == {"吃", "原地", "放", "余额", "工作状态", "碎碎念", "更多"}
        flat = [name for members in families.values() for name in members]
        assert sorted(flat) == sorted(pet.acts), "分组不得丢项或重复"
        for label, members in families.items():
            assert 0 < len(members) < len(pet.acts), label
            if label != "更多":
                # 前缀家族都是小桶（实机最大 17 项）；长尾集中在「更多」
                assert len(members) <= 17, label
            assert len(members) <= MAX_SUBMENU_ITEMS, f"{label} 仍有 {len(members)} 项"
        # 成员内部保持原清单相对顺序
        for members in families.values():
            indexes = [pet.acts.index(name) for name in members]
            assert indexes == sorted(indexes)
        # 家族按成员数降序，其余「更多」固定在最后
        sizes = [len(members) for label, members in families.items() if label != "更多"]
        assert sizes == sorted(sizes, reverse=True)
        assert list(families)[-1] == "更多"
    finally:
        _close_all(root)


def test_shipped_random_action_pool_uses_semantic_categories():
    """真实素材包（本仓 shenshen）：随机动作按语义分类分族（上游分类法）。

    2026-10-28 起优先语义分组（小动作/玩耍/吃什么/时节/文字 + 工作状态/碎碎念/
    余额），语义表对内置素材包全覆盖——「更多」不应出现；族内单桶仍须一屏内。
    """
    acts_names = _shipped_action_names()
    if not acts_names:
        pytest.skip("shenshen 素材包不在本机（外部 DLC 布局），跳过真实清单断言")
    pet = _FakePet(acts=acts_names)
    root = _build_menu(pet)
    try:
        acts = _find(_find(root, "播放动画").menu(), "随机动作").menu()
        families = _family_members(acts)
        assert set(families) == {
            "小动作", "玩耍", "吃什么", "时节", "文字", "工作状态", "碎碎念", "余额",
        }
        flat = [name for members in families.values() for name in members]
        assert sorted(flat) == sorted(acts_names), "语义分组不得丢项或重复"
        for label, members in families.items():
            assert 0 < len(members) <= MAX_SUBMENU_ITEMS, f"{label} 有 {len(members)} 项"
    finally:
        _close_all(root)


def test_shipped_random_action_pool_is_grouped_within_one_screen():
    """真实素材包（本仓 shenshen）的随机动作池：分组后每级回到一屏内。"""
    acts_names = _shipped_action_names()
    if not acts_names:
        pytest.skip("shenshen 素材包不在本机（外部 DLC 布局），跳过真实清单断言")
    pet = _FakePet(acts=acts_names)
    root = _build_menu(pet)
    try:
        acts = _find(_find(root, "播放动画").menu(), "随机动作").menu()
        families = _family_members(acts)
        assert "" not in families, "实机 95 项单列 1439px —— 必须分组"
        flat = [name for members in families.values() for name in members]
        assert sorted(flat) == sorted(acts_names)
        biggest = max((len(members), label) for label, members in families.items())
        assert biggest[0] <= MAX_SUBMENU_ITEMS, f"分组后仍有 {biggest} 项超一屏"
        assert biggest[0] < len(acts_names)
    finally:
        _close_all(root)


def test_short_action_lists_stay_flat():
    """清单短于阈值时不加层级：少数动作的角色菜单保持原状。"""
    pet = _FakePet(acts=["吃西瓜", "吃饺子", "吃年糕", "余额-钱袋满溢",
                         "写代码", "下五子棋"])
    root = _build_menu(pet)
    try:
        acts = _find(_find(root, "播放动画").menu(), "随机动作").menu()
        acts.aboutToShow.emit()
        assert [a.text() for a in acts.actions()] == pet.acts
    finally:
        _close_all(root)


def test_single_dominant_prefix_is_not_split():
    """整份清单都在同一个前缀家族里时分组没有收益，保持平铺。"""
    pet = _FakePet(acts=[f"act-{index}" for index in range(30)])
    root = _build_menu(pet)
    try:
        acts = _find(_find(root, "播放动画").menu(), "随机动作").menu()
        acts.aboutToShow.emit()
        assert [a.text() for a in acts.actions()] == pet.acts
    finally:
        _close_all(root)


# ---------------------------------------------------------------- 分组结构（单一布局）
def test_modern_layout_groups_the_random_action_list():
    """现代注册表树里，长清单被前缀分族（不丢项、不留空家族）。"""
    acts_names = _synthetic_group_acts()
    pet = _FakePet(acts=acts_names)
    menu = _build_menu(pet)
    try:
        acts = _find(_find(menu, "播放动画").menu(), "随机动作").menu()
        families = _family_members(acts)
        assert "" not in families and len(families) > 1
        assert sum(len(members) for members in families.values()) == len(acts_names)
    finally:
        _close_all(menu)


# ---------------------------------------------------------------- 清单来源
def _synthetic_group_acts() -> list[str]:
    """长清单样本：前缀家族与「更多」长尾（结构同实机 shenshen 清单）。"""
    return (
        [f"吃{n}号" for n in range(17)]
        + [f"碎碎念-场景{n}" for n in range(3)]
        + [f"工作状态-状态{n}" for n in range(6)]
        + [f"余额-档位{n}" for n in range(6)]
        + [f"原地{tag}动作" for tag in ("小憩", "敲击", "跳跃", "蹲下", "重力", "旋转")]
        + [f"放{n}灯" for n in range(4)]
        # 长尾：首字各不相同的零散动作（"更多"桶的真实形态）
        + [f"{head}动作{n}" for n, head in enumerate("甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉")]
    )


def _shipped_action_names():
    """本仓 shenshen 素材包的真实随机动作清单（MovieLibrary._load_all 同一扫描口径）。"""
    root_dir = catalog.resolve_character_video_dir("shenshen")
    if not root_dir.is_dir():
        return []
    folder_files: dict[str, list[str]] = {}
    manifest: dict[str, str] = {}
    for path in sorted(root_dir.rglob("*.webm")):
        relative = path.relative_to(root_dir)
        manifest[path.stem] = relative.as_posix()
        folder = relative.parts[0].lower() if len(relative.parts) > 1 else ""
        folder_files.setdefault(folder, []).append(path.stem)
    if not manifest:
        return []
    cats = catalog.build_categories(
        set(manifest),
        catalog.load_character_manifest("shenshen", root_dir),
        None,
        folder_files,
    )
    return list(cats["acts"])
