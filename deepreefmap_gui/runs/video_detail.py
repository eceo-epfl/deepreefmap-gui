"""What one clip is, and what became of the footage.

Footage outlives the runs cut from it: a card copied off the camera is a fact of
the day's diving whether or not anything has been processed from it yet. This is
the pane that says so, shown in Browse when the grouping is by video and a clip
is what is selected.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QWidget,
)

from deepreefmap_gui.core.icons import status_dot_icon
from deepreefmap_gui.core.theme import SPACE_SM, TEXT_MUTED
from deepreefmap_gui.core.widgets import (
    STATUS_COLORS,
    EmptyState,
    clip_outcome_color,
    muted_label,
)
from deepreefmap_gui.runs.run_detail import DetailCard
from deepreefmap_gui.survey.catalogue import (
    LINK_LINKED,
    LINK_MISSING,
    VideoLibraryEntry,
)
from deepreefmap_gui.survey.statuses import clip_spec

_PASS_PAGE, _NO_PASS_PAGE = 0, 1

PASS_ID_ROLE = Qt.ItemDataRole.UserRole


def clip_facts(entry: VideoLibraryEntry) -> str:
    """The line under a clip's name: how much of the survey hangs off it."""
    from deepreefmap_gui.profiling.system_probe import format_bytes

    video = entry.video
    bits = []
    if video.duration_s:
        total = int(round(video.duration_s))
        bits.append(f"{total // 60}m {total % 60:02d}s")
    if video.size_bytes:
        bits.append(format_bytes(video.size_bytes))
    if entry.pass_count:
        bits.append(f"{entry.pass_count} pass{'es' if entry.pass_count != 1 else ''}")
    if entry.run_count:
        bits.append(f"{entry.run_count} run{'s' if entry.run_count != 1 else ''}")
    return "  ·  ".join(bits)


def _window(begin_s: float, end_s: float) -> str:
    return (
        f"{int(begin_s) // 60}:{int(begin_s) % 60:02d}"
        f"–{int(end_s) // 60}:{int(end_s) % 60:02d}"
    )


def _short_date(stamp: str | None) -> str:
    """The date out of an ISO timestamp. The time of day says nothing here."""
    return (stamp or "").split("T")[0] or "unknown"


def _link_line(entry: VideoLibraryEntry) -> str:
    """The path, and whether the file is still at the end of it.

    Said in the row rather than only in an icon: the pane is where the decision
    to relocate gets made, and an icon in the rail is not next to the button.
    """
    if entry.link_state == LINK_MISSING:
        return f"{entry.video.path}  (not found)"
    return entry.video.path


class VideoDetailPanel(DetailCard):
    """A titled card describing the selected clip."""

    queue_requested = Signal()
    show_in_folder_requested = Signal()
    pass_activated = Signal(str)
    relocate_requested = Signal()
    preview_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = self.body

        heading = muted_label("Passes cut from this clip")
        layout.addWidget(heading)

        self.pass_list = QListWidget()
        self.pass_list.setAlternatingRowColors(True)
        # The pane is narrow and a row is four facts joined; elide rather than
        # grow a scrollbar that hides the outcome at the end of the line.
        self.pass_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.pass_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pass_list.itemDoubleClicked.connect(self._on_pass_activated)
        self._pass_stack = QStackedWidget()
        self._pass_stack.addWidget(self.pass_list)
        self._pass_stack.addWidget(
            EmptyState("Not cut into passes yet", "Queue it to process this clip.")
        )
        layout.addWidget(self._pass_stack, 1)

        # Relocating and previewing act on the file, so they sit above the row
        # that acts on the survey. Relocate only appears when there is something
        # to relocate; a permanently greyed button teaches nothing.
        file_row = QHBoxLayout()
        file_row.setSpacing(SPACE_SM)
        self.preview_btn = QPushButton("Preview…")
        self.preview_btn.setProperty("quiet", "true")
        self.preview_btn.setToolTip("Scrub through the footage without queueing it.")
        self.preview_btn.clicked.connect(self.preview_requested)
        file_row.addWidget(self.preview_btn)
        self.relocate_btn = QPushButton("Relocate…")
        self.relocate_btn.setProperty("quiet", "true")
        self.relocate_btn.setToolTip(
            "Point this clip at the file's new home. The replacement has to be "
            "the same footage, checked against this clip's checksum."
        )
        self.relocate_btn.setVisible(False)
        self.relocate_btn.clicked.connect(self.relocate_requested)
        file_row.addWidget(self.relocate_btn)
        file_row.addStretch(1)
        layout.addLayout(file_row)

        self.queue_btn = QPushButton("Queue as pass")
        self.queue_btn.setToolTip("Add this clip to the current session under Process.")
        self.queue_btn.clicked.connect(self.queue_requested)
        self.show_btn = QPushButton("Show in folder")
        self.show_btn.clicked.connect(self.show_in_folder_requested)
        self.add_actions(self.queue_btn, self.show_btn)

        self._entry: VideoLibraryEntry | None = None

    def _on_pass_activated(self, item: QListWidgetItem) -> None:
        self.pass_activated.emit(str(item.data(PASS_ID_ROLE) or ""))

    @property
    def entry(self) -> VideoLibraryEntry | None:
        return self._entry

    def set_queue_enabled(self, enabled: bool) -> None:
        self.queue_btn.setEnabled(enabled)

    def show_entry(self, entry: VideoLibraryEntry, transect_name) -> None:
        """Describe one clip. ``transect_name`` resolves a pass's transect id."""
        self.title.setText(entry.video.file_name)
        self.set_status(
            clip_spec(entry.outcome).label, clip_outcome_color(entry.outcome)
        )

        rows = [("File", _link_line(entry))]
        facts = clip_facts(entry)
        if facts:
            rows.append(("Footage", facts))
        # Added and last processed, because the question a library gets asked is
        # which card this came off and whether it has been done since.
        rows.append(("Added", _short_date(entry.video.created_at)))
        last_run = entry.last_run_at
        rows.append(("Last processed", _short_date(last_run) if last_run else "never"))
        # The checksum is what makes a clip recognisable when it turns up again
        # somewhere else, so its absence is worth as much space as its value.
        rows.append(
            ("Checksum", f"#{entry.video.hash[:8]}" if entry.video.hash else "none yet")
        )
        self.facts.set_rows(rows)

        self.relocate_btn.setVisible(entry.link_state == LINK_MISSING)
        # Previewing decodes the file, so it needs the file. Enabled only on a
        # confirmed link, which also means it stays off until the scan answers.
        self.preview_btn.setEnabled(entry.link_state == LINK_LINKED)

        self.pass_list.clear()
        runs_by_pass: dict = {}
        for run in entry.runs:
            runs_by_pass.setdefault(run.pass_id, []).append(run)
        for pass_ in entry.passes:
            name = transect_name(pass_.transect_id) or "Unassigned"
            runs = runs_by_pass.get(pass_.id, [])
            status = runs[-1].status if runs else "queued"
            item = QListWidgetItem(
                f"{name} · {pass_.direction} · {_window(pass_.begin_s, pass_.end_s)} · {status}"
            )
            item.setIcon(status_dot_icon(STATUS_COLORS.get(status, TEXT_MUTED)))
            item.setData(PASS_ID_ROLE, str(pass_.id))
            self.pass_list.addItem(item)
        self._pass_stack.setCurrentIndex(_PASS_PAGE if entry.passes else _NO_PASS_PAGE)
        self._entry = entry

    def clear(self) -> None:
        super().clear()
        self.pass_list.clear()
        self._pass_stack.setCurrentIndex(_NO_PASS_PAGE)
        self._entry = None
