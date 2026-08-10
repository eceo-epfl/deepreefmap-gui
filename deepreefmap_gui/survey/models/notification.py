"""One thing that wanted attention, and what became of it.

Two kinds of thing land in this table, and they keep different books. A
*condition* is derived from live state and resolves itself, so it gets one row
per episode: inserted when it becomes true, updated in place while it stays
true, stamped ``resolved_at`` when it stops. An *event* happened at a moment
and cannot un-happen, so it gets one row per occurrence.

``fingerprint`` is what joins an episode to itself across refreshes, and what a
reader silences when they never want to hear this kind of thing again. It names
the fault, never the count and never the sentence: ``videos.missing_clips``, so
that nine clips missing is the same episode as ten, and rewording the sentence
in a later release does not read as a new problem.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from deepreefmap_gui.survey.models.common import utc_now_iso

# How loudly it asks. A blocker stops work, a warning does not.
INFO = "info"
WARNING = "warning"
BLOCKER = "blocker"

NOTIFICATION_SEVERITIES = (INFO, WARNING, BLOCKER)

CONDITION = "condition"
EVENT = "event"

NOTIFICATION_KINDS = (CONDITION, EVENT)

# What the message is about. A survey fact travels with the survey; a machine
# fact is about the laptop in front of you and is left out of any export.
SURVEY = "survey"
MACHINE = "machine"

NOTIFICATION_SCOPES = (SURVEY, MACHINE)


@dataclass(slots=True)
class Notification:
    fingerprint: str
    kind: str
    severity: str
    scope: str
    title: str
    body: str = ""
    # The destination to open when the row is clicked, "" when there is nowhere
    # to go.
    section: str = ""
    # How many things the message counts, 0 when it counts nothing. Kept beside
    # the sentence so a log can read the number without parsing the words.
    subject_count: int = 0
    updated_at: str = field(default_factory=utc_now_iso)
    resolved_at: str | None = None
    read_at: str | None = None
    dismissed_at: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    # First seen. A condition keeps this across every update, so the history can
    # say how long an episode lasted.
    created_at: str = field(default_factory=utc_now_iso)
