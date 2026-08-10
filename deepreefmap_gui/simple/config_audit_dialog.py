"""Which settings every processed run actually used, against the current standard.

A read-only list. It exists so an administrator can answer "was this season run
the way we asked" without opening manifests by hand, and so a diver can see that
the machine in front of them is not quietly off standard.
"""

from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.theme import TEXT_MUTED
from deepreefmap_gui.core.widgets import EmptyState, configure_table, enable_sorting
from deepreefmap_gui.survey.config_audit import STANDARD, ConfigAuditRow, audit_summary
from deepreefmap_gui.survey.preset import OrgPreset

_COL_RUN, _COL_SETTINGS, _COL_NOTE = range(3)


class ConfigAuditDialog(QDialog):
    """One row per run: what it was named, what it ran under, and how it differed."""

    def __init__(
        self,
        parent: QWidget | None,
        rows: list[ConfigAuditRow],
        org: OrgPreset,
    ) -> None:
        super().__init__(parent)
        self._rows = rows

        self.setWindowTitle("Settings history")
        layout = QVBoxLayout(self)

        heading = QLabel(f"The standard now is {org.label}.\n{audit_summary(rows)}")
        heading.setWordWrap(True)
        layout.addWidget(heading)

        if rows:
            layout.addWidget(self._build_table(rows), 1)
        else:
            layout.addWidget(
                EmptyState(
                    "Nothing processed yet",
                    "Once you process a dive, the settings it used are listed here.",
                ),
                1,
            )

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(720, 420)

    def _build_table(self, rows: list[ConfigAuditRow]) -> QTableWidget:
        table = QTableWidget(len(rows), 3)
        configure_table(table, ["Run", "Settings", "Difference"])
        header = table.horizontalHeader()
        header.setSectionResizeMode(_COL_NOTE, QHeaderView.ResizeMode.Stretch)
        table.setColumnWidth(_COL_RUN, 220)
        table.setColumnWidth(_COL_SETTINGS, 180)
        for index, row in enumerate(rows):
            for column, text in (
                (_COL_RUN, row.display_name),
                (_COL_SETTINGS, row.preset_label),
                (_COL_NOTE, row.note),
            ):
                item = QTableWidgetItem(text)
                # The folder name is what you need to find the run on disk, and it
                # is not always the display name.
                item.setToolTip(row.dir_name)
                if column == _COL_NOTE and row.verdict == STANDARD:
                    # A standard run is the uninteresting case, so it recedes
                    # rather than competing with the rows worth reading.
                    item.setForeground(QColor(TEXT_MUTED))
                table.setItem(index, column, item)
        # No initial sort: the rows arrive newest first, and no column here
        # carries a date to get that order back once it is lost.
        enable_sorting(table, column=None)
        return table
