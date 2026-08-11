"""Frameless cards raised on hover, and where on the screen they land.

Placement flips before it clamps: a card asked to sit above an anchor at the top
of the screen has a negative y, and clamping would pin it over the anchor it
describes.

The screen comes from the point being placed at, not from the widget, so a
parentless card lands on the monitor the cursor is on.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

# Enough that the card never sits under the cursor that raised it, and near
# enough that it still reads as belonging to what it points at.
CARD_GAP = 8
CURSOR_OFFSET = QPoint(14, 16)


def apply_hover_card_flags(card: QWidget) -> None:
    """Make `card` a frameless, focus-free, stylesheet-painting popup window.

    A tooltip window rather than Qt.Popup: a popup grabs the mouse, and these sit
    directly over the control somebody is about to click.
    """
    card.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
    card.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    # Without this a plain QWidget draws none of its stylesheet's background,
    # border or radius, and the card renders as a bare rectangle.
    card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)


def _bounds_for(point: QPoint) -> QRect | None:
    screen = QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()
    return screen.availableGeometry() if screen is not None else None


def _fit(corner: QPoint, size, bounds: QRect, *, flip_to: QPoint | None) -> QPoint:
    """Nudge a card's top-left corner onto the screen, flipping before clamping."""
    x, y = corner.x(), corner.y()
    # Overhangs the far edge, or starts off the near one: the other side of the
    # anchor is the answer, and clamping is only the fallback.
    overhangs = y + size.height() > bounds.bottom() + 1 or y < bounds.top()
    if flip_to is not None and overhangs:
        alternative = flip_to.y()
        if bounds.top() <= alternative and alternative + size.height() <= bounds.bottom() + 1:
            y = alternative
    x = max(bounds.left(), min(x, bounds.right() + 1 - size.width()))
    y = max(bounds.top(), min(y, bounds.bottom() + 1 - size.height()))
    return QPoint(x, y)


def place_near_cursor(card: QWidget, global_pos, *, offset: QPoint = CURSOR_OFFSET) -> None:
    """Sit just off the cursor, on whichever side of it fits."""
    card.adjustSize()
    anchor = QPoint(int(global_pos.x()), int(global_pos.y()))
    bounds = _bounds_for(anchor)
    corner = anchor + offset
    if bounds is None:
        card.move(corner)
        return
    size = card.size()
    # Above-left of the cursor when below-right would not fit, so the card never
    # hangs off the corner of the screen the pointer is already in.
    if corner.x() + size.width() > bounds.right() + 1:
        corner.setX(anchor.x() - offset.x() - size.width())
    flip_to = QPoint(corner.x(), anchor.y() - offset.y() - size.height())
    card.move(_fit(corner, size, bounds, flip_to=flip_to))


def place_near_widget(
    card: QWidget,
    anchor: QWidget,
    *,
    anchor_rect: QRect | None = None,
    prefer: str = "above",
) -> None:
    """Sit against `anchor` (or `anchor_rect` in global coordinates), on screen.

    `prefer` is the side tried first; the other is used when the preferred one
    would put the card off the screen.
    """
    card.adjustSize()
    size = card.size()
    if anchor_rect is None:
        anchor_rect = QRect(anchor.mapToGlobal(QPoint(0, 0)), anchor.size())
    above = QPoint(anchor_rect.left(), anchor_rect.top() - size.height() - CARD_GAP)
    below = QPoint(anchor_rect.left(), anchor_rect.bottom() + 1 + CARD_GAP)
    corner, flip_to = (above, below) if prefer == "above" else (below, above)
    bounds = _bounds_for(anchor_rect.center())
    if bounds is None:
        card.move(corner)
        return
    card.move(_fit(corner, size, bounds, flip_to=flip_to))
