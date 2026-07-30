"""Plan tab: create, edit, and import the transects a survey runs over."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QToolTip,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.fonts import BASE_POINT_SIZE, MONO_FONT_FAMILY
from deepreefmap_gui.core.icons import check_icon, copy_icon, crosshair_icon
from deepreefmap_gui.core.theme import (
    BORDER,
    GUTTER,
    PRIMARY,
    RADIUS,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_SECONDARY,
)
from deepreefmap_gui.core.widgets import EmptyState, section_card
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.map.overlays import OverlayTransect
from deepreefmap_gui.map.widget import SlippyMapWidget
from deepreefmap_gui.simple.progress import plan_state
from deepreefmap_gui.survey.models import (
    Transect,
    compass_point,
    haversine_m,
    initial_bearing_deg,
)
from deepreefmap_gui.survey.models.exporters import save_transects_csv
from deepreefmap_gui.survey.models.importers import (
    import_transects_csv,
    import_transects_gpx,
    parse_latlon,
)

logger = logging.getLogger(__name__)

# A trailing spacer column absorbs the slack instead of the name column, so the
# figures stay beside the transect they belong to however wide the window is.
PLAN_COLUMNS = ("Transect", "Length", "Depth", "Passes", "Runs", "")
PLAN_SPACER_COLUMN = len(PLAN_COLUMNS) - 1
# The transect a click on the map is about to draw, and the one being typed into
# the form, share this row id: neither exists in the store yet.
DRAFT_ID = "draft"
# How much of the map a transect fills when it is picked from the list. Short of
# the whole viewport so the reef either side of it stays on screen.
FOCUS_FILL = 0.6


def transect_length_text(length_m: float | None, geodesic_m: float) -> str:
    """The one length worth showing for a transect.

    A typed tape length is the cable actually laid on the reef and is what the
    run is scaled to, so it supersedes the straight-line distance between the
    GPS endpoints; the geodesic only stands in when no tape length was recorded.
    """
    if length_m:
        return f"{length_m:g} m tape"
    return f"{geodesic_m:.0f} m GPS"


def bearing_text(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """Heading from start to end, as a diver would be briefed to swim it."""
    bearing = initial_bearing_deg(lat1, lon1, lat2, lon2)
    return f"{bearing:03.0f}° {compass_point(bearing)}"


def transect_geometry_text(transect: Transect) -> str:
    """The derived line under the coordinate fields: how far, and which way."""
    return (
        f"{transect.geodesic_length_m():.0f} m between the GPS ends  ·  heading "
        f"{bearing_text(transect.start_lat, transect.start_lon, transect.end_lat, transect.end_lon)}"
    )


def transect_row_columns(transect: Transect, passes: int, runs: int) -> list[str]:
    """One row of the transect table, column by column.

    Derived from the dataclass and two counts with no per-row store query,
    because the table is rebuilt on every keystroke while a transect is typed.
    """
    return [
        transect.name,
        transect_length_text(transect.length_m, transect.geodesic_length_m()),
        f"{transect.depth_m:g} m" if transect.depth_m else "—",
        str(passes) if passes else "—",
        str(runs) if runs else "—",
        "",
    ]


def transect_tooltip(transect: Transect, passes: int = 0, runs: int = 0) -> str:
    """Coordinates, heading and notes, which the row itself has no room for."""
    lines = [
        f"<b>{transect.name}</b>",
        f"Start {transect.start_lat:.5f}, {transect.start_lon:.5f}",
        f"End {transect.end_lat:.5f}, {transect.end_lon:.5f}",
        transect_geometry_text(transect),
    ]
    if transect.length_m:
        lines.append(f"{transect.length_m:g} m tape laid")
    if transect.depth_m:
        lines.append(f"{transect.depth_m:g} m deep")
    lines.append(f"{passes} pass(es) assigned, {runs} processed")
    if transect.description:
        lines.append(f"<i>{transect.description}</i>")
    return "<br>".join(lines)


def next_transect_name(existing: Iterable[str], stem: str = "Transect") -> str:
    """First ``stem N`` not already taken.

    Names are unique in the store, so a new transect arrives already named and
    saveable; the field worker renames it only if they have a better name.
    """
    used = {name.strip().casefold() for name in existing}
    number = 1
    while f"{stem} {number}".casefold() in used:
        number += 1
    return f"{stem} {number}"


def _field_label(text: str, top: bool = False) -> QLabel:
    """Muted, right-aligned caption, so the column of inputs reads as one edge."""
    label = QLabel(text)
    align = Qt.AlignmentFlag.AlignRight | (
        Qt.AlignmentFlag.AlignTop if top else Qt.AlignmentFlag.AlignVCenter
    )
    label.setAlignment(align)
    label.setStyleSheet(f"color: {TEXT_MUTED};")
    return label


def _coord_edit() -> QLineEdit:
    """Coordinate field, set in the mono face so digits line up between the two
    ends and a transposed decimal point is visible."""
    edit = QLineEdit()
    edit.setPlaceholderText("lat, lon")
    font = QFont(MONO_FONT_FAMILY)
    font.setPointSize(BASE_POINT_SIZE - 1)
    edit.setFont(font)
    return edit


def _framed(inner: QWidget) -> QWidget:
    """Put a hairline and rounded corners around a widget that paints its own
    content, so the map stops bleeding into the page background."""
    frame = QWidget()
    frame.setObjectName("mapFrame")
    frame.setStyleSheet(
        f"QWidget#mapFrame {{ border: 1px solid {BORDER}; border-radius: {RADIUS}px; }}"
    )
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(1, 1, 1, 1)
    layout.addWidget(inner)
    return frame


class OptionalMetresSpinBox(QDoubleSpinBox):
    """Metres, or nothing recorded.

    The unset state is a dash rather than a confident 0.0 m, dimmed so an
    unrecorded depth does not read as measured data.
    """

    def __init__(self, maximum: float, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setRange(0.0, maximum)
        self.setDecimals(1)
        self.setSuffix(" m")
        self.setSpecialValueText("—")
        self.valueChanged.connect(lambda _: self._restyle())
        self._restyle()

    def _restyle(self) -> None:
        unset = self.value() <= self.minimum()
        self.setStyleSheet(f"color: {TEXT_DIM};" if unset else "")


class NotesEdit(QPlainTextEdit):
    """Multi-line notes that commit on focus-out.

    QPlainTextEdit has no editingFinished, and the transect form autosaves on
    field exit, so the signal is supplied here.
    """

    editing_finished = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setPlaceholderText("Anything worth remembering about this transect")
        self.setTabChangesFocus(True)
        self.setFixedHeight(64)

    def focusOutEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().focusOutEvent(event)
        self.editing_finished.emit()


class SimplePlanMixin(MixinBase):
    """DeepReefMapWindow methods for the transect planning tab."""

    _transect_form_id: uuid.UUID | None = None
    _pick_stage: str | None = None
    _plan_map_fitted: bool = False
    _plan_list_rebuilding: bool = False
    _plan_visible_ids: tuple[str, ...] = ()

    def _build_plan_page(self) -> QWidget:
        """Plan step: the map beside the transect editor.

        The run browser used to sit underneath here, which put the same
        transects on the page twice and buried the archive inside a planning
        step. It lives in Browse now.
        """
        page = QSplitter(Qt.Orientation.Vertical)
        page.setHandleWidth(GUTTER)
        top = QSplitter(Qt.Orientation.Horizontal)
        top.setHandleWidth(GUTTER)

        map_pane = QWidget()
        map_layout = QVBoxLayout(map_pane)
        map_layout.setContentsMargins(0, 0, 0, 0)
        self._plan_map = SlippyMapWidget()
        self._plan_map.map_clicked.connect(self._on_plan_map_clicked)
        self._plan_map.transect_clicked.connect(self._on_plan_map_transect_clicked)
        self._plan_map.transect_endpoint_moved.connect(self._on_plan_endpoint_moved)
        self._plan_view_timer = QTimer(self)
        self._plan_view_timer.setSingleShot(True)
        self._plan_view_timer.setInterval(60)
        self._plan_view_timer.timeout.connect(self._apply_plan_view_change)
        self._plan_map.view_changed.connect(self._on_plan_view_changed)
        map_layout.addWidget(_framed(self._plan_map), 1)

        # Caching the visible tiles keeps a site drawable at sea, where the
        # laptop has no connection. Only the tiles on screen are saved, per the
        # OSM policy against bulk prefetching.
        offline_row = QHBoxLayout()
        offline_row.setContentsMargins(0, 0, 0, 0)
        self._save_offline_btn = QPushButton("Save this area for offline use")
        self._save_offline_btn.setProperty("quiet", "true")
        self._save_offline_btn.setToolTip(
            "Store the map tiles now on screen so this area still draws without internet."
        )
        self._save_offline_btn.clicked.connect(self._on_save_offline_area)
        offline_row.addWidget(self._save_offline_btn)
        offline_row.addStretch(1)
        map_layout.addLayout(offline_row)

        transects_group, group_layout = section_card("Transects")
        self._transect_list = QTreeWidget()
        self._transect_list.setColumnCount(len(PLAN_COLUMNS))
        self._transect_list.setHeaderLabels(list(PLAN_COLUMNS))
        self._transect_list.setRootIsDecorated(False)
        self._transect_list.setUniformRowHeights(True)
        self._transect_list.setAllColumnsShowFocus(True)
        self._transect_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        # The pane is narrow and the numeric columns are the point of the table,
        # so a long name gives way to an ellipsis rather than to a scrollbar that
        # would hide the counts off the right edge.
        self._transect_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        header = self._transect_list.header()
        header.setMinimumSectionSize(40)
        header.setStretchLastSection(False)
        for column in range(PLAN_SPACER_COLUMN):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(PLAN_SPACER_COLUMN, QHeaderView.ResizeMode.Stretch)
        header_item = self._transect_list.headerItem()
        header_item.setToolTip(3, "Video segments assigned to this transect")
        header_item.setToolTip(4, "Reconstructions produced from them")
        for column in range(1, PLAN_SPACER_COLUMN):
            header_item.setTextAlignment(column, Qt.AlignmentFlag.AlignRight)
        self._transect_list.currentItemChanged.connect(lambda *_: self._on_transect_selected())
        # The empty state stands in for the list until there is something in it,
        # so a fresh install says how to get started instead of showing a void.
        self._transect_stack = QStackedWidget()
        self._transect_stack.addWidget(self._transect_list)
        self._transect_stack.addWidget(
            EmptyState(
                "No transects yet",
                "Add one with New, or Import… a CSV or GPX file.",
            )
        )
        group_layout.addWidget(self._transect_stack, 1)
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        new_btn = QPushButton("New")
        new_btn.setProperty("cta", "true")
        new_btn.clicked.connect(self._on_transect_new)
        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(self._on_transect_delete)
        import_btn = QPushButton("Import…")
        import_btn.clicked.connect(self._on_transects_import)
        export_btn = QPushButton("Export CSV")
        export_btn.clicked.connect(self._on_transects_export)
        # Creating and deleting a transect is a different kind of act from
        # moving the whole set in and out of a file, so the two groups separate.
        buttons.addWidget(new_btn)
        buttons.addWidget(delete_btn)
        buttons.addStretch(1)
        buttons.addWidget(import_btn)
        buttons.addWidget(export_btn)
        group_layout.addLayout(buttons)

        details, details_layout = section_card("Details")
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        details_layout.addLayout(grid)
        details_layout.addStretch(1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        grid.addWidget(_field_label("Name"), 0, 0)
        self._tr_name_input = QLineEdit()
        grid.addWidget(self._tr_name_input, 0, 1, 1, 3)

        # One box per end takes a coordinate straight off a GPS, pasted or
        # typed, in either "lat lon" or "lat, lon" form. Copying one back out is
        # an action inside the field, so the row stays full width and the button
        # only appears once there is something to copy.
        self._tr_start_coord = _coord_edit()
        self._tr_end_coord = _coord_edit()
        grid.addWidget(_field_label("Start"), 1, 0)
        grid.addWidget(self._tr_start_coord, 1, 1, 1, 2)
        grid.addWidget(_field_label("End"), 2, 0)
        grid.addWidget(self._tr_end_coord, 2, 1, 1, 2)
        self._coord_copy_actions = {
            which: self._add_copy_action(which) for which in ("start", "end")
        }
        for edit in (self._tr_start_coord, self._tr_end_coord):
            edit.editingFinished.connect(self._on_coords_edited)

        # Drawing is the primary way to place a transect and typing the fallback,
        # so the tool stands beside the pair of fields it fills, spanning both.
        self._pick_both_btn = QToolButton()
        self._pick_both_btn.setIcon(crosshair_icon(20))
        self._pick_both_btn.setCheckable(True)
        self._pick_both_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self._pick_both_btn.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        self._pick_both_btn.setToolTip(
            "Draw the transect on the map: click its start, then its end. "
            "Drag either end afterwards to adjust it."
        )
        self._pick_both_btn.toggled.connect(self._on_pick_both_toggled)
        self._sync_map_pick_mode()
        grid.addWidget(self._pick_both_btn, 1, 3, 2, 1)

        # Length and heading are read off the two endpoints, so they are stated
        # rather than entered: a transect drawn the wrong way round shows it here.
        self._tr_geometry = QLabel("")
        self._tr_geometry.setStyleSheet(f"color: {TEXT_SECONDARY};")
        self._tr_geometry.setWordWrap(True)
        self._tr_geometry.setToolTip(
            "Straight-line distance and compass heading from the start point to the end point."
        )
        grid.addWidget(self._tr_geometry, 3, 1, 1, 3)
        self._refresh_geometry_readout()

        self._tr_length = OptionalMetresSpinBox(500.0)
        self._tr_length.setToolTip(
            "Tape length measured underwater. When set it is what the run is "
            "scaled to, in place of the distance between the GPS endpoints."
        )
        self._tr_depth = OptionalMetresSpinBox(100.0)
        self._tr_depth.setToolTip("Depth of the transect. Leave unset if not recorded.")
        grid.addWidget(_field_label("Length"), 4, 0)
        grid.addWidget(self._tr_length, 4, 1)
        grid.addWidget(_field_label("Depth"), 4, 2)
        grid.addWidget(self._tr_depth, 4, 3)

        grid.addWidget(_field_label("Notes", top=True), 5, 0, Qt.AlignmentFlag.AlignTop)
        self._tr_description = NotesEdit()
        grid.addWidget(self._tr_description, 5, 1, 1, 3)

        # No Save button: a new transect shows as a live draft row in the list
        # and commits itself the moment name and both endpoints are complete;
        # later edits commit on field exit.
        self._tr_name_input.textChanged.connect(self._on_draft_changed)
        self._tr_name_input.editingFinished.connect(self._maybe_autosave)
        # Typing a coordinate redraws the line as it is typed; committing it is
        # left to editingFinished, so a half-typed longitude never reaches the
        # store as a saved position.
        for edit in (self._tr_start_coord, self._tr_end_coord):
            edit.textChanged.connect(self._on_coords_typed)
        self._tr_length.editingFinished.connect(self._maybe_autosave)
        self._tr_depth.editingFinished.connect(self._maybe_autosave)
        self._tr_description.editing_finished.connect(self._maybe_autosave)

        # Map and form on top, table full width beneath: the five columns of the
        # table need the whole window, and the form belongs next to the map it
        # draws on.
        top.addWidget(map_pane)
        top.addWidget(details)
        top.setStretchFactor(0, 1)
        top.setStretchFactor(1, 0)
        details.setMinimumWidth(360)
        page.addWidget(top)
        page.addWidget(transects_group)
        page.setStretchFactor(0, 7)
        page.setStretchFactor(1, 3)
        page.setSizes([620, 320])
        transects_group.setMinimumHeight(220)
        # No list refresh here: refreshes happen when the simple mode is entered,
        # so opening the store (which creates survey.db) waits until then.
        return page

    # --- List handling ---

    def _refresh_transect_list(self, select_id: uuid.UUID | None = None) -> None:
        store = self._survey_store()
        saved = store.list_transects()
        counts = store.transect_usage_counts()
        self._plan_list_rebuilding = True
        try:
            # Overlays first: which rows belong in the "In view" section is read
            # back off the map, so it has to be holding the current set already.
            self._refresh_plan_map()
            visible = set(self._plan_map.visible_transect_ids())
            self._plan_visible_ids = tuple(sorted(visible))
            self._transect_list.clear()
            # Duplicating the on-screen transects into a section of their own is
            # what makes a long list usable while panning: the map is the filter.
            # Below two transects there is nothing to filter, only a double entry.
            in_view = [t for t in saved if str(t.id) in visible] if len(saved) > 1 else []
            if in_view:
                self._add_transect_group("In view", in_view, counts)
            all_group = self._add_transect_group("All transects", saved, counts, always=True)
            draft = self._draft_columns()
            if draft is not None:
                self._add_draft_row(all_group, draft)
            self._transect_stack.setCurrentIndex(0 if saved or draft else 1)
        finally:
            self._plan_list_rebuilding = False
        self._select_transect_row(str(select_id) if select_id is not None else DRAFT_ID)
        # Cached for the Plan badge, which must not query the store itself: this
        # runs on every keystroke while a transect is being typed.
        self._plan_state = plan_state(len(saved), draft is not None)
        self._refresh_section_state()

    def _add_transect_group(
        self,
        title: str,
        transects: list[Transect],
        counts: dict[uuid.UUID, tuple[int, int]],
        always: bool = False,
    ) -> QTreeWidgetItem:
        """A titled, non-selectable section holding one row per transect."""
        if not transects and not always:
            return QTreeWidgetItem()
        group = QTreeWidgetItem(self._transect_list, [f"{title}  ({len(transects)})"])
        group.setFirstColumnSpanned(True)
        group.setFlags(Qt.ItemFlag.ItemIsEnabled)
        group.setExpanded(True)
        for transect in transects:
            passes, runs = counts.get(transect.id, (0, 0))
            item = QTreeWidgetItem(group, transect_row_columns(transect, passes, runs))
            item.setData(0, Qt.ItemDataRole.UserRole, str(transect.id))
            tooltip = transect_tooltip(transect, passes, runs)
            for column in range(len(PLAN_COLUMNS)):
                item.setToolTip(column, tooltip)
                if 0 < column < PLAN_SPACER_COLUMN:
                    item.setTextAlignment(column, Qt.AlignmentFlag.AlignRight)
        return group

    def _add_draft_row(self, parent: QTreeWidgetItem, columns: list[str]) -> QTreeWidgetItem:
        """The transect being composed, italic, at the foot of the full list."""
        item = QTreeWidgetItem(parent, columns)
        item.setData(0, Qt.ItemDataRole.UserRole, DRAFT_ID)
        font = item.font(0)
        font.setItalic(True)
        for column in range(len(PLAN_COLUMNS)):
            item.setFont(column, font)
            if 0 < column < PLAN_SPACER_COLUMN:
                item.setTextAlignment(column, Qt.AlignmentFlag.AlignRight)
        return item

    def _draft_columns(self) -> list[str] | None:
        """Row for the transect being composed, before it exists in the store;
        None once saved or while the form is empty."""
        if self._transect_form_id is not None:
            return None
        name = self._tr_name_input.text().strip()
        if not (name or self._tr_start_coord.text().strip()):
            return None
        label = name or "New transect"
        # Once both ends parse the draft can say the same things a saved row
        # does, so the length appears while it is still being entered.
        try:
            lat1, lon1, lat2, lon2 = self._form_coordinates()
        except ValueError:
            return [label, "incomplete", "", "", "", ""]
        length = transect_length_text(
            self._tr_length.value() or None, haversine_m(lat1, lon1, lat2, lon2)
        )
        depth = f"{self._tr_depth.value():g} m" if self._tr_depth.value() else "—"
        return [label, length, depth, "—", "—", ""]

    def _select_transect_row(self, id_str: str) -> None:
        """Select the row for ``id_str``, preferring the "In view" copy of it."""
        for item in self._transect_rows():
            if str(item.data(0, Qt.ItemDataRole.UserRole)) == id_str:
                self._transect_list.blockSignals(True)
                try:
                    self._transect_list.setCurrentItem(item)
                finally:
                    self._transect_list.blockSignals(False)
                return

    def _transect_rows(self) -> list[QTreeWidgetItem]:
        """Every transect row, in display order, without the group headers."""
        rows: list[QTreeWidgetItem] = []
        for index in range(self._transect_list.topLevelItemCount()):
            group = self._transect_list.topLevelItem(index)
            if group is None:
                continue
            rows.extend(group.child(row) for row in range(group.childCount()))
        return rows

    def _on_plan_view_changed(self) -> None:
        """Coalesce the stream of view changes a drag produces.

        Deferred rather than immediate for a second reason: a view change can
        originate in a list selection, and rebuilding the list from inside its
        own selection signal would delete the item that is still emitting.
        """
        if self._plan_list_rebuilding:
            return
        self._plan_view_timer.start()

    def _apply_plan_view_change(self) -> None:
        """Pan and zoom re-decide which transects the "In view" section holds."""
        visible = tuple(sorted(self._plan_map.visible_transect_ids()))
        if visible == self._plan_visible_ids:
            return
        self._refresh_transect_list(select_id=self._transect_form_id)

    def _on_draft_changed(self) -> None:
        if self._transect_form_id is None:
            self._refresh_transect_list()

    def _maybe_autosave(self) -> None:
        """Commit silently once the form is complete; incomplete forms stay a
        draft without nagging."""
        if not self._tr_name_input.text().strip():
            return
        try:
            self._form_coordinates()
        except ValueError:
            return
        self._on_transect_save()

    def _selected_transect_id(self) -> uuid.UUID | None:
        item = self._transect_list.currentItem()
        if item is None:
            return None
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data is None or str(data) == DRAFT_ID:
            return None
        return uuid.UUID(str(data))

    def _on_transect_selected(self, focus_map: bool = True) -> None:
        transect_id = self._selected_transect_id()
        if transect_id is None:
            return
        transect = self._survey_store().get_transect(transect_id)
        if transect is None:
            return
        self._transect_form_id = transect.id
        self._set_pick_armed(False)
        self._tr_name_input.setText(transect.name)
        self._tr_start_coord.setText(f"{transect.start_lat:.6f}, {transect.start_lon:.6f}")
        self._tr_end_coord.setText(f"{transect.end_lat:.6f}, {transect.end_lon:.6f}")
        self._tr_length.setValue(transect.length_m or 0.0)
        self._tr_depth.setValue(transect.depth_m or 0.0)
        self._tr_description.setPlainText(transect.description)
        self._refresh_plan_map()
        # Picking a transect by name is a request to look at it, so the map goes
        # there rather than leaving the selected line off screen. A transect
        # picked by clicking its line is already on screen and the map holds
        # still, or the click would yank the view out from under the pointer.
        if focus_map:
            self._plan_map.focus_on(
                [(transect.start_lat, transect.start_lon), (transect.end_lat, transect.end_lon)],
                fill=FOCUS_FILL,
            )
        self._set_scope_transect(transect.id)

    # --- Form handling ---

    def _on_transect_new(self) -> None:
        """Start a transect that is already named and already being drawn.

        Naming is the step with nothing to decide — the store only needs the
        name unique — so it is filled in and left selected for anyone who has a
        better one, and the map tool arms itself for the two clicks that matter.
        """
        self._transect_form_id = None
        self._transect_list.setCurrentIndex(QModelIndex())
        for edit in (
            self._tr_start_coord,
            self._tr_end_coord,
            self._tr_description,
        ):
            edit.clear()
        self._tr_length.setValue(0.0)
        self._tr_depth.setValue(0.0)
        existing = [t.name for t in self._survey_store().list_transects()]
        self._tr_name_input.setText(next_transect_name(existing))
        self._tr_name_input.setFocus()
        self._tr_name_input.selectAll()
        self._set_pick_armed(True)

    def _coord_edit(self, which: str) -> QLineEdit:
        return self._tr_start_coord if which == "start" else self._tr_end_coord

    def _set_endpoint(self, which: str, lat: float, lon: float) -> None:
        self._coord_edit(which).setText(f"{lat:.6f}, {lon:.6f}")
        self._status_label.setText(f"{which.capitalize()} point set.")
        self._refresh_plan_map()
        self._maybe_autosave()

    def _form_coordinates(self) -> tuple[float, float, float, float]:
        """Both endpoints in decimal degrees, raising if either is blank or unparseable."""
        values: list[float] = []
        for which in ("start", "end"):
            text = self._coord_edit(which).text().strip()
            if not text:
                raise ValueError(f"Missing {which} point")
            try:
                values.extend(parse_latlon(text))
            except ValueError as exc:
                raise ValueError(f"{which.capitalize()} point: {exc}") from None
        return values[0], values[1], values[2], values[3]

    def _on_coords_typed(self) -> None:
        """Redraw from what is in the fields right now, without committing it."""
        self._refresh_plan_map()
        self._refresh_geometry_readout()
        if self._transect_form_id is None:
            self._on_draft_changed()

    def _on_coords_edited(self) -> None:
        self._on_coords_typed()
        self._maybe_autosave()

    def _refresh_geometry_readout(self) -> None:
        """State the length and heading the two fields currently describe."""
        try:
            lat1, lon1, lat2, lon2 = self._form_coordinates()
        except ValueError:
            self._tr_geometry.setText("Length and heading appear once both ends are set")
            return
        self._tr_geometry.setText(
            f"{haversine_m(lat1, lon1, lat2, lon2):.0f} m between the GPS ends  ·  "
            f"heading {bearing_text(lat1, lon1, lat2, lon2)}"
        )

    def _on_transect_save(self) -> None:
        store = self._survey_store()
        try:
            lat1, lon1, lat2, lon2 = self._form_coordinates()
            transect = Transect(
                name=self._tr_name_input.text().strip(),
                start_lat=lat1,
                start_lon=lon1,
                end_lat=lat2,
                end_lon=lon2,
                length_m=self._tr_length.value() or None,
                depth_m=self._tr_depth.value() or None,
                description=self._tr_description.toPlainText().strip(),
            )
        except ValueError as exc:
            self._status_label.setText(str(exc))
            return
        try:
            if self._transect_form_id is None:
                store.add_transect(transect)
            else:
                transect.id = self._transect_form_id
                store.update_transect(transect)
        except sqlite3.IntegrityError:
            self._status_label.setText(f"A transect named {transect.name!r} already exists.")
            return
        self._transect_form_id = transect.id
        self._status_label.setText(f"Saved transect {transect.name}.")
        self._refresh_transect_list(select_id=transect.id)
        self._survey_data_changed()

    def _on_transect_delete(self) -> None:
        transect_id = self._selected_transect_id()
        if transect_id is None:
            return
        try:
            self._survey_store().delete_transect(transect_id)
        except sqlite3.IntegrityError:
            self._status_label.setText("Transect has recorded passes and cannot be deleted.")
            return
        self._on_transect_new()
        self._refresh_transect_list()
        self._survey_data_changed()

    # --- Import / export ---

    def _on_transects_import(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Import transects",
            self._out_root_input.text(),
            "Transect files (*.csv *.gpx);;All files (*)",
        )
        if not path_str:
            return
        path = Path(path_str)
        try:
            if path.suffix.lower() == ".gpx":
                transects = import_transects_gpx(path)
            else:
                transects = import_transects_csv(path)
        except ValueError as exc:
            self._status_label.setText(f"Import failed: {exc}")
            return
        store = self._survey_store()
        added, skipped = 0, 0
        for transect in transects:
            try:
                store.add_transect(transect)
                added += 1
            except sqlite3.IntegrityError:
                skipped += 1
        message = f"Imported {added} transect(s)."
        if skipped:
            message += f" Skipped {skipped} already present."
        self._status_label.setText(message)
        self._refresh_transect_list()
        self._survey_data_changed()

    def _on_transects_export(self) -> None:
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Export transects",
            str(Path(self._out_root_input.text()) / "transects.csv"),
            "CSV files (*.csv)",
        )
        if not path_str:
            return
        transects = self._survey_store().list_transects()
        save_transects_csv(Path(path_str), transects)
        self._status_label.setText(f"Exported {len(transects)} transect(s).")

    # --- Map ---

    def _refresh_plan_map(self, fit: bool = False) -> None:
        selected = self._transect_form_id
        counts = self._survey_store().transect_usage_counts()
        try:
            typed = self._form_coordinates()
        except ValueError:
            typed = None
        overlays = []
        for transect in self._survey_store().list_transects():
            passes, runs = counts.get(transect.id, (0, 0))
            # The selected transect follows the fields as they are typed, so a
            # pasted coordinate lands on the map before it is committed.
            start = (transect.start_lat, transect.start_lon)
            end = (transect.end_lat, transect.end_lon)
            if transect.id == selected and typed is not None:
                start, end = (typed[0], typed[1]), (typed[2], typed[3])
            overlays.append(OverlayTransect(
                id=str(transect.id),
                start=start,
                end=end,
                color=QColor(PRIMARY),
                selected=transect.id == selected,
                label=transect.name,
                tooltip=transect_tooltip(transect, passes, runs),
            ))
        # An unsaved transect previews as soon as both endpoints are filled.
        if selected is None and typed is not None:
            overlays.append(OverlayTransect(
                id=DRAFT_ID,
                start=(typed[0], typed[1]),
                end=(typed[2], typed[3]),
                color=QColor(PRIMARY),
                selected=True,
                label=self._tr_name_input.text().strip(),
            ))
        self._plan_map.set_transects(overlays)
        self._plan_map.set_editable(str(selected) if selected is not None else None)
        if fit or not self._plan_map_fitted:
            self._plan_map.fit_transects()
            self._plan_map_fitted = bool(overlays)

    def _on_save_offline_area(self) -> None:
        from deepreefmap_gui.runs.run_cards import format_bytes

        count, saved = self._plan_map.save_visible_area()
        if count == 0:
            self._status_label.setText(
                "Nothing to save yet. Let the map finish loading, then try again."
            )
            return
        self._status_label.setText(
            f"Saved {count} map tiles ({format_bytes(saved)}) for offline use."
        )

    def _add_copy_action(self, which: str):
        """Copy button living inside the coordinate field, shown once it holds
        something worth copying."""
        edit = self._coord_edit(which)
        action = edit.addAction(copy_icon(16), QLineEdit.ActionPosition.TrailingPosition)
        action.setToolTip(f"Copy the {which} coordinates")
        action.triggered.connect(lambda _=False, w=which: self._copy_endpoint(w))
        action.setVisible(False)
        edit.textChanged.connect(lambda text, a=action: a.setVisible(bool(text.strip())))
        return action

    def _copy_endpoint(self, which: str) -> None:
        edit = self._coord_edit(which)
        text = edit.text().strip()
        if not text:
            self._status_label.setText(f"No {which} point to copy.")
            return
        QGuiApplication.clipboard().setText(text)
        self._status_label.setText(f"Copied {which} point {text} to the clipboard.")
        # The status bar is at the far corner of the window from the field that
        # was clicked, so the confirmation is also shown at the field itself and
        # the button briefly becomes a tick.
        QToolTip.showText(
            edit.mapToGlobal(edit.rect().topRight()), "Copied to clipboard", edit
        )
        action = self._coord_copy_actions[which]
        action.setIcon(check_icon(16))
        QTimer.singleShot(1200, lambda a=action: a.setIcon(copy_icon(16)))

    def _set_pick_armed(self, on: bool) -> None:
        """Arm or disarm the draw tool without going round the toggled signal."""
        if self._pick_both_btn.isChecked() == on:
            self._on_pick_both_toggled(on)
            return
        self._pick_both_btn.setChecked(on)

    def _on_pick_both_toggled(self, on: bool) -> None:
        self._pick_stage = "start" if on else None
        if on:
            self._status_label.setText("Click the start of the transect.")
        self._plan_map.set_pending_start(None)
        self._sync_map_pick_mode()

    def _sync_map_pick_mode(self) -> None:
        """Crosshair cursor and a narrating tool label whenever a click would
        land somewhere."""
        armed = self._pick_stage is not None
        self._plan_map.set_pick_mode(armed)
        self._pick_both_btn.setText(
            {"start": "Click start", "end": "Click end", None: "Draw"}[self._pick_stage]
        )

    def _on_plan_map_clicked(self, lat: float, lon: float) -> None:
        if self._pick_stage == "start":
            self._set_endpoint("start", lat, lon)
            self._pick_stage = "end"
            self._plan_map.set_pending_start((lat, lon))
            self._status_label.setText("Now click the end of the transect.")
            self._sync_map_pick_mode()
            return
        if self._pick_stage == "end":
            self._set_pick_armed(False)
            self._set_endpoint("end", lat, lon)

    def _on_plan_map_transect_clicked(self, transect_id: str) -> None:
        self._select_transect_row(transect_id)
        self._on_transect_selected(focus_map=False)

    def _on_plan_endpoint_moved(self, transect_id: str, which: str, lat: float, lon: float) -> None:
        if self._transect_form_id is None or str(self._transect_form_id) != transect_id:
            return
        self._coord_edit(which).setText(f"{lat:.6f}, {lon:.6f}")
        self._on_transect_save()

    def _survey_data_changed(self) -> None:
        """Refresh survey views that mirror the store."""
        self._refresh_survey_transect_combos()
        self._refresh_survey_analysis()
        self._refresh_videos_page()
        # Creating the transect an unassigned pass was waiting for has to
        # re-evaluate the Run gate, or the batch stays blocked with nothing left
        # to fix.
        self._recompute_survey_start()
