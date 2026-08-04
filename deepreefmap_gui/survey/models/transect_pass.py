"""One traversal of a transect: a time-trimmed segment of one or more videos."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from deepreefmap_gui.survey.models.common import utc_now_iso

PASS_DIRECTIONS = ("forward", "reverse")


@dataclass(slots=True)
class TransectPass:
    """One swim, which one video may hold several of, or several videos hold one of.

    A GoPro splits a recording at about 4 GB, so a long swim arrives as
    chapters. ``video_id`` is the first of them and stays the pass's video
    identity for everything that groups by clip; ``extra_video_ids`` holds the
    rest in playing order. ``begin_s`` and ``end_s`` are offsets into the
    chapters played back to back, which is how the pipeline reads a list of
    videos.

    ``transect_id`` may be None. Footage worth processing is not always footage
    laid against a tape: a spot check, a clip from a colleague, a swim nobody
    planned. Such a pass carries no tape length, so its run is left unscaled --
    which is already what a planned transect with no tape reading does.
    """

    transect_id: uuid.UUID | None
    video_id: uuid.UUID
    begin_s: float
    end_s: float
    direction: str = "forward"
    batch_id: uuid.UUID | None = None
    # Held back from processing: the pass stays in the batch and in the table,
    # and every run of the batch skips it until it is released.
    held: bool = False
    notes: str = ""
    extra_video_ids: list[uuid.UUID] = field(default_factory=list)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if self.direction not in PASS_DIRECTIONS:
            raise ValueError(f"direction must be one of {PASS_DIRECTIONS}, got {self.direction!r}")
        if self.begin_s < 0:
            raise ValueError(f"begin_s must be >= 0, got {self.begin_s}")
        if self.end_s <= self.begin_s:
            raise ValueError(f"end_s must be greater than begin_s ({self.begin_s})")
        if self.video_id in self.extra_video_ids:
            raise ValueError("extra_video_ids must not repeat video_id")

    def video_ids(self) -> list[uuid.UUID]:
        """Every chapter of the pass, in playing order."""
        return [self.video_id, *self.extra_video_ids]

    def duration_s(self) -> float:
        return self.end_s - self.begin_s
