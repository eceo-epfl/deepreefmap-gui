"""Where the centre writes what it has said.

A protocol rather than a direct call into ``SurveyStore``, for two reasons: the
centre stays testable with no sqlite at all, and "no survey is open" gets a real
implementation instead of a null check at every write.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Protocol

from deepreefmap_gui.survey.models.notification import Notification

if TYPE_CHECKING:
    from deepreefmap_gui.survey.store import SurveyStore

logger = logging.getLogger(__name__)


class NotificationLog(Protocol):
    def insert(self, note: Notification) -> None: ...

    def update(self, note: Notification) -> None: ...

    def resolve(self, note_id: uuid.UUID, at: str) -> None: ...

    def open_notifications(self) -> list[Notification]: ...

    def history(self, limit: int = 500, severity: str = "", scope: str = "") -> list[Notification]: ...


class MemoryLog:
    """The log a session with no survey open still keeps.

    Not a stub: somebody working before they have chosen an output root still
    deserves to see what is wrong, they just get no history of it afterwards.
    """

    def __init__(self) -> None:
        self._rows: list[Notification] = []

    def insert(self, note: Notification) -> None:
        self._rows.append(note)

    def update(self, note: Notification) -> None:
        pass  # The row is the object; the centre already changed it.

    def resolve(self, note_id: uuid.UUID, at: str) -> None:
        for row in self._rows:
            if row.id == note_id:
                row.resolved_at = at
                row.updated_at = at

    def open_notifications(self) -> list[Notification]:
        return [r for r in self._rows if r.resolved_at is None]

    def history(self, limit: int = 500, severity: str = "", scope: str = "") -> list[Notification]:
        rows = [
            r
            for r in self._rows
            if (not severity or r.severity == severity) and (not scope or r.scope == scope)
        ]
        return sorted(rows, key=lambda r: r.created_at, reverse=True)[:limit]


class StoreLog:
    """The survey database, with every write made unable to fail.

    A survey on a drive that was pulled out mid-session must not take the alert
    that would have said so down with it, so every call is logged and swallowed.
    The centre's in-memory state stands either way.
    """

    def __init__(self, store: SurveyStore) -> None:
        self._store = store

    def insert(self, note: Notification) -> None:
        self._guard("record", lambda: self._store.add_notification(note))

    def update(self, note: Notification) -> None:
        self._guard("update", lambda: self._store.update_notification(note))

    def resolve(self, note_id: uuid.UUID, at: str) -> None:
        self._guard("resolve", lambda: self._store.resolve_notification(note_id, at))

    def open_notifications(self) -> list[Notification]:
        return self._guard("read", self._store.open_notifications) or []

    def history(self, limit: int = 500, severity: str = "", scope: str = "") -> list[Notification]:
        return self._guard("read", lambda: self._store.list_notifications(limit, severity, scope)) or []

    @staticmethod
    def _guard(verb, call):
        try:
            return call()
        except Exception:
            logger.warning("Could not %s a notification", verb, exc_info=True)
            return None
