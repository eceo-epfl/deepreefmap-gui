"""Membership of a pass in a session's worklist.

A pass can be queued in several sessions over time (a rerun is the same pass
ordered again), so membership is a row of its own rather than a column on the
pass. The ``(batch_id, pass_id)`` pair is unique: a cart holds a pass once.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from deepreefmap_gui.survey.models.common import utc_now_iso


@dataclass(slots=True)
class BatchItem:
    batch_id: uuid.UUID
    pass_id: uuid.UUID
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: str = field(default_factory=utc_now_iso)
