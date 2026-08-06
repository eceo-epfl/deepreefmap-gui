"""What one section of a clip is, and which sessions have run it.

A section outlives any one attempt at it: the same cutout can be processed in
several sessions, and comparing those repeats is the point of processing it more
than once. The clip pane says what footage exists; this says what has been asked
of one piece of it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QWidget,
)

from deepreefmap_gui.core.icons import status_dot_icon
from deepreefmap_gui.core.theme import TEXT_MUTED
from deepreefmap_gui.core.widgets import STATUS_COLORS, EmptyState, muted_label
from deepreefmap_gui.profiling.system_probe import format_bytes
from deepreefmap_gui.runs.run_detail import DetailCard
from deepreefmap_gui.survey.models import RunRecord, TransectPass

_RUN_PAGE, _NO_RUN_PAGE = 0, 1

RUN_DIR_ROLE = Qt.ItemDataRole.UserRole

_NO_SESSION = "No session recorded"


def section_window(pass_: TransectPass) -> str:
    """The section's own name: where it starts and stops in the clip."""
    end = pass_.end_s
    tail = "end" if end is None else f"{int(end) // 60}:{int(end) % 60:02d}"
    return f"{int(pass_.begin_s) // 60}:{int(pass_.begin_s) % 60:02d}–{tail}"


def _length(pass_: TransectPass) -> str:
    if pass_.end_s is None:
        return "to the end of the clip"
    seconds = int(round(max(0.0, pass_.end_s - pass_.begin_s)))
    return f"{seconds // 60}m {seconds % 60:02d}s"


def _short_date(stamp: str | None) -> str:
    return (stamp or "").split("T")[0] or "unknown"


class SectionDetailPanel(DetailCard):
    """A titled card describing one section and the sessions that ran it."""

    add_to_cart_requested = Signal(str)
    retrim_requested = Signal(str)
    reassign_requested = Signal(str)
    delete_requested = Signal(str)
    run_activated = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = self.body

        layout.addWidget(muted_label("Sessions this section has run in"))

        self.run_list = QListWidget()
        self.run_list.setAlternatingRowColors(True)
        self.run_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.run_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.run_list.itemDoubleClicked.connect(self._on_run_activated)
        self._run_stack = QStackedWidget()
        self._run_stack.addWidget(self.run_list)
        self._run_stack.addWidget(
            EmptyState("Not processed yet", "Add it to the cart to run it.")
        )
        layout.addWidget(self._run_stack, 1)

        self.cart_btn = QPushButton("Add to cart")
        self.cart_btn.clicked.connect(self._emit_cart)
        self.trim_btn = QPushButton("Adjust trim…")
        self.trim_btn.setProperty("quiet", "true")
        self.trim_btn.clicked.connect(self._emit_retrim)
        self.transect_btn = QPushButton("Change transect…")
        self.transect_btn.setProperty("quiet", "true")
        self.transect_btn.clicked.connect(self._emit_reassign)
        self.delete_btn = QPushButton("Delete section")
        self.delete_btn.setProperty("quiet", "true")
        self.delete_btn.clicked.connect(self._emit_delete)
        self.add_actions(self.cart_btn, self.trim_btn, self.transect_btn, self.delete_btn)

        self._pass: TransectPass | None = None

    @property
    def pass_(self) -> TransectPass | None:
        return self._pass

    def _pass_id(self) -> str:
        return "" if self._pass is None else str(self._pass.id)

    def _emit_cart(self) -> None:
        self.add_to_cart_requested.emit(self._pass_id())

    def _emit_retrim(self) -> None:
        self.retrim_requested.emit(self._pass_id())

    def _emit_reassign(self) -> None:
        self.reassign_requested.emit(self._pass_id())

    def _emit_delete(self) -> None:
        self.delete_requested.emit(self._pass_id())

    def _on_run_activated(self, item: QListWidgetItem) -> None:
        self.run_activated.emit(str(item.data(RUN_DIR_ROLE) or ""))

    def show_section(
        self,
        pass_: TransectPass,
        *,
        clip_name: str,
        transect_name: str | None,
        status: str,
        runs: list[RunRecord],
        session_name,
        in_cart: bool,
        output_bytes: int = 0,
    ) -> None:
        """Describe one section. ``session_name`` resolves a run's batch id."""
        self.title.setText(section_window(pass_))
        self.set_status(status, STATUS_COLORS.get(status, TEXT_MUTED))
        rows = [
            ("Clip", clip_name),
            ("Transect", transect_name or "Unassigned"),
            ("Direction", pass_.direction),
            ("Length", _length(pass_)),
            ("In the cart", "yes" if in_cart else "no"),
        ]
        # What this cut has cost so far, which is the figure worth having when
        # deciding whether to run it again.
        made = f"{len(runs)} run{'' if len(runs) == 1 else 's'}" if runs else "none yet"
        if output_bytes:
            made += f" · {format_bytes(output_bytes)}"
        rows.append(("Runs", made))
        self.facts.set_rows(rows)

        self.run_list.clear()
        # Newest first: the question asked of a repeated section is what happened
        # last time, not what happened first.
        for run in sorted(runs, key=lambda r: r.created_at or "", reverse=True):
            name = session_name(run.batch_id) or _NO_SESSION
            item = QListWidgetItem(f"{name} · {run.status} · {_short_date(run.created_at)}")
            item.setIcon(status_dot_icon(STATUS_COLORS.get(run.status, TEXT_MUTED)))
            item.setData(RUN_DIR_ROLE, run.run_dir_name)
            item.setToolTip(run.error or run.run_dir_name)
            self.run_list.addItem(item)
        self._run_stack.setCurrentIndex(_RUN_PAGE if runs else _NO_RUN_PAGE)

        self.cart_btn.setEnabled(not in_cart)
        self.cart_btn.setText("In the cart" if in_cart else "Add to cart")
        # A section with runs is the record of what they processed, so it cannot
        # go while they are still there.
        self.delete_btn.setEnabled(not runs)
        self.delete_btn.setToolTip(
            "This section has runs. Delete them in Browse first."
            if runs
            else "Remove this cut. The clip itself is left alone."
        )
        self._pass = pass_

    def clear(self) -> None:
        super().clear()
        self.run_list.clear()
        self._run_stack.setCurrentIndex(_NO_RUN_PAGE)
        self._pass = None
