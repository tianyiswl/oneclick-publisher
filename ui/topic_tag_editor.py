# -*- coding: utf-8 -*-
"""发布中心复用的结构化话题编辑器。"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from PyQt6.QtCore import QPoint, QRect, QSize, Qt
from PyQt6.QtWidgets import (
    QFrame,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QMessageBox,
    QWidget,
)

from app_core import douyin_commerce_draft_service, publish_config_service

from .common import button


_HISTORY_TAG_ROW_HEIGHT = 36
_HISTORY_TAG_ROW_SPACING = 4
_HISTORY_TAG_VERTICAL_PADDING = 10


def history_tag_box_height(rows: int) -> int:
    """按当前紧凑按钮的真实行高计算最近标签框高度。"""

    safe_rows = max(1, min(int(rows or 2), 5))
    return (
        _HISTORY_TAG_VERTICAL_PADDING
        + safe_rows * _HISTORY_TAG_ROW_HEIGHT
        + (safe_rows - 1) * _HISTORY_TAG_ROW_SPACING
    )


class FlowLayout(QLayout):
    """让标签随可用宽度自动换行的轻量流式布局。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        contents_margins: tuple[int, int, int, int] = (0, 0, 0, 0),
        horizontal_spacing: int = 6,
        vertical_spacing: int = 4,
    ) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._horizontal_spacing = horizontal_spacing
        self._vertical_spacing = vertical_spacing
        self.setContentsMargins(*contents_margins)

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(
            margins.left() + margins.right(),
            margins.top() + margins.bottom(),
        )
        return size

    def _do_layout(self, rect: QRect, *, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(
            margins.left(),
            margins.top(),
            -margins.right(),
            -margins.bottom(),
        )
        x = effective.x()
        y = effective.y()
        line_height = 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._horizontal_spacing
            if next_x - self._horizontal_spacing > effective.right() and line_height:
                x = effective.x()
                y += line_height + self._vertical_spacing
                next_x = x + hint.width() + self._horizontal_spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


class TopicTagEditor(QWidget):
    """输入、展示和复用话题标签，兼容旧 QTextEdit 的文本接口。"""

    def __init__(
        self,
        *,
        placeholder: str = "例如：探店、团购",
        history_rows: int = 2,
        history_changed: Callable[[list[str]], None] | None = None,
    ) -> None:
        super().__init__()
        self._values: list[str] = []
        self._history: list[str] = []
        self._history_changed = history_changed

        root = QFormLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setHorizontalSpacing(12)
        root.setVerticalSpacing(6)
        root.setLabelAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        root.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        entry = QHBoxLayout()
        entry.setContentsMargins(0, 0, 0, 0)
        entry.setSpacing(8)
        self.input = QLineEdit()
        self.input.setObjectName("publishTopicTagInput")
        self.input.setPlaceholderText(placeholder)
        self.input.returnPressed.connect(self.add_from_input)
        self.add_button = button("添加", variant="secondary", compact=True)
        self.add_button.setObjectName("publishTopicTagAdd")
        self.add_button.clicked.connect(self.add_from_input)
        entry.addWidget(self.input, 1)
        entry.addWidget(self.add_button)
        self.topic_title = QLabel("话题标签")
        self.topic_title.setObjectName("douyinCommerceFieldLabel")
        root.addRow(self.topic_title, entry)

        self.selected_host = QFrame()
        self.selected_host.setObjectName("douyinCommerceTagHost")
        self.selected_host.setFixedHeight(40)
        self.selected_layout = QHBoxLayout(self.selected_host)
        self.selected_layout.setContentsMargins(8, 5, 8, 5)
        self.selected_layout.setSpacing(6)
        root.addRow("", self.selected_host)

        self.history_title = QLabel("历史标签")
        self.history_title.setObjectName("douyinCommerceFieldLabel")
        self.history_title.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.history_host = QFrame()
        self.history_host.setObjectName("douyinCommerceTagHistoryHost")
        self.history_host.setFixedHeight(history_tag_box_height(history_rows))
        self.history_layout = FlowLayout(
            self.history_host,
            contents_margins=(8, 5, 8, 5),
            horizontal_spacing=6,
            vertical_spacing=4,
        )
        root.addRow(self.history_title, self.history_host)

        self._render_selected()
        self.refresh_history()

    @staticmethod
    def _clear_layout(layout: QLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    @staticmethod
    def _normalize(values: object) -> list[str]:
        if isinstance(values, (list, tuple)):
            text = " ".join(str(value or "") for value in values)
        else:
            text = str(values or "")
        return publish_config_service.parse_tags(text)

    def tags(self) -> list[str]:
        return list(self._values)

    def toPlainText(self) -> str:  # noqa: N802 - 兼容 QTextEdit 接口
        return " ".join(f"#{tag}" for tag in self._values)

    def setPlainText(self, value: object) -> None:  # noqa: N802
        self._values = self._normalize(value)
        self.input.clear()
        self._render_selected()

    def clear(self) -> None:
        self._values = []
        self.input.clear()
        self._render_selected()

    def add_from_input(self) -> None:
        additions = self._normalize(self.input.text())
        if not additions:
            return
        for tag in additions:
            if tag not in self._values:
                self._values.append(tag)
        self.input.clear()
        self._render_selected()

    def add_history_tag(self, tag: str) -> None:
        normalized = self._normalize(tag)
        if normalized and normalized[0] not in self._values:
            self._values.append(normalized[0])
            self._render_selected()

    def remove_tag(self, tag: str) -> None:
        self._values = [value for value in self._values if value != tag]
        self._render_selected()

    def _render_selected(self) -> None:
        self._clear_layout(self.selected_layout)
        if not self._values:
            placeholder = QLabel("尚未添加标签")
            placeholder.setProperty("role", "caption")
            self.selected_layout.addWidget(placeholder)
        else:
            for tag in self._values:
                chip = button(f"#{tag} ×", variant="secondary", compact=True)
                chip.setObjectName("douyinCommerceTagChip")
                chip.setToolTip(f"移除 #{tag}")
                chip.clicked.connect(
                    lambda _checked=False, value=tag: self.remove_tag(value)
                )
                self.selected_layout.addWidget(chip)
        self.selected_layout.addStretch(1)

    def refresh_history(self, values: Iterable[object] | None = None) -> None:
        if values is None:
            try:
                history = douyin_commerce_draft_service.list_tag_history()
            except Exception:
                history = []
        else:
            history = [str(value or "") for value in values]
        self._history = self._normalize(history)
        self._render_history()

    def _render_history(self) -> None:
        self._clear_layout(self.history_layout)
        if not self._history:
            placeholder = QLabel("保存填写内容后会在这里保留常用标签")
            placeholder.setProperty("role", "caption")
            self.history_layout.addWidget(placeholder)
        else:
            for tag in self._history:
                chip = QFrame()
                chip.setObjectName("douyinCommerceTagHistoryChip")
                chip_layout = QHBoxLayout(chip)
                chip_layout.setContentsMargins(6, 1, 3, 1)
                chip_layout.setSpacing(1)
                add_button = button(f"+ #{tag}", variant="ghost", compact=True)
                add_button.setObjectName("douyinCommerceTagHistoryAdd")
                add_button.setToolTip(f"添加最近标签 #{tag}")
                add_button.clicked.connect(
                    lambda _checked=False, value=tag: self.add_history_tag(value)
                )
                remove_button = button("×", variant="ghost", compact=True)
                remove_button.setObjectName("douyinCommerceTagHistoryRemove")
                remove_button.setToolTip(f"删除最近标签 #{tag}")
                remove_button.clicked.connect(
                    lambda _checked=False, value=tag: self.remove_history_tag(value)
                )
                chip_layout.addWidget(add_button)
                chip_layout.addWidget(
                    remove_button,
                    0,
                    Qt.AlignmentFlag.AlignTop,
                )
                self.history_layout.addWidget(chip)

    def remove_history_tag(self, tag: str) -> None:
        try:
            history = douyin_commerce_draft_service.remove_tag_history(tag)
        except Exception as exc:
            QMessageBox.warning(self, "删除最近标签", f"删除失败：{exc}")
            return
        if self._history_changed is not None:
            self._history_changed(history)
        else:
            self.refresh_history(history)

    def remember_current_tags(self) -> list[str]:
        history = douyin_commerce_draft_service.remember_tag_history(self._values)
        if self._history_changed is not None:
            self._history_changed(history)
        else:
            self.refresh_history(history)
        return history
