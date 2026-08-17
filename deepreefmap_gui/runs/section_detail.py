"""What one section of a clip is, and which sessions have run it.

A section outlives any one attempt at it: the same cutout can be processed in
several sessions, and comparing those repeats is the point of processing it more
than once. The clip pane says what footage exists; this says what has been asked
of one piece of it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QListWidget,
    QListWidgetItem,
    QMenu,
    QToolButton,
    QWidget,
)

from deepreefmap_gui.core.icons import status_dot_icon
from deepreefmap_gui.core.theme import TEXT_MUTED
from deepreefmap_gui.core.widgets import (
    STATUS_COLORS,
    direction_html,
    fact_link,
    muted_label,
)
from deepreefmap_gui.profiling.system_probe import format_bytes
from deepreefmap_gui.runs.run_detail import DetailCard
from deepreefmap_gui.survey.models import RunRecord, TransectPass

RUN_DIR_ROLE = Qt.ItemDataRole.UserRole

# The href behind the transect-and-direction fact. Both are set in one dialog,
# so the fact that shows them is the way into it.
_FILING_LINK = "filing"

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
        # The list is always the list, empty or not, the same way the clip's own
        # section list is. Swapping it for a full-pane empty state moved every
        # control below it and left the pane a different shape for a section
        # that had run and one that had not.
        layout.addWidget(self.run_list, 1)

        # A menu rather than a row of buttons the pane cannot hold without
        # truncating every label. The cart is not among them: the section's own
        # row carries that, and two cart controls on one screen disagree the
        # moment one of them is stale.
        self.more_btn = QToolButton()
        self.more_btn.setText("More…")
        self.more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.more_btn)
        # The delete gate explains itself in a tooltip, so tooltips must show.
        menu.setToolTipsVisible(True)
        self.menu_actions = self._fill_section_actions(menu)
        self.more_btn.setMenu(menu)
        self.add_actions(None, self.more_btn)
        # The filing fact is the way into the dialog that sets it.
        self.facts.link_activated.connect(self._on_fact_link)

        self._pass: TransectPass | None = None

    def _section_action_specs(self) -> tuple[tuple[str | None, str, object], ...]:
        """Everything the menu offers on this section, in one list.

        A None key is a separator.
        """
        return (
            ("retrim", "Adjust trim…", self._emit_retrim),
            ("reassign", "Change transect…", self._emit_reassign),
            (None, "", None),
            ("delete", "Delete section", self._emit_delete),
        )

    def _fill_section_actions(self, menu: QMenu) -> dict[str, QAction]:
        actions = {}
        for key, label, slot in self._section_action_specs():
            if key is None:
                menu.addSeparator()
                continue
            actions[key] = menu.addAction(label, slot)
        return actions

    @property
    def pass_(self) -> TransectPass | None:
        return self._pass

    def _pass_id(self) -> str:
        return "" if self._pass is None else str(self._pass.id)

    def _emit_retrim(self) -> None:
        self.retrim_requested.emit(self._pass_id())

    def _emit_reassign(self) -> None:
        self.reassign_requested.emit(self._pass_id())

    def _emit_delete(self) -> None:
        self.delete_requested.emit(self._pass_id())

    def _on_fact_link(self, href: str) -> None:
        if href == _FILING_LINK:
            self._emit_reassign()

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
        # Transect and direction on one row, as one link. They are set together
        # in one dialog, and which way a swim went means nothing without the
        # line it went along. Cart membership is not here at all: the section's
        # own row carries that control and shows its state on it.
        filing = f"{transect_name or 'Unassigned'} · {direction_html(pass_.direction)}"
        rows = [
            ("Clip", clip_name),
            ("Transect", fact_link(filing, _FILING_LINK)),
            ("Length", _length(pass_)),
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
        if not runs:
            # In the list rather than instead of it, so the pane keeps its shape
            # and the empty case is answered where the answer would appear.
            empty = QListWidgetItem(
                "Not processed yet. Add it to the cart to run it."
                if not in_cart
                else "Not processed yet. It is in the cart for the next session."
            )
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            empty.setForeground(QColor(TEXT_MUTED))
            self.run_list.addItem(empty)

        # A section with runs is the record of what they processed, so it cannot
        # go while they are still there.
        delete = self.menu_actions["delete"]
        delete.setEnabled(not runs)
        delete.setToolTip(
            "This section has runs. Delete them in Browse first."
            if runs
            else "Remove this cut. The clip itself is left alone."
        )
        self._pass = pass_

    def clear(self) -> None:
        super().clear()
        self.run_list.clear()
        self._pass = None
