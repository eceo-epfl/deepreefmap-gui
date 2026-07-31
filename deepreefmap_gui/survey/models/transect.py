"""A physical reef transect: a named two-point lat/long line."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field

from deepreefmap_gui.survey.models.common import utc_now_iso

_EARTH_RADIUS_M = 6_371_000.0


_COMPASS_POINTS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Forward azimuth from the first point to the second, in [0, 360)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    return math.degrees(math.atan2(y, x)) % 360.0


def compass_point(bearing_deg: float) -> str:
    """Sixteen-point compass abbreviation for a bearing."""
    return _COMPASS_POINTS[int((bearing_deg % 360.0) / 22.5 + 0.5) % 16]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two WGS84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


@dataclass(slots=True)
class Transect:
    """User-defined survey line; ``length_m`` is the tape length used for scaling."""

    name: str
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    length_m: float | None = None
    depth_m: float | None = None
    description: str = ""
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Transect name must not be empty")
        for lat in (self.start_lat, self.end_lat):
            if not -90.0 <= lat <= 90.0:
                raise ValueError(f"Latitude out of range: {lat}")
        for lon in (self.start_lon, self.end_lon):
            if not -180.0 <= lon <= 180.0:
                raise ValueError(f"Longitude out of range: {lon}")
        for value, label in ((self.length_m, "length_m"), (self.depth_m, "depth_m")):
            if value is not None and value < 0:
                raise ValueError(f"{label} must be >= 0")

    def geodesic_length_m(self) -> float:
        """Great-circle length of the line, shown beside the tape length as a QC hint."""
        return haversine_m(self.start_lat, self.start_lon, self.end_lat, self.end_lon)

    def bearing_deg(self) -> float:
        """Heading a diver swims to go from the start marker to the end marker."""
        return initial_bearing_deg(self.start_lat, self.start_lon, self.end_lat, self.end_lon)
