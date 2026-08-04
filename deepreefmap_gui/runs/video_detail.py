"""What one clip is, and what became of the footage.

Footage outlives the runs cut from it: a card copied off the camera is a fact of
the day's diving whether or not anything has been processed from it yet. This is
the pane that says so, shown in Browse when the grouping is by video and a clip
is what is selected.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.theme import (
    FONT_LG_PT,
    SPACE_SM,
    TEXT_MUTED,
    WEIGHT_SEMIBOLD,
)
from deepreefmap_gui.core.widgets import (
    STATUS_COLORS,
    EmptyState,
    KeyValueList,
    section_card,
)
from deepreefmap_gui.survey.catalogue import (
    VIDEO_FAILED,
    VIDEO_PENDING,
    VIDEO_PROCESSED,
    VIDEO_UNPROCESSED,
    VideoLibraryEntry,
)

# What each outcome says, and which status colour it borrows so a clip and the
# runs cut from it are not described in two different colour languages.
OUTCOME_LABELS = {
    VIDEO_UNPROCESSED: ("Not processed", "queued"),
    VIDEO_PENDING: ("Part processed", "running"),
    VIDEO_FAILED: ("Failed", "failed"),
    VIDEO_PROCESSED: ("Processed", "succeeded"),
}

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


class VideoDetailPanel(QWidget):
    """A titled card describing the selected clip."""

    queue_requested = Signal()
    show_in_folder_requested = Signal()
    pass_activated = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card, layout = section_card()

        self.title = QLabel("")
        self.title.setWordWrap(True)
        title_font = self.title.font()
        title_font.setWeight(QFont.Weight.DemiBold)
        title_font.setPointSize(FONT_LG_PT)
        self.title.setFont(title_font)
        layout.addWidget(self.title)

        self.outcome = QLabel("")
        self.outcome.setWordWrap(True)
        layout.addWidget(self.outcome)

        self.facts = KeyValueList()
        layout.addWidget(self.facts)

        heading = QLabel("Passes cut from this clip")
        heading.setStyleSheet(f"color: {TEXT_MUTED};")
        layout.addWidget(heading)

        self.pass_list = QListWidget()
        self.pass_list.setAlternatingRowColors(True)
        self.pass_list.itemDoubleClicked.connect(self._on_pass_activated)
        self._pass_stack = QStackedWidget()
        self._pass_stack.addWidget(self.pass_list)
        self._pass_stack.addWidget(
            EmptyState("Not cut into passes yet", "Queue it to process this clip.")
        )
        layout.addWidget(self._pass_stack, 1)

        actions = QHBoxLayout()
        actions.setSpacing(SPACE_SM)
        self.queue_btn = QPushButton("Queue as pass")
        self.queue_btn.setProperty("cta", "true")
        self.queue_btn.setToolTip("Add this clip to the current batch on the Run step.")
        self.queue_btn.clicked.connect(self.queue_requested)
        actions.addWidget(self.queue_btn)
        actions.addStretch(1)
        self.show_btn = QPushButton("Show in folder")
        self.show_btn.setProperty("quiet", "true")
        self.show_btn.clicked.connect(self.show_in_folder_requested)
        actions.addWidget(self.show_btn)
        layout.addLayout(actions)

        self._entry: VideoLibraryEntry | None = None
        outer.addWidget(card)

    def _on_pass_activated(self, item: QListWidgetItem) -> None:
        self.pass_activated.emit(str(item.data(PASS_ID_ROLE) or ""))

    @property
    def entry(self) -> VideoLibraryEntry | None:
        return self._entry

    def set_queue_enabled(self, enabled: bool) -> None:
        self.queue_btn.setEnabled(enabled)

    def show_entry(self, entry: VideoLibraryEntry, transect_name) -> None:
        """Describe one clip. ``transect_name`` resolves a pass's transect id."""
        label, status_key = OUTCOME_LABELS[entry.outcome]
        colour = STATUS_COLORS.get(status_key, TEXT_MUTED)
        self.title.setText(entry.video.file_name)
        self.outcome.setText(
            f'<span style="color:{colour}; font-weight:{WEIGHT_SEMIBOLD};">{label}</span>'
        )

        rows = [("Folder", entry.video.path)]
        facts = clip_facts(entry)
        if facts:
            rows.append(("Footage", facts))
        if entry.video.hash:
            rows.append(("Checksum", f"#{entry.video.hash[:8]}"))
        self.facts.set_rows(rows)

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
            item.setData(PASS_ID_ROLE, str(pass_.id))
            self.pass_list.addItem(item)
        self._pass_stack.setCurrentIndex(_PASS_PAGE if entry.passes else _NO_PASS_PAGE)
        self._entry = entry

    def clear(self) -> None:
        self.title.setText("")
        self.outcome.setText("")
        self.facts.clear()
        self.pass_list.clear()
        self._pass_stack.setCurrentIndex(_NO_PASS_PAGE)
        self._entry = None
