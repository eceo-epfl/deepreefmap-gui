"""What a live message looks like before anything has been recorded about it."""

from __future__ import annotations

from dataclasses import dataclass

from deepreefmap_gui.survey.models.notification import BLOCKER, INFO, WARNING

# Which of two messages goes first, and which of two is the escalation. A
# blocker stops work, so it outranks anything that merely wants doing.
SEVERITY_RANK = {BLOCKER: 2, WARNING: 1, INFO: 0}


@dataclass(frozen=True)
class Condition:
    """One thing that is true right now and worth saying.

    The live twin of a stored ``Notification``: no ids, no timestamps, nothing
    about whether anybody has read it. The centre folds a set of these into the
    log and works the difference out for itself.
    """

    fingerprint: str
    severity: str
    scope: str
    title: str
    body: str = ""
    section: str = ""
    subject_count: int = 0


def can_mute(severity: str) -> bool:
    """A blocker is never silenced.

    Start processing is already disabled when one is up, and hiding the only
    sentence that says why leaves a dead button and no way to find out.
    """
    return severity != BLOCKER
