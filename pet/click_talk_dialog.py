# -*- coding: utf-8 -*-
"""点击动画台词绑定编辑对话框。

每个点击动画可绑定多条专属自言自语台词；点击角色时优先播放当前动画
绑定的台词，未绑定时回退全局随机自言自语。

2026-10-28 扩展：绑定对象不再只限 click/ 目录的固定几个动画——可以
**动态添加任意动画**（待机/移动/随机动作/事件动画全部可选）。绑定了
台词的非点击动画会并入点击回应池（见 ``window.py::_click_pool`` 与
``sprite_behavior.py`` 的 ``click_pool_extra_provider``），点击桌宠时
同样可能被抽中播放并念出台词，即"绑定即生效"。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from . import catalog


def _discover_click_names(character_id: str) -> list[str]:
    """从当前角色的有效素材目录发现点击动画名；目录缺失时回退 catalog 常量。

    走 catalog.resolve_character_video_dir（外部 DLC 目录 > 内置 webm >
    内置 gif），不读死内置路径；webm/gif 都识别。"""
    click_dir = catalog.resolve_character_video_dir(str(character_id)) / "click"
    if click_dir.is_dir():
        names = sorted(
            {p.stem for p in click_dir.glob("*.webm")}
            | {p.stem for p in click_dir.glob("*.gif")}
        )
        if names:
            return names
    return list(catalog.CLICKS)


def _discover_all_names(character_id: str) -> list[str]:
    """发现当前角色的**全部**动画名（添加动画选择器的候选池）。

    递归扫素材目录所有子目录（idle/turn/move/click/drag/random/events/
    未知目录都算——random 与 events 本就同进随机动作池）；目录缺失或为空
    时回退 catalog.ANIM_FILES（内置主形象的全量映射）。
    """
    video_dir = catalog.resolve_character_video_dir(str(character_id))
    names: set[str] = set()
    if video_dir.is_dir():
        for path in video_dir.rglob("*"):
            if path.suffix.lower() in (".webm", ".gif") and path.is_file():
                names.add(path.stem)
    if not names:
        names = set(catalog.ANIM_FILES)
    return sorted(names)


class ClickTalkBindingsDialog(QDialog):
    """维护当前角色的 动画id → [台词...] 绑定。

    列表 = 点击动画（固定，来自 click/ 目录）+ 已添加动画（用户动态加进
    来的其他动画，存在性由"绑定了非空台词"隐式持久化——没台词的添加项
    保存时丢弃，不落盘）。
    """

    def __init__(self, config, click_names: list[str] | None = None, parent=None):
        super().__init__(parent)
        self.config = config
        self.character_id = str(config.get("character", catalog.DEFAULT_CHARACTER))
        self.click_names = list(click_names) if click_names else _discover_click_names(self.character_id)
        self.all_names = _discover_all_names(self.character_id)
        self.bindings = {
            str(action_id): list(texts)
            for action_id, texts in config.click_talk_bindings(self.character_id).items()
        }
        # 动态添加的动画（不在 click_names 里的**非空**绑定键 + 本会话新加的），
        # 保序：先按绑定表里的既有顺序恢复，新加的追加在尾部
        self.extra_names: list[str] = [
            action_id for action_id, texts in self.bindings.items()
            if action_id not in self.click_names and isinstance(texts, list) and texts
        ]

        self.setWindowTitle("点击动画台词绑定")
        self.resize(560, 460)
        self.setMinimumSize(500, 400)

        layout = QVBoxLayout(self)
        hint = QLabel(
            "每个动画可绑定多条专属自言自语台词，每行一条。\n"
            "点击桌宠时优先念出所播动画绑定的台词；未绑定回退全局随机自言自语。\n"
            "除点击动画外，可点「添加动画…」给任意动画绑台词——绑定的动画"
            "会并入点击回应池，点击时同样可能被抽中播放。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        body = QHBoxLayout()
        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.setFixedWidth(200)
        left.addWidget(self.list)
        add_row = QHBoxLayout()
        self.add_btn = QPushButton("添加动画…")
        self.add_btn.clicked.connect(self._open_add_menu)
        add_row.addWidget(self.add_btn)
        self.remove_btn = QPushButton("移除")
        self.remove_btn.clicked.connect(self._remove_selected)
        self.remove_btn.setEnabled(False)  # 只对动态添加的项可用
        add_row.addWidget(self.remove_btn)
        left.addLayout(add_row)
        body.addLayout(left)

        right = QVBoxLayout()
        right.addWidget(QLabel("绑定台词（每行一条）："))
        self.text_edit = QPlainTextEdit()
        right.addWidget(self.text_edit)
        body.addLayout(right)
        layout.addLayout(body)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        save = QPushButton("保存")
        cancel = QPushButton("取消")
        save.clicked.connect(self._save)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

        self._reload_list()
        self.list.currentRowChanged.connect(self._load_selected)
        if self.entries:
            self.list.setCurrentRow(0)

    # ------------------------------------------------------------ 列表维护
    @property
    def entries(self) -> list[str]:
        """左侧列表的完整名单：点击动画在前，动态添加的在后。"""
        return [*self.click_names, *self.extra_names]

    def _reload_list(self) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for name in self.entries:
            item = QListWidgetItem(name)
            if name in self.extra_names:
                # 动态添加项给一个可视区分：斜体 + 提示
                item.setFont(self._italic_font())
                item.setToolTip("动态添加的动画：绑定台词后并入点击回应池")
            self.list.addItem(item)
        self.list.blockSignals(False)

    def _italic_font(self):
        from PySide6.QtGui import QFont

        font = QFont()
        font.setItalic(True)
        return font

    def _remaining_candidates(self) -> list[str]:
        """添加菜单的候选名单：全量动画 - 已入列项（保序）。"""
        existing = set(self.entries)
        return [name for name in self.all_names if name not in existing]

    def _open_add_menu(self) -> None:
        """弹出「添加动画」菜单：按语义分类分子菜单（对齐播放动画菜单）。"""
        remaining = self._remaining_candidates()
        if not remaining:
            self.add_btn.setToolTip("所有动画都已在列表里")
            return
        menu = QMenu(self)
        leftover = set(remaining)
        for label, names in catalog.ANIMATION_SEMANTIC_CATEGORIES:
            hits = [name for name in remaining if name in set(names)]
            if not hits:
                continue
            leftover.difference_update(hits)
            submenu = menu.addMenu(label)
            for name in hits:
                submenu.addAction(name, lambda n=name: self._add_animation(n))
        if leftover:
            others = menu.addMenu("其他（点击/待机/移动等）")
            for name in remaining:
                if name in leftover:
                    others.addAction(name, lambda n=name: self._add_animation(n))
        menu.exec(self.add_btn.mapToGlobal(self.add_btn.rect().bottomLeft()))

    def _add_animation(self, name: str) -> None:
        if name in self.entries:
            return
        self.extra_names.append(str(name))
        self._reload_list()
        self.list.setCurrentRow(len(self.entries) - 1)  # 选中并加载空台词

    def _remove_selected(self) -> None:
        row = self.list.currentRow()
        if not (0 <= row < len(self.entries)):
            return
        name = self.entries[row]
        if name in self.click_names:
            return  # 点击动画是素材目录固有项，不提供移除
        self.extra_names.remove(name)
        self.bindings.pop(name, None)
        self._reload_list()
        target = max(0, min(row, len(self.entries) - 1))
        if self.entries:
            self.list.setCurrentRow(target)
        else:
            self.text_edit.setPlainText("")

    # ------------------------------------------------------------ 编辑 / 保存
    def _load_selected(self, row: int) -> None:
        entries = self.entries
        if row < 0 or row >= len(entries):
            self.text_edit.setPlainText("")
            self.remove_btn.setEnabled(False)
            return
        self.text_edit.setPlainText("\n".join(self.bindings.get(entries[row], [])))
        self.remove_btn.setEnabled(entries[row] in self.extra_names)

    def _save(self) -> None:
        row = self.list.currentRow()
        entries = self.entries
        if 0 <= row < len(entries):
            self._collect_row(entries[row])
        # 逐项收口：有台词的进绑定表；没台词的点击动画清绑定，
        # 没台词的动态添加项连列表条目一起丢弃（不落盘幽灵行）
        kept_extras: list[str] = []
        for name in self.extra_names:
            texts = self.bindings.get(name)
            if isinstance(texts, list) and texts:
                kept_extras.append(name)
            else:
                self.bindings.pop(name, None)
        self.extra_names = kept_extras
        for name in self.click_names:
            texts = self.bindings.get(name)
            if not (isinstance(texts, list) and texts):
                self.bindings.pop(name, None)
        self.config.set_click_talk_bindings(self.character_id, self.bindings)
        self.accept()

    def _collect_row(self, action_id: str) -> None:
        """把右侧编辑框的当前内容收进绑定表（当前选中项）。"""
        texts = [line.strip() for line in self.text_edit.toPlainText().splitlines() if line.strip()]
        if texts:
            self.bindings[action_id] = texts[:50]
        else:
            self.bindings.pop(action_id, None)
