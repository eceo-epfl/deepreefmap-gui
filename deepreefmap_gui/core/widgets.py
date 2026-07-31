"""Small shared building blocks: section cards, empty states, status pills."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.theme import (
    BORDER,
    CARD_BG,
    ERROR,
    PRIMARY,
    RADIUS,
    RADIUS_SM,
    SUCCESS,
    TEXT_DIM,
    TEXT_MUTED,
    WARN_BG,
    WARN_BORDER,
    WARN_TEXT,
    WARNING,
    WINDOW,
    WINDOW_TEXT,
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


class FilterChips(QWidget):
    """A row of exclusive filters, each carrying its own count.

    The count is the point: a chip reading "Failed 3" answers the question
    before it is clicked, and a chip reading "Failed 0" says not to bother. Empty
    chips stay visible rather than disappearing, so the row does not reflow under
    the cursor as a batch runs.
    """

    changed = Signal(str)

    def __init__(
        self, options: tuple[tuple[str, str], ...], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._labels = dict(options)
        self._buttons: dict[str, QToolButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for key, title in options:
            button = QToolButton()
            button.setText(title)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(
                f"QToolButton {{ border: 1px solid {BORDER};"
                f" border-radius: {RADIUS_SM}px; padding: 3px 10px;"
                f" background: transparent; color: {TEXT_MUTED}; }}"
                f" QToolButton:hover {{ color: {WINDOW_TEXT}; }}"
                f" QToolButton:checked {{ background: {PRIMARY}; color: {WINDOW};"
                " font-weight: 600; border-color: transparent; }"
            )
            button.toggled.connect(
                lambda checked, k=key: self.changed.emit(k) if checked else None
            )
            group.addButton(button)
            layout.addWidget(button)
            self._buttons[key] = button
        first = options[0][0]
        self._buttons[first].setChecked(True)

    def set_counts(self, counts: dict[str, int]) -> None:
        for key, button in self._buttons.items():
            count = counts.get(key)
            button.setText(
                self._labels[key] if count is None else f"{self._labels[key]}  {count}"
            )

    def current(self) -> str:
        return next(k for k, b in self._buttons.items() if b.isChecked())

    def set_current(self, key: str) -> None:
        button = self._buttons.get(key)
        if button is not None and not button.isChecked():
            button.setChecked(True)


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
        # Parented before shown: setVisible on a parentless widget maps it as a
        # top-level window, which flashes an empty titlebar box on screen.
        layout.addWidget(self._hint)
        self._hint.setVisible(bool(hint))

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
# Public because the run detail pane colours its status line from the same map:
# one status must not read green in the list and grey beside it.
STATUS_COLORS = {
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


# Percent complete of the pass a status cell describes, 0-100. Absent on every
# row but the one being processed, which is what makes the pill a progress bar.
PASS_PERCENT_ROLE = Qt.ItemDataRole.UserRole + 1


class StatusPillDelegate(QStyledItemDelegate):
    """Paints a run status as a tinted pill, readable across the table.

    A delegate rather than a cell widget so the QTableWidgetItem stays the
    source of truth for the status text. The running row's pill doubles as its
    progress bar: PASS_PERCENT_ROLE fills it from the left.
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
        color = QColor(STATUS_COLORS.get(text.lower().split()[0], TEXT_MUTED))
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

        # A running pass fills its own pill rather than growing a second widget in
        # the row: the pill is already the thing the eye goes to for that pass.
        percent = index.data(PASS_PERCENT_ROLE)
        if percent is not None:
            done = QColor(color)
            done.setAlpha(120)
            painter.setBrush(done)
            painter.save()
            clip = QRectF(pill)
            clip.setWidth(pill.width() * max(0.0, min(100.0, float(percent))) / 100.0)
            painter.setClipRect(clip)
            painter.drawRoundedRect(pill, height / 2, height / 2)
            painter.restore()

        painter.setPen(color)
        painter.drawText(
            pill,
            Qt.AlignmentFlag.AlignCenter,
            metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(pill.width()) - pad_x),
        )
        painter.restore()
