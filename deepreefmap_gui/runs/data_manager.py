"""Data section: browse every run in the output root by run, transect, or video."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
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
from deepreefmap_gui.core.widgets import EmptyState, section_card
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.runs.run_cards import (
    RUN_META_ROLE,
    RunCardDelegate,
    build_run_card_meta,
    format_run_metadata,
    related_run_counts,
)
from deepreefmap_gui.profiling.eta import format_duration
from deepreefmap_gui.profiling.system_probe import format_bytes
from deepreefmap_gui.survey import catalogue
from deepreefmap_gui.survey.catalogue import FacetGroup, RunEntry
from deepreefmap_gui.survey.models.transect_pass import PASS_DIRECTIONS

logger = logging.getLogger(__name__)

# Keys are persisted and test-pinned; only the labels say what each view does.
_FACETS = (("runs", "All runs"), ("transects", "By transect"), ("videos", "By video"))

_RAIL_TITLES = {"transects": "Transects", "videos": "Videos"}

_GROUP_KEY_ROLE = Qt.ItemDataRole.UserRole


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
    """DeepReefMapWindow methods for the Data section, hosted by both modes."""

    _data_facet: str = "runs"
    _data_selected_key: tuple | None = None
    _data_rebuilt_root: Path | None = None

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
        top_row.addLayout(facet_row)
        top_row.addStretch(1)
        self._data_disk_label = QLabel("")
        self._data_disk_label.setStyleSheet(f"color: {TEXT_SECONDARY};")
        top_row.addWidget(self._data_disk_label)
        layout.addLayout(top_row)

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
        rail_layout.addWidget(self._data_tree_stack, 1)
        self._data_split.addWidget(self._data_rail)

        runs_card, runs_layout = section_card()
        self._data_group_header = QLabel("")
        self._data_group_header.setStyleSheet(f"color: {TEXT_SECONDARY};")
        self._data_group_header.setWordWrap(True)
        runs_layout.addWidget(self._data_group_header)

        self._data_run_list = QListWidget()
        self._data_run_list.setItemDelegate(RunCardDelegate(self._data_run_list))
        self._data_run_list.itemDoubleClicked.connect(self._on_data_run_activated)
        self._data_run_list.itemSelectionChanged.connect(self._update_data_actions)
        self._data_run_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._data_run_list.customContextMenuRequested.connect(self._on_data_context_menu)
        self._data_run_stack = QStackedWidget()
        self._data_run_stack.addWidget(self._data_run_list)
        self._data_run_stack.addWidget(
            EmptyState("No runs here yet", "Processed passes collect here.")
        )
        runs_layout.addWidget(self._data_run_stack, 1)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        self._data_open_btn = QPushButton("Open")
        self._data_open_btn.clicked.connect(self._on_data_open_clicked)
        actions.addWidget(self._data_open_btn)
        self._data_rename_btn = QPushButton("Rename…")
        self._data_rename_btn.clicked.connect(self._on_data_rename_clicked)
        actions.addWidget(self._data_rename_btn)
        self._data_assign_btn = QPushButton("Assign to transect…")
        self._data_assign_btn.clicked.connect(self._on_data_assign_clicked)
        actions.addWidget(self._data_assign_btn)
        self._data_delete_btn = QPushButton("Delete…")
        self._data_delete_btn.clicked.connect(self._on_data_delete_clicked)
        actions.addWidget(self._data_delete_btn)
        actions.addStretch(1)
        runs_layout.addLayout(actions)
        self._data_split.addWidget(runs_card)
        self._data_split.setStretchFactor(0, 0)
        self._data_split.setStretchFactor(1, 1)
        self._data_split.setSizes([260, 700])
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
        """Move the single Data panel into whichever mode is showing."""
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

    def _rebuild_data_tree(self) -> None:
        tree = self._data_tree
        tree.blockSignals(True)
        try:
            tree.clear()
            self._data_groups = {}
            grouped = self._data_facet != "runs"
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
        self._rebuild_data_run_list()

    def _set_rail_visible(self, visible: bool) -> None:
        """Hide the whole rail, not just the tree inside it.

        Hiding only the tree left a fixed-width column with nothing in it, which
        read as a broken panel rather than as a view without groups.
        """
        # Tracked rather than read back from isVisible(): a widget in a window
        # that has not been shown yet reports False whatever we asked for, so
        # the first hide would look like a no-op and never take effect.
        if getattr(self, "_data_rail_shown", None) == visible:
            return
        self._data_rail_shown = visible
        if not visible:
            # Qt collapses a hidden splitter child to zero and does not restore
            # the width by itself, so remember it.
            sizes = self._data_split.sizes()
            if sizes and sizes[0]:
                self._data_rail_width = sizes[0]
        self._data_rail.setVisible(visible)
        if visible:
            width = getattr(self, "_data_rail_width", 260)
            total = sum(self._data_split.sizes()) or 960
            self._data_split.setSizes([width, max(200, total - width)])

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
        """The four actions all need a selected run, so they say when there is none."""
        has = self._data_run_list.currentItem() is not None
        for button in (
            self._data_open_btn,
            self._data_rename_btn,
            self._data_assign_btn,
            self._data_delete_btn,
        ):
            button.setEnabled(has)

    def _on_data_facet_changed(self, name: str) -> None:
        # _focus_data_on_transect sets the facet and the key together and then
        # checks the button; without this the check would land here first and
        # throw the key away, rebuilding the tree twice for one selection.
        if name == self._data_facet:
            return
        self._data_facet = name
        self._data_selected_key = None
        if hasattr(self, "_data_tree"):
            self._rebuild_data_tree()

    def _on_data_tree_selection(self) -> None:
        items = self._data_tree.selectedItems()
        key = items[0].data(0, _GROUP_KEY_ROLE) if items else None
        self._data_selected_key = key
        self._rebuild_data_run_list()
        # Picking a transect here drives the comparison below it on the same page.
        if key is not None and len(key) == 2 and key[0] == "transect":
            self._set_scope_transect(uuid.UUID(key[1]))

    def _data_listed_entries(self) -> list[RunEntry]:
        if self._data_facet == "runs" or self._data_selected_key is None:
            return self._data_entries
        return self._data_groups.get(self._data_selected_key, [])

    def _rebuild_data_run_list(self) -> None:
        listed = self._data_listed_entries()
        related = related_run_counts([(e.run_dir, e.manifest) for e in self._data_entries])
        current = self._data_run_list.currentItem()
        keep = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        self._data_run_list.clear()
        for entry in listed:
            item = QListWidgetItem(entry.display_name)
            item.setData(Qt.ItemDataRole.UserRole, str(entry.run_dir))
            tooltip = format_run_metadata(
                entry.manifest,
                entry.run_dir,
                include_disk_size=entry.size_bytes is not None,
                disk_bytes=entry.size_bytes,
            )
            if entry.moved_from:
                tooltip += f"<br><i>Recorded at run time as: {entry.moved_from}</i>"
            item.setData(Qt.ItemDataRole.ToolTipRole, tooltip)
            meta = build_run_card_meta(entry.manifest, entry.run_dir, related.get(entry.run_dir, 0))
            if entry.size_bytes is not None:
                meta["facts"] = "  ·  ".join(
                    filter(None, [meta["facts"], format_bytes(entry.size_bytes)])
                )
            item.setData(RUN_META_ROLE, meta)
            self._data_run_list.addItem(item)
            if keep is not None and str(entry.run_dir) == keep:
                self._data_run_list.setCurrentItem(item)
        self._data_group_header.setText(self._data_header_text(listed))
        self._data_group_header.setVisible(bool(listed))
        self._data_run_stack.setCurrentIndex(0 if listed else 1)
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
                f"{_points_label(lo_p)}–{_points_label(hi_p)} points"
                if hi_p != lo_p
                else f"{_points_label(hi_p)} points"
            )
        if stats.total_bytes is not None:
            bits.append(f"{format_bytes(stats.total_bytes)} on disk")
        return " · ".join(bits)

    def _on_data_run_activated(self, item: QListWidgetItem) -> None:
        self._open_data_run(item)

    def _on_data_open_clicked(self) -> None:
        item = self._data_run_list.currentItem()
        if item is not None:
            self._open_data_run(item)

    def _open_data_run(self, item: QListWidgetItem) -> None:
        run_dir = item.data(Qt.ItemDataRole.UserRole)
        if not run_dir:
            return
        # Browse stays reachable while a batch runs, so opening an old run here
        # would take the viewer away from the run currently streaming into it.
        if self._run_in_flight():
            self._status_label.setText("Wait for the batch to finish before opening a run.")
            return
        path = Path(run_dir)
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

    # --- Actions ---

    def _data_selected_entry(self) -> RunEntry | None:
        item = self._data_run_list.currentItem()
        if item is None:
            return None
        run_dir = item.data(Qt.ItemDataRole.UserRole)
        for entry in self._data_entries:
            if str(entry.run_dir) == run_dir:
                return entry
        return None

    def _on_data_context_menu(self, pos) -> None:
        item = self._data_run_list.itemAt(pos)
        if item is not None:
            self._data_run_list.setCurrentItem(item)
        menu = QMenu(self._data_run_list)
        menu.addAction("Open", self._on_data_open_clicked)
        menu.addAction("Rename…", self._on_data_rename_clicked)
        menu.addAction("Assign to transect…", self._on_data_assign_clicked)
        menu.addSeparator()
        menu.addAction("Delete…", self._on_data_delete_clicked)
        menu.exec(self._data_run_list.mapToGlobal(pos))

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
        entry = self._data_selected_entry()
        if entry is None:
            return
        if self._active_run_dir is not None and entry.run_dir == self._active_run_dir:
            QMessageBox.information(
                self, "Delete run", "This run is open. Close it first with the + button."
            )
            return
        if self._pipeline_thread is not None and self._pipeline_thread.is_alive():
            QMessageBox.information(
                self, "Delete run", "Wait for the current run to finish."
            )
            return
        size = self._run_size_cache.get(entry.dir_name)
        size_txt = f" ({format_bytes(size)})" if size is not None else ""
        answer = QMessageBox.question(
            self,
            "Delete run",
            f"Delete '{entry.display_name}'{size_txt}?\nThis cannot be undone.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        store = self._survey_store() if getattr(self, "_data_store_ok", False) else None
        try:
            catalogue.delete_run(self._data_out_root(), entry.run_dir, store)
        except Exception as exc:
            self._status_label.setText(f"Delete failed: {exc}")
            logger.exception("Failed to delete run")
            return
        self._run_size_cache.pop(entry.dir_name, None)
        self._status_label.setText(f"Deleted '{entry.display_name}'.")
        self._refresh_data_manager()

    def _data_assign_targets(self) -> list[RunEntry]:
        """The whole footage group of the selected run, or the selected tree
        group, so reruns of the same pass move together."""
        entry = self._data_selected_entry()
        if entry is not None:
            key = catalogue.group_key(entry)
            return [e for e in self._data_entries if catalogue.group_key(e) == key]
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


def _points_label(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)
