# -*- coding: utf-8 -*-
"""素材列表共用的右键操作菜单。"""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtWidgets import QApplication, QMenu, QWidget


MEDIA_CONTEXT_ACTION_LABELS = (
    "预览素材",
    "重命名",
    "编辑备注",
    "刷新封面",
    "复制文件名",
    "打开文件夹",
    "删除素材",
)


def build_media_context_menu(
    parent: QWidget,
    row: dict,
    *,
    preview: Callable[[dict], None],
    rename: Callable[[dict], None],
    edit_remark: Callable[[dict], None],
    refresh_cover: Callable[[dict], None],
    open_folder: Callable[[dict], None],
    delete: Callable[[dict], None],
) -> QMenu:
    """为素材管理和发布中心生成顺序、文案一致的操作菜单。"""

    menu = QMenu(parent)
    menu.addAction("预览素材", lambda _checked=False: preview(row))
    menu.addAction("重命名", lambda _checked=False: rename(row))
    menu.addAction("编辑备注", lambda _checked=False: edit_remark(row))
    menu.addAction("刷新封面", lambda _checked=False: refresh_cover(row))
    menu.addAction(
        "复制文件名",
        lambda _checked=False: QApplication.clipboard().setText(
            str(row.get("filename") or "")
        ),
    )
    menu.addAction("打开文件夹", lambda _checked=False: open_folder(row))
    menu.addSeparator()
    menu.addAction("删除素材", lambda _checked=False: delete(row))
    return menu
