"""The bell in the header, and the list that drops from it."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.icons import (
    ICON_MD,
    ICON_SM,
    bell_icon,
    blocked_icon,
    check_icon,
    close_icon,
    icon_pixmap,
    silence_icon,
    warning_icon,
)
from deepreefmap_gui.core.theme import (
    BLOCK,
    BORDER,
    BUTTON,
    CARD_BG,
    FONT_SM,
    FONT_XS,
    ON_ACCENT,
    PRIMARY,
    RADIUS,
    RADIUS_SM,
    SPACE_MD,
    SPACE_SM,
    SPACE_XS,
    SURFACE_HI,
    TEXT_DIM,
    TEXT_MUTED,
    WARNING,
    WINDOW_TEXT,
)
from deepreefmap_gui.core.widgets import muted_label, utility_button_qss
from deepreefmap_gui.notify.model import can_mute
from deepreefmap_gui.survey.models.notification import (
    BLOCKER,
    INFO,
    Notification,
)
from deepreefmap_gui.survey.models.notification import (
    WARNING as SEVERITY_WARNING,
)

# Fixed width, so a message that wraps does not widen the panel and every row
# breaks in the same place. Wide enough for a sentence of advice on two lines.
POPOVER_WIDTH = 460
POPOVER_MAX_LIST_HEIGHT = 420

_SEVERITY_COLOUR = {BLOCKER: BLOCK, SEVERITY_WARNING: WARNING, INFO: PRIMARY}
_SEVERITY_GLYPH = {BLOCKER: blocked_icon, SEVERITY_WARNING: warning_icon, INFO: check_icon}
# The bands the popover groups by, loudest first.
_BANDS = ((BLOCKER, "Blocking"), (SEVERITY_WARNING, "Needs attention"), (INFO, "Recent"))

# The popover's own outline, subtracted when the list inside it is measured.
_BORDER_PX = 1

# Fixed, so every row's age sits on the same edge and the text above wraps in
# the same place whatever the age happens to say.
_STAMP_WIDTH = 52


class BellButton(QToolButton):
    """How many messages are waiting, and how loudly the loudest one asks.

    The badge counts unread only, so a number that never falls never appears.
    Once they have all been read the badge goes but the glyph keeps the top
    severity's colour, because a blocker must not fall silent from one glance at
    a list.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._unread = 0
        self._severity = ""
        self._active = 0
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        # Larger than the badged buttons beside it. Those carry a label saying
        # what they are; this is the glyph and nothing else, so it has to be
        # legible on its own.
        self.setIconSize(QSize(ICON_MD, ICON_MD))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_state(0, "")

    def set_state(self, unread: int, top_severity: str, active: int = 0) -> None:
        self._unread = max(0, unread)
        self._severity = top_severity
        self._active = max(active, self._unread)
        colour = _SEVERITY_COLOUR.get(top_severity)
        self.setIcon(bell_icon(ICON_MD, QColor(colour) if colour else None))
        reserved = SPACE_SM + self._badge_width() + SPACE_XS if self._unread else SPACE_SM
        self.setStyleSheet(utility_button_qss(reserved))
        self.setToolTip(self._tooltip())
        self.setAccessibleName(f"Notifications: {self._unread} unread, {self._active} active")
        self.update()

    def _tooltip(self) -> str:
        if not self._active:
            return "Nothing needs your attention."
        seen = "" if self._unread else ", all seen"
        return f"{self._active} message{'' if self._active == 1 else 's'}{seen}."

    def _badge_text(self) -> str:
        return str(self._unread) if self._unread < 100 else "99+"

    def _badge_width(self) -> int:
        metrics = QFontMetrics(self.font())
        return max(ICON_SM, metrics.horizontalAdvance(self._badge_text()) + 2 * SPACE_XS)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._unread:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self._badge_width()
        height = ICON_SM
        x = self.width() - SPACE_SM - width
        y = (self.height() - height) // 2
        pill = QPainterPath()
        pill.addRoundedRect(QRectF(x, y, width, height), RADIUS_SM, RADIUS_SM)
        painter.fillPath(pill, QColor(_SEVERITY_COLOUR.get(self._severity, PRIMARY)))
        painter.setPen(QColor(ON_ACCENT))
        painter.drawText(
            QRectF(x, y, width, height), Qt.AlignmentFlag.AlignCenter, self._badge_text()
        )
        painter.end()


class NotificationRow(QWidget):
    """One message: what is wrong, the advice under it, and how long it has been.

    Dismiss and mute are plain buttons rather than a menu. This row lives inside
    a ``Qt.Popup``, and a menu opened from one closes the popup under it on some
    platforms. Neither appears on a blocker.
    """

    activated = Signal(str)
    dismissed = Signal(object)
    muted = Signal(str)

    def __init__(self, note: Notification, age: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._note = note
        self.setObjectName("notificationRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"QWidget#notificationRow:hover {{ background: {SURFACE_HI}; }}"
            f" QLabel {{ background: transparent; }}"
        )
        if note.section:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACE_MD, SPACE_XS, SPACE_SM, SPACE_XS)
        layout.setSpacing(SPACE_SM)

        glyph = QLabel()
        draw = _SEVERITY_GLYPH.get(note.severity)
        if draw is not None:
            glyph.setPixmap(icon_pixmap(draw(ICON_SM), ICON_SM, self.devicePixelRatio()))
        glyph.setAlignment(Qt.AlignmentFlag.AlignTop)
        glyph.setContentsMargins(0, SPACE_XS // 2, 0, 0)
        layout.addWidget(glyph)

        text = QVBoxLayout()
        text.setSpacing(0)
        title = QLabel(note.title)
        title.setWordWrap(True)
        title.setStyleSheet(f"color: {WINDOW_TEXT}; font-size: {FONT_SM};")
        text.addWidget(title)
        if note.body:
            body = QLabel(note.body)
            body.setWordWrap(True)
            body.setStyleSheet(f"color: {TEXT_MUTED}; font-size: {FONT_XS};")
            text.addWidget(body)
        layout.addLayout(text, 1)

        stamp = QLabel(age)
        stamp.setStyleSheet(f"color: {TEXT_DIM}; font-size: {FONT_XS};")
        stamp.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        stamp.setFixedWidth(_STAMP_WIDTH)
        layout.addWidget(stamp)

        if can_mute(note.severity):
            layout.addLayout(self._actions())
        else:
            # The two buttons' worth of room, so a blocker's text wraps where
            # every other row's does instead of running under the timestamp.
            layout.addSpacing(2 * (ICON_SM + SPACE_XS))

    def _actions(self) -> QHBoxLayout:
        """Two glyphs, not two words.

        "Clear" and "Never" spelled out took more width than the message beside
        them and read as the point of the row. The tooltips carry the sentence.
        """
        row = QHBoxLayout()
        row.setSpacing(0)
        row.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        for icon, tip, slot in (
            (close_icon, "Clear this. It comes back if it happens again.", self._on_dismiss),
            (silence_icon, "Never show this kind of message again.", self._on_mute),
        ):
            button = QToolButton()
            button.setIcon(icon(ICON_SM))
            button.setIconSize(QSize(ICON_SM, ICON_SM))
            button.setToolTip(tip)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(
                f"QToolButton {{ border: none; background: transparent;"
                f" padding: {SPACE_XS // 2}px; border-radius: {RADIUS_SM}px; }}"
                f" QToolButton:hover {{ background: {BUTTON}; }}"
            )
            button.clicked.connect(slot)
            row.addWidget(button)
        return row

    def _on_dismiss(self) -> None:
        self.dismissed.emit(self._note.id)

    def _on_mute(self) -> None:
        self.muted.emit(self._note.fingerprint)

    def mousePressEvent(self, event) -> None:
        if self._note.section:
            self.activated.emit(self._note.section)
        super().mousePressEvent(event)


class NotificationPopover(QWidget):
    """Everything waiting, grouped by how loudly it asks."""

    activated = Signal(str)
    dismissed = Signal(object)
    muted = Signal(str)
    history_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("notificationPopover")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"QWidget#notificationPopover {{ background: {CARD_BG};"
            f" border: 1px solid {BORDER}; border-radius: {RADIUS}px; }}"
        )
        self.setFixedWidth(POPOVER_WIDTH)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._outer = outer

        head = QHBoxLayout()
        head.setContentsMargins(SPACE_MD, SPACE_XS, SPACE_SM, SPACE_XS)
        title = QLabel("Notifications")
        title.setStyleSheet(f"color: {WINDOW_TEXT}; font-size: {FONT_SM};")
        head.addWidget(title)
        head.addStretch(1)
        # The one route back to a message somebody said never to show again, so
        # it is on the panel they said it from.
        link = QToolButton()
        link.setText("History ›")
        link.setToolTip("Everything this survey has reported, and what was silenced.")
        link.setCursor(Qt.CursorShape.PointingHandCursor)
        link.setStyleSheet(
            f"QToolButton {{ border: none; background: transparent; color: {TEXT_MUTED};"
            f" font-size: {FONT_XS}; }}"
            f" QToolButton:hover {{ color: {WINDOW_TEXT}; }}"
        )
        link.clicked.connect(self.history_requested)
        head.addWidget(link)
        outer.addLayout(head)

        self._list = QWidget()
        self._rows = QVBoxLayout(self._list)
        self._rows.setContentsMargins(0, 0, 0, SPACE_XS)
        self._rows.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setMaximumHeight(POPOVER_MAX_LIST_HEIGHT)
        scroll.setWidget(self._list)
        # Sized to the list rather than stretched: the panel has to shrink back
        # when a row is cleared, or an emptied band leaves a hole where it was.
        scroll.setSizeAdjustPolicy(QScrollArea.SizeAdjustPolicy.AdjustToContents)
        self._scroll = scroll
        outer.addWidget(scroll)

    def set_notifications(
        self, notes: list[Notification], age: Callable[[Notification], str]
    ) -> None:
        """Repaint the list. ``age`` renders one row's timestamp as "4 min"."""
        while self._rows.count():
            item = self._rows.takeAt(0)
            old = item.widget() if item is not None else None
            if old is not None:
                # Unparented before deleteLater, which only runs once the event
                # loop next turns. A repaint that beat it would otherwise find
                # the old rows still children and stack the new ones under them.
                old.setParent(None)
                old.deleteLater()
        if not notes:
            empty = muted_label("Nothing needs your attention.")
            empty.setStyleSheet(
                f"color: {TEXT_MUTED}; font-size: {FONT_XS};"
                f" padding: {SPACE_SM}px {SPACE_MD}px;"
            )
            self._rows.addWidget(empty)
            self._fit()
            return
        for severity, caption in _BANDS:
            band = [n for n in notes if n.severity == severity]
            if not band:
                continue
            self._rows.addWidget(self._caption(caption))
            for note in band:
                row = NotificationRow(note, age(note))
                row.activated.connect(self.activated)
                row.dismissed.connect(self.dismissed)
                row.muted.connect(self.muted)
                self._rows.addWidget(row)
        self._fit()

    def _fit(self) -> None:
        """Shrink to what is left after a row is cleared.

        Without this the panel keeps the height it opened at, and an emptied band
        leaves a hole where it was while the remaining row stretches into it.
        """
        # Measured at a known width, not from the plain size hint. Every message
        # wraps, and a wrapped label cannot say how tall it is until it is told
        # how wide it may be, so the hint before the first layout pass is a few
        # pixels and the panel collapses to its header.
        width = POPOVER_WIDTH - 2 * _BORDER_PX
        self._list.setFixedWidth(width)
        # A widget added to a layout is not shown until the parent's next show,
        # and a hidden widget contributes nothing to the hint below.
        for index in range(self._rows.count()):
            item = self._rows.itemAt(index)
            child = item.widget() if item is not None else None
            if child is not None:
                child.setVisible(True)
                child.ensurePolished()
        self._rows.activate()
        wanted = self._rows.heightForWidth(width)
        if wanted <= 0:
            wanted = self._rows.totalSizeHint().height()
        self._scroll.setFixedHeight(min(wanted, POPOVER_MAX_LIST_HEIGHT))
        self.adjustSize()
        # adjustSize resizes the panel but does not re-divide it, so without
        # this the scroll area keeps whatever height the last list left it.
        self._outer.activate()

    @staticmethod
    def _caption(text: str) -> QLabel:
        label = QLabel(text.upper())
        label.setStyleSheet(
            f"color: {TEXT_DIM}; font-size: {FONT_XS};"
            f" padding: {SPACE_SM}px {SPACE_MD}px {SPACE_XS // 2}px {SPACE_MD}px;"
        )
        return label

    def show_under(self, anchor: QWidget) -> None:
        """Right-aligned under the bell, nudged back on screen if it overhangs."""
        self.adjustSize()
        corner = anchor.mapToGlobal(QPoint(anchor.width() - self.width(), anchor.height()))
        corner.setY(corner.y() + SPACE_XS)
        screen = anchor.screen()
        if screen is not None:
            bounds = screen.availableGeometry()
            corner.setX(max(bounds.left(), min(corner.x(), bounds.right() - self.width())))
            corner.setY(min(corner.y(), bounds.bottom() - self.height()))
        self.move(corner)
        self.show()


def relative_age(stamp: str, now: str) -> str:
    """"just now", "4 min", "3 h", "2 d". Coarse on purpose: the exact second is
    in the history, and a list of them is read at a glance."""
    try:
        then = datetime.fromisoformat(stamp)
        seconds = (datetime.fromisoformat(now) - then).total_seconds()
    except ValueError:
        return ""
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h"
    return f"{int(seconds // 86400)} d"


