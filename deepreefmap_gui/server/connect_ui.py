"""The Connect dialog: one pasted code, one device name.

The dialog collects and reports; it never enrols. Enrolment is network I/O, so the
window runs it on a worker thread and drives this through `working()` and
`show_failure()`, which is also why the dialog stays open until it has an answer:
a refused code is fixed by editing what is already typed here.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.spinner import BusySpinner
from deepreefmap_gui.core.theme import ERROR, TEXT_MUTED, WEIGHT_SEMIBOLD
from deepreefmap_gui.core.widgets import muted_label
from deepreefmap_gui.server.state import default_device_name
from deepreefmap_gui.sync.connect_code import CODE_PREFIX

TITLE = "Connect to server"

INTRO = "Paste the connect code from the registry's web interface. It enrols this installation, not you."

NAME_LABEL = "Device name"
NAME_HINT = "Everything this computer uploads is attributed to this name."

CONNECT = "Connect"
CONNECTING = "Connecting…"


class ConnectDialog(QDialog):
    """Collects a connect code, and reports what the window made of it."""

    # The pasted code and the device name, once. Emitted rather than read off the
    # dialog so the code lives in the handler's arguments, not in dialog state.
    submitted = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(TITLE)
        self.setModal(True)

        layout = QVBoxLayout(self)

        intro = QLabel(INTRO)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._code_edit = QPlainTextEdit()
        # Multiline: a code is a couple of hundred characters and arrives wrapped
        # out of an email or a chat window.
        self._code_edit.setPlaceholderText(f"{CODE_PREFIX}…")
        self._code_edit.setFixedHeight(88)
        self._code_edit.textChanged.connect(self._sync_connect_enabled)
        layout.addWidget(self._code_edit)

        layout.addWidget(muted_label(NAME_LABEL))
        self._name_edit = QLineEdit(default_device_name())
        self._name_edit.setToolTip(NAME_HINT)
        layout.addWidget(self._name_edit)
        hint = muted_label(NAME_HINT)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._message = QLabel("")
        self._message.setWordWrap(True)
        self._message.setVisible(False)
        layout.addWidget(self._message)

        busy_row = QHBoxLayout()
        busy_row.setContentsMargins(0, 0, 0, 0)
        self._spinner = BusySpinner()
        self._spinner.setVisible(False)
        busy_row.addWidget(self._spinner)
        self._busy_label = muted_label("")
        busy_row.addWidget(self._busy_label, 1)
        layout.addLayout(busy_row)

        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._connect_btn = QPushButton(CONNECT)
        self._buttons.addButton(self._connect_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        self._connect_btn.setProperty("cta", "true")
        self._connect_btn.setDefault(True)
        self._connect_btn.setEnabled(False)
        # Not accepted: the dialog closes when the enrolment succeeds, not when
        # the button is pressed.
        self._connect_btn.clicked.connect(self._on_connect_clicked)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self.resize(480, 380)

    def code(self) -> str:
        return self._code_edit.toPlainText().strip()

    def device_name(self) -> str:
        return self._name_edit.text().strip()

    def working(self, busy: bool) -> None:
        """Lock the fields while the enrolment is in flight."""
        self._spinner.setVisible(busy)
        self._busy_label.setText(CONNECTING if busy else "")
        self._connect_btn.setText(CONNECTING if busy else CONNECT)
        self._connect_btn.setEnabled(not busy and bool(self.code()))
        self._code_edit.setReadOnly(busy)
        self._name_edit.setReadOnly(busy)
        if busy:
            self._message.setVisible(False)

    def show_failure(self, title: str, detail: str) -> None:
        """Say why it did not work, and leave what was typed alone to be fixed."""
        self.working(False)
        self._message.setStyleSheet(f"color: {ERROR}; font-weight: {WEIGHT_SEMIBOLD};")
        self._message.setText(f"{title}. {detail}")
        self._message.setVisible(True)

    def show_note(self, text: str) -> None:
        self._message.setStyleSheet(f"color: {TEXT_MUTED};")
        self._message.setText(text)
        self._message.setVisible(bool(text))

    def _on_connect_clicked(self) -> None:
        code = self.code()
        if not code:
            return
        self.working(True)
        self.submitted.emit(code, self.device_name())

    def _sync_connect_enabled(self) -> None:
        self._connect_btn.setEnabled(bool(self.code()) and not self._code_edit.isReadOnly())
