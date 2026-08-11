"""One execution of a pass through the reconstruction pipeline."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from deepreefmap_gui.survey.models.common import utc_now_iso
from deepreefmap_gui.survey.statuses import RUN_STATUSES, TERMINAL_STATUSES

__all__ = ["RUN_STATUSES", "TERMINAL_STATUSES", "RunRecord"]


@dataclass(slots=True)
class RunRecord:
    """Re-running a pass creates a new record; repeats are the reproducibility data.

    ``batch_id`` is the session the attempt ran in; a rerun of the same pass can
    belong to a later session than the pass itself.
    ``run_dir_name`` is relative to the output root so a moved folder keeps working.
    Cover numbers stay in the run directory's benthic_cover.json, never in the database.
    """

    pass_id: uuid.UUID
    run_dir_name: str
    status: str = "pending"
    started_at: str | None = None
    finished_at: str | None = None
    error: str = ""
    batch_id: uuid.UUID | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if self.status not in RUN_STATUSES:
            raise ValueError(f"status must be one of {RUN_STATUSES}, got {self.status!r}")
        if not self.run_dir_name.strip():
            raise ValueError("run_dir_name must not be empty")
