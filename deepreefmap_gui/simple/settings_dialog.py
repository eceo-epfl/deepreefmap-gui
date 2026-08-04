"""The run settings dialog, and the live form it edits.

Rather than keep a second copy of the run form, the dialog borrows the real one
out of the hidden holder that owns it and hands it back when it closes. The form
is never on screen otherwise, so this is the only place its settings are edited,
and every setting stays a single widget with a single value.

Borrowing the live form means the widgets are edited in place, so Cancel cannot
simply drop a pending copy. The caller snapshots the settings before opening and
puts them back when the dialog is rejected, which is why the dialog only reports
its result and never persists anything itself.
"""

from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)


class RunSettingsDialog(QDialog):
    """Hosts the borrowed run form until it is closed."""

    def __init__(self, window, form: QWidget, per_run: list[QWidget]) -> None:
        super().__init__(window)
        self._window = window
        self._form = form
        self._per_run = per_run
        self._restored = False

        self.setWindowTitle("Run settings")
        self.setModal(True)
        layout = QVBoxLayout(self)

        # The form is taller than most screens, so it scrolls inside the dialog.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(form)
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Reset
        )
        # Named for what they do to the settings. "OK" and "Cancel" say nothing
        # about whether the edit is kept, which is the only question here.
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Save settings")
        ok.setProperty("cta", "true")
        ok.setDefault(True)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Discard")
        reset = buttons.button(QDialogButtonBox.StandardButton.Reset)
        # The standard is the organisation preset, not the values a fresh window
        # happens to construct: those two drifted apart the moment a preset
        # shipped, and only one of them is the configuration anybody blessed.
        reset.setText("Restore standard settings")
        reset.setToolTip("Put every setting back to the standard for this survey.")
        # Restore writes into the live form like every other edit here, so Cancel
        # still undoes it, and only OK persists the machine override.
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setToolTip(
            "Close without keeping any of these changes."
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        reset.clicked.connect(self._window._load_standard_into_form)
        layout.addWidget(buttons)
        self.resize(560, 640)

        # Per-run values come from the pass table on the Run step, so showing
        # them here would invite edits that go nowhere.
        for widget in per_run:
            widget.setVisible(False)

    def restore_form(self) -> None:
        """Put the form back in its holder. Safe to call more than once."""
        if self._restored:
            return
        self._restored = True
        for widget in self._per_run:
            widget.setVisible(True)
        self._window._form_home_layout.addWidget(self._form)

    def done(self, result: int) -> None:
        # Covers OK, Cancel and Escape: the form must never be left inside a
        # dialog that is going away.
        self.restore_form()
        super().done(result)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        # close() on a dialog that was never shown skips done() entirely.
        self.restore_form()
        super().closeEvent(event)
