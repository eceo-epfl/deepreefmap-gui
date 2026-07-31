"""Browse: every run in the output root, by run, transect, or video."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.theme import (
    BORDER,
    BUTTON,
    GUTTER,
    PRIMARY,
    RADIUS_SM,
    SURFACE_HI,
    TEXT_MUTED,
    TEXT_SECONDARY,
    WINDOW,
    WINDOW_TEXT,
)
from deepreefmap_gui.core.widgets import EmptyState, FilterChips, section_card
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.map.overlays import transect_overlays
from deepreefmap_gui.map.widget import SlippyMapWidget
from deepreefmap_gui.profiling.eta import format_duration
from deepreefmap_gui.profiling.system_probe import format_bytes
from deepreefmap_gui.runs.run_cards import points_label, related_run_counts
from deepreefmap_gui.runs.run_detail import RunDetailPanel
from deepreefmap_gui.runs.run_table import COL_NAME, RunTable
from deepreefmap_gui.survey import catalogue
from deepreefmap_gui.survey.catalogue import FacetGroup, RunEntry
from deepreefmap_gui.survey.models.transect_pass import PASS_DIRECTIONS

logger = logging.getLogger(__name__)

# How the runs are grouped, not what they are: the clip library is its own
# workspace now, so every entry here is a way of arranging the same runs.
# Keys are persisted and test-pinned; only the labels say what each view does.
_FACETS = (
    ("runs", "All runs"),
    ("transects", "By transect"),
    ("videos", "By video"),
)

# Facets whose left rail groups runs into a tree. "runs" has no grouping, so its
# rail is hidden.
_GROUPED_FACETS = ("transects", "videos")

_RAIL_TITLES = {"transects": "Transects", "videos": "Videos"}

# Outcome filters over the listed runs. Counts come from the scan, so a chip
# reading "Failed 3" answers the question without being clicked.
_STATUS_FILTERS = (
    ("all", "All"),
    (catalogue.RUN_SUCCEEDED, "Completed"),
    (catalogue.RUN_FAILED, "Failed"),
    (catalogue.RUN_UNFINISHED, "Unfinished"),
)

# Right-pane pages inside the runs stack.
_RUN_LIST_PAGE, _EMPTY_PAGE = 0, 1

# Detail pane pages: nothing selected, a run, a transect.
_DETAIL_EMPTY, _DETAIL_RUN, _DETAIL_TRANSECT = 0, 1, 2

# What a dropped file has to be to queue as a pass.
_VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv"}

_GROUP_KEY_ROLE = Qt.ItemDataRole.UserRole

# Below this the splitter has not been laid out yet, so its width says nothing
# about how much room Browse actually has.
_SPLIT_MIN_TOTAL = 400

# Wide enough for a transect name or a GoPro filename to survive elision; the
# old 260 with a tree indent left "GX0100..." and a horizontal scrollbar.
_RAIL_WIDTH = 300

# Below this the rail is not showing names any more, so a remembered width this
# small is a layout artefact rather than a choice the user made.
_RAIL_MIN_WIDTH = 200

# Tall enough to hold a transect and the water around it; the list below it is
# what grows when the rail is dragged wider.
_RAIL_MAP_HEIGHT = 240
_RAIL_MAP_MIN_HEIGHT = 140

# How much of the space left over from the rail the detail pane takes. The run
# table is the page and gets the rest; the pane holds one run's facts and its
# ortho strip, which is not a 50/50 amount of content.
_DETAIL_SHARE = 0.30

# Grouping by transect puts a chart and a stats table in the pane instead, and
# those do need close to half.
_DETAIL_SHARE_ANALYSIS = 0.45

# Narrow enough that the proportion still holds in the advanced sidebar,
# which is a fraction of the width simple mode gives Browse.
_DETAIL_MIN_WIDTH = 260


def _facet_qss(*, first: bool, last: bool) -> str:
    """One segment of the joined facet switch; only the outer corners round."""
    corners = ""
    if first:
        corners += f"border-top-left-radius: {RADIUS_SM}px;"
        corners += f"border-bottom-left-radius: {RADIUS_SM}px;"
    else:
        corners += "border-left: none;"
    if last:
        corners += f"border-top-right-radius: {RADIUS_SM}px;"
        corners += f"border-bottom-right-radius: {RADIUS_SM}px;"
    return (
        f"QToolButton {{ border: 1px solid {BORDER}; border-radius: 0; {corners}"
        f" padding: 4px 8px; background: {BUTTON}; color: {TEXT_MUTED}; }}"
        f" QToolButton:hover {{ background: {SURFACE_HI}; color: {WINDOW_TEXT}; }}"
        f" QToolButton:checked {{ background: {PRIMARY}; color: {WINDOW};"
        " font-weight: 600; }"
    )


class DataManagerMixin(MixinBase):
    """DeepReefMapWindow methods for Browse, one panel hosted by both modes."""

    _data_facet: str = "runs"
    _data_status_filter: str = "all"
    _data_selected_key: tuple | None = None
    _data_rebuilt_root: Path | None = None
    _data_rail_shown: bool | None = None
    _data_split_user_sized: bool = False
    _data_split_applying: bool = False

    def _build_data_panel(self) -> QWidget:
        self._data_entries: list[RunEntry] = []
        self._data_groups: dict[tuple, list[RunEntry]] = {}
        self._run_size_cache: dict[str, int] = {}
        self._data_sizes_scan_running = False

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(GUTTER)

        # Facets and the disk total sit above the split, so neither disappears
        # with the rail when a facet has no grouping to show.
        top_row = QHBoxLayout()
        top_row.setSpacing(GUTTER)
        facet_row = QHBoxLayout()
        facet_row.setSpacing(0)
        group = QButtonGroup(panel)
        group.setExclusive(True)
        self._data_facet_buttons: dict[str, QToolButton] = {}
        for index, (name, title) in enumerate(_FACETS):
            btn = QToolButton()
            btn.setText(title)
            btn.setCheckable(True)
            # One joined control, so it reads as three views of the same data
            # rather than three unrelated buttons.
            btn.setStyleSheet(
                _facet_qss(first=index == 0, last=index == len(_FACETS) - 1)
            )
            group.addButton(btn)
            facet_row.addWidget(btn)
            btn.toggled.connect(
                lambda checked, n=name: self._on_data_facet_changed(n) if checked else None
            )
            self._data_facet_buttons[name] = btn
        top_row.addWidget(QLabel("Group"))
        top_row.addLayout(facet_row)
        self._data_search = QLineEdit()
        self._data_search.setPlaceholderText("Search runs…")
        self._data_search.setClearButtonEnabled(True)
        self._data_search.setMaximumWidth(240)
        self._data_search.textChanged.connect(lambda *_: self._rebuild_data_run_list())
        top_row.addWidget(self._data_search)
        self._data_status_chips = FilterChips(_STATUS_FILTERS)
        self._data_status_chips.changed.connect(self._on_data_status_filter_changed)
        top_row.addWidget(self._data_status_chips)
        top_row.addStretch(1)
        layout.addLayout(top_row)

        # Disk sits with the group header rather than on the filter row: the
        # filters already fill that row, and squeezing a growing byte count in
        # beside them clipped it on a narrow window.
        self._data_disk_label = QLabel("")
        self._data_disk_label.setStyleSheet(f"color: {TEXT_SECONDARY};")

        self._data_split = QSplitter(Qt.Orientation.Horizontal)
        self._data_split.setHandleWidth(GUTTER)

        # "All runs" has no grouping, so the whole rail goes rather than leaving
        # a dead column where the tree used to be.
        self._data_rail, rail_layout = section_card("Group")
        self._data_tree = QTreeWidget()
        self._data_tree.setHeaderHidden(True)
        self._data_tree.itemSelectionChanged.connect(self._on_data_tree_selection)
        self._data_tree_stack = QStackedWidget()
        self._data_tree_stack.addWidget(self._data_tree)
        self._data_tree_stack.addWidget(EmptyState("Nothing to group yet"))

        # Transects are places before they are rows, so the rail shows where they
        # are and the list underneath stays for the ones a map cannot hold: the
        # unassigned bucket, and any transect with no coordinates yet.
        self._data_map = SlippyMapWidget()
        self._data_map.setMinimumHeight(_RAIL_MAP_MIN_HEIGHT)
        self._data_map.transect_clicked.connect(self._on_data_map_transect_clicked)
        rail_split = QSplitter(Qt.Orientation.Vertical)
        rail_split.setHandleWidth(GUTTER)
        rail_split.addWidget(self._data_map)
        rail_split.addWidget(self._data_tree_stack)
        rail_split.setStretchFactor(0, 0)
        rail_split.setStretchFactor(1, 1)
        rail_split.setSizes([_RAIL_MAP_HEIGHT, 400])
        self._data_rail_split = rail_split
        rail_layout.addWidget(rail_split, 1)
        self._data_rail.setMinimumWidth(_RAIL_MIN_WIDTH)
        self._data_split.addWidget(self._data_rail)

        runs_card, runs_layout = section_card()
        header_row = QHBoxLayout()
        self._data_group_header = QLabel("")
        self._data_group_header.setStyleSheet(f"color: {TEXT_SECONDARY};")
        self._data_group_header.setWordWrap(True)
        header_row.addWidget(self._data_group_header, 1)
        header_row.addWidget(self._data_disk_label)
        runs_layout.addLayout(header_row)

        self._data_run_table = RunTable()
        self._data_run_table.itemDoubleClicked.connect(self._on_data_run_activated)
        self._data_run_table.itemSelectionChanged.connect(self._update_data_actions)
        self._data_run_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._data_run_table.customContextMenuRequested.connect(self._on_data_context_menu)

        self._data_run_stack = QStackedWidget()
        self._data_run_stack.addWidget(self._data_run_table)
        self._data_empty_state = EmptyState("No runs here yet", "Processed passes collect here.")
        self._data_run_stack.addWidget(self._data_empty_state)
        runs_layout.addWidget(self._data_run_stack, 1)

        # Two actions and a menu, rather than a row of six mostly-disabled
        # buttons: opening and finding a run are what you do constantly, the
        # rest are occasional housekeeping.
        actions = QHBoxLayout()
        actions.setSpacing(6)
        self._data_open_btn = QPushButton("Open")
        self._data_open_btn.clicked.connect(self._on_data_open_clicked)
        actions.addWidget(self._data_open_btn)
        self._data_show_btn = QPushButton("Show in folder")
        self._data_show_btn.clicked.connect(self._on_data_show_in_folder_clicked)
        actions.addWidget(self._data_show_btn)
        self._data_more_btn = QToolButton()
        self._data_more_btn.setText("More…")
        self._data_more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more_menu = QMenu(self._data_more_btn)
        self._data_rename_action = more_menu.addAction("Rename…", self._on_data_rename_clicked)
        self._data_assign_action = more_menu.addAction(
            "Assign to transect…", self._on_data_assign_clicked
        )
        more_menu.addSeparator()
        self._data_delete_action = more_menu.addAction("Delete…", self._on_data_delete_clicked)
        more_menu.addSeparator()
        # Housekeeping that belongs to the panel, not to a selection: a run
        # folder from anywhere on disk, and a re-read of the output root.
        more_menu.addAction("Open run folder…", self._on_data_open_folder_clicked)
        more_menu.addAction("Rescan output folder", self._on_data_rescan_clicked)
        self._data_more_btn.setMenu(more_menu)
        actions.addWidget(self._data_more_btn)
        actions.addStretch(1)
        runs_layout.addLayout(actions)

        # A shared drop handler queues dropped videos as passes and opens a
        # dropped run folder, rather than three widgets each learning to drop.
        for widget in (self._data_run_table, self._data_tree):
            widget.setAcceptDrops(True)
            widget.installEventFilter(self)
        self._data_split.addWidget(runs_card)

        # One detail pane, showing whichever kind of thing is selected. Transect
        # analysis lives here rather than under the list, so nothing
        # transect-shaped appears while you are grouped by video or run.
        self._data_detail_stack = QStackedWidget()
        self._data_detail_stack.addWidget(
            EmptyState("Nothing selected", "Pick a run or a transect to see its detail.")
        )
        self._run_detail = RunDetailPanel()
        self._data_detail_stack.addWidget(self._run_detail)
        # Scrolled, so the chart and stats table below it stop dictating how
        # narrow the pane may be. A splitter only divides the space left over
        # above each pane's minimum, and this page's minimum was wide enough to
        # hold Browse at half and half however wide the window got.
        analysis_scroll = QScrollArea()
        analysis_scroll.setWidgetResizable(True)
        analysis_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        analysis_scroll.setWidget(self._build_analysis_page())
        self._data_detail_stack.addWidget(analysis_scroll)
        self._data_detail_stack.setMinimumWidth(_DETAIL_MIN_WIDTH)
        self._data_split.addWidget(self._data_detail_stack)

        self._data_split.setStretchFactor(0, 0)
        self._data_split.setStretchFactor(1, 7)
        self._data_split.setStretchFactor(2, 3)
        self._data_split.splitterMoved.connect(self._on_data_split_moved)
        self._data_split.installEventFilter(self)
        self._apply_data_split_sizes(rail_visible=False)
        layout.addWidget(self._data_split, 1)
        self._update_data_actions()

        # Debounced refresh: the out-root watcher fires for every file touched
        # at the top level during a run (survey.db-wal churn included). Sizes
        # are re-measured because a running job grows its directory.
        self._data_refresh_timer = QTimer(self)
        self._data_refresh_timer.setSingleShot(True)
        self._data_refresh_timer.setInterval(300)
        self._data_refresh_timer.timeout.connect(self._on_data_watch_refresh)

        self._data_panel = panel
        self._data_facet_buttons["runs"].setChecked(True)
        return panel

    def _build_simple_data_host(self) -> QWidget:
        # The panel cards its own halves, so the host is a bare slot in both
        # modes rather than a card wrapping cards.
        self._data_host_simple = QWidget()
        host_layout = QVBoxLayout(self._data_host_simple)
        host_layout.setContentsMargins(0, 0, 0, 0)
        return self._data_host_simple

    def _host_data_panel(self, simple: bool) -> None:
        """Move the single Browse panel into whichever mode is showing."""
        host = self._data_host_simple if simple else self._data_tab
        layout = host.layout()
        if layout is not None and self._data_panel.parentWidget() is not host:
            layout.addWidget(self._data_panel)

    def _request_data_refresh(self) -> None:
        if hasattr(self, "_data_refresh_timer"):
            self._data_refresh_timer.start()

    def _data_out_root(self) -> Path:
        return Path(self._out_root_input.text()).expanduser()

    def _refresh_data_manager(self) -> None:
        if not hasattr(self, "_out_root_input") or not hasattr(self, "_data_panel"):
            return
        root = self._data_out_root()
        entries = catalogue.scan_out_root(root)
        store = None
        if root.is_dir():
            try:
                store = self._survey_store()
                if self._data_rebuilt_root != root:
                    store.rebuild_from_scan(root)
                    self._data_rebuilt_root = root
                    # A different root is a different survey, so the map earns a
                    # fresh fit rather than keeping the last one's viewport.
                    self._data_map_fitted = False
                # Crashed runs never wrote a manifest, so scan_out_root skips
                # them; surface them here so they can be seen and cleared.
                entries += catalogue.scan_incomplete_runs(
                    root, store, {e.dir_name for e in entries}
                )
                entries.sort(key=lambda e: e.sort_key, reverse=True)
                catalogue.reconcile(entries, store)
            except Exception:
                logger.exception("Survey database unavailable for %s", root)
                store = None
        self._data_entries = entries
        self._data_store_ok = store is not None
        live = {e.dir_name for e in entries}
        for name in [n for n in self._run_size_cache if n not in live]:
            del self._run_size_cache[name]
        for entry in entries:
            entry.size_bytes = self._run_size_cache.get(entry.dir_name)
        self._rebuild_data_tree()
        self._update_data_disk_label()
        self._start_data_size_scan()
        # Guarded: this runs during form construction, before the simple shell
        # that owns the header exists.
        if hasattr(self, "_section_counts"):
            self._refresh_browse_state()

    def _on_data_watch_refresh(self) -> None:
        self._run_size_cache.clear()
        self._refresh_data_manager()

    def _on_data_rescan_clicked(self) -> None:
        """Re-read the folder, including the manifest rebuild.

        The rebuild runs once per output root per session, so a run dropped in
        from a colleague's drive while the app is open is invisible until this
        clears the gate and reads it back.
        """
        self._data_rebuilt_root = None
        self._run_size_cache.clear()
        self._refresh_data_manager()
        self._status_label.setText("Rescanned the output folder.")

    def _rebuild_data_tree(self) -> None:
        tree = self._data_tree
        tree.blockSignals(True)
        try:
            tree.clear()
            self._data_groups = {}
            grouped = self._data_facet in _GROUPED_FACETS
            if not grouped:
                self._data_selected_key = None
            else:
                self._set_rail_title(_RAIL_TITLES.get(self._data_facet, "Group"))
                for facet_group in self._data_facet_groups():
                    self._add_tree_group(facet_group, None)
                self._restore_tree_selection()
                self._data_tree_stack.setCurrentIndex(
                    0 if tree.topLevelItemCount() else 1
                )
        finally:
            tree.blockSignals(False)
        self._set_rail_visible(grouped)
        self._refresh_data_map()
        self._rebuild_data_run_list()

    def _refresh_data_map(self) -> None:
        """Draw the survey's transects, highlighting whichever one is in focus.

        Only the transect facet has a map: grouping by video says nothing about
        where anything is, and an unrelated map beside that list would invite
        clicks that change the grouping out from under it.
        """
        if not hasattr(self, "_data_map"):
            return
        shown = self._data_facet == "transects"
        self._data_map.setVisible(shown)
        if not shown or not getattr(self, "_data_store_ok", False):
            return
        key = self._data_selected_key
        selected = (
            uuid.UUID(key[1]) if key is not None and key[0] == "transect" else None
        )
        try:
            overlays = transect_overlays(self._survey_store(), selected)
        except Exception:
            logger.exception("Could not build the Browse transect overlays")
            return
        self._data_map.set_transects(overlays)
        # Fit once per output root: refitting on every selection would yank the
        # map back to the whole survey each time a transect is picked.
        if not getattr(self, "_data_map_fitted", False) and overlays:
            self._data_map.fit_transects()
            self._data_map_fitted = True

    def _on_data_map_transect_clicked(self, transect_id: str) -> None:
        """A click on the map selects that transect's node, and nothing else.

        Routed through the tree rather than filtering directly, so the map, the
        tree and the analysis combo cannot end up pointing at different
        transects.
        """
        try:
            self._set_scope_transect(uuid.UUID(transect_id))
        except ValueError:
            logger.warning("Map click carried an unusable transect id: %r", transect_id)

    def _set_rail_visible(self, visible: bool) -> None:
        """Hide the whole rail, not just the tree inside it.

        Hiding only the tree left a fixed-width column with nothing in it, which
        read as a broken panel rather than as a view without groups.
        """
        # Tracked rather than read back from isVisible(): a widget in a window
        # that has not been shown yet reports False whatever we asked for, so
        # the first hide would look like a no-op and never take effect.
        if getattr(self, "_data_rail_shown", None) == visible:
            # The rail has not moved, but the detail pane's share follows the
            # facet, and transects <-> videos changes that without changing this.
            self._apply_data_split_sizes(rail_visible=visible)
            return
        self._data_rail_shown = visible
        if not visible:
            # Qt collapses a hidden splitter child to zero and does not restore
            # the width by itself, so remember it. Only a width the splitter has
            # actually laid out counts: before the first show it reports a size
            # hint of a hundred-odd pixels, and remembering that pinned the rail
            # narrow enough to elide every name in it for the rest of the session.
            sizes = self._data_split.sizes()
            if sizes and sizes[0] >= _RAIL_MIN_WIDTH:
                self._data_rail_width = sizes[0]
        self._data_rail.setVisible(visible)
        self._apply_data_split_sizes(rail_visible=visible)

    def _apply_data_split_sizes(self, *, rail_visible: bool) -> None:
        """Divide Browse between the rail, the run table, and the detail pane.

        The table is the page: it is what you scan, sort and select in, so it
        takes the bulk. The detail pane only has to hold one run's facts and its
        ortho strip. Grouping by transect is the exception — there the pane holds
        a chart and a stats table, which need closer to half.

        The sizes are set outright rather than left to stretch factors. A
        splitter only shares out the space *above* each pane's minimum hint, and
        the analysis page's hint is ~320px, so stretch alone settled at roughly
        half and half however wide the window got.
        """
        if getattr(self, "_data_split_user_sized", False):
            return
        # The splitter's own width, not the sum of its sizes: during
        # construction the panes have not been laid out and the sum is a
        # placeholder, which is what the fallback is for.
        total = self._data_split.width()
        if total < _SPLIT_MIN_TOTAL:
            total = sum(self._data_split.sizes()) or 1200
        rail = getattr(self, "_data_rail_width", _RAIL_WIDTH) if rail_visible else 0
        share = _DETAIL_SHARE_ANALYSIS if self._data_facet == "transects" else _DETAIL_SHARE
        detail = max(_DETAIL_MIN_WIDTH, int((total - rail) * share))
        # Every pane gets a size. Handing setSizes fewer entries than the
        # splitter has children leaves the rest at zero, which collapsed the
        # detail pane to a hairline the moment a grouping was picked.
        self._data_split_applying = True
        try:
            self._data_split.setSizes([rail, max(280, total - rail - detail), detail])
        finally:
            self._data_split_applying = False

    def _data_split_event_filter(self, obj, event) -> None:
        """Re-divide Browse when the splitter is resized, in either UI mode.

        Browse is re-parented between the simple shell and the advanced tab, and
        each host hands it a different width; recomputing on resize is what makes
        the proportion the same in both.
        """
        if obj is self._data_split and event.type() == QEvent.Type.Resize:
            self._apply_data_split_sizes(rail_visible=bool(self._data_rail_shown))

    def _on_data_split_moved(self, *_args) -> None:
        """A dragged handle is a decision; stop overriding it on every resize."""
        if not getattr(self, "_data_split_applying", False):
            self._data_split_user_sized = True

    def _set_rail_title(self, title: str) -> None:
        label = self._data_rail.findChild(QLabel)
        if label is not None:
            label.setText(title)

    def _data_facet_groups(self) -> list[FacetGroup]:
        if self._data_facet == "transects":
            transects = []
            if getattr(self, "_data_store_ok", False):
                try:
                    transects = self._survey_store().list_transects()
                except Exception:
                    logger.exception("Could not list transects")
            return catalogue.transects_facet(self._data_entries, transects)
        return catalogue.videos_facet(self._data_entries)

    def _add_tree_group(self, group: FacetGroup, parent: QTreeWidgetItem | None) -> None:
        count = len(group.all_entries())
        item = QTreeWidgetItem([f"{group.title}  ({count})"])
        item.setData(0, _GROUP_KEY_ROLE, group.key)
        self._data_groups[group.key] = group.all_entries()
        if parent is None:
            self._data_tree.addTopLevelItem(item)
        else:
            parent.addChild(item)
        for child in group.children:
            self._add_tree_group(child, item)
        item.setExpanded(True)

    def _restore_tree_selection(self) -> None:
        if self._data_selected_key is None:
            return
        matches = self._data_tree.findItems(
            "", Qt.MatchFlag.MatchContains | Qt.MatchFlag.MatchRecursive
        )
        for item in matches:
            if item.data(0, _GROUP_KEY_ROLE) == self._data_selected_key:
                self._data_tree.setCurrentItem(item)
                return
        self._data_selected_key = None

    def _focus_data_on_transect(self, transect_id: uuid.UUID) -> None:
        """Point the browser at one transect."""
        if not hasattr(self, "_data_tree"):
            return
        # Facet and key first, then the button: the toggle short-circuits when
        # the facet already matches, so the key survives.
        self._data_facet = "transects"
        self._data_selected_key = ("transect", str(transect_id))
        self._data_facet_buttons["transects"].setChecked(True)
        self._rebuild_data_tree()

    def _set_scope_transect(self, transect_id: uuid.UUID | None) -> None:
        """One transect in focus across every widget that has an opinion.

        The Browse page carries a browser tree and an analysis combo that both
        pick a transect; left independent they would reproduce on one page the
        duplication being removed from another.
        """
        if getattr(self, "_scope_syncing", False):
            return
        self._scope_syncing = True
        try:
            self._scope_transect_id = transect_id
            if transect_id is not None:
                self._focus_data_on_transect(transect_id)
            combo = getattr(self, "_analysis_transect_combo", None)
            if combo is not None:
                wanted = str(transect_id) if transect_id is not None else None
                for index in range(combo.count()):
                    if combo.itemData(index) == wanted:
                        combo.setCurrentIndex(index)
                        break
        finally:
            self._scope_syncing = False

    def _update_data_actions(self) -> None:
        """Gate each action on the selection, and point the detail pane at it.

        A run with no manifest can only be shown in a folder or deleted, so the
        openers stay off for one.
        """
        current = self._data_selected_entry()
        selected = self._data_selected_entries()
        complete_current = current is not None and not current.incomplete
        self._data_open_btn.setEnabled(complete_current)
        self._data_rename_action.setEnabled(complete_current)
        # Showing a crashed run in its folder is exactly how you inspect it.
        self._data_show_btn.setEnabled(current is not None)
        self._data_assign_action.setEnabled(any(not e.incomplete for e in selected))
        self._data_delete_action.setEnabled(bool(selected))
        self._refresh_data_detail()

    def _refresh_data_detail(self) -> None:
        """Show the selected run, else the selected transect, else nothing.

        The run wins when both are selected: it is the more specific of the two,
        and it is what was clicked last to get here.
        """
        entry = self._data_selected_entry()
        if entry is not None:
            self._run_detail.show_entry(entry)
            self._data_detail_stack.setCurrentIndex(_DETAIL_RUN)
            return
        key = self._data_selected_key
        if self._data_facet == "transects" and key is not None and key[0] == "transect":
            self._data_detail_stack.setCurrentIndex(_DETAIL_TRANSECT)
            return
        self._data_detail_stack.setCurrentIndex(_DETAIL_EMPTY)

    def _on_data_facet_changed(self, name: str) -> None:
        # _focus_data_on_transect sets the facet and the key together and then
        # checks the button; without this the check would land here first and
        # throw the key away, rebuilding the tree twice for one selection.
        if name == self._data_facet:
            return
        self._data_facet = name
        self._data_selected_key = None
        self._data_split_user_sized = False
        if hasattr(self, "_data_tree"):
            self._rebuild_data_tree()

    def _on_data_tree_selection(self) -> None:
        items = self._data_tree.selectedItems()
        key = items[0].data(0, _GROUP_KEY_ROLE) if items else None
        self._data_selected_key = key
        self._refresh_data_map()
        self._rebuild_data_run_list()
        # Picking a transect here drives the comparison below it on the same page.
        if key is not None and len(key) == 2 and key[0] == "transect":
            self._set_scope_transect(uuid.UUID(key[1]))

    def _data_grouped_entries(self) -> list[RunEntry]:
        """The runs the current grouping selection covers, before filtering."""
        if self._data_facet == "runs" or self._data_selected_key is None:
            return self._data_entries
        return self._data_groups.get(self._data_selected_key, [])

    def _data_listed_entries(self) -> list[RunEntry]:
        """What the list actually shows: the group, narrowed by outcome and search."""
        entries = self._data_grouped_entries()
        if self._data_status_filter != "all":
            entries = [
                e for e in entries if catalogue.entry_outcome(e) == self._data_status_filter
            ]
        needle = self._data_search.text().strip().lower()
        if needle:
            entries = [e for e in entries if needle in self._entry_search_text(e)]
        return entries

    @staticmethod
    def _entry_search_text(entry: RunEntry) -> str:
        """Everything about a run worth typing to find it again."""
        return " ".join(
            part.lower()
            for part in (
                entry.display_name,
                entry.dir_name,
                entry.video_name or "",
                entry.transect_name or "",
            )
        )

    def _on_data_status_filter_changed(self, key: str) -> None:
        self._data_status_filter = key
        self._rebuild_data_run_list()

    def _refresh_data_status_counts(self) -> None:
        """Count over the grouping, not the whole root: the chips filter what is listed."""
        entries = self._data_grouped_entries()
        counts = {key: 0 for key, _ in _STATUS_FILTERS}
        counts["all"] = len(entries)
        for entry in entries:
            outcome = catalogue.entry_outcome(entry)
            counts[outcome] = counts.get(outcome, 0) + 1
        self._data_status_chips.set_counts(counts)

    def _rebuild_data_run_list(self) -> None:
        self._refresh_data_status_counts()
        listed = self._data_listed_entries()
        related = related_run_counts([(e.run_dir, e.manifest) for e in self._data_entries])
        self._data_run_table.set_entries(listed, related)
        self._data_group_header.setText(self._data_header_text(listed))
        self._data_group_header.setVisible(bool(listed))
        # An empty list means one of two different things, and saying which is
        # the difference between "nothing here" and "nothing matches".
        if not listed and self._data_grouped_entries():
            self._data_empty_state.set_text(
                "No runs match these filters", "Clear the search or pick a different outcome."
            )
        else:
            self._data_empty_state.set_text("No runs here yet", "Processed passes collect here.")
        self._data_run_stack.setCurrentIndex(_RUN_LIST_PAGE if listed else _EMPTY_PAGE)
        self._update_data_actions()

    def _data_header_text(self, listed: list[RunEntry]) -> str:
        if not listed:
            return ""
        stats = catalogue.group_stats(listed)
        bits = [f"{stats.run_count} run{'s' if stats.run_count != 1 else ''}"]
        if stats.duration_range:
            lo, hi = stats.duration_range
            bits.append(
                f"runtime {format_duration(lo)}"
                + (f" – {format_duration(hi)}" if hi != lo else "")
            )
        if stats.point_range:
            lo_p, hi_p = stats.point_range
            bits.append(
                f"{points_label(lo_p)}–{points_label(hi_p)} points"
                if hi_p != lo_p
                else f"{points_label(hi_p)} points"
            )
        if stats.total_bytes is not None:
            bits.append(f"{format_bytes(stats.total_bytes)} on disk")
        return " · ".join(bits)

    def _on_data_run_activated(self, _item) -> None:
        self._on_data_open_clicked()

    def _on_data_open_clicked(self) -> None:
        run_dir = self._data_run_table.current_run_dir()
        if run_dir:
            self._load_run_from_dir(Path(run_dir))

    def _load_run_from_dir(self, path: Path) -> None:
        """Load a run directory into the viewer, guarded against a live run.

        The single seam every open path routes through: the run cards, the
        open-folder action, and a dropped folder all end up here.
        """
        # Browse stays reachable while a batch runs, so opening an old run here
        # would take the viewer away from the run currently streaming into it.
        if self._run_in_flight():
            self._status_label.setText("Wait for the batch to finish before opening a run.")
            return
        # Banner first, straight from the manifest, so the click lands
        # instantly even when the load itself takes a while.
        manifest_path = path / "run_manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                self._show_run_meta_banner(manifest, path, include_disk_size=False)
            except Exception:
                self._hide_run_meta_banner()
        self._auto_load_run(path)

    def _on_data_open_folder_clicked(self) -> None:
        """Open a run directory picked from disk, wherever it sits."""
        if self._run_in_flight():
            self._status_label.setText("Wait for the batch to finish before opening a run.")
            return
        path = QFileDialog.getExistingDirectory(
            self, "Open run folder", self._out_root_input.text()
        )
        if path:
            self._load_run_from_dir(Path(path))

    # --- Actions ---

    def _data_selected_entry(self) -> RunEntry | None:
        run_dir = self._data_run_table.current_run_dir()
        if run_dir is None:
            return None
        for entry in self._data_entries:
            if str(entry.run_dir) == run_dir:
                return entry
        return None

    def _data_selected_entries(self) -> list[RunEntry]:
        """Every run picked in the table, for the actions that act on many."""
        run_dirs = self._data_run_table.selected_run_dirs()
        return [e for e in self._data_entries if str(e.run_dir) in run_dirs]

    def _on_data_show_in_folder_clicked(self) -> None:
        entry = self._data_selected_entry()
        if entry is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(entry.run_dir)))

    def _on_data_context_menu(self, pos) -> None:
        item = self._data_run_table.itemAt(pos)
        # Leave an existing multi-selection alone: collapsing it to the
        # right-clicked row would throw away the runs the menu is about to act on.
        if item is not None and not item.isSelected():
            self._data_run_table.setCurrentCell(item.row(), COL_NAME)
        menu = QMenu(self._data_run_table)
        menu.addAction("Open", self._on_data_open_clicked)
        menu.addAction("Show in folder", self._on_data_show_in_folder_clicked)
        menu.addAction("Rename…", self._on_data_rename_clicked)
        menu.addAction("Assign to transect…", self._on_data_assign_clicked)
        menu.addSeparator()
        menu.addAction("Delete…", self._on_data_delete_clicked)
        menu.exec(self._data_run_table.mapToGlobal(pos))

    # --- Drag and drop ---

    def _data_drop_event_filter(self, obj, event) -> bool:
        """Filter file drops on the run list, group tree and pass table.

        One handler serves all three rather than subclassing each widget. Video
        files queue as passes; a run folder opens. Returns True when the event is
        consumed. DeepReefMapWindow.eventFilter calls this, because QObject owns
        eventFilter earlier in the MRO than this mixin.
        """
        etype = event.type()
        if etype in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
                return True
            return False
        if etype == QEvent.Type.Drop and event.mimeData().hasUrls():
            paths = [
                Path(url.toLocalFile())
                for url in event.mimeData().urls()
                if url.isLocalFile()
            ]
            self._handle_data_drop(paths)
            event.acceptProposedAction()
            return True
        return False

    def _handle_data_drop(self, paths: list[Path]) -> None:
        videos = [
            p for p in paths if p.is_file() and p.suffix.lower() in _VIDEO_SUFFIXES
        ]
        run_dirs = [p for p in paths if p.is_dir()]
        if videos:
            # Probing happens off the GUI thread, so the rows and the count both
            # land in _on_videos_probed rather than here.
            self._add_video_paths([str(p) for p in videos])
            return
        if run_dirs:
            self._load_run_from_dir(run_dirs[0])
            if len(run_dirs) > 1:
                self._status_label.setText("Dropped several folders; opened the first.")
            return
        self._status_label.setText("Drop video files or a run folder here.")

    def _on_data_rename_clicked(self) -> None:
        entry = self._data_selected_entry()
        if entry is None:
            return
        current = (entry.manifest.get("name") or "").strip() or entry.dir_name
        new_name, ok = QInputDialog.getText(self, "Rename run", "Run name:", text=current)
        if not ok or not new_name.strip():
            return
        try:
            manifest = catalogue.rename_run(entry.run_dir, new_name)
        except Exception as exc:
            self._status_label.setText(f"Rename failed: {exc}")
            logger.exception("Failed to rename run")
            return
        if self._active_run_dir == entry.run_dir:
            self._active_run_manifest = manifest
            self._show_run_meta_banner(manifest, entry.run_dir, include_disk_size=False)
        self._status_label.setText(f"Renamed run to '{new_name.strip()}'.")
        self._refresh_data_manager()

    def _on_data_delete_clicked(self) -> None:
        entries = self._data_selected_entries()
        if not entries:
            return
        if self._active_run_dir is not None and any(
            e.run_dir == self._active_run_dir for e in entries
        ):
            QMessageBox.information(
                self, "Delete run", "One of these runs is open. Close it first with the + button."
            )
            return
        if self._pipeline_thread is not None and self._pipeline_thread.is_alive():
            QMessageBox.information(
                self, "Delete run", "Wait for the current run to finish."
            )
            return
        answer = QMessageBox.question(self, "Delete run", self._delete_prompt(entries))
        if answer != QMessageBox.StandardButton.Yes:
            return
        store = self._survey_store() if getattr(self, "_data_store_ok", False) else None
        deleted = 0
        for entry in entries:
            try:
                # A crashed run has no manifest, so it needs the manifest-free
                # remover; both keep delete_run_dir's direct-child guard.
                if entry.incomplete:
                    catalogue.delete_run_dir(self._data_out_root(), entry.run_dir, store)
                else:
                    catalogue.delete_run(self._data_out_root(), entry.run_dir, store)
            except Exception as exc:
                self._status_label.setText(f"Delete failed: {exc}")
                logger.exception("Failed to delete run")
                continue
            self._run_size_cache.pop(entry.dir_name, None)
            deleted += 1
        if deleted:
            self._status_label.setText(f"Deleted {deleted} run{'s' if deleted != 1 else ''}.")
        self._refresh_data_manager()

    def _delete_prompt(self, entries: list[RunEntry]) -> str:
        if len(entries) == 1:
            entry = entries[0]
            size = self._run_size_cache.get(entry.dir_name)
            size_txt = f" ({format_bytes(size)})" if size is not None else ""
            return f"Delete '{entry.display_name}'{size_txt}?\nThis cannot be undone."
        return f"Delete {len(entries)} runs?\nThis cannot be undone."

    def _data_assign_targets(self) -> list[RunEntry]:
        """The whole footage group of every selected run, or the selected tree
        group, so reruns of the same pass move together."""
        # A crashed run has no time window, so it cannot become a pass; leave it
        # out rather than fail the whole assignment.
        selected = [e for e in self._data_selected_entries() if not e.incomplete]
        if selected:
            keys = {catalogue.group_key(e) for e in selected}
            return [e for e in self._data_entries if catalogue.group_key(e) in keys]
        if self._data_selected_key is not None:
            return self._data_groups.get(self._data_selected_key, [])
        return []

    def _ask_assign_target(self, transects: list) -> tuple[uuid.UUID, str] | None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Assign to transect")
        form = QFormLayout(dialog)
        transect_combo = QComboBox()
        for transect in transects:
            transect_combo.addItem(transect.name, userData=transect.id)
        direction_combo = QComboBox()
        direction_combo.addItems(list(PASS_DIRECTIONS))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow("Transect", transect_combo)
        form.addRow("Direction", direction_combo)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return transect_combo.currentData(), direction_combo.currentText()

    def _on_data_assign_clicked(self) -> None:
        entries = self._data_assign_targets()
        if not entries:
            return
        if not getattr(self, "_data_store_ok", False):
            self._status_label.setText("Survey database unavailable; cannot assign.")
            return
        store = self._survey_store()
        transects = store.list_transects()
        if not transects:
            QMessageBox.information(
                self, "Assign to transect", "Create a transect in the Plan section first."
            )
            return
        target = self._ask_assign_target(transects)
        if target is None:
            return
        transect_id, direction = target
        try:
            catalogue.assign_to_transect(store, entries, transect_id, direction)
        except Exception as exc:
            self._status_label.setText(f"Assign failed: {exc}")
            logger.exception("Failed to assign runs to transect")
            return
        n = len(entries)
        name = next(t.name for t in transects if t.id == transect_id)
        self._status_label.setText(f"Assigned {n} run{'s' if n != 1 else ''} to '{name}'.")
        self._refresh_data_manager()
        self._refresh_transect_list()
        self._refresh_survey_analysis()

    def _queue_video_path(self, path: str | None) -> None:
        """Turn a library clip into a fresh pass, off the GUI thread."""
        if not path:
            return
        self._add_video_paths([path])

    # --- Disk sizes ---

    def _start_data_size_scan(self) -> None:
        if self._data_sizes_scan_running:
            return
        todo = [
            (e.dir_name, e.run_dir)
            for e in self._data_entries
            if e.dir_name not in self._run_size_cache
        ]
        if not todo:
            return
        self._data_sizes_scan_running = True

        def worker() -> None:
            sizes: dict[str, int] = {}
            for name, path in todo:
                try:
                    sizes[name] = catalogue.dir_size_bytes(path)
                except Exception:
                    logger.exception("Could not size %s", path)
            # Widgets are off limits here; the Signal hands over to the GUI thread.
            self._sig_run_sizes_done.emit(sizes)

        threading.Thread(target=worker, daemon=True, name="run-size-scan").start()

    def _apply_run_sizes(self, sizes: dict) -> None:
        self._data_sizes_scan_running = False
        self._run_size_cache.update(sizes)
        for entry in self._data_entries:
            entry.size_bytes = self._run_size_cache.get(entry.dir_name)
        self._update_data_disk_label()
        self._rebuild_data_run_list()

    def _update_data_disk_label(self) -> None:
        sized = [e for e in self._data_entries if e.size_bytes is not None]
        if not sized:
            self._data_disk_label.setText("")
            return
        total = sum(e.size_bytes for e in sized if e.size_bytes is not None)
        n = len(self._data_entries)
        suffix = "" if len(sized) == n else " so far"
        self._data_disk_label.setText(
            f"Space used: {format_bytes(total)} across {n} run{'s' if n != 1 else ''}{suffix}"
        )
