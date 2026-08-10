"""Everything that wants attention, and the record of everything that did.

Conditions are reconciled, not appended. The caller hands over the whole set of
what is true now and the difference against what is already open becomes the
inserts, the in-place updates and the resolutions. This matters because the
caller is on the typing path: the header recomputes its verdicts on nearly every
keystroke, and a log that grew a row each time would be useless within a minute.

Events are appended, because a session that finished does not un-finish.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from typing import Protocol

from deepreefmap_gui.notify.log import MemoryLog, NotificationLog
from deepreefmap_gui.notify.model import SEVERITY_RANK, Condition, can_mute
from deepreefmap_gui.survey.models.common import utc_now_iso
from deepreefmap_gui.survey.models.notification import (
    CONDITION,
    EVENT,
    INFO,
    SURVEY,
    Notification,
)


class MuteBook(Protocol):
    """Which messages this reader never wants to see again."""

    def muted(self) -> set[str]: ...

    def mute(self, fingerprint: str) -> None: ...

    def unmute(self, fingerprint: str) -> None: ...


class SetMuteBook:
    """A mute book that forgets when the app closes. Tests and headless use."""

    def __init__(self, muted: set[str] | None = None) -> None:
        self._muted = set(muted or ())

    def muted(self) -> set[str]:
        return set(self._muted)

    def mute(self, fingerprint: str) -> None:
        self._muted.add(fingerprint)

    def unmute(self, fingerprint: str) -> None:
        self._muted.discard(fingerprint)


class NotificationCenter:
    def __init__(
        self,
        log: NotificationLog | None = None,
        mutes: MuteBook | None = None,
        now: Callable[[], str] = utc_now_iso,
    ) -> None:
        self._log: NotificationLog = log or MemoryLog()
        self._mutes: MuteBook = mutes or SetMuteBook()
        self._now = now
        self._open: dict[str, Notification] = {}
        self._events: list[Notification] = []
        self._last_signature: tuple | None = None
        self._adopt()

    # --- Conditions ---

    def reconcile(self, conditions: Sequence[Condition], *, authoritative: bool = True) -> bool:
        """Fold the live set into the log. True when what the user sees changed.

        ``authoritative=False`` inserts and updates but never resolves. The first
        paint happens before the video library and the run archive have been
        read, and taking that empty set at its word would close every episode a
        moment before reopening it as a duplicate.
        """
        signature = tuple(
            sorted(
                (c.fingerprint, c.severity, c.subject_count, c.title, c.body)
                for c in conditions
            )
        )
        if signature == self._last_signature:
            return False
        self._last_signature = signature

        before = self._visible_signature()
        wanted = {c.fingerprint: c for c in conditions}
        for fingerprint, condition in wanted.items():
            row = self._open.get(fingerprint)
            if row is None:
                self._appear(condition)
            else:
                self._persist(row, condition)
        if authoritative:
            for fingerprint in [f for f in self._open if f not in wanted]:
                self._disappear(fingerprint)
        return self._visible_signature() != before

    def _appear(self, condition: Condition) -> None:
        stamp = self._now()
        row = Notification(
            fingerprint=condition.fingerprint,
            kind=CONDITION,
            severity=condition.severity,
            scope=condition.scope,
            title=condition.title,
            body=condition.body,
            section=condition.section,
            subject_count=condition.subject_count,
            created_at=stamp,
            updated_at=stamp,
        )
        self._open[condition.fingerprint] = row
        self._log.insert(row)

    def _persist(self, row: Notification, condition: Condition) -> None:
        """The same fault, said again, possibly with a different number in it.

        A count that changes must not re-badge the bell: relocating ten clips one
        at a time would otherwise interrupt ten times for a problem that is
        getting smaller. Only a rise in severity is new information, and that
        also brings back a message the reader had put away.
        """
        if (
            row.severity == condition.severity
            and row.title == condition.title
            and row.body == condition.body
            and row.subject_count == condition.subject_count
        ):
            return
        escalated = SEVERITY_RANK[condition.severity] > SEVERITY_RANK[row.severity]
        row.severity = condition.severity
        row.title = condition.title
        row.body = condition.body
        row.subject_count = condition.subject_count
        row.section = condition.section
        row.updated_at = self._now()
        if escalated:
            row.read_at = None
            row.dismissed_at = None
        self._log.update(row)

    def _disappear(self, fingerprint: str) -> None:
        row = self._open.pop(fingerprint)
        row.resolved_at = self._now()
        row.updated_at = row.resolved_at
        self._log.resolve(row.id, row.resolved_at)

    # --- Events ---

    def post(
        self,
        *,
        fingerprint: str,
        title: str,
        body: str = "",
        severity: str = INFO,
        section: str = "",
        scope: str = SURVEY,
    ) -> Notification:
        """A one-shot.

        Never deduplicated, because two sessions that finished are two things
        that happened. Still fingerprinted, so a reader can silence a whole class
        of event as easily as a class of condition.
        """
        stamp = self._now()
        row = Notification(
            fingerprint=fingerprint,
            kind=EVENT,
            severity=severity,
            scope=scope,
            title=title,
            body=body,
            section=section,
            created_at=stamp,
            updated_at=stamp,
        )
        self._events.append(row)
        self._log.insert(row)
        return row

    # --- Reading ---

    def active(self) -> list[Notification]:
        """What the popover shows: loudest first, and newest within a severity."""
        muted = self._mutes.muted()
        rows = [
            row
            for row in [*self._open.values(), *self._events]
            if row.dismissed_at is None
            and row.resolved_at is None
            # Checked here rather than only where mute is set, so a warning
            # silenced last month that has since become a blocker still shows.
            and (row.fingerprint not in muted or not can_mute(row.severity))
        ]
        return sorted(rows, key=lambda r: (SEVERITY_RANK[r.severity], r.updated_at), reverse=True)

    def unread_count(self) -> int:
        return sum(1 for row in self.active() if row.read_at is None)

    def top_severity(self) -> str:
        rows = self.active()
        return rows[0].severity if rows else ""

    def history(self, limit: int = 200, severity: str = "", scope: str = "") -> list[Notification]:
        return self._log.history(limit, severity, scope)

    def muted(self) -> list[tuple[str, str]]:
        """Each silenced fingerprint with the last thing it said, for undoing it."""
        titles = {row.fingerprint: row.title for row in reversed(self.history(limit=500))}
        return sorted((f, titles.get(f, f)) for f in self._mutes.muted())

    # --- Acting ---

    def mark_all_read(self) -> None:
        stamp = self._now()
        for row in self.active():
            if row.read_at is None:
                row.read_at = stamp
                self._log.update(row)

    def dismiss(self, note_id: uuid.UUID) -> None:
        """Put one occurrence away. It comes back if the fault recurs or worsens."""
        for row in [*self._open.values(), *self._events]:
            if row.id == note_id:
                row.dismissed_at = self._now()
                self._log.update(row)
                return

    def mute(self, fingerprint: str) -> None:
        self._mutes.mute(fingerprint)

    def unmute(self, fingerprint: str) -> None:
        self._mutes.unmute(fingerprint)

    # --- Lifecycle ---

    def rebind(self, log: NotificationLog) -> None:
        """Follow the output root. Episodes belong to the survey they were about.

        The previous log is left as it stands: those episodes are still true of
        that survey, and stamping them resolved would claim somebody fixed them.
        """
        self._log = log
        self._open.clear()
        self._events.clear()
        self._last_signature = None
        self._adopt()

    def _adopt(self) -> None:
        """Pick up what a previous session left open in this log.

        A condition is adopted whatever its age: it was true when the app closed
        and the next reconcile decides whether it still is. An event is different.
        Nothing will ever come along to close it, so one already read is closed
        here, and only the ones nobody saw survive the restart. A run that failed
        overnight is still worth hearing about; last week's is not.
        """
        for row in self._log.open_notifications():
            if row.kind == CONDITION:
                self._open[row.fingerprint] = row
            elif row.read_at is None:
                self._events.append(row)
            else:
                row.resolved_at = self._now()
                self._log.resolve(row.id, row.resolved_at)

    def _visible_signature(self) -> tuple:
        return tuple(
            (row.id, row.severity, row.title, row.read_at is None) for row in self.active()
        )
