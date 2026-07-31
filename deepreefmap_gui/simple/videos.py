"""Videos: every clip the survey knows about, and what became of each one.

A place rather than a view of the runs, because footage outlives the runs cut
from it: a card copied off the camera is a fact of the day's diving whether or
not anything has been processed from it yet. The run-oriented pages can only
show clips that already produced a run, which is exactly the wrong half when the
question is "what still needs doing".
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.theme import GUTTER, TEXT_MUTED, TEXT_SECONDARY
from deepreefmap_gui.core.widgets import EmptyState, FilterChips, section_card
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.survey import catalogue
from deepreefmap_gui.survey.catalogue import (
    VIDEO_FAILED,
    VIDEO_PENDING,
    VIDEO_PROCESSED,
    VIDEO_UNPROCESSED,
    VideoLibraryEntry,
)

logger = logging.getLogger(__name__)

# Chip order is the order work moves through: nothing done, part done, broken,
# finished. "All" leads because it is the default and the widest net.
_VIDEO_FILTERS = (
    ("all", "All"),
    (VIDEO_UNPROCESSED, "Not processed"),
    (VIDEO_PENDING, "Part processed"),
    (VIDEO_FAILED, "Failed"),
    (VIDEO_PROCESSED, "Processed"),
)

_VIDEO_PATH_ROLE = Qt.ItemDataRole.UserRole

# What each outcome says on a card, and the colour key it paints with.
_OUTCOME_LABELS = {
    VIDEO_UNPROCESSED: ("Not processed", "queued"),
    VIDEO_PENDING: ("Part processed", "running"),
    VIDEO_FAILED: ("Failed", "failed"),
    VIDEO_PROCESSED: ("Processed", "succeeded"),
}

_LIST_PAGE, _EMPTY_PAGE = 0, 1


def _clip_facts(entry: VideoLibraryEntry) -> str:
    """The line under a clip's name: how much of the survey hangs off it."""
    video = entry.video
    bits = []
    if video.duration_s:
        total = int(round(video.duration_s))
        bits.append(f"{total // 60}m {total % 60:02d}s")
    if video.size_bytes:
        from deepreefmap_gui.profiling.system_probe import format_bytes

        bits.append(format_bytes(video.size_bytes))
    if entry.pass_count:
        bits.append(f"{entry.pass_count} pass{'es' if entry.pass_count != 1 else ''}")
    if entry.run_count:
        bits.append(f"{entry.run_count} run{'s' if entry.run_count != 1 else ''}")
    return "  ·  ".join(bits)


class SimpleVideosMixin(MixinBase):
    """DeepReefMapWindow methods for the Videos workspace."""

    _video_filter: str = "all"

    def _build_videos_page(self) -> QWidget:
        """Master list of clips beside a detail pane for the selected one."""
        self._video_entries: list[VideoLibraryEntry] = []

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(GUTTER)

        filters = QHBoxLayout()
        filters.setSpacing(GUTTER)
        self._video_search = QLineEdit()
        self._video_search.setPlaceholderText("Search clips…")
        self._video_search.setClearButtonEnabled(True)
        self._video_search.setMaximumWidth(280)
        self._video_search.textChanged.connect(lambda *_: self._rebuild_video_page())
        filters.addWidget(self._video_search)
        self._video_chips = FilterChips(_VIDEO_FILTERS)
        self._video_chips.changed.connect(self._on_video_filter_changed)
        filters.addWidget(self._video_chips)
        filters.addStretch(1)
        self._video_total_label = QLabel("")
        self._video_total_label.setStyleSheet(f"color: {TEXT_SECONDARY};")
        filters.addWidget(self._video_total_label)
        layout.addLayout(filters)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(GUTTER)

        list_card, list_layout = section_card()
        self._video_list = QListWidget()
        self._video_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._video_list.setAlternatingRowColors(True)
        self._video_list.itemSelectionChanged.connect(self._refresh_video_detail)
        self._video_list.itemDoubleClicked.connect(self._on_video_activated)
        self._video_stack = QStackedWidget()
        self._video_stack.addWidget(self._video_list)
        self._video_stack.addWidget(
            EmptyState(
                "No clips here",
                "Add videos on the Run step, or drop them onto the pass table.",
            )
        )
        list_layout.addWidget(self._video_stack, 1)
        split.addWidget(list_card)

        detail_card, detail_layout = section_card()
        self._video_detail_name = QLabel("")
        self._video_detail_name.setStyleSheet("font-weight: bold;")
        self._video_detail_name.setWordWrap(True)
        detail_layout.addWidget(self._video_detail_name)
        self._video_detail_facts = QLabel("")
        self._video_detail_facts.setWordWrap(True)
        self._video_detail_facts.setStyleSheet(f"color: {TEXT_SECONDARY};")
        detail_layout.addWidget(self._video_detail_facts)
        self._video_detail_path = QLabel("")
        self._video_detail_path.setWordWrap(True)
        self._video_detail_path.setStyleSheet(f"color: {TEXT_MUTED};")
        detail_layout.addWidget(self._video_detail_path)

        detail_layout.addWidget(QLabel("Passes cut from this clip"))
        self._video_pass_list = QListWidget()
        self._video_pass_list.setAlternatingRowColors(True)
        self._video_pass_list.itemDoubleClicked.connect(self._on_video_pass_activated)
        self._video_pass_stack = QStackedWidget()
        self._video_pass_stack.addWidget(self._video_pass_list)
        self._video_pass_stack.addWidget(
            EmptyState("Not cut into passes yet", "Queue it to process this clip.")
        )
        detail_layout.addWidget(self._video_pass_stack, 1)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        self._video_queue_btn = QPushButton("Queue as pass")
        self._video_queue_btn.setToolTip("Add this clip to the current batch on the Run step.")
        self._video_queue_btn.clicked.connect(self._on_video_queue_clicked)
        actions.addWidget(self._video_queue_btn)
        self._video_show_btn = QPushButton("Show in folder")
        self._video_show_btn.clicked.connect(self._on_video_show_clicked)
        actions.addWidget(self._video_show_btn)
        actions.addStretch(1)
        detail_layout.addLayout(actions)
        split.addWidget(detail_card)

        # The list is the page; the detail pane reads a selection rather than
        # competing with it.
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([760, 500])
        layout.addWidget(split, 1)

        self._refresh_video_detail()
        return page

    # --- Data ---

    def _load_video_entries(self) -> list[VideoLibraryEntry]:
        try:
            store = self._survey_store()
            return catalogue.video_library(
                store.list_videos(), store.list_passes(), store.list_runs()
            )
        except Exception:
            logger.exception("Could not list the video library")
            return []

    def _visible_video_entries(self) -> list[VideoLibraryEntry]:
        needle = self._video_search.text().strip().lower()
        entries = self._video_entries
        if self._video_filter != "all":
            entries = [e for e in entries if e.outcome == self._video_filter]
        if needle:
            entries = [e for e in entries if needle in e.video.file_name.lower()]
        return entries

    def _refresh_videos_page(self) -> None:
        """Re-read the library from the store, then repaint."""
        if not hasattr(self, "_video_list"):
            return
        self._video_entries = self._load_video_entries()
        counts = {key: 0 for key, _ in _VIDEO_FILTERS}
        counts["all"] = len(self._video_entries)
        for entry in self._video_entries:
            counts[entry.outcome] = counts.get(entry.outcome, 0) + 1
        self._video_chips.set_counts(counts)
        self._rebuild_video_page()

    def _rebuild_video_page(self) -> None:
        keep = self._selected_video_path()
        entries = self._visible_video_entries()
        self._video_list.blockSignals(True)
        self._video_list.clear()
        for entry in entries:
            label, key = _OUTCOME_LABELS[entry.outcome]
            item = QListWidgetItem(f"{entry.video.file_name}\n{_clip_facts(entry)}")
            item.setData(_VIDEO_PATH_ROLE, entry.video.path)
            item.setData(Qt.ItemDataRole.ToolTipRole, f"{entry.video.path}\n{label}")
            item.setData(Qt.ItemDataRole.UserRole + 2, key)
            self._video_list.addItem(item)
            if entry.video.path == keep:
                self._video_list.setCurrentItem(item)
        self._video_list.blockSignals(False)
        if self._video_list.currentItem() is None and self._video_list.count():
            self._video_list.setCurrentRow(0)
        self._video_stack.setCurrentIndex(_LIST_PAGE if entries else _EMPTY_PAGE)
        total = len(self._video_entries)
        shown = len(entries)
        text = f"{total} clip{'s' if total != 1 else ''}"
        if shown != total:
            text = f"{shown} of {text}"
        self._video_total_label.setText(text)
        self._refresh_video_detail()

    def _on_video_filter_changed(self, key: str) -> None:
        self._video_filter = key
        self._rebuild_video_page()

    # --- Selection and detail ---

    def _selected_video_path(self) -> str | None:
        item = self._video_list.currentItem()
        return item.data(_VIDEO_PATH_ROLE) if item is not None else None

    def _selected_video_entry(self) -> VideoLibraryEntry | None:
        path = self._selected_video_path()
        if path is None:
            return None
        return next((e for e in self._video_entries if e.video.path == path), None)

    def _refresh_video_detail(self) -> None:
        entry = self._selected_video_entry()
        self._video_queue_btn.setEnabled(entry is not None and not self._run_in_flight())
        self._video_show_btn.setEnabled(entry is not None)
        if entry is None:
            self._video_detail_name.setText("No clip selected")
            self._video_detail_facts.setText("")
            self._video_detail_path.setText("")
            self._video_pass_list.clear()
            self._video_pass_stack.setCurrentIndex(1)
            return
        label, _ = _OUTCOME_LABELS[entry.outcome]
        self._video_detail_name.setText(entry.video.file_name)
        self._video_detail_facts.setText(
            "  ·  ".join(filter(None, [label, _clip_facts(entry)]))
        )
        self._video_detail_path.setText(entry.video.path)

        self._video_pass_list.clear()
        store = self._survey_store()
        runs_by_pass: dict = {}
        for run in entry.runs:
            runs_by_pass.setdefault(run.pass_id, []).append(run)
        for pass_ in entry.passes:
            transect = store.get_transect(pass_.transect_id)
            name = transect.name if transect is not None else "Unassigned"
            runs = runs_by_pass.get(pass_.id, [])
            status = runs[-1].status if runs else "queued"
            window = f"{int(pass_.begin_s) // 60}:{int(pass_.begin_s) % 60:02d}"
            window += f"–{int(pass_.end_s) // 60}:{int(pass_.end_s) % 60:02d}"
            item = QListWidgetItem(f"{name} · {pass_.direction} · {window} — {status}")
            item.setData(Qt.ItemDataRole.UserRole, str(pass_.id))
            self._video_pass_list.addItem(item)
        self._video_pass_stack.setCurrentIndex(0 if entry.passes else 1)

    # --- Actions ---

    def _on_video_activated(self, _item: QListWidgetItem) -> None:
        self._on_video_queue_clicked()

    def _on_video_queue_clicked(self) -> None:
        path = self._selected_video_path()
        if path is None:
            return
        self._queue_video_path(path)

    def _on_video_show_clicked(self) -> None:
        """Open the clip's folder, not the clip: a player is not what is wanted here."""
        path = self._selected_video_path()
        if path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))

    def _on_video_pass_activated(self, _item: QListWidgetItem) -> None:
        """A pass is a run's worth of work, so open what it produced in Browse."""
        self._go_to_step("browse")
