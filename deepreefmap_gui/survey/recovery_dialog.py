"""The dialog offering a way back from a survey database that will not open."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.theme import TEXT_MUTED, WARNING
from deepreefmap_gui.survey.health import SurveyDbHealth
from deepreefmap_gui.survey.recovery import (
    RecoveryKind,
    RecoveryOption,
    rebuild_losses,
    recovery_options,
)

logger = logging.getLogger(__name__)


class SurveyRecoveryDialog(QDialog):
    """Pick a route out. Nothing is touched until Continue is pressed.

    Cancelling is allowed and leaves the app running with the survey features
    blocked -- the readiness row offers this dialog again, so declining once is
    not a decision the user is stuck with.
    """

    def __init__(
        self, health: SurveyDbHealth, out_root: Path, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Survey database")
        self.setModal(True)
        self._options = recovery_options(health, out_root)
        self._group = QButtonGroup(self)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        headline = QLabel(health.detail or "This survey database could not be opened.")
        headline.setWordWrap(True)
        headline.setStyleSheet(f"color: {WARNING}; font-weight: 600;")
        layout.addWidget(headline)

        path_label = QLabel(str(health.path))
        path_label.setWordWrap(True)
        path_label.setStyleSheet(f"color: {TEXT_MUTED};")
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        layout.addWidget(path_label)

        for index, option in enumerate(self._options):
            layout.addWidget(self._build_option(index, option))

        note = QLabel("Nothing is deleted. The current database is kept under a new name.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {TEXT_MUTED};")
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Continue")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_option(self, index: int, option: RecoveryOption) -> QWidget:
        holder = QWidget()
        box = QVBoxLayout(holder)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)

        button = QRadioButton(option.title)
        if option.recommended:
            button.setChecked(True)
        self._group.addButton(button, index)
        box.addWidget(button)

        detail = QLabel(option.detail)
        detail.setWordWrap(True)
        detail.setStyleSheet(f"color: {TEXT_MUTED}; margin-left: 22px;")
        box.addWidget(detail)

        # The rebuild's limits are listed rather than summarised: "some data may
        # be lost" is not something a choice can be made against.
        if option.kind is RecoveryKind.REBUILD:
            losses = QLabel("\n".join(f"• {line}" for line in rebuild_losses()))
            losses.setWordWrap(True)
            losses.setStyleSheet(f"color: {TEXT_MUTED}; margin-left: 22px;")
            box.addWidget(losses)
        return holder

    def selected(self) -> RecoveryOption | None:
        index = self._group.checkedId()
        return self._options[index] if 0 <= index < len(self._options) else None
