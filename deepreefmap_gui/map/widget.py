"""A minimal Leaflet-style map widget on QPainter and cached raster tiles."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QPen, QWheelEvent
from PySide6.QtWidgets import QToolTip, QWidget

from deepreefmap_gui.core.theme import BORDER, PREVIEW_BG
from deepreefmap_gui.map.overlays import (
    ENDPOINT_HIT_PX,
    LINE_HIT_PX,
    OverlayTransect,
    segment_distance_px,
)
from deepreefmap_gui.map.tile_cache import TileCache, shared_tile_cache
from deepreefmap_gui.map.tile_math import TILE_SIZE, clamp_zoom, deg2tile, fit_zoom, tile2deg
from deepreefmap_gui.survey.models.transect import (
    compass_point,
    haversine_m,
    initial_bearing_deg,
)


def _segment_intersects_rect(a: QPointF, b: QPointF, rect: QRectF) -> bool:
    """Cohen-Sutherland: does the segment a-b touch ``rect`` at all?

    Endpoint containment is not enough — a transect longer than the viewport
    crosses it with both ends off screen.
    """

    def code(point: QPointF) -> int:
        out = 0
        out |= 1 if point.x() < rect.left() else 0
        out |= 2 if point.x() > rect.right() else 0
        out |= 4 if point.y() < rect.top() else 0
        out |= 8 if point.y() > rect.bottom() else 0
        return out

    x1, y1, x2, y2 = a.x(), a.y(), b.x(), b.y()
    code1, code2 = code(a), code(b)
    while True:
        if not (code1 | code2):
            return True
        if code1 & code2:
            return False
        outside = code1 or code2
        if outside & 8:
            x = x1 + (x2 - x1) * (rect.bottom() - y1) / (y2 - y1)
            y = rect.bottom()
        elif outside & 4:
            x = x1 + (x2 - x1) * (rect.top() - y1) / (y2 - y1)
            y = rect.top()
        elif outside & 2:
            y = y1 + (y2 - y1) * (rect.right() - x1) / (x2 - x1)
            x = rect.right()
        else:
            y = y1 + (y2 - y1) * (rect.left() - x1) / (x2 - x1)
            x = rect.left()
        if outside == code1:
            x1, y1 = x, y
            code1 = code(QPointF(x1, y1))
        else:
            x2, y2 = x, y
            code2 = code(QPointF(x2, y2))


# Zoom is continuous, so a notch nudges the view instead of doubling it. A wheel
# that reports free-scrolling deltas sends many small events per gesture; one
# that clicks sends 120 units a notch, and roughly three of those cross a tile
# level. The per-event cap keeps a flung trackpad from crossing the whole range.
_ZOOM_PER_NOTCH = 0.34
_MAX_NOTCHES_PER_EVENT = 3.0


class SlippyMapWidget(QWidget):
    """Pan/zoom raster map with transect line overlays.

    Works offline from the persistent tile cache; tiles never seen online are
    drawn as an empty grid.
    """

    map_clicked = Signal(float, float)
    transect_clicked = Signal(str)
    transect_endpoint_moved = Signal(str, str, float, float)
    # Pan, zoom and resize all change which transects are on screen, which the
    # plan list mirrors in its "In view" section.
    view_changed = Signal()

    def __init__(self, cache: TileCache | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cache = cache if cache is not None else shared_tile_cache()
        self._cache.tile_ready.connect(lambda *_: self.update())
        # Losing (or regaining) the connection repaints, so the offline banner
        # appears without waiting for the next pan or zoom.
        self._cache.offline_changed.connect(lambda *_: self.update())
        self._center = (0.0, 0.0)
        self._zoom = 2.0
        self._transects: list[OverlayTransect] = []
        self._editable_id: str | None = None
        self._press_pos: QPointF | None = None
        self._press_center_tile: tuple[float, float] | None = None
        self._dragging_endpoint: tuple[OverlayTransect, str] | None = None
        self._moved = False
        self._pick_mode = False
        self._hovered_id: str | None = None
        self._pending_start: tuple[float, float] | None = None
        self._cursor_latlon: tuple[float, float] | None = None
        # Tracking without a button held is what makes hover tooltips possible.
        self.setMouseTracking(True)
        self.setMinimumHeight(240)

    # --- view state ---

    def set_view(self, lat: float, lon: float, zoom: float) -> None:
        self._center = (lat, lon)
        self._zoom = clamp_zoom(zoom)
        self.update()
        self.view_changed.emit()

    def _tile_zoom(self) -> int:
        """Integer level whose tiles are fetched for the current zoom."""
        return int(clamp_zoom(math.floor(self._zoom)))

    def _tile_px(self) -> float:
        """On-screen size of one tile: TILE_SIZE at a whole level, up to twice
        that just below the next one."""
        return TILE_SIZE * 2.0 ** (self._zoom - self._tile_zoom())

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self.view_changed.emit()

    def set_transects(self, transects: list[OverlayTransect]) -> None:
        self._transects = list(transects)
        self.update()

    def set_editable(self, transect_id: str | None) -> None:
        self._editable_id = transect_id
        self.update()

    def set_pick_mode(self, picking: bool) -> None:
        """Arm the map for coordinate picking: crosshair cursor, and a click on
        an existing transect sets a point rather than selecting that transect."""
        self._pick_mode = picking
        if not picking:
            self._pending_start = None
        self.setCursor(
            Qt.CursorShape.CrossCursor if picking else Qt.CursorShape.ArrowCursor
        )
        self.update()

    def set_pending_start(self, latlon: tuple[float, float] | None) -> None:
        """Anchor for the rubber band drawn to the cursor while a line is being
        placed, so the second click is aimed at a length and heading rather than
        at empty water."""
        self._pending_start = latlon
        self.update()

    def fit_transects(self) -> None:
        points = [p for t in self._transects for p in (t.start, t.end)]
        self.focus_on(points)

    def focus_on(self, points: list[tuple[float, float]], fill: float = 1.0) -> None:
        """Centre the view on ``points``, filling ``fill`` of the shorter side.

        A transect asked for by name should land with room around it, so the
        reef either side of it stays readable; ``fill`` below 1 reserves that
        margin.
        """
        if not points:
            return
        width = self.width() or 400
        height = self.height() or 240
        padding = int(min(width, height) * max(0.0, 1.0 - fill) / 2)
        zoom = fit_zoom(points, width, height, padding_px=max(20, padding))
        lat = sum(p[0] for p in points) / len(points)
        lon = sum(p[1] for p in points) / len(points)
        self.set_view(lat, lon, zoom)

    def transect_count(self) -> int:
        """How many overlay transects the map is holding.

        Zero means "in view" has nothing to say: a caller filtering by the
        viewport has to stand aside rather than hide everything.
        """
        return len(self._transects)

    def visible_transect_ids(self) -> list[str]:
        """Ids of the overlay transects whose line crosses the viewport."""
        viewport = QRectF(0, 0, self.width(), self.height())
        return [
            transect.id
            for transect in self._transects
            if _segment_intersects_rect(
                self._px_of(*transect.start), self._px_of(*transect.end), viewport
            )
        ]

    def is_offline(self) -> bool:
        return self._cache.offline

    def latlon_at(self, pos: QPointF) -> tuple[float, float]:
        zoom, size = self._tile_zoom(), self._tile_px()
        cx, cy = deg2tile(*self._center, zoom)
        tx = cx + (pos.x() - self.width() / 2) / size
        ty = cy + (pos.y() - self.height() / 2) / size
        return tile2deg(tx, ty, zoom)

    def _px_of(self, lat: float, lon: float) -> QPointF:
        zoom, size = self._tile_zoom(), self._tile_px()
        cx, cy = deg2tile(*self._center, zoom)
        tx, ty = deg2tile(lat, lon, zoom)
        return QPointF(
            self.width() / 2 + (tx - cx) * size,
            self.height() / 2 + (ty - cy) * size,
        )

    # --- painting ---

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(PREVIEW_BG))
        self._paint_tiles(painter)
        self._paint_transects(painter)
        self._paint_rubber_band(painter)
        self._paint_attribution(painter)
        # Offline: the saved tiles above are all that will draw, so say so rather
        # than leave the empty grid looking like a broken map.
        if self._cache.offline:
            self._paint_offline_banner(painter)

    def _tile_bounds(self) -> tuple[float, float, int, int, int, int]:
        """Centre tile and the inclusive tile-index box that covers the viewport."""
        size = self._tile_px()
        cx, cy = deg2tile(*self._center, self._tile_zoom())
        half_w = self.width() / 2
        half_h = self.height() / 2
        return (
            cx,
            cy,
            math.floor(cx - half_w / size),
            math.floor(cx + half_w / size),
            math.floor(cy - half_h / size),
            math.floor(cy + half_h / size),
        )

    def _paint_tiles(self, painter: QPainter) -> None:
        cx, cy, first_x, last_x, first_y, last_y = self._tile_bounds()
        zoom, size = self._tile_zoom(), self._tile_px()
        half_w = self.width() / 2
        half_h = self.height() / 2
        grid_pen = QPen(QColor(BORDER))
        # Between whole levels the tiles are drawn larger than their pixels, so
        # they need resampling; the extra pixel on each side covers the hairline
        # seams float placement would otherwise leave between neighbours.
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        for tx in range(first_x, last_x + 1):
            for ty in range(first_y, last_y + 1):
                left = half_w + (tx - cx) * size
                top = half_h + (ty - cy) * size
                target = QRectF(left, top, size + 1, size + 1)
                pixmap = self._cache.pixmap(zoom, tx, ty)
                if pixmap is not None:
                    painter.drawPixmap(target, pixmap, QRectF(pixmap.rect()))
                else:
                    painter.setPen(grid_pen)
                    painter.drawRect(QRectF(left, top, size, size))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)

    def _paint_transects(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for transect in self._transects:
            p1 = self._px_of(*transect.start)
            p2 = self._px_of(*transect.end)
            width = 4 if transect.selected else 3
            painter.setPen(QPen(transect.color, width))
            painter.drawLine(p1, p2)
            editable = transect.id == self._editable_id
            radius = 6.0 if editable else 4.0
            painter.setBrush(transect.color)
            painter.setPen(QPen(QColor(255, 255, 255), 1.5 if editable else 0.5))
            painter.drawEllipse(p1, radius, radius)
            painter.drawEllipse(p2, radius, radius)
            if transect.label:
                self._paint_label(painter, transect, p1, p2)

    def _paint_label(
        self, painter: QPainter, transect: OverlayTransect, p1: QPointF, p2: QPointF
    ) -> None:
        """Name plate at the midpoint, on a dark plate so it reads over any tile."""
        metrics = painter.fontMetrics()
        pad = 3
        width = metrics.horizontalAdvance(transect.label)
        mid_x = (p1.x() + p2.x()) / 2 - width / 2
        mid_y = (p1.y() + p2.y()) / 2 - metrics.height() - 6
        plate = QRectF(mid_x - pad, mid_y - pad, width + 2 * pad, metrics.height() + 2 * pad)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 150))
        painter.drawRoundedRect(plate, 3, 3)
        painter.setPen(QPen(QColor(255, 255, 255)))
        painter.drawText(QPointF(mid_x, mid_y + metrics.ascent()), transect.label)

    def _paint_rubber_band(self, painter: QPainter) -> None:
        """Dashed line from the placed start point to the cursor, captioned with
        the length and heading the second click would commit to."""
        if self._pending_start is None or self._cursor_latlon is None:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        p1 = self._px_of(*self._pending_start)
        p2 = self._px_of(*self._cursor_latlon)
        pen = QPen(QColor(255, 255, 255, 200), 2, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(p1, p2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255))
        painter.drawEllipse(p1, 4.0, 4.0)
        length = haversine_m(*self._pending_start, *self._cursor_latlon)
        bearing = initial_bearing_deg(*self._pending_start, *self._cursor_latlon)
        self._paint_plate(
            painter,
            f"{length:.0f} m  ·  {bearing:03.0f}° {compass_point(bearing)}",
            QPointF(p2.x() + 12, p2.y() - 12),
        )

    def _paint_plate(self, painter: QPainter, text: str, at: QPointF) -> None:
        """Dark caption plate with its top-left at ``at``."""
        metrics = painter.fontMetrics()
        pad = 3
        width = metrics.horizontalAdvance(text)
        plate = QRectF(at.x() - pad, at.y() - pad, width + 2 * pad, metrics.height() + 2 * pad)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 170))
        painter.drawRoundedRect(plate, 3, 3)
        painter.setPen(QPen(QColor(255, 255, 255)))
        painter.drawText(QPointF(at.x(), at.y() + metrics.ascent()), text)

    def _paint_attribution(self, painter: QPainter) -> None:
        text = self._cache.layer.attribution
        metrics = painter.fontMetrics()
        pad = 4
        rect = QRectF(
            self.width() - metrics.horizontalAdvance(text) - 2 * pad - 2,
            self.height() - metrics.height() - 2 * pad,
            metrics.horizontalAdvance(text) + 2 * pad,
            metrics.height() + pad,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 110))
        painter.drawRect(rect)
        painter.setPen(QColor(235, 235, 235))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_offline_banner(self, painter: QPainter) -> None:
        """A top-centre pill naming the offline state, over whatever tiles exist."""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        text = "Offline: showing saved map"
        metrics = painter.fontMetrics()
        pad = 6
        width = metrics.horizontalAdvance(text) + 2 * pad
        height = metrics.height() + pad
        rect = QRectF((self.width() - width) / 2, 8, width, height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 160))
        painter.drawRoundedRect(rect, 4, 4)
        painter.setPen(QColor(235, 235, 235))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    # --- interaction ---

    def _endpoint_at(self, pos: QPointF) -> tuple[OverlayTransect, str] | None:
        for transect in self._transects:
            if transect.id != self._editable_id:
                continue
            for which, latlon in (("start", transect.start), ("end", transect.end)):
                point = self._px_of(*latlon)
                if math.hypot(pos.x() - point.x(), pos.y() - point.y()) <= ENDPOINT_HIT_PX:
                    return transect, which
        return None

    def _transect_at(self, pos: QPointF) -> OverlayTransect | None:
        for transect in self._transects:
            p1 = self._px_of(*transect.start)
            p2 = self._px_of(*transect.end)
            if segment_distance_px(pos, p1, p2) <= LINE_HIT_PX:
                return transect
        return None

    def _update_hover(self, pos: QPointF) -> None:
        """Tooltip and cursor for the transect under the pointer, so a map full
        of lines can be read without clicking each one."""
        if self._pick_mode:
            return
        hit = self._transect_at(pos)
        if hit is None:
            QToolTip.hideText()
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self._hovered_id = None
            return
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if hit.id != self._hovered_id:
            self._hovered_id = hit.id
        if hit.tooltip:
            QToolTip.showText(self.mapToGlobal(pos.toPoint()), hit.tooltip, self)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().leaveEvent(event)
        self._hovered_id = None
        self._cursor_latlon = None
        QToolTip.hideText()
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._press_pos = event.position()
        self._press_center_tile = deg2tile(*self._center, self._tile_zoom())
        self._dragging_endpoint = self._endpoint_at(event.position())
        self._moved = False

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._pending_start is not None:
            self._cursor_latlon = self.latlon_at(event.position())
            self.update()
        if self._press_pos is None:
            self._update_hover(event.position())
            return
        delta = event.position() - self._press_pos
        if not self._moved and math.hypot(delta.x(), delta.y()) < 3:
            return
        self._moved = True
        if self._dragging_endpoint is not None:
            transect, which = self._dragging_endpoint
            latlon = self.latlon_at(event.position())
            if which == "start":
                transect.start = latlon
            else:
                transect.end = latlon
            self.update()
            return
        assert self._press_center_tile is not None
        size = self._tile_px()
        cx = self._press_center_tile[0] - delta.x() / size
        cy = self._press_center_tile[1] - delta.y() / size
        self._center = tile2deg(cx, cy, self._tile_zoom())
        self.update()
        self.view_changed.emit()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._press_pos is None:
            return
        if self._dragging_endpoint is not None and self._moved:
            transect, which = self._dragging_endpoint
            latlon = transect.start if which == "start" else transect.end
            self.transect_endpoint_moved.emit(transect.id, which, latlon[0], latlon[1])
        elif not self._moved:
            hit = None if self._pick_mode else self._transect_at(event.position())
            if hit is not None:
                self.transect_clicked.emit(hit.id)
            else:
                lat, lon = self.latlon_at(event.position())
                self.map_clicked.emit(lat, lon)
        self._press_pos = None
        self._press_center_tile = None
        self._dragging_endpoint = None
        self._moved = False

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.accept()
        notches = event.angleDelta().y() / 120.0
        notches = max(-_MAX_NOTCHES_PER_EVENT, min(_MAX_NOTCHES_PER_EVENT, notches))
        new_zoom = clamp_zoom(self._zoom + notches * _ZOOM_PER_NOTCH)
        if new_zoom == self._zoom:
            return
        anchor = event.position()
        lat, lon = self.latlon_at(anchor)
        self._zoom = new_zoom
        # Keep the point under the cursor fixed while zooming.
        size = self._tile_px()
        tx, ty = deg2tile(lat, lon, self._tile_zoom())
        cx = tx - (anchor.x() - self.width() / 2) / size
        cy = ty - (anchor.y() - self.height() / 2) / size
        self._center = tile2deg(cx, cy, self._tile_zoom())
        self.update()
        self.view_changed.emit()
