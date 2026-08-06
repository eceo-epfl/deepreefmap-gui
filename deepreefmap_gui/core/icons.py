"""QPainter-rendered icons for consistent, DPI-aware toolbar buttons.

Three sizes and one stroke weight, so two glyphs in the same button are drawn
at the same weight.
"""

from __future__ import annotations

import functools
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QTransform

from deepreefmap_gui.core.theme import (
    ERROR,
    SUCCESS,
    TEXT_MUTED,
    WARNING,
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


# Ink for a glyph with no role colour of its own: near-white, so it reads on the
# dark surfaces without the hard contrast of pure white. Not a theme token
# because it is the icon layer's own default, not a text colour.
DEFAULT_INK = "#e6e6e6"


def _drawn(
    *,
    size: int = ICON_MD,
    ink: str = DEFAULT_INK,
    pen: bool = True,
    cap: bool = False,
    join: bool = False,
) -> Callable[[Callable[..., None]], Callable[..., QIcon]]:
    """Wrap a glyph's drawing steps in the pixmap and pen setup they all share.

    Every icon opened with the same five lines and closed with the same two; the
    only thing that differed was the default size, the ink, and which pen styles
    the glyph needed. Those are the arguments here, so what is left in each
    function body is the drawing itself.

    The wrapped function receives ``(painter, size, colour)``.
    """

    def decorate(draw: Callable[..., None]) -> Callable[..., QIcon]:
        @functools.wraps(draw)
        def build(size_: int = size, color: QColor | None = None) -> QIcon:
            c = color or QColor(ink)
            pm, p = _px(size_)
            if pen:
                q = QPen(c, _stroke(size_))
                if cap:
                    q.setCapStyle(Qt.PenCapStyle.RoundCap)
                if join:
                    q.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                p.setPen(q)
            else:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(c)
            draw(p, size_, c)
            p.end()
            return QIcon(pm)

        return build

    return decorate


@_drawn(size=ICON_MD)
def crosshair_icon(p: QPainter, size: int, c: QColor) -> None:
    cx, cy = size / 2, size / 2
    r = size * 0.32
    gap = size * 0.08
    p.drawEllipse(QPointF(cx, cy), r, r)
    p.drawLine(QPointF(cx, cy - r - gap), QPointF(cx, cy - gap))
    p.drawLine(QPointF(cx, cy + gap), QPointF(cx, cy + r + gap))
    p.drawLine(QPointF(cx - r - gap, cy), QPointF(cx - gap, cy))
    p.drawLine(QPointF(cx + gap, cy), QPointF(cx + r + gap, cy))


@_drawn(size=ICON_MD, cap=True)
def refresh_icon(p: QPainter, size: int, c: QColor) -> None:
    cx, cy = size / 2, size / 2
    r = size * 0.3
    p.drawArc(QRectF(cx - r, cy - r, 2 * r, 2 * r), 60 * 16, 270 * 16)
    ax, ay = cx + r * 0.5, cy - r
    a = size * 0.12
    p.drawLine(QPointF(ax - a, ay - a * 0.3), QPointF(ax, ay))
    p.drawLine(QPointF(ax, ay), QPointF(ax - a * 0.3, ay + a))


@_drawn(size=ICON_MD, pen=False)
def play_icon(p: QPainter, size: int, c: QColor) -> None:
    m = size * 0.3
    tri = [
        QPointF(m, m),
        QPointF(size - m, size / 2),
        QPointF(m, size - m),
    ]
    from PySide6.QtGui import QPolygonF

    p.drawPolygon(QPolygonF(tri))


@_drawn(size=ICON_MD, pen=False)
def pause_icon(p: QPainter, size: int, c: QColor) -> None:
    bar_w = size * 0.16
    gap = size * 0.14
    top, bot = size * 0.28, size * 0.72
    cx = size / 2
    p.drawRoundedRect(QRectF(cx - gap / 2 - bar_w, top, bar_w, bot - top), 1.5, 1.5)
    p.drawRoundedRect(QRectF(cx + gap / 2, top, bar_w, bot - top), 1.5, 1.5)


@_drawn(size=ICON_MD, cap=True, join=True)
def arrow_right_icon(p: QPainter, size: int, c: QColor) -> None:
    cx, cy = size / 2, size / 2
    arm = size * 0.28
    head = size * 0.16
    p.drawLine(QPointF(cx - arm, cy), QPointF(cx + arm, cy))
    p.drawLine(QPointF(cx + arm - head, cy - head), QPointF(cx + arm, cy))
    p.drawLine(QPointF(cx + arm - head, cy + head), QPointF(cx + arm, cy))


@_drawn(size=ICON_SM, ink=SUCCESS, cap=True, join=True)
def check_icon(p: QPainter, size: int, c: QColor) -> None:
    m = size * 0.22
    p.drawLine(QPointF(m, size * 0.52), QPointF(size * 0.42, size - m))
    p.drawLine(QPointF(size * 0.42, size - m), QPointF(size - m, m))


@_drawn(size=ICON_SM, ink=WARNING, cap=True)
def download_icon(p: QPainter, size: int, c: QColor) -> None:
    cx = size / 2
    top = size * 0.15
    bot = size * 0.62
    p.drawLine(QPointF(cx, top), QPointF(cx, bot))
    a = size * 0.18
    p.drawLine(QPointF(cx - a, bot - a), QPointF(cx, bot))
    p.drawLine(QPointF(cx + a, bot - a), QPointF(cx, bot))
    base_y = size * 0.82
    p.drawLine(QPointF(size * 0.22, base_y), QPointF(size * 0.78, base_y))


@_drawn(size=ICON_SM, ink=WARNING, join=True)
def warning_icon(p: QPainter, size: int, c: QColor) -> None:
    """Triangle and exclamation: something needs looking at, nothing is stopped."""
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


@_drawn(size=ICON_SM, ink=ERROR, pen=False)
def blocked_icon(p: QPainter, size: int, c: QColor) -> None:
    """The warning triangle, filled, with the exclamation cut out of it.

    Same silhouette as warning_icon so both read as road signs; the weight and
    the colour carry the severity between them.
    """
    cx = size / 2
    top, bottom, half = size * 0.12, size * 0.86, size * 0.44
    body = QPainterPath()
    body.moveTo(QPointF(cx, top))
    body.lineTo(QPointF(cx + half, bottom))
    body.lineTo(QPointF(cx - half, bottom))
    body.closeSubpath()
    mark = QPainterPath()
    bar = size * 0.10
    mark.addRoundedRect(
        QRectF(cx - bar / 2, size * 0.38, bar, size * 0.22), bar / 2, bar / 2
    )
    mark.addEllipse(QPointF(cx, size * 0.71), bar * 0.6, bar * 0.6)
    p.drawPath(body.subtracted(mark))


@_drawn(size=ICON_SM, ink=TEXT_MUTED, pen=False)
def cog_icon(p: QPainter, size: int, c: QColor) -> None:
    """The settings gear: a toothed disc with a hole through it.

    One filled path: a gear is recognised by its outline, so teeth drawn as
    separate strokes read as a sun instead.
    """
    cx = size / 2
    body = QPainterPath()
    body.addEllipse(QPointF(cx, cx), size * 0.30, size * 0.30)
    tooth_w = size * 0.16
    tooth_h = size * 0.86
    for step in range(4):
        tooth = QPainterPath()
        tooth.addRoundedRect(
            QRectF(cx - tooth_w / 2, cx - tooth_h / 2, tooth_w, tooth_h), 1.2, 1.2
        )
        spin = QTransform().translate(cx, cx).rotate(step * 45).translate(-cx, -cx)
        body = body.united(spin.map(tooth))
    hole = QPainterPath()
    hole.addEllipse(QPointF(cx, cx), size * 0.13, size * 0.13)
    p.drawPath(body.subtracted(hole))


@_drawn(size=ICON_SM, ink=TEXT_MUTED, cap=True, join=True)
def log_icon(p: QPainter, size: int, c: QColor) -> None:
    """A sheet of written lines: the run's output as it is printed."""
    p.drawRoundedRect(QRectF(size * 0.20, size * 0.14, size * 0.60, size * 0.72), 1.5, 1.5)
    left = size * 0.32
    for row, width in enumerate((0.36, 0.30, 0.36)):
        y = size * (0.34 + 0.18 * row)
        p.drawLine(QPointF(left, y), QPointF(left + size * width, y))


@_drawn(size=ICON_SM, join=True)
def copy_icon(p: QPainter, size: int, c: QColor) -> None:
    """Two offset sheets, the usual shorthand for copy-to-clipboard."""
    w = size * 0.42
    h = size * 0.52
    p.drawRoundedRect(QRectF(size * 0.20, size * 0.16, w, h), 1.5, 1.5)
    p.drawRoundedRect(QRectF(size * 0.38, size * 0.32, w, h), 1.5, 1.5)


def _chain_links(p: QPainter, size: int, gap: float) -> None:
    """Two rounded links reaching for each other across ``gap``."""
    w = size * 0.34
    h = size * 0.30
    y = (size - h) / 2
    p.drawRoundedRect(QRectF(size / 2 - gap / 2 - w, y, w, h), h / 2, h / 2)
    p.drawRoundedRect(QRectF(size / 2 + gap / 2, y, w, h), h / 2, h / 2)


@_drawn(size=ICON_SM, ink=TEXT_MUTED, cap=True, join=True)
def link_icon(p: QPainter, size: int, c: QColor) -> None:
    """Two links joined: the file this clip names is where it was left."""
    _chain_links(p, size, size * 0.10)
    p.drawLine(QPointF(size * 0.42, size / 2), QPointF(size * 0.58, size / 2))


@_drawn(size=ICON_SM, ink=WARNING, cap=True, join=True)
def broken_link_icon(p: QPainter, size: int, c: QColor) -> None:
    """The same two links, pulled apart. A warning rather than an error: the
    footage is somewhere, and everything already made from it still stands."""
    _chain_links(p, size, size * 0.34)


@_drawn(size=ICON_SM, join=True)
def folder_icon(p: QPainter, size: int, c: QColor) -> None:
    """A tabbed folder: where the file itself sits, rather than where the app files it."""
    w = size
    body = QPainterPath(QPointF(0.12 * w, 0.28 * w))
    body.lineTo(QPointF(0.44 * w, 0.28 * w))
    body.lineTo(QPointF(0.54 * w, 0.42 * w))
    body.lineTo(QPointF(0.88 * w, 0.42 * w))
    body.lineTo(QPointF(0.88 * w, 0.80 * w))
    body.lineTo(QPointF(0.12 * w, 0.80 * w))
    body.closeSubpath()
    p.drawPath(body)


@_drawn(size=ICON_SM, cap=True, join=True)
def transects_icon(p: QPainter, size: int, c: QColor) -> None:
    """A tape between two marked ends: the transect as the map draws it."""
    a = QPointF(size * 0.22, size * 0.74)
    b = QPointF(size * 0.78, size * 0.26)
    p.drawLine(a, b)
    r = size * 0.13
    p.drawEllipse(a, r, r)
    p.drawEllipse(b, r, r)


@_drawn(size=ICON_SM, cap=True, join=True)
def process_icon(p: QPainter, size: int, c: QColor) -> None:
    """A queue of passes with a play head: rows waiting, worked top to bottom."""
    left = size * 0.18
    for row, width in enumerate((0.50, 0.38, 0.26)):
        y = size * (0.28 + 0.22 * row)
        p.drawLine(QPointF(left, y), QPointF(left + size * width, y))
    tip = size * 0.86
    back = size * 0.62
    p.drawPolyline(
        [
            QPointF(back, size * 0.60),
            QPointF(tip, size * 0.76),
            QPointF(back, size * 0.92),
        ]
    )


@_drawn(size=ICON_SM, join=True)
def browse_icon(p: QPainter, size: int, c: QColor) -> None:
    """Stacked bins: the archive everything processed so far files itself into."""
    m = size * 0.16
    p.drawRect(QRectF(m, m, size - 2 * m, size * 0.26))
    p.drawRect(QRectF(m, size * 0.50, size - 2 * m, size * 0.34))


@_drawn(size=ICON_SM, cap=True, join=True)
def cart_icon(p: QPainter, size: int, c: QColor) -> None:
    """A shopping trolley: handle, tilted basket, two wheels."""
    w = size
    p.drawLine(QPointF(0.10 * w, 0.20 * w), QPointF(0.24 * w, 0.20 * w))
    basket = QPainterPath(QPointF(0.24 * w, 0.20 * w))
    basket.lineTo(QPointF(0.34 * w, 0.60 * w))
    basket.lineTo(QPointF(0.76 * w, 0.60 * w))
    basket.lineTo(QPointF(0.86 * w, 0.32 * w))
    basket.lineTo(QPointF(0.27 * w, 0.32 * w))
    p.drawPath(basket)
    p.setBrush(c)
    r = 0.07 * w
    p.drawEllipse(QPointF(0.40 * w, 0.78 * w), r, r)
    p.drawEllipse(QPointF(0.70 * w, 0.78 * w), r, r)


@_drawn(size=ICON_SM, join=True)
def videos_icon(p: QPainter, size: int, c: QColor) -> None:
    """A film frame: the footage itself, before anything has been cut from it."""
    w = size
    p.drawRect(QRectF(0.10 * w, 0.20 * w, 0.80 * w, 0.60 * w))
    p.setBrush(c)
    hole = 0.10 * w
    for y in (0.31 * w, 0.59 * w):
        p.drawRect(QRectF(0.16 * w, y, hole, hole))
        p.drawRect(QRectF(0.74 * w, y, hole, hole))


# Glyphs for the header's alert box. Only the two states worth acting on get
# one: a badge that is always lit is a badge nobody reads.
#
# The vocabulary belongs to simple/section_state.py, which is Qt-free and so
# cannot be imported from core. tests/simple/test_section_state.py holds the two
# lists together.
STEP_STATES = ("todo", "ok", "attention", "blocked")

_STATE_GLYPHS = {"attention": warning_icon, "blocked": blocked_icon}


def status_dot_icon(colour: str, size: int = ICON_SM) -> QIcon:
    """A filled dot in an outcome's colour, for a row whose status is one word
    inside a longer line and has no room for a chip."""
    pm, p = _px(size)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(colour))
    r = size * 0.22
    p.drawEllipse(QPointF(size / 2, size / 2), r, r)
    p.end()
    return QIcon(pm)


def section_state_icon(state: str, size: int = ICON_SM) -> QIcon | None:
    """The glyph for a destination's verdict, or None when there is nothing to say."""
    if state not in STEP_STATES:
        raise ValueError(f"Unknown section state: {state!r}")
    draw = _STATE_GLYPHS.get(state)
    return draw(size) if draw is not None else None


@_drawn(size=ICON_SM, ink=WARNING, cap=True)
def lock_icon(p: QPainter, size: int, c: QColor) -> None:
    cx = size / 2
    bw = size * 0.44
    bh = size * 0.34
    by = size * 0.52
    p.drawRect(QRectF(cx - bw / 2, by, bw, bh))
    ar = size * 0.22
    p.drawArc(QRectF(cx - ar, by - ar * 1.6, ar * 2, ar * 2), 0, 180 * 16)
