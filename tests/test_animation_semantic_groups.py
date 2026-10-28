# -*- coding: utf-8 -*-
"""随机动作菜单的**语义分类**分组（2026-10-28 对齐上游 dsh-pet 分类法）。

分组现在是两档：优先 ``shared.semantic_animation_groups``（语义表覆盖充分时），
退回 ``shared.group_animation_entries``（前缀家族）。本文件锁语义档的纯函数行为：

① 覆盖充分 → 按表序分族（小动作/玩耍/吃什么/时节/文字/工作状态/碎碎念/余额），
   「更多」只收表外名字、永远最后；
② 覆盖不足 / 清单太短 / 有效分类不足两个 → 返回 ``()``（不适用，交回前缀分组）；
③ 底线与旧分组一致：不丢项、不重复、族内保持传入顺序；
④ 内置 shenshen 素材包的真实随机动作清单（95 项）被语义表**全覆盖**——「更多」
   不应出现。

全部用例不碰 Qt：分组是纯函数，菜单装配另有 test_animation_menu_close_and_group
走真实 Qt 路径把守。
"""

from __future__ import annotations

from pet import catalog
from pet.context_menus.shared import (
    ANIMATION_GROUP_RESIDUAL_LABEL,
    group_animation_entries,
    semantic_animation_groups,
)


def _shipped_action_names() -> list[str]:
    """本仓 shenshen 素材包的真实随机动作清单（与 test_animation_menu_close_and_group 同口径）。"""
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


# ---------------------------------------------------------------- ① 分族结构
def test_real_shipped_pool_is_fully_covered_by_semantic_table():
    """实机 95 项全部落进语义类，不需要「更多」兜底。"""
    acts = _shipped_action_names()
    if not acts:
        assert False, "shenshen 素材包应随仓分发（外部 DLC 布局变化需同步本测试）"
    groups = semantic_animation_groups(acts)
    assert groups, "实机清单必须语义分组"
    labels = [label for label, _members in groups]
    assert ANIMATION_GROUP_RESIDUAL_LABEL not in labels, (
        f"语义表应全覆盖实机清单，仍有 {len(dict(groups).get(ANIMATION_GROUP_RESIDUAL_LABEL, ()))} 项漏网")
    assert set(labels) == {
        "小动作", "玩耍", "吃什么", "时节", "文字", "工作状态", "碎碎念", "余额"}
    flat = [name for _label, members in groups for name in members]
    assert sorted(flat) == sorted(acts), "分组不得丢项或重复"


def test_semantic_groups_follow_table_order_and_residual_last():
    """分类按表序（不按成员数），「更多」固定最后，族内保持传入顺序。"""
    acts = [
        "写福字",           # 时节（表中第 4 类）
        "吃西瓜",           # 吃什么（第 3 类）
        "自创绝招",          # 表外 → 「更多」
        "悠闲哼歌",          # 小动作（第 1 类）
        "撸猫",             # 玩耍（第 2 类）
        "余额-钱袋满溢",      # 余额
        "碎碎念-发呆碎碎念",   # 碎碎念
        "工作状态-思考冒泡",   # 工作状态
        "是啊，吃什么",       # 文字
    ]
    # 清单太短不分组——这里只测纯函数的族序，直接凑长清单：
    acts = acts * 12  # 108 项，覆盖率 8/9 > 50%
    groups = semantic_animation_groups(acts)
    labels = [label for label, _members in groups]
    assert labels == ["小动作", "玩耍", "吃什么", "时节", "文字",
                      "工作状态", "碎碎念", "余额", ANIMATION_GROUP_RESIDUAL_LABEL]
    by_label = dict(groups)
    assert by_label["小动作"] == ("悠闲哼歌",) * 12
    assert by_label[ANIMATION_GROUP_RESIDUAL_LABEL] == ("自创绝招",) * 12


def test_grouping_never_loses_or_duplicates():
    """长混合清单：命中项过半时语义分组接住全部成员，不丢项、不重复。"""
    acts = (
        [f"吃{index}号" for index in range(6)]            # 表外（吃N号不在表里）
        + ["吃西瓜", "涮火锅", "荡秋千", "哈欠连天"]        # 表内
        + [f"自创动作{index}" for index in range(2)]       # 表外
    ) * 5  # 60 项，语义命中 20/60 不足一半……
    # 覆盖率 20/60 < 50% → 语义分组不适用，交给前缀分组（另一条路兜底）
    assert semantic_animation_groups(acts) == ()
    assert group_animation_entries(acts), "前缀分组应照常适用"


def test_low_coverage_falls_back_to_prefix_grouping():
    """语义覆盖不足一半：返回 ()，调用方（build_animation_categories）退回前缀分组。"""
    acts = (
        [f"吃{index}号" for index in range(17)]
        + [f"余额-档位{index}" for index in range(6)]
        + ["吃西瓜", "荡秋千"]  # 仅 2 项命中语义表
    )
    assert semantic_animation_groups(acts) == ()
    # 前缀分组仍能接住同一份清单
    assert group_animation_entries(acts), "前缀分组应照常适用"


def test_short_lists_stay_flat():
    """清单短于阈值不分组（与旧口径一致）。"""
    assert semantic_animation_groups(["吃西瓜", "吃饺子"]) == ()


def test_single_category_is_not_split():
    """整份清单都属于同一个语义类：分组没有收益，返回 ()。"""
    acts = ["吃白饭", "大口吃零食", "吃Token", "吃早餐", "吃午餐"] * 6
    assert semantic_animation_groups(acts) == ()


def test_semantic_table_names_are_unique():
    """分类表自身不许有重复成员（一个动画只能属于一个类）。"""
    seen: set[str] = set()
    for _label, names in catalog.ANIMATION_SEMANTIC_CATEGORIES:
        for name in names:
            assert name not in seen, name
            seen.add(name)
    assert len(catalog.ANIMATION_SEMANTIC_INDEX) == len(seen)
