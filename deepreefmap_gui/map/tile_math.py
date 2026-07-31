"""Web Mercator tile arithmetic. Pure functions, no Qt."""

from __future__ import annotations

import math

TILE_SIZE = 256
MAX_LATITUDE = 85.0511
MIN_ZOOM = 1
MAX_ZOOM = 19


def normalise_longitude(lon: float) -> float:
    """Wrap a longitude into [-180, 180).

    Panning is unbounded in tile space, so a view dragged past the antimeridian
    produces tile x outside [0, 2**zoom) and, through tile2deg, a longitude like
    214.3. That is a valid position on the map and an invalid coordinate to
    store: it leaves the survey database holding transect endpoints no other
    tool will place correctly.
    """
    return (lon + 180.0) % 360.0 - 180.0


def deg2tile(lat: float, lon: float, zoom: float) -> tuple[float, float]:
    """Fractional tile coordinates of a WGS84 point."""
    lat = max(-MAX_LATITUDE, min(MAX_LATITUDE, lat))
    lon = normalise_longitude(lon)
    n = 2.0**zoom
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def tile2deg(x: float, y: float, zoom: float) -> tuple[float, float]:
    """WGS84 point of fractional tile coordinates."""
    n = 2.0**zoom
    lon = normalise_longitude(x / n * 360.0 - 180.0)
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lat, lon


def clamp_zoom(zoom: float) -> float:
    """Hold a zoom inside the layer's range.

    Zoom is continuous — the map draws between tile levels — so this takes and
    returns a float; the integer level whose tiles are fetched is derived from it.
    """
    return max(float(MIN_ZOOM), min(float(MAX_ZOOM), zoom))


def fit_zoom(
    points: list[tuple[float, float]], width_px: int, height_px: int, padding_px: int = 40
) -> int:
    """Largest zoom at which all points fit inside the given pixel viewport."""
    if not points or width_px <= 0 or height_px <= 0:
        return MIN_ZOOM
    usable_w = max(1, width_px - 2 * padding_px)
    usable_h = max(1, height_px - 2 * padding_px)
    for zoom in range(MAX_ZOOM, MIN_ZOOM - 1, -1):
        xs, ys = zip(*(deg2tile(lat, lon, zoom) for lat, lon in points), strict=True)
        span_w = (max(xs) - min(xs)) * TILE_SIZE
        span_h = (max(ys) - min(ys)) * TILE_SIZE
        if span_w <= usable_w and span_h <= usable_h:
            return zoom
    return MIN_ZOOM
