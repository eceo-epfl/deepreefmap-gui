"""Dialogs for filing a freshly cut section.

The scrub dialog picks the window; these pick where the section belongs. A
transect stays optional: a section is first a cutout of the video.
"""

from __future__ import annotations

import sqlite3
import uuid

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from deepreefmap_gui.core.theme import SPACE_SM, TEXT_MUTED
from deepreefmap_gui.survey.models import PASS_DIRECTIONS, Transect
from deepreefmap_gui.survey.models.importers import build_transect
from deepreefmap_gui.survey.store import SurveyStore


class NewTransectDialog(QDialog):
    """Create a transect without leaving the flow that needs it.

    Validation is shared with the Transects page via build_transect.
    """

    def __init__(self, parent: QWidget | None, store: SurveyStore) -> None:
        super().__init__(parent)
        self._store = store
        self.transect: Transect | None = None
        self.setWindowTitle("New transect")
        form = QFormLayout(self)
        from deepreefmap_gui.simple.plan import next_transect_name

        existing = [t.name for t in store.list_transects()]
        self._name = QLineEdit(next_transect_name(existing))
        form.addRow("Name", self._name)
        self._start = QLineEdit()
        self._start.setPlaceholderText("-17.5005, 177.1005")
        form.addRow("Start point", self._start)
        self._end = QLineEdit()
        self._end.setPlaceholderText("-17.5010, 177.1010")
        form.addRow("End point", self._end)
        self._length = QDoubleSpinBox()
        self._length.setRange(0.0, 10_000.0)
        self._length.setSuffix(" m")
        self._length.setToolTip(
            "The tape reading. Without it the transect's runs are not scaled."
        )
        form.addRow("Tape length", self._length)
        self._depth = QDoubleSpinBox()
        self._depth.setRange(0.0, 1_000.0)
        self._depth.setSuffix(" m")
        form.addRow("Depth", self._depth)
        self._error = QLabel("")
        self._error.setWordWrap(True)
        self._error.setStyleSheet(f"color: {TEXT_MUTED};")
        form.addRow(self._error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _on_save(self) -> None:
        try:
            transect = build_transect(
                self._name.text(),
                self._start.text(),
                self._end.text(),
                length_m=self._length.value(),
                depth_m=self._depth.value(),
            )
            self._store.add_transect(transect)
        except ValueError as exc:
            self._error.setText(str(exc))
            return
        except sqlite3.IntegrityError:
            self._error.setText(f"A transect named {self._name.text().strip()!r} already exists.")
            return
        self.transect = transect
        self.accept()


class SectionAssignDialog(QDialog):
    """File a freshly cut section: transect (or none) and direction.

    Returns its answer through ``choice()``: (transect_id | None, direction).
    """

    def __init__(self, parent: QWidget | None, store: SurveyStore) -> None:
        super().__init__(parent)
        self._store = store
        self.setWindowTitle("File this section")
        form = QFormLayout(self)
        transect_row = QHBoxLayout()
        transect_row.setSpacing(SPACE_SM)
        self._transects = QComboBox()
        self._fill_transects(selected=None)
        transect_row.addWidget(self._transects, 1)
        new_btn = QPushButton("New transect…")
        new_btn.setProperty("quiet", "true")
        new_btn.clicked.connect(self._on_new_transect)
        transect_row.addWidget(new_btn)
        form.addRow("Transect", transect_row)
        self._direction = QComboBox()
        self._direction.addItems(list(PASS_DIRECTIONS))
        form.addRow("Direction", self._direction)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Add to cart")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _fill_transects(self, selected: uuid.UUID | None) -> None:
        self._transects.clear()
        self._transects.addItem("Skip transect", None)
        for transect in self._store.list_transects():
            self._transects.addItem(transect.name, str(transect.id))
            if selected is not None and transect.id == selected:
                self._transects.setCurrentIndex(self._transects.count() - 1)

    def _on_new_transect(self) -> None:
        dialog = NewTransectDialog(self, self._store)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.transect is None:
            return
        self._fill_transects(selected=dialog.transect.id)

    def choice(self) -> tuple[uuid.UUID | None, str]:
        data = self._transects.currentData()
        return (uuid.UUID(data) if data else None), self._direction.currentText()
