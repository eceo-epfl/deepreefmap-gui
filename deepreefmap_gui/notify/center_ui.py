"""The bell on the window: what it counts, and what pressing it does."""

from __future__ import annotations

import logging
import uuid

from PySide6.QtCore import QSettings

from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.notify.center import NotificationCenter
from deepreefmap_gui.notify.log import MemoryLog, StoreLog
from deepreefmap_gui.notify.widgets import BellButton, NotificationPopover, relative_age
from deepreefmap_gui.survey.models.common import utc_now_iso
from deepreefmap_gui.survey.models.notification import INFO, SURVEY, Notification
from deepreefmap_gui.survey.store import SurveyStore

logger = logging.getLogger(__name__)

# Per reader and per machine, never in the survey. Silencing a message says
# nothing about the survey, only about what one person wants to be told today,
# and a colleague opening the same output root should still be warned.
MUTED_KEY = "notify_muted_fingerprints"


class QSettingsMuteBook:
    """The messages this reader has said they never want to see again."""

    def __init__(self, settings: QSettings) -> None:
        self._settings = settings

    def muted(self) -> set[str]:
        stored = self._settings.value(MUTED_KEY, [])
        # Some backends hand a one-element list back as a bare string.
        if isinstance(stored, str):
            return {stored} if stored else set()
        if not isinstance(stored, list):
            return set()
        return {str(value) for value in stored}

    def mute(self, fingerprint: str) -> None:
        self._write(self.muted() | {fingerprint})

    def unmute(self, fingerprint: str) -> None:
        self._write(self.muted() - {fingerprint})

    def _write(self, fingerprints: set[str]) -> None:
        self._settings.setValue(MUTED_KEY, sorted(fingerprints))


class NotificationCenterMixin(MixinBase):
    """Everything that wants attention, behind one button in the header."""

    def _build_notification_bell(self) -> BellButton:
        self._notify = NotificationCenter(MemoryLog(), QSettingsMuteBook(self._settings))
        self._notify_bell = BellButton()
        self._notify_bell.clicked.connect(self._toggle_notification_popover)
        self._notify_popover = None
        self._refresh_notification_bell()
        return self._notify_bell

    def _refresh_notification_bell(self) -> None:
        if not hasattr(self, "_notify_bell"):
            return
        active = self._notify.active()
        self._notify_bell.set_state(
            self._notify.unread_count(), self._notify.top_severity(), len(active)
        )
        if self._notify_popover is not None and self._notify_popover.isVisible():
            self._paint_notification_popover()

    def _paint_notification_popover(self) -> None:
        if self._notify_popover is None:
            return
        now = utc_now_iso()
        self._notify_popover.set_notifications(
            self._notify.active(), lambda note: relative_age(note.updated_at, now)
        )

    def _toggle_notification_popover(self) -> None:
        if self._notify_popover is not None and self._notify_popover.isVisible():
            self._notify_popover.hide()
            return
        if self._notify_popover is None:
            self._notify_popover = NotificationPopover(self)
            self._notify_popover.activated.connect(self._on_notification_activated)
            self._notify_popover.dismissed.connect(self._on_notification_dismissed)
            self._notify_popover.muted.connect(self._on_notification_muted)
            self._notify_popover.history_requested.connect(self._show_notification_history)
        self._paint_notification_popover()
        self._notify_popover.show_under(self._notify_bell)
        # After painting, not before: the list keeps what it was showing, and
        # only the badge goes.
        self._notify.mark_all_read()
        self._refresh_notification_bell()

    def _on_notification_activated(self, section: str) -> None:
        self._hide_notification_popover()
        self._go_to_section(section)

    def _hide_notification_popover(self) -> None:
        if self._notify_popover is not None:
            self._notify_popover.hide()

    def _on_notification_dismissed(self, note_id: uuid.UUID) -> None:
        self._notify.dismiss(note_id)
        self._paint_notification_popover()
        self._refresh_notification_bell()

    def _on_notification_muted(self, fingerprint: str) -> None:
        self._notify.mute(fingerprint)
        self._paint_notification_popover()
        self._refresh_notification_bell()

    def _show_notification_history(self) -> None:
        self._hide_notification_popover()
        self._set_simple_section("machine")
        self._set_machine_view("activity")

    def _notify_post(self, payload: dict) -> Notification:
        """Slot for ``_sig_notify``: the one way a worker thread reports anything.

        A dict rather than a signal per message: what a worker has to say is data,
        and a new kind of event should not need a new signal on the window.
        """
        note = self._notify.post(
            fingerprint=payload["fingerprint"],
            title=payload["title"],
            body=payload.get("body", ""),
            severity=payload.get("severity", INFO),
            section=payload.get("section", ""),
            scope=payload.get("scope", SURVEY),
        )
        self._refresh_notification_bell()
        return note

    def _rebind_notification_log(self, store: SurveyStore | None) -> None:
        """Follow the output root: episodes belong to the survey they were about."""
        if not hasattr(self, "_notify"):
            return
        self._notify.rebind(StoreLog(store) if store is not None else MemoryLog())
        self._refresh_notification_bell()
