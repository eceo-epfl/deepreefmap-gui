"""The Server page: what this device is enrolled with, and the sync it drives.

A section rather than a fourth destination. The destinations are where the work is
(footage, transects, the cart, the archive) and this is a place you visit to check
a connection and leave again, which is what Setup and the storage pages already
are, so it takes their shape: a bordered utility button in the header, no pill,
nothing lit while you are here.

Both network calls run on a worker thread and report back over the window's
signals. Sync conflicts are not reported on this page at all: the engine posts them
to the notification centre, where everything else wrong with the survey is already
read, and pressing one brings the reader back here.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QToolButton,
    QWidget,
)

from deepreefmap_gui.core.icons import ICON_SM, server_icon
from deepreefmap_gui.core.spinner import BusySpinner
from deepreefmap_gui.core.theme import FONT_LG, SPACE_SM, SUCCESS, WEIGHT_SEMIBOLD
from deepreefmap_gui.core.widgets import (
    EmptyState,
    KeyValueList,
    NoticeStrip,
    NotReadyStrip,
    SectionHeader,
    centred_column,
    confirm,
    muted_label,
    section_card,
    utility_button_qss,
)
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.notify.widgets import relative_age
from deepreefmap_gui.server import enrolment as enrolment_mod
from deepreefmap_gui.server.connect_ui import ConnectDialog
from deepreefmap_gui.server.state import (
    DEVICE_NAME_KEY,
    ENROLLED_BY_KEY,
    LAST_SYNC_KEY,
    SECTION_LABELS,
    SERVER_SECTION,
    Failure,
    ServerState,
    default_device_name,
    describe_failure,
    read_state,
    summarise,
)
from deepreefmap_gui.survey.models.common import utc_now_iso
from deepreefmap_gui.survey.models.notification import INFO, SURVEY
from deepreefmap_gui.sync.credentials import BACKEND_KEYRING
from deepreefmap_gui.sync.engine import SyncEngine

logger = logging.getLogger(__name__)

PAGE_TITLE = "Server"
PAGE_CAPTION = "Share this survey's records with a registry."

NOT_CONNECTED = "Not connected."
NOT_CONNECTED_HINT = "Paste a connect code to join a registry."

DEVICE_CARD = "This device"
ATTRIBUTION_NOTE = "Uploads are attributed to this name. Rename it in the web interface."
ONBOARDED_BY = "Onboarded by"

CONNECT = "Connect to server"
RECONNECT = "Connect again"
SYNC_NOW = "Sync now"
DISCONNECT = "Disconnect"

# Said beside the button as well as on it: the two things confused here are
# forgetting a token and revoking a device.
DISCONNECT_NOTE = (
    "Disconnect only forgets the token on this laptop. It does not revoke the device: "
    "that is done in the registry's web interface."
)

SESSION_RUNNING = "Wait for the current session to finish, then sync."

WHERE_KEYRING = "the operating system keyring"
WHERE_FILE = "a private file in this user's data directory"

PULLING = "Pulling changes (page {page})…"
SENDING = "Sending {rows} row(s)…"


class ConflictNotifier:
    """The engine's conflict sink, delivered to the bell on the GUI thread.

    The engine posts from the worker thread and the notification centre belongs to
    the GUI one, so everything goes through `_sig_notify`, the one route a worker
    has to the bell. The section is stamped here: a conflict is read on the Server
    page, so pressing the notification has to land there.
    """

    def __init__(self, emit: Callable[[dict], None]) -> None:
        self._emit = emit

    def post(
        self,
        *,
        fingerprint: str,
        title: str,
        body: str = "",
        severity: str = INFO,
        scope: str = SURVEY,
    ) -> None:
        self._emit(
            {
                "fingerprint": fingerprint,
                "title": title,
                "body": body,
                "severity": severity,
                "scope": scope,
                "section": SERVER_SECTION,
            }
        )


class ProgressTransport:
    """The sync client, reporting each exchange as it is made.

    Wrapping the transport rather than instrumenting the engine: a pull is as many
    requests as the registry has pages, and that count only exists out here.
    """

    def __init__(self, client: Any, report: Callable[[str], None]) -> None:
        self._client = client
        self._report = report
        self._pages = 0

    def pull(self, since: int | None = None, limit: int = 1000) -> Mapping[str, Any]:
        self._pages += 1
        self._report(PULLING.format(page=self._pages))
        return self._client.pull(since=since, limit=limit)

    def push(self, sections: Mapping[str, Sequence[Mapping[str, Any]]]) -> Mapping[str, Any]:
        self._report(SENDING.format(rows=sum(len(rows) for rows in sections.values())))
        return self._client.push(sections)


class ServerPageMixin(MixinBase):
    """DeepReefMapWindow methods for the Server section and the sync it runs."""

    _server_syncing: bool = False
    _connect_dialog: ConnectDialog | None = None
    _server_state: ServerState | None = None

    # --- building -----------------------------------------------------------

    def _build_server_page(self) -> QWidget:
        """The connection, the position, and the actions on them."""
        page, body = centred_column()
        body.addWidget(SectionHeader(PAGE_TITLE))
        caption = muted_label(PAGE_CAPTION)
        caption.setWordWrap(True)
        body.addWidget(caption)

        self._server_blocker = NotReadyStrip()
        self._server_blocker.action_clicked.connect(self._on_connect_server)
        body.addWidget(self._server_blocker)

        self._server_notice = NoticeStrip(SUCCESS)
        self._server_notice.action_clicked.connect(self._on_sync_now)
        body.addWidget(self._server_notice)

        self._server_empty = EmptyState(NOT_CONNECTED, NOT_CONNECTED_HINT)
        body.addWidget(self._server_empty)

        self._server_device_card, device_layout = section_card(DEVICE_CARD)
        self._server_device_label = QLabel("")
        self._server_device_label.setStyleSheet(
            f"font-size: {FONT_LG}; font-weight: {WEIGHT_SEMIBOLD};"
        )
        self._server_device_label.setWordWrap(True)
        device_layout.addWidget(self._server_device_label)
        attribution = muted_label(ATTRIBUTION_NOTE)
        attribution.setWordWrap(True)
        device_layout.addWidget(attribution)
        self._server_device_facts = KeyValueList()
        device_layout.addWidget(self._server_device_facts)
        body.addWidget(self._server_device_card)

        self._server_card, card_layout = section_card("Connection")
        self._server_facts = KeyValueList()
        card_layout.addWidget(self._server_facts)
        body.addWidget(self._server_card)

        self._server_waiting_card, waiting_layout = section_card("Waiting to send")
        self._server_waiting = KeyValueList()
        waiting_layout.addWidget(self._server_waiting)
        body.addWidget(self._server_waiting_card)

        body.addWidget(self._build_server_actions())
        note = muted_label(DISCONNECT_NOTE)
        note.setWordWrap(True)
        self._server_disconnect_note = note
        body.addWidget(note)
        body.addStretch(1)

        holder = QScrollArea()
        holder.setWidgetResizable(True)
        holder.setWidget(page)
        holder.setFrameShape(QScrollArea.Shape.NoFrame)
        holder.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return holder

    def _build_server_actions(self) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE_SM)

        self._server_spinner = BusySpinner()
        self._server_spinner.setVisible(False)
        row.addWidget(self._server_spinner)
        self._server_progress = muted_label("")
        row.addWidget(self._server_progress, 1)

        self._server_connect_btn = QPushButton(CONNECT)
        self._server_connect_btn.setProperty("cta", "true")
        self._server_connect_btn.clicked.connect(self._on_connect_server)
        row.addWidget(self._server_connect_btn)

        self._server_sync_btn = QPushButton(SYNC_NOW)
        self._server_sync_btn.setToolTip(
            "Take everything the registry has for this survey, then offer everything "
            "edited here."
        )
        self._server_sync_btn.clicked.connect(self._on_sync_now)
        row.addWidget(self._server_sync_btn)

        self._server_disconnect_btn = QPushButton(DISCONNECT)
        self._server_disconnect_btn.setToolTip(DISCONNECT_NOTE)
        self._server_disconnect_btn.clicked.connect(self._on_disconnect_server)
        row.addWidget(self._server_disconnect_btn)
        return holder

    def _build_server_nav_button(self) -> QToolButton:
        """Header entry, beside the other utilities and never a destination pill."""
        button = QToolButton()
        button.setText(PAGE_TITLE)
        button.setIcon(server_icon(ICON_SM))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(utility_button_qss())
        button.setToolTip("The registry this survey syncs with, and what is waiting to go.")
        button.clicked.connect(lambda: self._set_simple_section(SERVER_SECTION))
        self._server_nav_button = button
        return button

    # --- painting -----------------------------------------------------------

    def _refresh_server_page(self) -> None:
        """Re-read the credential and the sync position, and paint them."""
        if not hasattr(self, "_server_facts"):
            return
        state = read_state(
            self._try_survey_store(), self._server_device_name(), self._server_enrolled_by()
        )
        self._server_state = state
        connected = state.connected
        self._server_empty.setVisible(not connected)
        self._server_device_card.setVisible(connected)
        self._server_card.setVisible(connected)
        self._server_waiting_card.setVisible(connected and bool(state.pending))
        self._server_connect_btn.setVisible(not connected)
        self._server_sync_btn.setVisible(connected)
        self._server_sync_btn.setEnabled(connected and not self._server_syncing)
        self._server_disconnect_btn.setVisible(connected)
        self._server_disconnect_note.setVisible(connected)
        if state.fault:
            self._server_blocker.show_blocker(state.fault, RECONNECT)
        if connected:
            self._server_device_label.setText(state.device_name or default_device_name())
            self._server_device_facts.set_rows(_device_rows(state))
            self._server_facts.set_rows(_fact_rows(state))
            self._server_waiting.set_rows(
                [
                    (SECTION_LABELS.get(section, section), str(count))
                    for section, count in state.pending.items()
                ]
            )

    def _server_device_name(self) -> str:
        stored = self._settings.value(DEVICE_NAME_KEY, "")
        return str(stored) if stored else default_device_name()

    def _server_enrolled_by(self) -> str:
        stored = self._settings.value(ENROLLED_BY_KEY, "")
        return str(stored) if stored else ""

    def _set_server_busy(self, busy: bool, text: str = "") -> None:
        self._server_spinner.setVisible(busy)
        self._server_progress.setText(text)
        self._server_sync_btn.setEnabled(not busy)
        self._server_disconnect_btn.setEnabled(not busy)

    # --- connecting ---------------------------------------------------------

    def _on_connect_server(self) -> None:
        """Open the connect dialog, and enrol whatever it hands over."""
        dialog = ConnectDialog(self)
        dialog.submitted.connect(self._start_enrolment)
        dialog.finished.connect(self._on_connect_dialog_finished)
        self._connect_dialog = dialog
        dialog.exec()
        # Destroyed rather than parked on the window: the field holds a credential.
        dialog.deleteLater()

    def _on_connect_dialog_finished(self) -> None:
        # An enrolment can outlive the dialog: whoever cancelled mid-flight still
        # gets the outcome, on the page instead.
        self._connect_dialog = None

    def _start_enrolment(self, code: str, device_name: str) -> None:
        """Enrol on a worker thread. The code is never logged and never stored."""
        name = device_name.strip() or default_device_name()

        def worker() -> None:
            try:
                connected = enrolment_mod.connect(code, name)
            except Exception as exc:
                logger.warning("Enrolment failed: %s", exc)
                payload: tuple[object, object] = (None, describe_failure(exc))
            else:
                payload = (connected, None)
            try:
                self._sig_enrol_done.emit(*payload)
            except (RuntimeError, TypeError):
                logger.debug("The window closed before the enrolment finished")

        threading.Thread(target=worker, daemon=True, name="registry-enrol").start()

    def _on_enrol_done(self, connected: object, failure: object) -> None:
        dialog = self._connect_dialog
        if isinstance(failure, Failure):
            if dialog is not None:
                dialog.show_failure(failure.title, failure.detail)
            else:
                self._server_blocker.show_blocker(f"{failure.title}. {failure.detail}", RECONNECT)
            return
        if not isinstance(connected, enrolment_mod.Connected):
            return
        self._settings.setValue(DEVICE_NAME_KEY, connected.device_name)
        self._settings.setValue(ENROLLED_BY_KEY, connected.enrolled_by)
        if dialog is not None:
            dialog.accept()
        self._server_blocker.clear()
        self._refresh_server_page()
        where = WHERE_KEYRING if connected.backend == BACKEND_KEYRING else WHERE_FILE
        message = f"Connected to {connected.base_url}. The device token is kept in {where}."
        if connected.warning:
            message = f"{message} {connected.warning}"
        self._server_notice.show_notice(message, SYNC_NOW)

    # --- syncing ------------------------------------------------------------

    def _on_sync_now(self) -> None:
        """Pull, then push, on a worker thread."""
        if self._server_syncing:
            return
        # A pull rewrites rows the batch worker is writing pass statuses into, so
        # the two never run at once.
        if self._survey_worker_running:
            self._server_blocker.show_blocker(SESSION_RUNNING)
            return
        engine = self._build_sync_engine()
        if engine is None:
            return
        self._server_syncing = True
        self._server_notice.clear()
        self._set_server_busy(True, PULLING.format(page=1))

        def worker() -> None:
            try:
                pulled = engine.pull()
                pushed = engine.push()
            except Exception as exc:
                logger.warning("Sync failed: %s", exc)
                payload: tuple[object, object] = (None, describe_failure(exc))
            else:
                payload = ((pulled, pushed), None)
            try:
                self._sig_sync_done.emit(*payload)
            except (RuntimeError, TypeError):
                logger.debug("The window closed before the sync finished")

        threading.Thread(target=worker, daemon=True, name="registry-sync").start()

    def _build_sync_engine(self) -> SyncEngine | None:
        """A sync engine for the survey under the current output root, or None.

        None when this device is not enrolled. The page's own state says so, and
        the button that calls this is hidden then.
        """
        from deepreefmap_gui.sync import credentials
        from deepreefmap_gui.sync.client import SyncClient

        store = self._try_survey_store()
        if store is None:
            return None
        try:
            held = credentials.load()
        except Exception as exc:
            self._on_sync_done(None, describe_failure(exc))
            return None
        if held is None:
            self._refresh_server_page()
            return None
        transport = ProgressTransport(
            SyncClient(held.base_url, held.token), self._sig_sync_progress.emit
        )
        return SyncEngine(
            store,
            transport,
            out_root=store.path.parent,
            classes_config=self._classes_config,
            notifications=ConflictNotifier(self._sig_notify.emit),
        )

    def _on_sync_progress(self, text: str) -> None:
        if self._server_syncing:
            self._set_server_busy(True, text)

    def _on_sync_done(self, reports: object, failure: object) -> None:
        self._server_syncing = False
        self._set_server_busy(False)
        if isinstance(failure, Failure):
            self._server_notice.clear()
            self._server_blocker.show_blocker(
                f"{failure.title}. {failure.detail}",
                RECONNECT if failure.reconnect else "",
            )
            self._refresh_server_page()
            return
        if not isinstance(reports, tuple):
            return
        pulled, pushed = reports
        store = self._try_survey_store()
        if store is not None:
            store.set_sync_state(LAST_SYNC_KEY, utc_now_iso())
        self._server_blocker.clear()
        self._refresh_server_page()
        self._server_notice.show_notice(summarise(pulled, pushed))
        # A pull rewrites the survey underneath every list drawn from it.
        self._refresh_transect_list()
        self._refresh_data_manager()
        self._refresh_survey_analysis()

    # --- disconnecting ------------------------------------------------------

    def _on_disconnect_server(self) -> None:
        """Forget the token and this survey's sync position, after one question."""
        if not confirm(
            self,
            DISCONNECT,
            f"{DISCONNECT_NOTE}\n\nThe survey stays exactly as it is. Disconnect this laptop?",
        ):
            return
        enrolment_mod.forget(self._try_survey_store())
        self._settings.remove(ENROLLED_BY_KEY)
        self._server_notice.clear()
        self._server_blocker.clear()
        self._refresh_server_page()


def _device_rows(state: ServerState) -> list[tuple[str, str]]:
    """The installation's own identity. `Onboarded by` is absent unless reported."""
    rows = [("Device id", state.device_id or "Unknown")]
    if state.enrolled_by:
        rows.append((ONBOARDED_BY, state.enrolled_by))
    return rows


def _fact_rows(state: ServerState) -> list[tuple[str, str]]:
    where = WHERE_KEYRING if state.backend == BACKEND_KEYRING else WHERE_FILE
    age = relative_age(state.last_sync, utc_now_iso()) if state.last_sync else ""
    if not state.last_sync:
        last = "Never"
    elif age in ("", "just now"):
        last = f"Just now ({state.last_sync})"
    else:
        last = f"{age} ago ({state.last_sync})"
    return [
        ("Server", state.base_url),
        ("Token kept in", where),
        ("Last sync", last),
        ("Pulled up to", "Nothing yet" if state.cursor is None else str(state.cursor)),
        ("Waiting to send", f"{state.waiting} row(s)"),
    ]
