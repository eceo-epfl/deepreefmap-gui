"""Batch tab: assign a day's videos to transects as passes and run them."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path

from deepreefmap.pipeline.artifacts import ReconstructionCancelled
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.theme import (
    GUTTER,
    RADIUS_SM,
    TEXT_DIM,
    TEXT_MUTED,
    WARN_BG,
    WARN_BORDER,
    WARN_TEXT,
)
from deepreefmap_gui.core.widgets import (
    PASS_PERCENT_ROLE,
    EmptyState,
    NotReadyStrip,
    StatusPillDelegate,
    section_card,
)
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.form.video_scrub import VideoScrubDialog
from deepreefmap_gui.simple.batch_progress import BatchProgressCard
from deepreefmap_gui.simple.progress import (
    ATTENTION,
    BLOCKED,
    FIX_HERE,
    FIX_SETTINGS,
    FIX_SETUP,
    SectionState,
    run_gate,
)
from deepreefmap_gui.survey.models import (
    PASS_DIRECTIONS,
    RunRecord,
    SurveyBatch,
    Transect,
    TransectPass,
    VideoAsset,
)
from deepreefmap_gui.survey.models.convert import survey_manifest_block
from deepreefmap_gui.survey.preset import (
    MACHINE_OVERRIDABLE_KEYS,
    describe_keys,
    manifest_config_block,
)
from deepreefmap_gui.survey.store import SurveyStore

logger = logging.getLogger(__name__)

_COL_VIDEO, _COL_TRANSECT, _COL_DIRECTION, _COL_TRIM, _COL_STATUS, _COL_ACTION = range(6)

# Where a pass sits relative to the next batch. Every row is in exactly one of
# these, and the table is grouped in this order.
QUEUED, HELD, DONE = "queued", "held", "done"
_GROUP_TITLES = {
    QUEUED: "In the next batch",
    HELD: "Held back",
    DONE: "Already processed",
}
_GROUP_HINTS = {
    QUEUED: "Processing runs these, top to bottom.",
    HELD: "Skipped by every batch until returned.",
    DONE: "Succeeded once. Run again to redo one.",
}
# The one move each row can make, on a button in the row itself. A pass is held
# or released one at a time far more often than in bulk, and a button beside the
# row it acts on needs no selection and no explanation.
_MOVE_LABELS = {QUEUED: "Hold", HELD: "Return", DONE: "Run again"}
_MOVE_HINTS = {
    QUEUED: "Keep this pass in the batch but skip it when processing runs.",
    HELD: "Put this pass back into the next batch.",
    DONE: "Reconstruct this pass again in the next batch.",
}

# File-name prefixes a GoPro uses for the second and later chapters of one
# recording (GX/GH/GL on HERO6 and up, GP on the older models).
_CHAPTER_PREFIXES = ("GX", "GH", "GL", "GP")

# One probed clip: its path, and the (duration_s, fps) the decoder reported.
_ProbedClip = tuple[str, tuple[float, float]]

# Button text per fix destination, so the strip names the place it goes rather
# than describing the journey. The header entry point uses the same words.
_FIX_ACTIONS = {FIX_SETUP: "Open Environment", FIX_SETTINGS: "Edit settings…"}

def _mmss(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def _one_sentence(text: str) -> str:
    """First line of an error, short enough for a tooltip or the status line."""
    stripped = (text or "").strip()
    return stripped.splitlines()[0][:200] if stripped else ""


def _diagnose_failure(text: str) -> str:
    """Turn a raw pipeline error into one plain sentence with what to try next.

    A diver cannot act on a Python traceback. Known signatures get advice; the
    fallback keeps the raw first line so nothing is hidden. The raw text is always
    available under "Copy error details".
    """
    low = (text or "").lower()
    if "out of memory" in low or "cuda" in low and "memory" in low:
        return (
            "Out of graphics memory. Retry with a smaller processing size, or a "
            "lower batch size."
        )
    if "no space left" in low or "disk" in low and "full" in low:
        return "Out of disk space. Free space and process the pass again."
    if "not found" in low and ("model" in low or "checkpoint" in low or ".pt" in low):
        return "A required model is not installed. Install it under Environment."
    if any(word in low for word in ("decode", "codec", "corrupt", "unreadable")):
        return "The video could not be read. Check the clip copied off the camera intact."
    return _one_sentence(text) or "The run failed. No cause was recorded."


def _median_pass_seconds() -> float | None:
    """What one pass has typically cost on this machine, or None with no history.

    Rough on purpose: the median of every recorded run, not of runs matching the
    current config, so a first-ever run says nothing rather than guessing.
    """
    import statistics

    from deepreefmap_gui.profiling.run_history import summarise_recorded_runs

    seconds = [r["run_seconds"] for r in summarise_recorded_runs() if r.get("run_seconds")]
    return statistics.median(seconds) if seconds else None


def _rough_batch_time(pass_count: int) -> str | None:
    """A plain "about N hours" for a batch that has not started yet."""
    median = _median_pass_seconds()
    if median is None:
        return None
    total = median * pass_count
    if total < 5400:
        return f"about {max(1, round(total / 60))} minutes"
    hours = round(total / 3600)
    return f"about {hours} hour{'' if hours == 1 else 's'}"


def _import_summary(file_name: str, queued: int, skipped: int, unmatched: int) -> str:
    """What a CSV import did, including the rows it could not place."""
    parts = [f"Queued {queued} pass(es) from {file_name}."]
    if skipped:
        parts.append(f"{skipped} video(s) could not be opened and were excluded.")
    if unmatched:
        parts.append(f"{unmatched} named a transect that is not yet planned.")
    return " ".join(parts)


def _pass_number(run_dir_name: str) -> int | None:
    """The `pNN` ordinal baked into a survey run-dir slug, if one is present."""
    for token in run_dir_name.split("__"):
        if len(token) > 1 and token[0] == "p" and token[1:].isdigit():
            return int(token[1:])
    return None


def _failed_pass_label(transect: Transect | None, run_dir_name: str) -> str:
    """Name a failed pass by transect and pass number, never by the run-dir slug."""
    name = transect.name if transect is not None else "Unassigned"
    number = _pass_number(run_dir_name)
    return f"{name} pass {number}" if number is not None else name


def _style_warning_combo(combo: QComboBox, *, ok: bool, filled: bool = True) -> None:
    """Mark a dropdown that needs a second look.

    ``filled`` is for the cell that stops the batch — an unassigned transect —
    which has to look wrong from across the room. The outlined variant is for a
    cell that is merely worth checking, and which may be right: filling every
    row of a genuinely one-way survey turns the table amber and says nothing.

    A per-widget stylesheet replaces the global QComboBox rule outright, so both
    variants restate the padding and radius they displace.
    """
    if ok:
        combo.setStyleSheet("")
        return
    background = f"background-color: {WARN_BG};" if filled else ""
    combo.setStyleSheet(
        f"QComboBox {{ {background} color: {WARN_TEXT};"
        f" border: 1px solid {WARN_BORDER}; border-radius: {RADIUS_SM}px;"
        " padding: 4px 8px; }"
        " QComboBox::drop-down { subcontrol-origin: padding;"
        " subcontrol-position: center right; border: none; width: 20px; }"
    )


def _probe_video(path: str) -> tuple[float, float] | None:
    """(duration_s, fps) via cv2, or None when the file cannot be decoded.

    Opening and measuring a 4 GB clip off an SD card takes long enough to freeze
    the window, which is why _add_video_paths hands this to a worker thread.
    """
    import cv2

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return None
    try:
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        fps = cap.get(cv2.CAP_PROP_FPS)
    finally:
        cap.release()
    if not fps or fps <= 0 or not frames or frames <= 0:
        return None
    return float(frames) / float(fps), float(fps)


def _clip_time(mtime: str | None) -> str:
    """When the clip was recorded, in local time.

    A GoPro's mtime is the moment recording stopped, which is what a diver
    matches against their slate.
    """
    if not mtime:
        return "time unknown"
    try:
        stamp = datetime.fromisoformat(mtime)
    except ValueError:
        return "time unknown"
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone()
    return stamp.strftime("%H:%M")


def _clip_length(duration_s: float | None) -> str:
    if not duration_s or duration_s <= 0:
        return "length unknown"
    total = int(round(duration_s))
    return f"{total}s" if total < 60 else f"{total // 60}m {total % 60:02d}s"


def _video_cell_text(videos: list[VideoAsset]) -> str:
    """Name, time and length: a card of GX01nnnn.MP4 files is otherwise unreadable."""
    first = videos[0]
    name = first.file_name
    if len(videos) > 1:
        name += f" +{len(videos) - 1} chapter{'' if len(videos) == 2 else 's'}"
    return f"{name} · {_clip_time(first.mtime)} · {_clip_length(_total_duration_s(videos))}"


def _total_duration_s(videos: list[VideoAsset]) -> float | None:
    """Length of the chapters played back to back, or None when any is unknown."""
    if any(video.duration_s is None for video in videos):
        return None
    return sum(video.duration_s or 0.0 for video in videos)


def _chapter_key(file_name: str) -> tuple[str, int] | None:
    """(recording, chapter) for a chaptered GoPro file, else None.

    A recording split at the camera's ~4 GB limit continues in GX02nnnn,
    GX03nnnn and so on, where nnnn names the recording and the middle pair is
    the chapter. Older HEROs open the same recording with GOPRnnnn and continue
    in GPnnnn.
    """
    stem = Path(file_name).stem.upper()
    if stem.startswith("GOPR") and stem[4:].isdigit():
        return stem[4:], 0
    if len(stem) != 8 or stem[:2] not in _CHAPTER_PREFIXES:
        return None
    chapter, recording = stem[2:4], stem[4:]
    if not chapter.isdigit() or not recording.isdigit():
        return None
    return recording, int(chapter)


def _group_chapters(probed: list[_ProbedClip]) -> list[list[_ProbedClip]]:
    """Split one add into passes, gathering the chapters of a recording into one.

    Grouping only spans the clips added together, which is how a card is read:
    select the lot, and a swim that overran 4 GB stays one pass.
    """
    groups: list[list[_ProbedClip]] = []
    by_recording: dict[str, int] = {}
    for path, result in probed:
        key = _chapter_key(Path(path).name)
        recording = key[0] if key is not None else None
        if recording is not None and recording in by_recording:
            groups[by_recording[recording]].append((path, result))
            continue
        if recording is not None:
            by_recording[recording] = len(groups)
        groups.append([(path, result)])
    for group in groups:
        group.sort(key=lambda item: _chapter_key(Path(item[0]).name) or ("", 0))
    return groups


@dataclass
class _PassRow:
    videos: list[VideoAsset]
    begin_s: float
    end_s: float
    direction: str = "forward"
    transect_id: uuid.UUID | None = None
    pass_id: uuid.UUID | None = None
    # Held back from processing, and kept that way in the database.
    held: bool = False
    # A pass that already succeeded and has been asked for again. Deliberately
    # not persisted: it says what this batch should do next, not what the pass is.
    requeued: bool = False

    @property
    def video(self) -> VideoAsset:
        """The first chapter, which is the pass's video identity."""
        return self.videos[0]

    def total_duration_s(self) -> float | None:
        return _total_duration_s(self.videos)


def _clip_sort_key(row: _PassRow) -> tuple[int, str]:
    """Order by recording time, with unreadable timestamps kept at the bottom."""
    return (1, "") if not row.video.mtime else (0, row.video.mtime)


@dataclass
class _SurveyJob:
    run: RunRecord
    pass_: TransectPass
    transect: Transect
    videos: list[VideoAsset]
    dir_name: str


class SimpleBatchMixin(MixinBase):
    """DeepReefMapWindow methods for the survey batch tab."""

    def _build_simple_run_page(self) -> QWidget:
        """Full-page Run section: the day's passes and the batch controls."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self._survey_rows = []
        # Table row -> index into _survey_rows, with None for a group heading.
        self._survey_table_index: list[int | None] = []
        self._survey_transects = []
        self._survey_batch = None
        self._survey_cancel_event = None
        self._survey_worker_running = False
        # Which job the worker is on, and the pass behind each job, so a percent
        # arriving from the pipeline can find the row it belongs to.
        self._survey_running_index: int | None = None
        self._survey_job_pass_ids: list[uuid.UUID] = []
        self._survey_pass_percent = 0
        self._reload_active_preset()

        layout.setSpacing(GUTTER)

        # Above everything, so a laptop that cannot run says so before the user
        # fills a queue with it. _recompute_survey_start owns its text.
        self._survey_not_ready = NotReadyStrip()
        self._survey_not_ready.action_clicked.connect(self._on_survey_fix_blocker)
        layout.addWidget(self._survey_not_ready)

        # Batch identity and the settings every pass in it will run under, as
        # one block: they answer the same question, "what is about to happen".
        header_card, header_layout = section_card()
        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        name_row.addWidget(QLabel("Batch"))
        self._survey_batch_name = QLineEdit(datetime.now().strftime("%Y-%m-%d"))  # noqa: DTZ005 (local time is intended: this is a user-facing default name)
        name_row.addWidget(self._survey_batch_name, 1)
        self._survey_new_batch_btn = QPushButton("New")
        self._survey_new_batch_btn.setToolTip(
            "Start a new batch. The current one is retained in the database."
        )
        self._survey_new_batch_btn.clicked.connect(self._on_survey_new_batch)
        name_row.addWidget(self._survey_new_batch_btn)
        header_layout.addLayout(name_row)

        preset_row = QHBoxLayout()
        preset_row.setSpacing(6)
        self._survey_preset_label = QLabel(self._survey_preset_summary())
        self._survey_preset_label.setWordWrap(True)
        self._survey_preset_label.setStyleSheet(f"color: {TEXT_MUTED};")
        preset_row.addWidget(self._survey_preset_label, 1)
        self._survey_settings_btn = QPushButton("Edit settings…")
        self._survey_settings_btn.setProperty("quiet", "true")
        self._survey_settings_btn.setToolTip("Change any run setting for this batch.")
        self._survey_settings_btn.clicked.connect(self._on_edit_run_settings)
        preset_row.addWidget(self._survey_settings_btn)
        # Beside the name of the settings, because that is where you look when you
        # want to know whether this machine is still on the standard.
        self._survey_restore_btn = QPushButton("Restore standard settings")
        self._survey_restore_btn.setProperty("quiet", "true")
        self._survey_restore_btn.setToolTip(
            "Discard this machine's changes and return to the standard settings."
        )
        self._survey_restore_btn.clicked.connect(self._restore_standard_settings)
        preset_row.addWidget(self._survey_restore_btn)
        self._survey_audit_btn = QPushButton("Settings history…")
        self._survey_audit_btn.setProperty("quiet", "true")
        self._survey_audit_btn.setToolTip("Which settings every processed run actually used.")
        self._survey_audit_btn.clicked.connect(self._on_show_config_audit)
        preset_row.addWidget(self._survey_audit_btn)
        header_layout.addLayout(preset_row)
        layout.addWidget(header_card)

        # Where the run reports itself now that the Run step has no viewer beside
        # it. Hidden until a batch starts: an idle card is a row of blanks.
        self._batch_progress = BatchProgressCard()
        self._batch_progress.pass_percent_changed.connect(self._on_pass_percent)
        self._batch_progress.setVisible(False)
        layout.addWidget(self._batch_progress)

        self._survey_pass_table = QTableWidget(0, 6)
        self._survey_pass_table.setHorizontalHeaderLabels(
            ["Video", "Transect", "Direction", "Trim", "Status", ""]
        )
        self._survey_pass_table.verticalHeader().setVisible(False)
        self._survey_pass_table.verticalHeader().setDefaultSectionSize(34)
        self._survey_pass_table.setShowGrid(False)
        self._survey_pass_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        # A survey day is dozens of clips over a handful of transects, so
        # assigning is a bulk action: select the run of rows, assign them once.
        self._survey_pass_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self._survey_pass_table.setItemDelegateForColumn(_COL_STATUS, StatusPillDelegate(self))
        self._survey_pass_table.itemSelectionChanged.connect(self._recompute_row_actions)
        # Seeing the result is what you actually want after processing, so the
        # row you processed opens it. Only the Video and Status columns get the
        # signal; the three between them hold cell widgets that eat the click.
        self._survey_pass_table.cellDoubleClicked.connect(self._on_survey_pass_activated)
        # A failed pass keeps its error on the row (tooltip). Right-click copies
        # the full text so it can be pasted into a bug report.
        self._survey_pass_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._survey_pass_table.customContextMenuRequested.connect(self._on_survey_pass_menu)
        h_header = self._survey_pass_table.horizontalHeader()
        h_header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        h_header.setSectionResizeMode(_COL_VIDEO, QHeaderView.ResizeMode.Stretch)
        # The video name stretches; the rest hold widgets that must not clip, so
        # they get widths that fit a transect name and a status pill.
        for column, width in (
            (_COL_TRANSECT, 170),
            (_COL_DIRECTION, 120),
            (_COL_TRIM, 110),
            (_COL_STATUS, 110),
            (_COL_ACTION, 110),
        ):
            self._survey_pass_table.setColumnWidth(column, width)
        # Dropping video files onto the table queues them, handled centrally by
        # the browser's eventFilter (DataManagerMixin) so all three drop targets
        # share one path.
        self._survey_pass_table.setAcceptDrops(True)
        self._survey_pass_table.installEventFilter(self)

        self._survey_table_stack = QStackedWidget()
        self._survey_table_stack.addWidget(self._survey_pass_table)
        self._survey_table_stack.addWidget(
            EmptyState(
                "No videos in this batch",
                "Add videos… to queue the passes you want processed.",
            )
        )
        passes_card, passes_layout = section_card("Passes")
        passes_layout.addWidget(self._survey_table_stack, 1)
        # How much of the batch is behind you. Sits with the table rather than in
        # the status bar because it is what you read before deciding to stop.
        self._survey_standing_label = QLabel("")
        self._survey_standing_label.setWordWrap(True)
        self._survey_standing_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self._survey_standing_label.setVisible(False)
        passes_layout.addWidget(self._survey_standing_label)
        # Outcome of the last batch, kept on the page rather than written to the
        # status bar, which the next thing to happen overwrites.
        self._survey_summary_label = QLabel("")
        self._survey_summary_label.setWordWrap(True)
        self._survey_summary_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self._survey_summary_label.setVisible(False)
        passes_layout.addWidget(self._survey_summary_label)
        layout.addWidget(passes_card, 1)

        # Two rows: what you do to the whole batch, then what you do to one pass.
        bulk_buttons = QHBoxLayout()
        bulk_buttons.setSpacing(6)
        self._survey_add_btn = QPushButton("Add videos…")
        self._survey_add_btn.clicked.connect(self._on_survey_add_videos)
        self._survey_assign_btn = QPushButton("Assign selected to…")
        self._survey_assign_btn.setToolTip(
            "Set the transect on every selected row at once. Shift-click or "
            "Ctrl-click to select a run of rows."
        )
        self._survey_assign_btn.clicked.connect(self._on_survey_assign_selected)
        # Named for the action it modifies rather than for what it does: on its
        # own "Alternate direction" says nothing about when it takes effect.
        self._survey_alternate_check = QCheckBox("…alternating forward/reverse")
        self._survey_alternate_check.setToolTip(
            "Changes what Assign does: it sets forward, reverse, forward… down "
            "the selected rows, for a transect swum out and back."
        )
        self._survey_sort_btn = QPushButton("Sort by time")
        self._survey_sort_btn.setProperty("quiet", "true")
        self._survey_sort_btn.setToolTip("Order the rows by when each clip was recorded.")
        self._survey_sort_btn.clicked.connect(self._on_survey_sort_by_time)
        for widget in (
            self._survey_add_btn,
            self._survey_assign_btn,
            self._survey_alternate_check,
            self._survey_sort_btn,
        ):
            bulk_buttons.addWidget(widget)
        bulk_buttons.addStretch(1)
        layout.addLayout(bulk_buttons)

        row_buttons = QHBoxLayout()
        row_buttons.setSpacing(6)
        # A CSV of the day's dives is a queue too, so it fills this one rather
        # than starting a second kind of batch somewhere else.
        self._survey_import_btn = QPushButton("Import queue from CSV…")
        self._survey_import_btn.setToolTip(
            "Queue passes from a spreadsheet. Columns: videos, timestamps "
            "(begin-end seconds), transect_length, crop_width, and an optional "
            "transect naming a planned transect."
        )
        self._survey_import_btn.clicked.connect(self._on_survey_import_csv)
        self._survey_split_btn = QPushButton("Add another pass from this clip")
        self._survey_split_btn.setToolTip(
            "One recording can hold several swims. This copies the selected row "
            "so you can trim the second swim out of the same file."
        )
        self._survey_split_btn.clicked.connect(self._on_survey_split_pass)
        self._survey_remove_btn = QPushButton("Remove from batch")
        self._survey_remove_btn.setToolTip(
            "Take the selected pass out of this batch. The video file is left alone."
        )
        self._survey_remove_btn.clicked.connect(self._on_survey_remove_pass)
        for btn in (
            self._survey_import_btn,
            self._survey_split_btn,
            self._survey_remove_btn,
        ):
            row_buttons.addWidget(btn)
        row_buttons.addStretch(1)
        layout.addLayout(row_buttons)
        self._recompute_row_actions()

        # The run button is the Run step's forward action, so the wizard footer
        # hosts it rather than this page. Stopping is the bottom bar's job.
        self._survey_start_btn = QPushButton("Next: Process")
        self._survey_start_btn.setProperty("cta", "true")
        self._survey_start_btn.setEnabled(False)
        self._survey_start_btn.clicked.connect(self._on_survey_start)
        return page

    def _set_batch_editing_enabled(self, enabled: bool) -> None:
        """Freeze what a batch is made of while that batch is being processed.

        Assembling a batch and watching it run share this page, and only the
        second is available once the worker starts: a transect or trim edited
        mid-run would never reach the pass in flight, leaving the table claiming
        something the run did not do.
        """
        for widget in (
            self._survey_batch_name,
            self._survey_new_batch_btn,
            self._survey_settings_btn,
            self._survey_audit_btn,
            self._survey_add_btn,
            self._survey_import_btn,
        ):
            widget.setEnabled(enabled)
        for table_row, model_index in enumerate(self._survey_table_index):
            if model_index is None:
                continue
            for column in (_COL_TRANSECT, _COL_DIRECTION, _COL_TRIM, _COL_ACTION):
                cell = self._survey_pass_table.cellWidget(table_row, column)
                if cell is not None:
                    cell.setEnabled(enabled)

    def _recompute_row_actions(self) -> None:
        """Split and remove act on a selected row, so they say when there isn't one."""
        running = self._survey_worker_running
        selected = self._model_index(self._survey_pass_table.currentRow()) is not None
        for btn in (self._survey_split_btn, self._survey_remove_btn):
            btn.setEnabled(selected and not running)
        # Assigning needs both a selection and somewhere to assign it to.
        can_assign = bool(self._selected_survey_rows()) and bool(self._survey_transects)
        self._survey_assign_btn.setEnabled(can_assign and not running)
        # Greyed out with Assign, so the two read as one control rather than as a
        # button and an unrelated checkbox beside it.
        self._survey_alternate_check.setEnabled(can_assign and not running)
        self._survey_sort_btn.setEnabled(len(self._survey_rows) > 1 and not running)
        self._survey_table_stack.setCurrentIndex(0 if self._survey_rows else 1)
        self._set_batch_editing_enabled(not running)

    def _selected_survey_rows(self) -> list[int]:
        """Indices of every selected row, in table order.

        Read from the selection rather than currentRow(): the bulk actions are
        the reason the table is multi-select in the first place.
        """
        table_rows = {index.row() for index in self._survey_pass_table.selectedIndexes()}
        models = {self._model_index(row) for row in table_rows}
        return sorted(index for index in models if index is not None)

    def _on_survey_pass_activated(self, table_row: int, _column: int) -> None:
        """Open the run this pass produced, over in Browse.

        The point cloud lives in Browse and nowhere else, so activating a finished
        row travels there rather than opening a viewer beside the queue.
        """
        if self._run_in_flight():
            self._status_label.setText("Unavailable while processing.")
            return
        index = self._model_index(table_row)
        if index is None:
            return
        row = self._survey_rows[index]
        if row.pass_id is None:
            self._status_label.setText("Pass not yet processed.")
            return
        runs = self._survey_store().runs_for_pass(row.pass_id)
        # The last run that actually succeeded, not simply the last one: a pass
        # retried after a failure would otherwise open a directory with no
        # manifest in it.
        succeeded = [run for run in runs if run.status == "succeeded"]
        if not succeeded:
            last = runs[-1] if runs else None
            if last is not None and last.status == "failed":
                self._status_label.setText(f"Pass failed: {_diagnose_failure(last.error)}")
            else:
                self._status_label.setText("Pass has no successful run to open.")
            return
        run_dir = Path(self._out_root_input.text()).expanduser() / succeeded[-1].run_dir_name
        if not run_dir.is_dir():
            self._status_label.setText(f"Run folder is missing: {run_dir.name}")
            return
        # Travel first: _update_work_area only reveals the viewer in Browse, so
        # loading from the Run step would leave the cloud with nowhere to render.
        self._go_to_step("browse")
        self._auto_load_run(run_dir)

    def _on_edit_run_settings(self) -> None:
        """Open the real run form in a dialog, and keep the edit only on OK."""
        from deepreefmap_gui.simple.settings_dialog import RunSettingsDialog

        if self._survey_worker_running:
            self._status_label.setText("Unavailable while processing.")
            return
        per_run = [
            self._video_row_widget,
            self._range_row_widget,
            self._run_name_widget,
            self._transect_length_widget,
        ]
        # The dialog edits the live form widgets, so abandoning the edit means
        # putting the values back here rather than dropping a pending copy.
        before = self._snapshot_form_settings()
        dialog = RunSettingsDialog(self, self._setup_page, per_run)
        self._settings_dialog_open = True
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            dialog.restore_form()
            self._settings_dialog_open = False
        if accepted:
            self._adopt_form_as_preset()
        else:
            # Cancel, Escape and the window's close button all land here.
            self._restore_form_settings(before)
        self._recompute_survey_start()

    def _survey_preset_summary(self) -> str:
        """Name the settings this batch will run under, then say how they differ.

        The configuration has an identity worth citing in a report, so the name
        and version lead. The technical line still follows, read from the form
        rather than the preset dict: the batch runs from _collect_run_settings(),
        so the label must describe the values the run will actually use.
        """
        if self._survey_preset is None or self._active_preset is None:
            return "Settings could not be loaded. Correct the settings file to run batches."
        org = self._active_preset.org
        lines = [f"Settings: {org.label}"]
        if org.locked:
            lines[0] += ", set by your organisation"
        # Split by what this machine is allowed to keep rather than by what is
        # already on disk: the answer must not change depending on whether the
        # settings dialog has closed yet. The page carries it, not the status
        # bar, because the next thing to happen overwrites the status bar.
        deviations = self._survey_deviations()
        machine = [key for key in deviations if key in MACHINE_OVERRIDABLE_KEYS]
        organisation = [key for key in deviations if key not in MACHINE_OVERRIDABLE_KEYS]
        if machine:
            lines.append(f"Changed on this machine: {describe_keys(machine)}.")
        if organisation:
            lines.append(
                f"Changed for this batch only: {describe_keys(organisation)}."
                f" {org.name} sets these, so they go back to standard next launch."
            )
        s = self._collect_run_settings()
        lines.append(
            f"{s['segmentation_name']} + {s['mapping_name']}"
            f" @ {s['fps']} fps, {s['camera_profile_name']}"
        )
        return "\n".join(lines)

    def _on_show_config_audit(self) -> None:
        """List what every processed run under this output root actually used."""
        from deepreefmap_gui.simple.config_audit_dialog import ConfigAuditDialog
        from deepreefmap_gui.survey.config_audit import audit_out_root

        if self._active_preset is None:
            self._status_label.setText("The settings could not be read.")
            return
        org = self._active_preset.org
        rows = audit_out_root(Path(self._out_root_input.text()).expanduser(), org)
        ConfigAuditDialog(self, rows, org).exec()

    # --- Batch and table state ---

    def _ensure_survey_batch(self) -> SurveyBatch:
        if self._survey_batch is None:
            name = self._survey_batch_name.text().strip() or datetime.now().strftime("%Y-%m-%d")  # noqa: DTZ005 (local time is intended: this is a user-facing default name)
            batch = SurveyBatch(name=name)
            # Name the configuration on the batch too, so a folder rebuilt from
            # manifests alone still knows which settings the day was run under.
            if self._active_preset is not None:
                batch.preset_name = self._active_preset.org.name
            self._survey_store().add_batch(batch)
            self._survey_batch = batch
        return self._survey_batch

    def _on_survey_new_batch(self) -> None:
        self._survey_batch = None
        self._survey_rows = []
        self._survey_pass_table.setRowCount(0)
        self._survey_batch_name.setText(datetime.now().strftime("%Y-%m-%d"))  # noqa: DTZ005 (local time is intended: this is a user-facing default name)
        self._recompute_survey_start()

    def _refresh_survey_batch_tab(self) -> None:
        """Adopt the most recent batch from the store and rebuild the pass table."""
        store = self._survey_store()
        self._survey_transects = store.list_transects()
        if self._survey_batch is None:
            batches = store.list_batches()
            if batches:
                self._survey_batch = batches[0]
                self._survey_batch_name.setText(batches[0].name)
        self._survey_rows = []
        self._survey_pass_table.setRowCount(0)
        if self._survey_batch is not None:
            for pass_ in store.list_passes(batch_id=self._survey_batch.id):
                # A chapter the library has lost is dropped rather than faked, so
                # the row cannot claim a file the run would fail to open.
                videos = [store.get_video(video_id) for video_id in pass_.video_ids()]
                if any(video is None for video in videos):
                    continue
                self._survey_rows.append(_PassRow(
                    videos=[video for video in videos if video is not None],
                    begin_s=pass_.begin_s,
                    end_s=pass_.end_s,
                    direction=pass_.direction,
                    transect_id=pass_.transect_id,
                    pass_id=pass_.id,
                    held=pass_.held,
                ))
        self._rebuild_survey_table()
        self._recompute_survey_start()

    def _refresh_survey_transect_combos(self) -> None:
        self._survey_transects = self._survey_store().list_transects()
        for index, row in enumerate(self._survey_rows):
            combo = self._survey_pass_table.cellWidget(
                self._table_row_of(index), _COL_TRANSECT
            )
            if isinstance(combo, QComboBox):
                self._fill_transect_combo(combo, row.transect_id)

    def _fill_transect_combo(self, combo: QComboBox, selected: uuid.UUID | None) -> None:
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Not assigned yet", None)
        for transect in self._survey_transects:
            combo.addItem(transect.name, str(transect.id))
            if selected is not None and transect.id == selected:
                combo.setCurrentIndex(combo.count() - 1)
        combo.blockSignals(False)
        _style_warning_combo(combo, ok=combo.currentData() is not None)

    # --- Table shape ---

    def _survey_row_states(self) -> list[str]:
        """Which group each row belongs to, from one query rather than per row."""
        if not self._survey_rows:
            return []
        succeeded = self._survey_store().succeeded_pass_ids()
        states = []
        for row in self._survey_rows:
            if row.held:
                states.append(HELD)
            elif row.pass_id is not None and row.pass_id in succeeded and not row.requeued:
                states.append(DONE)
            else:
                states.append(QUEUED)
        return states

    def _model_index(self, table_row: int) -> int | None:
        """The pass behind a table row, or None for a group heading."""
        if not 0 <= table_row < len(self._survey_table_index):
            return None
        return self._survey_table_index[table_row]

    def _table_row_of(self, model_index: int) -> int:
        """Where a pass currently sits in the table; -1 if it is not shown."""
        try:
            return self._survey_table_index.index(model_index)
        except ValueError:
            return -1

    def _rebuild_survey_table(self) -> None:
        """Repaint the whole table, grouped by what the next batch will do.

        A batch left running overnight is read at a glance from the groups: what
        is still to run, what was deliberately held back, and what is finished.
        """
        table = self._survey_pass_table
        keep = set(self._selected_survey_rows())
        current = self._model_index(table.currentRow())
        table.clearSpans()
        table.setRowCount(0)
        self._survey_table_index = []
        states = self._survey_row_states()
        for state in (QUEUED, HELD, DONE):
            members = [index for index, value in enumerate(states) if value == state]
            # Only the groups that have something in them: an empty "Held back"
            # heading is a permanent reminder of a feature, not information.
            if not members:
                continue
            self._append_group_heading(state, len(members))
            for index in members:
                self._append_survey_cells(index, state)
        self._refresh_survey_pass_statuses()
        selection = table.selectionModel()
        for model_index in keep:
            row = self._table_row_of(model_index)
            if row >= 0:
                table.selectRow(row)
        if current is not None and self._table_row_of(current) >= 0 and selection is not None:
            table.setCurrentCell(self._table_row_of(current), _COL_VIDEO)

    def _append_group_heading(self, state: str, count: int) -> None:
        table = self._survey_pass_table
        index = table.rowCount()
        table.insertRow(index)
        self._survey_table_index.append(None)
        item = QTableWidgetItem(f"{_GROUP_TITLES[state]}  ({count})")
        item.setToolTip(_GROUP_HINTS[state])
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        font = item.font()
        font.setWeight(QFont.Weight.DemiBold)
        item.setFont(font)
        item.setForeground(QColor(TEXT_MUTED if state == QUEUED else TEXT_DIM))
        table.setItem(index, 0, item)
        table.setSpan(index, 0, 1, table.columnCount())

    def _append_survey_cells(self, model_index: int, state: str) -> None:
        row = self._survey_rows[model_index]
        table = self._survey_pass_table
        index = table.rowCount()
        table.insertRow(index)
        self._survey_table_index.append(model_index)

        video_item = QTableWidgetItem(_video_cell_text(row.videos))
        video_item.setToolTip("\n".join(video.path for video in row.videos))
        video_item.setFlags(video_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(index, _COL_VIDEO, video_item)

        transect_combo = QComboBox()
        self._fill_transect_combo(transect_combo, row.transect_id)
        transect_combo.currentIndexChanged.connect(
            partial(self._on_survey_row_transect, row, transect_combo)
        )
        table.setCellWidget(index, _COL_TRANSECT, transect_combo)

        direction_combo = QComboBox()
        direction_combo.addItems(list(PASS_DIRECTIONS))
        direction_combo.setCurrentText(row.direction)
        direction_combo.currentTextChanged.connect(partial(self._on_survey_row_direction, row))
        table.setCellWidget(index, _COL_DIRECTION, direction_combo)

        trim_btn = QPushButton(f"{_mmss(row.begin_s)}-{_mmss(row.end_s)}")
        # Quiet: this is an editable cell you can click, not a form action.
        trim_btn.setProperty("quiet", "true")
        trim_btn.setToolTip("Click to trim the section of the recording this pass covers.")
        trim_btn.clicked.connect(partial(self._on_survey_row_trim, row, trim_btn))
        table.setCellWidget(index, _COL_TRIM, trim_btn)

        status_item = QTableWidgetItem("")
        status_item.setFlags(status_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(index, _COL_STATUS, status_item)

        move_btn = QPushButton(_MOVE_LABELS[state])
        move_btn.setProperty("quiet", "true")
        move_btn.setToolTip(_MOVE_HINTS[state])
        move_btn.setEnabled(not self._survey_worker_running)
        move_btn.clicked.connect(
            partial(self._move_rows, [model_index], state == QUEUED)
        )
        table.setCellWidget(index, _COL_ACTION, move_btn)

    def _append_survey_row(self, row: _PassRow) -> None:
        """Add a pass to the batch and put it in the group it belongs to."""
        self._survey_rows.append(row)
        self._rebuild_survey_table()

    def _refresh_row_widgets(self, index: int) -> None:
        """Repaint one row's cells from the row, without re-entering their slots.

        A bulk action writes the model first, so the widget signals would
        persist and re-gate once per row on top of the pass the action already
        wrote.
        """
        row = self._survey_rows[index]
        table = self._survey_pass_table
        table_row = self._table_row_of(index)
        if table_row < 0:
            return
        combo = table.cellWidget(table_row, _COL_TRANSECT)
        if isinstance(combo, QComboBox):
            self._fill_transect_combo(combo, row.transect_id)
        direction = table.cellWidget(table_row, _COL_DIRECTION)
        if isinstance(direction, QComboBox):
            direction.blockSignals(True)
            direction.setCurrentText(row.direction)
            direction.blockSignals(False)
        trim = table.cellWidget(table_row, _COL_TRIM)
        if isinstance(trim, QPushButton):
            trim.setText(f"{_mmss(row.begin_s)}-{_mmss(row.end_s)}")

    # --- Row actions ---

    def _on_survey_add_videos(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Add videos",
            str(self._settings.value("last_video_path", "")),
            "Videos (*.mp4 *.mov *.avi *.mkv);;All files (*)",
        )
        self._add_video_paths(paths)

    def _add_video_paths(self, paths: list[str]) -> None:
        """Queue clips as passes, decoding them off the GUI thread.

        One card of GoPro clips is dozens of multi-gigabyte files and cv2 has to
        open every one to learn its length, so probing on the GUI thread froze
        the window for as long as the card took to read. The rows appear when the
        worker reports back.
        """
        if not paths:
            return
        # Dropping onto the table bypasses the greyed-out Add videos button, so
        # the freeze is enforced here as well as on the controls.
        if self._survey_worker_running:
            self._status_label.setText("Unavailable while processing.")
            return
        self._status_label.setText(f"Reading {len(paths)} video(s)…")

        def worker() -> None:
            probed = [(path, _probe_video(path)) for path in paths]
            # Widgets are off limits here; the Signal hands over to the GUI thread.
            self._sig_videos_probed.emit(probed)

        self._video_probe_thread = threading.Thread(target=worker, daemon=True)
        self._video_probe_thread.start()

    def _on_videos_probed(self, probed: list) -> None:
        """Append one row per readable recording, then report the batch once."""
        readable = [(path, result) for path, result in probed if result is not None]
        groups = _group_chapters(readable)
        for group in groups:
            self._add_pass_for_chapters(group)
        self._recompute_survey_start()
        parts = []
        if groups:
            self._refresh_data_manager()
            # Passes and videos are counted separately: a chaptered recording is
            # several files and one pass.
            parts.append(
                f"Queued {len(groups)} pass{'' if len(groups) == 1 else 'es'}"
                f" from {len(readable)} video{'' if len(readable) == 1 else 's'}."
            )
        skipped = len(probed) - len(readable)
        if skipped:
            parts.append(f"Skipped {skipped} unreadable video(s).")
        if parts:
            self._status_label.setText(" ".join(parts))

    def _record_video(self, path: str) -> VideoAsset | None:
        """Probe a clip and record it in the store. None when it will not decode.

        The CSV importer records one clip per row; the button and drag-and-drop go
        through _add_pass_for_chapters, which groups a recording's chapters.
        """
        probed = _probe_video(path)
        if probed is None:
            return None
        duration_s, fps = probed
        asset = VideoAsset.from_path(Path(path))
        asset.duration_s = duration_s
        asset.fps = fps
        return self._survey_store().upsert_video(asset)

    def _only_transect_id(self) -> uuid.UUID | None:
        """With exactly one transect the choice is unambiguous, so preselect it.

        Never copy the previous row's transect: a silently wrong assignment is
        worse than a loud empty one.
        """
        return self._survey_transects[0].id if len(self._survey_transects) == 1 else None

    def _add_video_path(self, path: str, probed: tuple[float, float] | None = None) -> bool:
        """Queue one probed video as a fresh pass. False when it will not decode.

        Shared by the Add videos button and the browser's drag-and-drop, so a
        dropped clip and a picked one land the same row. ``probed`` is the worker
        thread's measurement, so this only decodes when a caller has none.
        """
        if probed is None:
            probed = _probe_video(path)
        if probed is None:
            return False
        self._add_pass_for_chapters([(path, probed)])
        return True

    def _add_pass_for_chapters(self, group: list[_ProbedClip]) -> None:
        """Queue one recording, however many chapters it arrived in, as one pass."""
        store = self._survey_store()
        videos: list[VideoAsset] = []
        seen: set[uuid.UUID] = set()
        for path, (duration_s, fps) in group:
            asset = VideoAsset.from_path(Path(path))
            asset.duration_s = duration_s
            asset.fps = fps
            asset = store.upsert_video(asset)
            # The library is keyed by content hash, so the same recording picked
            # from two folders is one chapter, not a chapter played twice.
            if asset.id in seen:
                continue
            seen.add(asset.id)
            videos.append(asset)
        # With exactly one transect the choice is unambiguous, so preselect it.
        # Never copy the previous row's transect: a silently wrong assignment is
        # worse than a loud empty one.
        only = self._survey_transects[0].id if len(self._survey_transects) == 1 else None
        # The pass covers the chapters played back to back, which is how the
        # pipeline reads a list of videos.
        self._append_survey_row(_PassRow(
            videos=videos,
            begin_s=0.0,
            end_s=sum(video.duration_s or 0.0 for video in videos),
            transect_id=only,
        ))
        if only is not None:
            self._persist_survey_row(self._survey_rows[-1])

    def _on_survey_assign_selected(self) -> None:
        """Offer the transect list for every selected row at once."""
        indices = self._selected_survey_rows()
        if not indices:
            self._status_label.setText("Select the rows you want to assign first.")
            return
        if not self._survey_transects:
            self._status_label.setText("Add a transect in Plan before assigning passes.")
            return
        menu = QMenu(self)
        for transect in self._survey_transects:
            menu.addAction(
                transect.name, partial(self._assign_rows_to_transect, indices, transect.id)
            )
        button = self._survey_assign_btn
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def _assign_rows_to_transect(self, indices: list[int], transect_id: uuid.UUID) -> None:
        """Set one transect across many rows, writing each pass and gating once."""
        alternate = self._survey_alternate_check.isChecked()
        assigned = 0
        for position, index in enumerate(indices):
            if not 0 <= index < len(self._survey_rows):
                continue
            row = self._survey_rows[index]
            row.transect_id = transect_id
            if alternate:
                row.direction = PASS_DIRECTIONS[position % len(PASS_DIRECTIONS)]
            self._refresh_row_widgets(index)
            self._write_survey_row(row)
            assigned += 1
        self._recompute_survey_start()
        name = next((t.name for t in self._survey_transects if t.id == transect_id), "")
        self._status_label.setText(
            f"Assigned {assigned} pass{'' if assigned == 1 else 'es'} to {name}."
        )

    def _on_survey_sort_by_time(self) -> None:
        """Reorder the rows by recording time, so the day reads in the order it happened."""
        ordered = sorted(self._survey_rows, key=_clip_sort_key)
        if ordered == self._survey_rows:
            return
        self._survey_rows = ordered
        self._rebuild_survey_table()
        self._recompute_survey_start()

    def _on_survey_import_csv(self) -> None:
        """Queue a CSV of passes, so a spreadsheet and this table are one queue.

        The columns are the batch CSV's, plus an optional `transect` naming a
        planned transect: a row that names one lands assigned, and a row that does
        not lands like a dropped video, waiting for one. Per-row transect_length
        and crop_width are ignored here, because a pass takes its length from the
        transect it belongs to and its crop width from the run settings.
        """
        from deepreefmap_gui.form.batch import load_batch_csv

        if self._survey_worker_running:
            self._status_label.setText("Unavailable while processing.")
            return
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Import passes from CSV",
            str(self._settings.value("last_video_path", "")),
            "CSV files (*.csv);;All files (*)",
        )
        if not path_str:
            return
        try:
            jobs = load_batch_csv(Path(path_str))
        except (OSError, ValueError) as exc:
            self._status_label.setText(f"Nothing imported: {exc}")
            logger.warning("Could not import %s: %s", path_str, exc)
            return

        by_name = {t.name.strip().lower(): t.id for t in self._survey_transects}
        queued = skipped = unmatched = 0
        for job in jobs:
            asset = self._record_video(job.video)
            if asset is None:
                skipped += 1
                continue
            named = by_name.get(job.transect.lower()) if job.transect else None
            if job.transect and named is None:
                unmatched += 1
            duration_s = asset.duration_s or 0.0
            begin_s = min(max(job.begin_s or 0.0, 0.0), duration_s)
            end_s = min(job.end_s, duration_s) if job.end_s else duration_s
            row = _PassRow(
                videos=[asset],
                begin_s=begin_s,
                end_s=max(end_s, begin_s),
                transect_id=named or self._only_transect_id(),
            )
            self._append_survey_row(row)
            if row.transect_id is not None:
                self._persist_survey_row(row)
            queued += 1
        self._recompute_survey_start()
        self._status_label.setText(
            _import_summary(Path(path_str).name, queued, skipped, unmatched)
        )

    def _on_survey_split_pass(self) -> None:
        index = self._model_index(self._survey_pass_table.currentRow())
        if index is None:
            return
        source = self._survey_rows[index]
        self._append_survey_row(_PassRow(
            videos=list(source.videos),
            begin_s=source.begin_s,
            end_s=source.end_s,
            direction=source.direction,
            transect_id=source.transect_id,
        ))
        row = self._survey_rows[-1]
        self._persist_survey_row(row)

    def _on_survey_remove_pass(self) -> None:
        index = self._model_index(self._survey_pass_table.currentRow())
        if index is None:
            return
        row = self._survey_rows[index]
        if row.pass_id is not None:
            try:
                self._survey_store().delete_pass(row.pass_id)
            except sqlite3.IntegrityError:
                self._status_label.setText("Pass has recorded runs and cannot be removed.")
                return
        self._survey_rows.pop(index)
        self._rebuild_survey_table()
        self._recompute_survey_start()

    # --- Holding a pass back ---

    def _move_rows(self, indices: list[int], hold: bool) -> None:
        """Move a selection between the batch and the held group."""
        if self._survey_worker_running:
            self._status_label.setText("Unavailable while processing.")
            return
        states = self._survey_row_states()
        moved = 0
        for index in indices:
            if not 0 <= index < len(self._survey_rows):
                continue
            row = self._survey_rows[index]
            if hold:
                if states[index] == HELD:
                    continue
                row.held = True
                row.requeued = False
            else:
                if states[index] == QUEUED:
                    continue
                row.held = False
                # A pass that already succeeded needs saying so explicitly, or
                # the group it just left would take it straight back.
                row.requeued = states[index] == DONE
            self._write_survey_row(row)
            moved += 1
        if not moved:
            return
        self._rebuild_survey_table()
        self._recompute_survey_start()
        verb = "Held back" if hold else "Returned to the batch"
        self._status_label.setText(f"{verb} {moved} pass{'' if moved == 1 else 'es'}.")

    def _on_survey_row_transect(self, row: _PassRow, combo: QComboBox, _index: int) -> None:
        data = combo.currentData()
        row.transect_id = uuid.UUID(data) if data else None
        _style_warning_combo(combo, ok=row.transect_id is not None)
        self._persist_survey_row(row)

    def _on_survey_row_direction(self, row: _PassRow, direction: str) -> None:
        row.direction = direction
        self._persist_survey_row(row)

    def _on_survey_row_trim(self, row: _PassRow, button: QPushButton) -> None:
        # The range spans every chapter, so the slider runs the whole swim. The
        # preview only decodes the first file, so it holds its last frame beyond
        # that: previewing across chapters needs the dialog to switch captures.
        duration_s = row.total_duration_s() or row.end_s
        dialog = VideoScrubDialog(row.video.path, duration_s, row.begin_s, row.end_s, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        row.begin_s, row.end_s = dialog.time_range()
        button.setText(f"{_mmss(row.begin_s)}-{_mmss(row.end_s)}")
        self._persist_survey_row(row)
        self._offer_trim_to_transect(row)

    def _offer_trim_to_transect(self, source: _PassRow) -> None:
        """Ask whether one trim covers every pass of the same transect.

        Passes of one transect are usually the same swim filmed the same way, so
        the tape-in and tape-out cuts repeat. Only ask when there are siblings to
        apply it to.
        """
        if source.transect_id is None:
            return
        siblings = [
            index
            for index, row in enumerate(self._survey_rows)
            if row is not source and row.transect_id == source.transect_id
        ]
        if not siblings:
            return
        name = next(
            (t.name for t in self._survey_transects if t.id == source.transect_id), "this transect"
        )
        answer = QMessageBox.question(
            self,
            "Apply to all rows?",
            f"Apply {_mmss(source.begin_s)}-{_mmss(source.end_s)} to all "
            f"{len(siblings) + 1} passes of {name}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._apply_trim_to_rows(siblings, source.begin_s, source.end_s)

    def _apply_trim_to_rows(self, indices: list[int], begin_s: float, end_s: float) -> None:
        """Write one time range across many rows, skipping clips too short to hold it."""
        applied, skipped = 0, 0
        for index in indices:
            row = self._survey_rows[index]
            duration_s = row.total_duration_s()
            # A shorter clip keeps the same start and simply runs to its own end.
            limit = end_s if duration_s is None else min(end_s, duration_s)
            if limit <= begin_s:
                skipped += 1
                continue
            row.begin_s, row.end_s = begin_s, limit
            self._refresh_row_widgets(index)
            self._write_survey_row(row)
            applied += 1
        self._recompute_survey_start()
        message = f"Trimmed {applied + 1} pass{'' if applied == 0 else 'es'} to the same range."
        if skipped:
            message += f" {skipped} clip(s) were too short and kept their own range."
        self._status_label.setText(message)

    def _persist_survey_row(self, row: _PassRow) -> None:
        self._write_survey_row(row)
        self._recompute_survey_start()

    def _write_survey_row(self, row: _PassRow) -> None:
        """Insert or update this row's pass. An unassigned row has no pass yet.

        Separate from _persist_survey_row so a bulk action can write every row it
        touches and rebuild the gate once at the end rather than per row.
        """
        if row.transect_id is None:
            return
        store = self._survey_store()
        batch = self._ensure_survey_batch()
        extra_video_ids = [video.id for video in row.videos[1:]]
        if row.pass_id is None:
            pass_ = TransectPass(
                transect_id=row.transect_id,
                video_id=row.video.id,
                extra_video_ids=extra_video_ids,
                begin_s=row.begin_s,
                end_s=row.end_s,
                direction=row.direction,
                batch_id=batch.id,
                held=row.held,
            )
            store.add_pass(pass_)
            row.pass_id = pass_.id
        else:
            stored = store.get_pass(row.pass_id)
            if stored is not None:
                stored.transect_id = row.transect_id
                stored.begin_s = row.begin_s
                stored.end_s = row.end_s
                stored.direction = row.direction
                stored.extra_video_ids = extra_video_ids
                stored.held = row.held
                store.update_pass(stored)

    def _single_direction_transects(self) -> dict[uuid.UUID, str]:
        """Transects every pass of which runs the same way, and why that is odd.

        Repeat passes are normally swum out and back, and nothing downstream can
        tell a deliberate one-way survey from a row of forgotten dropdowns.
        """
        directions: dict[uuid.UUID, list[str]] = {}
        for row in self._survey_rows:
            if row.transect_id is not None:
                directions.setdefault(row.transect_id, []).append(row.direction)
        names = {transect.id: transect.name for transect in self._survey_transects}
        flagged = {}
        for transect_id, values in directions.items():
            if len(values) < 2 or len(set(values)) != 1:
                continue
            name = names.get(transect_id, "Unnamed transect")
            flagged[transect_id] = (
                f"All {len(values)} passes of {name} are set to {values[0]}. "
                "Repeat passes are usually swum out and back."
            )
        return flagged

    def _refresh_direction_notice(self) -> None:
        """Mark the direction dropdowns of a transect swum only one way.

        A banner under the table said the same thing further from the control
        that fixes it, and stayed on screen for a survey that really was one-way.
        The marking is on the cell, where the answer is either changed or ignored.
        """
        one_way = self._single_direction_transects()
        for index, row in enumerate(self._survey_rows):
            combo = self._survey_pass_table.cellWidget(
                self._table_row_of(index), _COL_DIRECTION
            )
            if not isinstance(combo, QComboBox):
                continue
            flagged = row.transect_id in one_way
            _style_warning_combo(combo, ok=not flagged, filled=False)
            combo.setToolTip(
                one_way[row.transect_id]
                if flagged
                else "Which way this pass was swum along the transect."
            )

    # --- Run gating and execution ---

    def _survey_missing_models(self) -> list[str]:
        """Required-but-uncached models, judged against what the run will load.

        _required_model_names() reads the run form (mapping, segmentation, the
        DPT backbone), the same widgets _collect_run_settings() reads and the
        batch runs from, so the gate cannot block on a model the run would not
        load nor pass one it would. Iterating all_known_models() rather than the
        hardcoded catalogue means a model discovered this session is gated too.
        """
        if self._survey_preset is None:
            return []
        from deepreefmap_gui.models.manager import all_known_models, is_model_cached

        required = self._required_model_names()
        return sorted(
            info.name for info in all_known_models()
            if info.name in required and not is_model_cached(info)
        )

    def _simple_peak_frames(self, fps: int) -> int | None:
        """Frames in the longest queued pass, for the memory grade.

        A batch is many passes, but they run one at a time, so the pass that
        peaks memory is simply the longest clip. None when nothing is queued.
        """
        spans = [
            row.end_s - row.begin_s
            for row in getattr(self, "_survey_rows", [])
            if row.end_s > row.begin_s
        ]
        if not spans:
            return None
        return int(max(spans) * max(1, fps)) or None

    def _survey_remaining_rows(self) -> list[_PassRow]:
        """The passes the next batch would actually process, in table order."""
        states = self._survey_row_states()
        return [
            row
            for row, state in zip(self._survey_rows, states, strict=True)
            if state == QUEUED and row.pass_id is not None
        ]

    def _survey_failed_count(self) -> int:
        """Passes whose most recent run failed and has not since succeeded."""
        store = self._survey_store()
        failed = 0
        for row in self._survey_rows:
            if row.pass_id is None:
                continue
            runs = store.runs_for_pass(row.pass_id)
            if runs and not any(run.status == "succeeded" for run in runs):
                failed += any(run.status == "failed" for run in runs)
        return failed

    def _recompute_survey_start(self) -> None:
        """The Run step's one verdict, applied through a single exit.

        The badge, the count in the header and the forward button all read this,
        so they cannot disagree. Every row mutation funnels through here, which
        is why it must not return early before the repaint.
        """
        # Row actions and the empty state follow the table from one place.
        self._recompute_row_actions()
        self._refresh_direction_notice()
        # Keep the summary label on the same source as the gate and the run: all
        # three derive from the form, so the label cannot claim settings the run
        # would not use.
        self._survey_preset_label.setText(self._survey_preset_summary())
        # Restoring is only an action when there is something to restore, and the
        # tooltip says which of the two situations you are in.
        deviated = bool(self._survey_deviations())
        self._survey_restore_btn.setEnabled(deviated and not self._survey_worker_running)
        self._survey_restore_btn.setToolTip(
            "Discard this machine's changes and return to the standard settings."
            if deviated
            else "Already on the standard settings."
        )
        self._refresh_batch_standing()
        if self._survey_worker_running:
            self._survey_start_btn.setEnabled(False)
            self._refresh_section_state()
            return

        unassigned = sum(1 for row in self._survey_rows if row.transect_id is None)
        # An unassigned row has no pass yet, so it never counts as remaining.
        remaining = self._survey_remaining_rows() if self._survey_rows else []
        missing = self._survey_missing_models() if self._survey_preset is not None else []
        gate = run_gate(
            pass_count=len(self._survey_rows),
            unassigned=unassigned,
            remaining=len(remaining),
            failed=self._survey_failed_count() if self._survey_rows else 0,
            has_preset=self._survey_preset is not None,
            missing_models=missing,
            gpu_only_mapper=self._gpu_only_mapper(),
        )
        self._survey_gate = gate
        self._paint_not_ready_strip(gate)

        if gate.state == BLOCKED:
            self._set_survey_forward_action("process", count=len(remaining))
            self._survey_start_btn.setEnabled(False)
            if unassigned:
                # The message sits on the button itself: the status label is at
                # the far end of the window, easy to miss from the pass table.
                self._survey_start_btn.setText(f"Assign transects first ({unassigned} to do)")
            self._status_label.setText(gate.reason)
        elif gate.state == ATTENTION and gate.reason:
            # A warning still leaves the batch runnable, so the button carries no
            # sign of it. Without this the failed-pass warning lives only in the
            # nav badge and the tooltip.
            self._status_label.setText(gate.reason)
            self._set_survey_forward_action("process", count=len(remaining))
            self._survey_start_btn.setEnabled(bool(remaining) or bool(self._survey_rows))
        elif remaining:
            self._set_survey_forward_action("process", count=len(remaining))
            self._survey_start_btn.setEnabled(True)
        else:
            self._set_survey_forward_action("browse")
            self._survey_start_btn.setEnabled(bool(self._survey_rows))
        self._survey_start_btn.setToolTip(gate.reason)
        self._refresh_section_state()
        # The queued passes drive both the memory grade and the setup step's
        # models row, so re-read them whenever the batch changes.
        self._update_memory_profile_warning()
        self._refresh_setup_page()

    def _paint_not_ready_strip(self, gate: SectionState) -> None:
        """Show the blocker only when getting to it needs leaving this page.

        An unassigned transect is fixed in the table two rows down, so a strip
        pointing at it would be noise. A missing model or a missing graphics card
        is not fixable from here at all, which is what the strip is for.
        """
        if gate.state != BLOCKED or gate.fix == FIX_HERE:
            self._survey_not_ready.clear()
            return
        self._survey_not_ready.show_blocker(gate.reason, _FIX_ACTIONS[gate.fix])

    def _on_survey_fix_blocker(self) -> None:
        """Go where the live gate says the blocker is fixed."""
        gate = getattr(self, "_survey_gate", None)
        if gate is None:
            return
        if gate.fix == FIX_SETUP:
            self._set_simple_section("setup")
        elif gate.fix == FIX_SETTINGS:
            self._on_edit_run_settings()

    def _set_survey_forward_action(self, action: str, count: int = 0) -> None:
        """Point the Run step's one forward button at processing or at Browse."""
        try:
            self._survey_start_btn.clicked.disconnect()
        except RuntimeError:
            pass
        if action == "browse":
            self._survey_start_btn.setText("Open Browse →")
            self._survey_start_btn.clicked.connect(partial(self._go_to_step, "browse"))
        else:
            # A batch that has already run some of its passes is continued, not
            # started: the word is the promise that the finished ones stay done
            # and the half-processed one picks up from its cached frames.
            verb = "Continue batch" if self._survey_processed_count() else "Next: Process"
            self._survey_start_btn.setText(f"{verb} ({count}) →")
            self._survey_start_btn.clicked.connect(self._on_survey_start)

    def _survey_processed_count(self) -> int:
        """Passes in this batch that have already been through a run of any outcome."""
        store = self._survey_store()
        return sum(
            1
            for row in self._survey_rows
            if row.pass_id is not None and store.runs_for_pass(row.pass_id)
        )

    def _refresh_batch_standing(self) -> None:
        """Say how much of the batch is behind you, so stopping is safe to do.

        Without this a resumed batch looks identical to a fresh one: the button
        counts what is left but nothing counts what is done.
        """
        states = self._survey_row_states()
        done = sum(1 for state in states if state == DONE)
        remaining = len(self._survey_remaining_rows())
        held = sum(1 for state in states if state == HELD)
        if not states or (not done and not held):
            self._survey_standing_label.setVisible(False)
            return
        parts = [f"{done} done", f"{remaining} remaining"]
        if held:
            parts.append(f"{held} held back")
        text = " · ".join(parts)
        if done and remaining:
            text += ". Processing again continues where it stopped."
        self._survey_standing_label.setText(text)
        self._survey_standing_label.setVisible(True)

    def _confirm_batch_space(self, pass_count: int) -> bool:
        """Pre-flight the disk before a batch: only interrupt when it may not fit.

        The estimate is deliberately rough (see setup.ROUGH_PASS_BYTES), so a
        comfortable margin proceeds silently and a tight one asks first, rather
        than nagging on every run.
        """
        import shutil

        from deepreefmap_gui.simple.setup import estimate_batch_disk

        out_root = Path(self._out_root_input.text()).expanduser()
        out_root.mkdir(parents=True, exist_ok=True)
        try:
            free = shutil.disk_usage(out_root).free
        except OSError:
            return True  # can't measure the drive, so don't stand in the way
        estimate = estimate_batch_disk(pass_count, free)
        if estimate.fits:
            return True

        from PySide6.QtWidgets import QMessageBox

        from deepreefmap_gui.profiling.system_probe import format_bytes

        time_str = _rough_batch_time(pass_count)
        opening = f"{pass_count} pass{'' if pass_count == 1 else 'es'} queued"
        opening += f", {time_str}." if time_str else "."
        answer = QMessageBox.question(
            self,
            "Insufficient disk space",
            f"{opening}\n\n"
            f"Estimated {format_bytes(estimate.need_bytes)} required against "
            f"{format_bytes(estimate.free_bytes)} free. Processing may stop part "
            "way and leave passes unfinished.\n\nStart anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _pass_dir_name(
        self, pass_: TransectPass, transect: Transect, store: SurveyStore
    ) -> str:
        """The run-dir name a pass keeps for every attempt at it.

        A fresh timestamp per attempt handed each retry an empty directory, so a
        pass that failed in mapping decoded and segmented its clip again from
        scratch. Keying the name on the pass id makes a retry land back in the
        directory holding that work, where the library's own resume cache picks
        it up. The ordinal is there only so the folder reads as a pass.
        """
        siblings = store.list_passes(transect_id=pass_.transect_id)
        number = next(
            (index for index, sibling in enumerate(siblings, start=1) if sibling.id == pass_.id),
            1,
        )
        return self._sanitize_run_name(f"{transect.name}__p{number:02d}__{pass_.id.hex[:8]}")

    def _on_survey_start(self) -> None:
        if self._survey_preset is None or self._survey_worker_running:
            return
        remaining = self._survey_remaining_rows()
        # Confirm before any run records are written, so a Cancel leaves the
        # batch exactly as it was.
        if not remaining or not self._confirm_batch_space(len(remaining)):
            return
        store = self._survey_store()
        batch = self._ensure_survey_batch()
        jobs = []
        for row in remaining:
            assert row.pass_id is not None
            pass_ = store.get_pass(row.pass_id)
            if pass_ is None:
                continue
            transect = store.get_transect(pass_.transect_id)
            if transect is None:
                continue
            dir_name = self._pass_dir_name(pass_, transect, store)
            run = RunRecord(pass_id=pass_.id, run_dir_name=dir_name)
            store.add_run(run)
            jobs.append(_SurveyJob(
                run=run,
                pass_=pass_,
                transect=transect,
                videos=list(row.videos),
                dir_name=dir_name,
            ))
        if not jobs:
            return
        self._survey_job_pass_ids = [job.pass_.id for job in jobs]
        self._survey_running_index = None
        self._batch_progress.set_batch_plan(len(jobs), _median_pass_seconds())
        self._batch_progress.set_idle("Starting…")
        self._batch_progress.setVisible(True)
        self._survey_worker_running = True
        # Share the window's cancel and pause events so the bottom-bar transport
        # controls drive a survey batch exactly as they drive a single run.
        self._survey_cancel_event = threading.Event()
        self._cancel_event = self._survey_cancel_event
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._survey_start_btn.setEnabled(False)
        self._begin_run_controls()
        self._set_wizard_navigation_enabled(False)
        self._recompute_row_actions()
        self._refresh_survey_pass_statuses()
        self._set_app_mode("RUNNING")
        out_root = Path(self._out_root_input.text()).expanduser()
        # Read the form on the GUI thread: a survey run honours every setting,
        # not just the seven the preset used to carry.
        settings = self._collect_run_settings()
        # Snapshot the configuration identity here too, from the same form read,
        # so the manifest records what this batch ran rather than what the preset
        # file happens to say once the batch finishes.
        config = (
            manifest_config_block(self._active_preset.org, self._survey_deviations())
            if self._active_preset is not None
            else None
        )
        self._pipeline_thread = threading.Thread(
            target=self._run_survey_worker,
            args=(jobs, out_root, settings, store, batch, self._pause_event, config),
            daemon=True,
        )
        self._pipeline_thread.start()

    def _run_survey_worker(
        self,
        jobs: list[_SurveyJob],
        out_root: Path,
        settings: dict,
        store: SurveyStore,
        batch: SurveyBatch,
        pause_event: threading.Event,
        config: dict | None = None,
    ) -> None:
        import shutil

        from deepreefmap_gui.models.manager import resolve_model_versions
        from deepreefmap_gui.profiling.instrumentation import instrumented_reconstruction
        from deepreefmap_gui.runs.seeding import seed_from_settings
        from deepreefmap_gui.simple.setup import ROUGH_PASS_BYTES

        # Which model versions this batch ran against, constant across its passes.
        # These are HuggingFace commit revisions read off the cache, so the call
        # is disk-only and safe on this worker thread.
        version_names = [settings.get("mapping_name")]
        if not settings.get("skip_segmentation"):
            version_names.append(settings.get("segmentation_name"))
        model_versions = resolve_model_versions(n for n in version_names if n)

        ok = 0
        last_error = ""
        disk_stopped = False
        for index, job in enumerate(jobs, start=1):
            # Hold between passes too, so pausing doesn't let the next one start.
            pause_event.wait()
            cancel_event = self._survey_cancel_event
            if cancel_event is not None and cancel_event.is_set():
                store.set_run_status(job.run.id, "cancelled")
                continue
            try:
                free_bytes = shutil.disk_usage(out_root).free
            except OSError:
                free_bytes = None
            if free_bytes is not None and free_bytes < ROUGH_PASS_BYTES:
                # Stop cleanly: mark this pass and every one after it not-started,
                # so the batch runs again from here once space is freed.
                disk_stopped = True
                for pending in jobs[index - 1:]:
                    store.set_run_status(pending.run.id, "cancelled")
                break
            # The transect name reads as a place, not the run-dir slug: the panel
            # already carries the "pass N of M" number, so the name need not.
            self._sig_survey_progress.emit(index, len(jobs), job.transect.name)
            store.set_run_status(job.run.id, "running")
            out_dir = out_root / job.dir_name
            out_dir.mkdir(parents=True, exist_ok=True)
            # A retry lands back in its own directory and resumes from what is
            # already there. Seeding covers the rest: another pass of the same
            # clip and settings, or a first attempt whose name has since changed.
            seeded = seed_from_settings(
                out_dir,
                out_root,
                settings,
                [Path(video.path) for video in job.videos],
                job.pass_.begin_s,
                job.pass_.end_s,
            )
            if seeded is not None:
                # Say the afternoon is not being spent again, so a diver watching
                # a retry knows preparation was skipped.
                self._sig_status_text.emit(
                    f"Pass {index} of {len(jobs)}: reusing prepared frames from an "
                    "earlier attempt."
                )
            try:
                instrumented_reconstruction(
                    video_paths=[video.path for video in job.videos],
                    output_dir=out_dir,
                    transect_length=job.transect.length_m,
                    begin_s=job.pass_.begin_s,
                    end_s=job.pass_.end_s,
                    run_name=job.dir_name,
                    viewer=self._viewer,
                    cancel_event=cancel_event,
                    pause_event=pause_event,
                    manifest_extra={
                        "survey": survey_manifest_block(
                            job.run, job.pass_, job.transect, batch,
                            config=config, model_versions=model_versions,
                        )
                    },
                    **settings,
                )
                store.set_run_status(job.run.id, "succeeded")
                ok += 1
            except ReconstructionCancelled:
                store.set_run_status(job.run.id, "cancelled")
            except Exception as exc:
                logger.exception("Pass %s failed", job.dir_name)
                last_error = f"{job.dir_name}: {exc}"
                store.set_run_status(job.run.id, "failed", error=str(exc)[:300])
        if disk_stopped and not last_error:
            last_error = "Ran out of disk space before every pass finished."
        self._sig_survey_done.emit(ok, len(jobs), last_error[:300])

    def _on_survey_progress(self, index: int, total: int, name: str) -> None:
        self._status_label.setText(f"Batch: pass {index} of {total}: {name}")
        # Fresh estimator per pass so the ETA does not blend across passes. The
        # batch card spans them instead, from the median of past runs.
        self._begin_progress(self._recon_model)
        for sink in self._progress_sinks():
            sink.set_batch_context(index, total, name)
        self._survey_running_index = index - 1
        self._refresh_survey_pass_statuses()

    def _on_survey_done(self, ok: int, total: int, last_error: str) -> None:
        self._survey_worker_running = False
        # The re-run request is spent: a pass asked for again belongs back with
        # the processed ones once the batch that redid it has finished.
        for row in self._survey_rows:
            row.requeued = False
        # These passes are the newest evidence of what a run costs on disk, so
        # the setup step's footage capacity is measured again rather than kept.
        self._footage_rate_cache = None
        self._end_run_controls()
        self._set_wizard_navigation_enabled(True)
        self._reset_progress_bars()
        for sink in self._progress_sinks():
            sink.clear_batch_context()
        self._batch_progress.setVisible(False)
        self._survey_running_index = None
        self._survey_job_pass_ids = []
        self._recompute_row_actions()
        self._set_app_mode("SETUP")
        # The outcome goes on the page and stays there. The status bar is the
        # wrong home for it: the next thing that happens overwrites it, and a
        # finished batch is exactly when the user walks away from the laptop.
        # Failures name themselves by transect/pass. The last raw error stays out
        # of the way on the failed row's own tooltip.
        failed = self._failed_pass_labels()
        summary = f"{ok} of {total} pass{'' if total == 1 else 'es'} succeeded"
        if failed:
            summary += " · Failed: " + ", ".join(failed)
        elif last_error:
            # A batch-level stop (e.g. ran out of disk) leaves the remaining
            # passes cancelled, with no failed row to carry the reason.
            summary += " · " + last_error
        if ok:
            summary += " · double-click a row to open its run"
        self._survey_summary_label.setText(summary)
        self._survey_summary_label.setVisible(True)
        if ok == total:
            self._status_label.setText(f"Batch complete: {ok}/{total} pass(es) succeeded.")
        elif failed:
            self._status_label.setText(
                f"Batch finished: {ok}/{total} succeeded. Failed: {', '.join(failed)}."
            )
        elif last_error:
            self._status_label.setText(
                f"Batch finished: {ok}/{total} succeeded. {last_error}"
            )
        else:
            self._status_label.setText(f"Batch finished: {ok}/{total} succeeded.")
        self._refresh_survey_pass_statuses()
        self._recompute_survey_start()
        self._refresh_data_manager()
        self._refresh_survey_analysis()

    def _on_survey_stop(self) -> None:
        if self._survey_cancel_event is not None:
            self._survey_cancel_event.set()
            # Release a paused worker, or it never reaches the cancel check.
            if getattr(self, "_pause_event", None) is not None:
                self._pause_event.set()
            self._status_label.setText("Stopping survey batch…")

    def _running_status_item(self) -> QTableWidgetItem | None:
        """The status cell of the pass being processed, if one is."""
        index = self._survey_running_index
        if index is None or not 0 <= index < len(self._survey_job_pass_ids):
            return None
        pass_id = self._survey_job_pass_ids[index]
        for model_index, row in enumerate(self._survey_rows):
            if row.pass_id != pass_id:
                continue
            table_row = self._table_row_of(model_index)
            return None if table_row < 0 else self._survey_pass_table.item(table_row, _COL_STATUS)
        return None

    def _on_pass_percent(self, percent: int) -> None:
        """Move the running row's own progress, so the queue shows where it is."""
        self._survey_pass_percent = percent
        item = self._running_status_item()
        if item is None:
            return
        item.setData(PASS_PERCENT_ROLE, percent)
        item.setText(f"Running {percent}%")

    def _refresh_survey_pass_statuses(self) -> None:
        store = self._survey_store()
        for index, row in enumerate(self._survey_rows):
            item = self._survey_pass_table.item(self._table_row_of(index), _COL_STATUS)
            if item is None:
                continue
            # Cleared for every row and re-applied below to the one in flight, so
            # a finished pass does not keep the fill it had while running.
            item.setData(PASS_PERCENT_ROLE, None)
            if row.held:
                item.setText("Held")
                item.setToolTip("Held back: every batch skips this pass until it is returned.")
                continue
            if row.pass_id is None:
                item.setText("")
                item.setToolTip("")
                continue
            runs = store.runs_for_pass(row.pass_id)
            last = runs[-1] if runs else None
            item.setText(last.status.capitalize() if last is not None else "")
            # A failed pass carries its cause on the row, where it survives the
            # next event, rather than flashing once in the shared status bar. A
            # succeeded pass flags any non-fatal quality warning the same way.
            if last is not None and last.status == "failed":
                item.setToolTip(_diagnose_failure(last.error))
            elif last is not None and last.status == "succeeded":
                warnings = self._pass_quality_warnings(last.run_dir_name)
                if warnings:
                    item.setText("Succeeded ⚠")
                    item.setToolTip("\n".join(warnings))
                else:
                    item.setToolTip("")
            else:
                item.setToolTip("")
        if self._survey_worker_running:
            self._on_pass_percent(self._survey_pass_percent)

    def _pass_quality_warnings(self, run_dir_name: str) -> list[str]:
        """Non-fatal warnings a succeeded run recorded in its manifest, if any."""
        path = Path(self._out_root_input.text()).expanduser() / run_dir_name / "run_manifest.json"
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        warnings = manifest.get("quality_warnings") if isinstance(manifest, dict) else None
        return [str(w) for w in warnings] if isinstance(warnings, list) else []

    def _survey_pass_error(self, row: _PassRow) -> str:
        """The full error of a pass's latest run when it failed, else the empty string."""
        if row.pass_id is None:
            return ""
        runs = self._survey_store().runs_for_pass(row.pass_id)
        last = runs[-1] if runs else None
        return last.error if last is not None and last.status == "failed" else ""

    def _failed_pass_labels(self) -> list[str]:
        """Passes whose latest run failed, named by transect and pass number."""
        store = self._survey_store()
        labels: list[str] = []
        for row in self._survey_rows:
            if row.pass_id is None:
                continue
            runs = store.runs_for_pass(row.pass_id)
            if not runs or any(run.status == "succeeded" for run in runs):
                continue
            if runs[-1].status != "failed":
                continue
            transect = store.get_transect(row.transect_id) if row.transect_id else None
            labels.append(_failed_pass_label(transect, runs[-1].run_dir_name))
        return labels

    def _on_survey_pass_menu(self, pos) -> None:
        """Right-click for the bulk assign, and for a failed pass's full error."""
        index = self._model_index(self._survey_pass_table.rowAt(pos.y()))
        if index is None:
            return
        # Right-clicking outside the selection acts on the row under the cursor,
        # which is what every file manager does.
        if index not in self._selected_survey_rows():
            self._survey_pass_table.selectRow(self._table_row_of(index))
        menu = QMenu(self)
        indices = self._selected_survey_rows()
        if indices and self._survey_transects:
            assign = menu.addMenu(f"Assign {len(indices)} selected to")
            for transect in self._survey_transects:
                assign.addAction(
                    transect.name,
                    partial(self._assign_rows_to_transect, indices, transect.id),
                )
        states = self._survey_row_states()
        selected = indices or [index]
        if any(states[i] != HELD for i in selected):
            menu.addAction(
                f"Hold back {len(selected)} selected", partial(self._move_rows, selected, True)
            )
        if any(states[i] in (HELD, DONE) for i in selected):
            menu.addAction(
                f"Return {len(selected)} to the batch", partial(self._move_rows, selected, False)
            )
        error = self._survey_pass_error(self._survey_rows[index])
        if error:
            menu.addAction("Copy error details", partial(self._copy_pass_error, error))
        if menu.isEmpty():
            return
        menu.exec(self._survey_pass_table.viewport().mapToGlobal(pos))

    def _copy_pass_error(self, error: str) -> None:
        QGuiApplication.clipboard().setText(error)
        self._status_label.setText("Copied the error details to the clipboard.")
