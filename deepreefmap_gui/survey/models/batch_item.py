"""Membership of a pass in a session's worklist.

A pass can be queued in several sessions over time (a rerun is the same pass
ordered again), so membership is a row of its own rather than a column on the
pass. The ``(batch_id, pass_id)`` pair is unique: a cart holds a pass once.

Order and settings belong here rather than on the pass for the same reason: they
describe a plan to process the pass, which the next session may make differently.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from deepreefmap_gui.survey.models.common import utc_now_iso


@dataclass(slots=True)
class BatchItem:
    batch_id: uuid.UUID
    pass_id: uuid.UUID
    # Where this row sits in the processing order, which the cart's rows are
    # dragged into. Ties fall back to insertion order.
    position: int = 0
    # Run settings this pass alone departs from the session on, as whole values
    # keyed by preset name. Only the keys that differ are stored, so a session
    # setting nobody overrode still reaches the pass when it changes.
    overrides: dict[str, Any] = field(default_factory=dict)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: str = field(default_factory=utc_now_iso)
