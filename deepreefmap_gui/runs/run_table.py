"""The run archive as a sortable table, one row per run.

Runs are compared, not read: which pass produced the most points, which one ate
the disk, which transect has three attempts and which has none. A column answers
that at a glance and a click sorts by it, where the prose card it replaces
buried every fact in one wrapped sentence.
"""

from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
)

from deepreefmap_gui.core.widgets import StatusPillDelegate
from deepreefmap_gui.profiling.eta import format_duration
from deepreefmap_gui.profiling.system_probe import format_bytes
from deepreefmap_gui.runs.run_cards import format_run_metadata, points_label
from deepreefmap_gui.survey import catalogue
from deepreefmap_gui.survey.catalogue import RunEntry

COL_NAME, COL_STATUS, COL_CREATED, COL_FRAMES = 0, 1, 2, 3
COL_POINTS, COL_RUNTIME, COL_SIZE, COL_TRANSECT, COL_VIDEO = 4, 5, 6, 7, 8

_HEADERS = (
    "Name",
    "Status",
    "Created",
    "Frames",
    "Points",
    "Runtime",
    "Size",
    "Transect",
    "Video",
)

# Name absorbs the slack; every other column is as wide as its content needs
# and no wider, so the numeric columns stay in a readable block.
_STRETCH_COLUMNS = (COL_NAME,)

# No column narrower than this, whatever its content measures.
_MIN_SECTION_WIDTH = 64

# Enough for a GoPro filename; draggable from there.
_VIDEO_WIDTH = 150

# Numbers read right-aligned, which also lines up their digits down the column.
_NUMERIC_COLUMNS = (COL_FRAMES, COL_POINTS, COL_RUNTIME, COL_SIZE)

_MISSING = "—"


class SortableItem(QTableWidgetItem):
    """A cell that sorts by a value rather than by its formatted text.

    "1.2M pts" above "988k pts", "1.6 GB" above "1015 MB": the display strings
    order wrongly under every string comparison, so the raw number rides along.

    A cell with no value sinks to the bottom in *both* directions. Qt sorts with
    ``__lt__`` and reverses for descending, so a plain comparison would float the
    blanks to the top of a descending sort; the current sort order is read back
    off the header and the answer inverted to cancel that out.
    """

    def __init__(self, text: str, value: object = None) -> None:
        super().__init__(text)
        self._value = value

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if not isinstance(other, SortableItem):
            return super().__lt__(other)
        mine, theirs = self._value, other._value
        if (mine is None) != (theirs is None):
            table = self.tableWidget()
            descending = (
                table is not None
                and table.horizontalHeader().sortIndicatorOrder()
                == Qt.SortOrder.DescendingOrder
            )
            return descending if mine is None else not descending
        if mine is None:
            return False
        try:
            return mine < theirs  # type: ignore[operator]
        except TypeError:
            return str(mine) < str(theirs)


def _sort_text(value: str | None) -> str | None:
    """Lowercased for a case-blind sort; None when absent, so it sinks."""
    return value.lower() if value else None


def _created_text(entry: RunEntry) -> str:
    try:
        stamp = datetime.fromtimestamp(entry.sort_key, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    return stamp.astimezone().strftime("%Y-%m-%d %H:%M")


def _frame_count(entry: RunEntry) -> int | None:
    frames = entry.manifest.get("frames_processed")
    return int(frames) if isinstance(frames, (int, float)) else None


def _row_tooltip(entry: RunEntry, related: int) -> str:
    """The full manifest, for the facts no column has room for."""
    if entry.incomplete:
        return (
            f"<b>{entry.dir_name}</b><br>"
            "<i>No run manifest: this run did not finish.</i>"
        )
    tooltip = format_run_metadata(
        entry.manifest,
        entry.run_dir,
        include_disk_size=entry.size_bytes is not None,
        disk_bytes=entry.size_bytes,
    )
    if entry.moved_from:
        tooltip += f"<br><i>Recorded at run time as: {entry.moved_from}</i>"
    if related:
        tooltip += f"<br><i>{related} related run{'s' if related != 1 else ''}</i>"
    return tooltip


class RunTable(QTableWidget):
    """Every run in the current grouping, one per row.

    Identity is the run directory, carried on the Name cell under ``UserRole``;
    the panel resolves selections back to ``RunEntry`` through it, so no widget
    holds onto an entry that a rescan has replaced.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(0, len(_HEADERS), parent)
        self.setHorizontalHeaderLabels(list(_HEADERS))
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # Delete and Assign act on a whole selection, so several runs can be
        # picked at once.
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.verticalHeader().setVisible(False)
        self.setWordWrap(False)
        self.setSortingEnabled(True)
        self.setItemDelegateForColumn(COL_STATUS, StatusPillDelegate(self))

        header = self.horizontalHeader()
        header.setStretchLastSection(False)
        # A floor under every column, because the content-sized ones are greedy:
        # left alone they took the whole viewport and squeezed Name, the one
        # column identifying the row, down to an ellipsis.
        header.setMinimumSectionSize(_MIN_SECTION_WIDTH)
        for column in range(len(_HEADERS)):
            mode = (
                QHeaderView.ResizeMode.Stretch
                if column in _STRETCH_COLUMNS
                else QHeaderView.ResizeMode.ResizeToContents
            )
            header.setSectionResizeMode(column, mode)
        # Interactive, not content-sized: a video name is long enough to eat the
        # row on its own, and Name has the stronger claim on the slack.
        header.setSectionResizeMode(COL_VIDEO, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(COL_VIDEO, _VIDEO_WIDTH)
        self.sortByColumn(COL_CREATED, Qt.SortOrder.DescendingOrder)

    def current_run_dir(self) -> str | None:
        row = self.currentRow()
        if row < 0:
            return None
        item = self.item(row, COL_NAME)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def selected_run_dirs(self) -> set[str]:
        items = (self.item(i.row(), COL_NAME) for i in self.selectionModel().selectedRows())
        return {
            item.data(Qt.ItemDataRole.UserRole) for item in items if item is not None
        }

    def select_run_dir(self, run_dir: str) -> bool:
        for row in range(self.rowCount()):
            item = self.item(row, COL_NAME)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == run_dir:
                self.setCurrentCell(row, COL_NAME)
                return True
        return False

    def set_entries(self, entries: list[RunEntry], related: dict) -> None:
        """Repaint the whole table, preserving the sort and the selected run."""
        keep = self.current_run_dir()
        # Sorting is suspended while rows are filled: with it live, each new row
        # is re-sorted into place as it lands and the cells of a half-built row
        # scatter across the table.
        self.setSortingEnabled(False)
        self.setRowCount(0)
        self.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            self._fill_row(row, entry, related.get(entry.run_dir, 0))
        self.setSortingEnabled(True)
        if keep is not None:
            self.select_run_dir(keep)

    def _fill_row(self, row: int, entry: RunEntry, related: int) -> None:
        status = catalogue.entry_status(entry)
        frames = _frame_count(entry)
        cells = (
            (COL_NAME, entry.display_name, entry.display_name.lower()),
            (COL_STATUS, status.capitalize(), status),
            (COL_CREATED, _created_text(entry), entry.sort_key),
            (COL_FRAMES, f"{frames:,}" if frames else "", frames),
            (
                COL_POINTS,
                points_label(entry.points) if entry.points else "",
                entry.points,
            ),
            (
                COL_RUNTIME,
                format_duration(entry.duration_s) if entry.duration_s else "",
                entry.duration_s,
            ),
            (
                COL_SIZE,
                format_bytes(entry.size_bytes) if entry.size_bytes is not None else "",
                entry.size_bytes,
            ),
            (COL_TRANSECT, entry.transect_name or "", _sort_text(entry.transect_name)),
            (COL_VIDEO, entry.video_name or "", _sort_text(entry.video_name)),
        )
        tooltip = _row_tooltip(entry, related)
        for column, text, value in cells:
            # An em dash rather than an empty cell: a blank reads as a column
            # that failed to load, where a dash says the run has no such fact.
            item = SortableItem(text or (_MISSING if column != COL_NAME else ""), value)
            item.setToolTip(tooltip)
            if column in _NUMERIC_COLUMNS:
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
            # Identity rides on the Name cell, which every lookup goes through.
            if column == COL_NAME:
                item.setData(Qt.ItemDataRole.UserRole, str(entry.run_dir))
            self.setItem(row, column, item)
