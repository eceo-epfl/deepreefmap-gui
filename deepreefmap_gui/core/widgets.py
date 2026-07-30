"""Small shared building blocks: section cards, empty states, status pills."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.theme import (
    BORDER,
    CARD_BG,
    ERROR,
    RADIUS,
    RADIUS_SM,
    SUCCESS,
    TEXT_DIM,
    TEXT_MUTED,
    WARN_BG,
    WARN_BORDER,
    WARN_TEXT,
    WARNING,
)


def section_card(title: str = "", *, spacing: int = 8) -> tuple[QWidget, QVBoxLayout]:
    """A titled panel that sits above the shell, and the layout to fill it.

    Returns the card and its content layout, so callers add their widgets to the
    layout without needing to know how the title is built.
    """
    card = QWidget()
    card.setObjectName("sectionCard")
    # Object-name scoped so the fill lands on the card only: an unscoped rule
    # would cascade into every child widget's background too. Views inside a
    # card drop their own border, which would otherwise double the card's.
    card.setStyleSheet(
        f"QWidget#sectionCard {{ background-color: {CARD_BG};"
        f" border: 1px solid {BORDER}; border-radius: {RADIUS}px; }}"
        " QWidget#sectionCard QAbstractItemView { border: none; }"
    )
    outer = QVBoxLayout(card)
    outer.setContentsMargins(10, 8, 10, 10)
    outer.setSpacing(spacing)
    if title:
        label = QLabel(title)
        font = label.font()
        font.setWeight(QFont.Weight.DemiBold)
        label.setFont(font)
        label.setStyleSheet(f"color: {TEXT_MUTED};")
        outer.addWidget(label)
    return card, outer


class EmptyState(QWidget):
    """What an empty pane says: what is missing, and what fills it."""

    def __init__(self, message: str, hint: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(4)
        layout.addStretch(1)

        self._message = QLabel(message)
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setWordWrap(True)
        self._message.setStyleSheet(f"color: {TEXT_MUTED};")
        layout.addWidget(self._message)

        self._hint = QLabel(hint)
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet(f"color: {TEXT_DIM};")
        self._hint.setVisible(bool(hint))
        layout.addWidget(self._hint)

        layout.addStretch(1)

    def set_text(self, message: str, hint: str = "") -> None:
        self._message.setText(message)
        self._hint.setText(hint)
        self._hint.setVisible(bool(hint))


class NotReadyStrip(QWidget):
    """The one thing blocking a page, next to the button that goes and fixes it.

    Directions the page cannot follow ("switch modes, then find the tab") are
    not an action, so the destination is a button. Hidden whenever nothing
    blocks, which is why it sits above the content rather than inside it.
    """

    action_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("notReadyStrip")
        self.setStyleSheet(
            f"QWidget#notReadyStrip {{ background-color: {WARN_BG};"
            f" border: 1px solid {WARN_BORDER}; border-radius: {RADIUS_SM}px; }}"
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 6, 10, 6)
        row.setSpacing(10)

        self._reason = QLabel("")
        self._reason.setWordWrap(True)
        self._reason.setStyleSheet(f"color: {WARN_TEXT};")
        row.addWidget(self._reason, 1)

        self._action = QPushButton("")
        self._action.clicked.connect(self.action_clicked)
        row.addWidget(self._action)

        self.setVisible(False)

    def show_blocker(self, reason: str, action: str = "") -> None:
        """Name the blocker. An empty action means it is fixed on this page."""
        self._reason.setText(reason)
        self._action.setText(action)
        self._action.setVisible(bool(action))
        self.setVisible(bool(reason))

    def clear(self) -> None:
        self._reason.setText("")
        self.setVisible(False)


# Run statuses come from SurveyStore; anything unrecognised stays neutral.
_STATUS_COLORS = {
    "succeeded": SUCCESS,
    "running": WARNING,
    "failed": ERROR,
    "cancelled": TEXT_DIM,
    # Abandoned by a crash or quit, not a clean stop: amber flags it as work to
    # redo rather than the neutral grey a deliberate cancel gets.
    "interrupted": WARNING,
    "queued": TEXT_MUTED,
    # Held back by the user, not by anything that went wrong.
    "held": TEXT_DIM,
}


class StatusPillDelegate(QStyledItemDelegate):
    """Paints a run status as a tinted pill, readable across the table.

    A delegate rather than a cell widget so the QTableWidgetItem stays the
    source of truth for the status text.
    """

    def paint(self, painter, option: QStyleOptionViewItem, index) -> None:
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        if not text:
            super().paint(painter, option, index)
            return

        # Selection and the alternating row fill still come from the style.
        blank = QStyleOptionViewItem(option)
        self.initStyleOption(blank, index)
        blank.text = ""
        style = option.widget.style() if option.widget else None
        if style is not None:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, blank, painter, option.widget)

        # Key off the leading word so a decorated pill ("Succeeded ⚠") keeps its
        # colour; case-insensitive so a title-cased "Failed" pill still reads red.
        color = QColor(_STATUS_COLORS.get(text.lower().split()[0], TEXT_MUTED))
        metrics = option.fontMetrics
        pad_x = 8
        width = min(metrics.horizontalAdvance(text) + pad_x * 2, option.rect.width() - 8)
        height = metrics.height() + 4
        pill = QRectF(
            option.rect.left() + 4,
            option.rect.center().y() - height / 2 + 1,
            max(1, width),
            height,
        )

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fill = QColor(color)
        fill.setAlpha(46)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(pill, height / 2, height / 2)
        painter.setPen(color)
        painter.drawText(
            pill,
            Qt.AlignmentFlag.AlignCenter,
            metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(pill.width()) - pad_x),
        )
        painter.restore()
