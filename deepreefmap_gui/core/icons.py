"""QPainter-rendered icons for consistent, DPI-aware toolbar buttons.

Three sizes and one stroke weight. Every icon used to carry its own default
size and its own pen width -- six weights between 1.2 and 2.2 at the same
nominal size -- so a tick sat visibly bolder than the copy sheets it replaced in
the same button.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap

from deepreefmap_gui.core.theme import (
    BORDER_STRONG,
    ERROR,
    SUCCESS,
    TEXT_MUTED,
    WARNING,
    WINDOW,
)

# Inline with text, on a toolbar button, and on the transport controls.
ICON_SM, ICON_MD, ICON_LG = 16, 20, 24

# One stroke weight, scaled with the icon so a 16px glyph is not drawn with the
# line weight of a 24px one.
_STROKE = 1.6


def _stroke(size: int) -> float:
    return max(1.0, _STROKE * size / ICON_LG)


def _px(size: int = ICON_LG, bg: QColor | None = None) -> tuple[QPixmap, QPainter]:
    pm = QPixmap(size, size)
    pm.fill(bg or Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    return pm, p


def crosshair_icon(size: int = ICON_MD, color: QColor | None = None) -> QIcon:
    c = color or QColor(230, 230, 230)
    pm, p = _px(size)
    pen = QPen(c, _stroke(size))
    p.setPen(pen)
    cx, cy = size / 2, size / 2
    r = size * 0.32
    gap = size * 0.08
    p.drawEllipse(QPointF(cx, cy), r, r)
    p.drawLine(QPointF(cx, cy - r - gap), QPointF(cx, cy - gap))
    p.drawLine(QPointF(cx, cy + gap), QPointF(cx, cy + r + gap))
    p.drawLine(QPointF(cx - r - gap, cy), QPointF(cx - gap, cy))
    p.drawLine(QPointF(cx + gap, cy), QPointF(cx + r + gap, cy))
    p.end()
    return QIcon(pm)


def refresh_icon(size: int = ICON_MD, color: QColor | None = None) -> QIcon:
    c = color or QColor(230, 230, 230)
    pm, p = _px(size)
    pen = QPen(c, _stroke(size))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    cx, cy = size / 2, size / 2
    r = size * 0.3
    p.drawArc(QRectF(cx - r, cy - r, 2 * r, 2 * r), 60 * 16, 270 * 16)
    ax, ay = cx + r * 0.5, cy - r
    a = size * 0.12
    p.drawLine(QPointF(ax - a, ay - a * 0.3), QPointF(ax, ay))
    p.drawLine(QPointF(ax, ay), QPointF(ax - a * 0.3, ay + a))
    p.end()
    return QIcon(pm)


def play_icon(size: int = ICON_MD, color: QColor | None = None) -> QIcon:
    c = color or QColor(230, 230, 230)
    pm, p = _px(size)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(c)
    m = size * 0.3
    tri = [
        QPointF(m, m),
        QPointF(size - m, size / 2),
        QPointF(m, size - m),
    ]
    from PySide6.QtGui import QPolygonF

    p.drawPolygon(QPolygonF(tri))
    p.end()
    return QIcon(pm)


def pause_icon(size: int = ICON_MD, color: QColor | None = None) -> QIcon:
    c = color or QColor(230, 230, 230)
    pm, p = _px(size)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(c)
    bar_w = size * 0.16
    gap = size * 0.14
    top, bot = size * 0.28, size * 0.72
    cx = size / 2
    p.drawRoundedRect(QRectF(cx - gap / 2 - bar_w, top, bar_w, bot - top), 1.5, 1.5)
    p.drawRoundedRect(QRectF(cx + gap / 2, top, bar_w, bot - top), 1.5, 1.5)
    p.end()
    return QIcon(pm)


def plus_icon(size: int = ICON_MD, color: QColor | None = None) -> QIcon:
    c = color or QColor(230, 230, 230)
    pm, p = _px(size)
    pen = QPen(c, _stroke(size))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    cx, cy = size / 2, size / 2
    arm = size * 0.28
    p.drawLine(QPointF(cx - arm, cy), QPointF(cx + arm, cy))
    p.drawLine(QPointF(cx, cy - arm), QPointF(cx, cy + arm))
    p.end()
    return QIcon(pm)


def arrow_right_icon(size: int = ICON_MD, color: QColor | None = None) -> QIcon:
    c = color or QColor(230, 230, 230)
    pm, p = _px(size)
    pen = QPen(c, _stroke(size))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    cx, cy = size / 2, size / 2
    arm = size * 0.28
    head = size * 0.16
    p.drawLine(QPointF(cx - arm, cy), QPointF(cx + arm, cy))
    p.drawLine(QPointF(cx + arm - head, cy - head), QPointF(cx + arm, cy))
    p.drawLine(QPointF(cx + arm - head, cy + head), QPointF(cx + arm, cy))
    p.end()
    return QIcon(pm)


def check_icon(size: int = ICON_SM, color: QColor | None = None) -> QIcon:
    c = color or QColor(SUCCESS)
    pm, p = _px(size)
    pen = QPen(c, _stroke(size))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    m = size * 0.22
    p.drawLine(QPointF(m, size * 0.52), QPointF(size * 0.42, size - m))
    p.drawLine(QPointF(size * 0.42, size - m), QPointF(size - m, m))
    p.end()
    return QIcon(pm)


def download_icon(size: int = ICON_SM, color: QColor | None = None) -> QIcon:
    c = color or QColor(WARNING)
    pm, p = _px(size)
    pen = QPen(c, _stroke(size))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    cx = size / 2
    top = size * 0.15
    bot = size * 0.62
    p.drawLine(QPointF(cx, top), QPointF(cx, bot))
    a = size * 0.18
    p.drawLine(QPointF(cx - a, bot - a), QPointF(cx, bot))
    p.drawLine(QPointF(cx + a, bot - a), QPointF(cx, bot))
    base_y = size * 0.82
    p.drawLine(QPointF(size * 0.22, base_y), QPointF(size * 0.78, base_y))
    p.end()
    return QIcon(pm)


def warning_icon(size: int = ICON_SM, color: QColor | None = None) -> QIcon:
    """Triangle and exclamation: something needs looking at, nothing is stopped."""
    c = color or QColor(WARNING)
    pm, p = _px(size)
    pen = QPen(c, _stroke(size))
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    cx = size / 2
    top = size * 0.16
    bot = size * 0.82
    half = size * 0.38
    p.drawPolyline(
        [
            QPointF(cx, top),
            QPointF(cx + half, bot),
            QPointF(cx - half, bot),
            QPointF(cx, top),
        ]
    )
    p.drawLine(QPointF(cx, size * 0.40), QPointF(cx, size * 0.60))
    p.drawPoint(QPointF(cx, size * 0.71))
    p.end()
    return QIcon(pm)


def blocked_icon(size: int = ICON_SM, color: QColor | None = None) -> QIcon:
    """Ringed cross: distinct in silhouette from the warning triangle, so the
    two are told apart without relying on their colours."""
    c = color or QColor(ERROR)
    pm, p = _px(size)
    pen = QPen(c, _stroke(size))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    cx = size / 2
    r = size * 0.38
    p.drawEllipse(QPointF(cx, cx), r, r)
    arm = size * 0.17
    p.drawLine(QPointF(cx - arm, cx - arm), QPointF(cx + arm, cx + arm))
    p.drawLine(QPointF(cx + arm, cx - arm), QPointF(cx - arm, cx + arm))
    p.end()
    return QIcon(pm)


def machine_icon(size: int = ICON_SM, color: QColor | None = None) -> QIcon:
    """A laptop, for the destination that describes the computer you are on."""
    c = color or QColor(TEXT_MUTED)
    pm, p = _px(size)
    pen = QPen(c, _stroke(size))
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.drawRoundedRect(
        QRectF(size * 0.18, size * 0.22, size * 0.64, size * 0.44), 1.5, 1.5
    )
    p.drawLine(QPointF(size * 0.08, size * 0.78), QPointF(size * 0.92, size * 0.78))
    p.end()
    return QIcon(pm)


def copy_icon(size: int = ICON_SM, color: QColor | None = None) -> QIcon:
    """Two offset sheets, the usual shorthand for copy-to-clipboard."""
    c = color or QColor(230, 230, 230)
    pm, p = _px(size)
    pen = QPen(c, _stroke(size))
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    w = size * 0.42
    h = size * 0.52
    p.drawRoundedRect(QRectF(size * 0.20, size * 0.16, w, h), 1.5, 1.5)
    p.drawRoundedRect(QRectF(size * 0.38, size * 0.32, w, h), 1.5, 1.5)
    p.end()
    return QIcon(pm)


# What a step has to say about itself. Deliberately not about position: the
# checked pill in the header already shows which step you are on, which frees
# the badge to carry meaning instead of repeating the selection.
#
# The vocabulary is owned by simple/progress.py, which must stay Qt-free and so
# cannot be imported from core. Spelled out here rather than imported upwards;
# tests/gui/test_simple_progress.py asserts the two never drift apart.
STEP_STATES = ("todo", "ok", "attention", "blocked")

_STEP_RING = {
    "todo": BORDER_STRONG,
    "ok": SUCCESS,
    "attention": WARNING,
    "blocked": ERROR,
}
_STEP_INK = {
    "todo": TEXT_MUTED,
    "ok": SUCCESS,
    "attention": WARNING,
    "blocked": ERROR,
}


def step_badge_icon(number: int, state: str, size: int = ICON_MD) -> QIcon:
    """Numbered disc for the wizard stepper.

    A satisfied step carries a tick and a blocked one an exclamation, so the
    header says what still needs doing without the labels being read.
    """
    if state not in STEP_STATES:
        raise ValueError(f"Unknown step state: {state!r}")
    pm, p = _px(size)
    cx, cy = size / 2, size / 2
    r = size * 0.42

    # Filled with the shell colour rather than left transparent: the badge sits
    # on the accent pill when its step is selected, and a dim ring and numeral
    # drawn straight onto that blue are barely legible.
    p.setPen(QPen(QColor(_STEP_RING[state]), 1.2 if state == "todo" else 1.4))
    p.setBrush(QColor(WINDOW))
    p.drawEllipse(QPointF(cx, cy), r, r)
    ink = QColor(_STEP_INK[state])

    if state == "ok":
        pen = QPen(ink, _stroke(size))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawLine(QPointF(size * 0.30, cy), QPointF(size * 0.44, size * 0.66))
        p.drawLine(QPointF(size * 0.44, size * 0.66), QPointF(size * 0.71, size * 0.34))
    else:
        f = QFont()
        f.setPixelSize(max(8, int(size * 0.52)))
        f.setBold(True)
        p.setFont(f)
        p.setPen(ink)
        glyph = "!" if state == "blocked" else str(number)
        p.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, glyph)
    p.end()
    return QIcon(pm)


def lock_icon(size: int = ICON_SM, color: QColor | None = None) -> QIcon:
    c = color or QColor(WARNING)
    pm, p = _px(size)
    pen = QPen(c, _stroke(size))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    cx = size / 2
    bw = size * 0.44
    bh = size * 0.34
    by = size * 0.52
    p.drawRect(QRectF(cx - bw / 2, by, bw, bh))
    ar = size * 0.22
    p.drawArc(QRectF(cx - ar, by - ar * 1.6, ar * 2, ar * 2), 0, 180 * 16)
    p.end()
    return QIcon(pm)
