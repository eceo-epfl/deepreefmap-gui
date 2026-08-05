"""The interface shell: destinations, and the shared survey store accessor.

One interface, and every panel in it is a single widget lent to the destination
that shows it: Browse into the shell, the model library and system panel into
Setup, the results panel into View mode, the run form into the settings dialog.
Two copies of any of them would disagree the moment a download or a path edit
landed against only one.

The header holds three peer destinations and no sequence: run_gate() treats a
pass with no transect as fine, so transects are not a precondition for
processing. Anything wrong with one of them is named once, in the alert box.
"""

from __future__ import annotations

import logging
from functools import partial
from pathlib import Path
from typing import Any

import yaml
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.icons import (
    browse_icon,
    process_icon,
    section_state_icon,
    transects_icon,
)
from deepreefmap_gui.core.theme import (
    BORDER,
    CARD_BG,
    DISABLED_FG,
    GUTTER,
    ON_ACCENT,
    PAGE_MARGIN,
    SPACE_MD,
    SPACE_SM,
    SPACE_XS,
    WINDOW_TEXT,
)
from deepreefmap_gui.core.widgets import HeaderAlert, muted_label, utility_button_qss
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.runs.run_detail import RunDetailPanel
from deepreefmap_gui.simple.section_state import (
    browse_state,
    headline,
    most_urgent,
    run_gate,
    transects_state,
)
from deepreefmap_gui.survey.health import SurveyDbHealth, SurveyDbState, inspect_survey_db
from deepreefmap_gui.survey.preset import (
    ActivePreset,
    OrgPreset,
    OverrideResult,
    clear_machine_override,
    describe_keys,
    deviations_from_org,
    load_active_preset,
    save_machine_override,
)
from deepreefmap_gui.survey.store import _MIGRATIONS, SURVEY_DB_NAME, SurveyStore

logger = logging.getLogger(__name__)

# Two nouns and a verb, all peers. None is a prerequisite for another: a pass
# with no transect processes perfectly well, so ordering them as steps claimed a
# dependency the gate does not enforce.
#
# Three, not four: the clip library is Browse's By video grouping, its rail and
# its detail pane, rather than a destination answering the same question.
DESTINATIONS = ("transects", "process", "browse")

# The glyph that says what a destination holds. Constant per destination: what
# it currently has to report is the badge's job, and an icon that changed with
# state would leave the pill with no stable identity to recognise it by.
_DESTINATION_ICONS = {
    "transects": transects_icon,
    "process": process_icon,
    "browse": browse_icon,
}

# One line per destination, said in the terms of the work rather than the widget.
_DESTINATION_TIPS = {
    "transects": "The lines you survey, and what repeat passes of each one found.",
    "process": "Queue this session's videos as passes, and watch them run.",
    "browse": "Every run and clip so far, grouped however you need to read it.",
}

# Every destination the stack can show, in stack order. Machine and view are
# appended last so the first three stack indices other code and tests rely on
# stay put; neither is a destination. Machine is a utility you visit and leave,
# and view is where an opened run goes, reached by opening one.
SIMPLE_SECTIONS = (*DESTINATIONS, "machine", "view")

# What the info panel takes when it is open. Wide enough for the metadata block
# without eating into the cloud, which is what View mode is for.
VIEW_INFO_WIDTH = 340

_DESTINATION_ICON_PX = 16

# The same control as Log and Setup at the other end of the band: everything in
# the header is a button, so everything in the header is drawn as one.
#
# Not segmented_qss: a joined segmented control is what this app uses for filters
# and facets, and navigation in that shape reads as another filter.
_DESTINATION_QSS = utility_button_qss() + (
    f" QToolButton:disabled {{ color: {DISABLED_FG}; background: transparent;"
    f" border-color: {BORDER}; }}"
)

# Bars that top a page and are separated from it by a hairline. Object-name
# scoped: an unscoped `border-bottom` on the container is inherited by every
# child, which drew a stray underline beneath each label and button in the row.
_BAR_QSS = (
    f"QWidget {{ background-color: {CARD_BG}; }}"
    f" QWidget#simpleBar {{ border-bottom: 1px solid {BORDER}; }}"
)


# Preset key -> run-form widget attribute, one entry per settings widget. The
# settings dialog can edit the whole run form, so the preset snapshots all of it.
# Per-run inputs (video, run name, trim, transect length, output root) are not
# settings and never appear here.
#
# The resolution preset comes before the explicit width/height so restoring a
# Custom size wins over the native size the combo recomputes.
_PRESET_FIELD_WIDGETS = (
    ("fps", "_fps_spin"),
    ("segmentation_name", "_seg_combo"),
    ("mapping_name", "_map_combo"),
    ("camera_profile_name", "_profile_combo"),
    ("transect_crop_width", "_crop_width"),
    ("enable_tsdf", "_tsdf_check"),
    ("skip_segmentation", "_skip_seg_check"),
    ("resolution_preset", "_resolution_preset_combo"),
    ("processing_width", "_proc_width_spin"),
    ("processing_height", "_proc_height_spin"),
    ("preprocess_batch_size", "_batch_size_spin"),
    ("grid_bins", "_grid_bins_spin"),
    ("require_gravity_telemetry", "_require_gravity_check"),
    ("replacement_radius_factor", "_rr_factor_spin"),
    ("replacement_radius_estimation_frames", "_rr_est_frames_spin"),
    ("replacement_radius_override", "_rr_override_spin"),
    ("loger_window_size", "_loger_window_spin"),
    ("loger_overlap_size", "_loger_overlap_spin"),
    ("loger_model_path", "_loger_model_path_input"),
    ("refine_intrinsics_from_mapper", "_refine_intrinsics_check"),
    ("scs_target_width", "_scs_width_spin"),
    ("scs_target_height", "_scs_height_spin"),
    ("scs_checkpoint_path", "_scs_checkpoint_input"),
)

# Processing size follows the segmentation model's native size unless the user
# picks Custom, so a non-Custom preset stores null and leaves the form alone.
_NATIVE_SIZE_KEYS = ("processing_width", "processing_height")


def _widget_value(widget: QWidget) -> Any:
    if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
        return widget.value()
    if isinstance(widget, QCheckBox):
        return widget.isChecked()
    if isinstance(widget, QComboBox):
        return widget.currentText()
    if isinstance(widget, QLineEdit):
        return widget.text().strip()
    raise TypeError(f"Unsupported form widget: {type(widget).__name__}")


def _set_widget_value(widget: QWidget, value: Any) -> None:
    if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
        widget.setValue(value)
    elif isinstance(widget, QCheckBox):
        widget.setChecked(bool(value))
    elif isinstance(widget, QComboBox):
        widget.setCurrentText(str(value))
    elif isinstance(widget, QLineEdit):
        widget.setText(str(value))
    else:
        raise TypeError(f"Unsupported form widget: {type(widget).__name__}")


class InterfaceShellMixin(MixinBase):
    """DeepReefMapWindow methods for the workspace shell and the survey store."""

    _survey_store_obj: SurveyStore | None = None
    # Why the survey database could not be opened, when it could not be. None
    # until something has looked; read it through _survey_db_health.
    _survey_health: SurveyDbHealth | None = None
    # The configuration as persisted: organisation preset plus this machine's
    # allow-listed changes. _survey_preset is what the run will use, which can
    # additionally hold a session-only edit the override would not keep.
    _active_preset: ActivePreset | None = None
    _form_defaults: dict[str, Any]
    _simple_nav_buttons: dict[str, QToolButton]
    _destination_group: QButtonGroup
    # The header's one alert, and the destination it opens.
    _section_alert: HeaderAlert
    _section_alert_target: str
    _section_state_cache: tuple | None = None
    _work_area_state: tuple[bool, str, bool] | None = None

    def _reveal_memory_detail(self) -> None:
        """Where the memory warning sends you: the readiness rows, which carry
        the same sentence without the system panel's jargon."""
        self._set_simple_section("machine")
        self._set_machine_view("readiness")

    def _idle_status_text(self) -> str:
        return "Ready. Add videos under Process, and mark out transects to compare them against."

    def _refresh_browse_state(self) -> None:
        """Cache Browse's count from entries the data manager already scanned."""
        entries = getattr(self, "_data_entries", None) or []
        unfiled = sum(1 for entry in entries if entry.transect_id is None)
        self._browse_state = browse_state(len(entries), unfiled)
        self._refresh_section_state()

    def _adopt_form_as_preset(self) -> None:
        """Persist the settings this machine is allowed to change, and only those.

        The organisation preset owns the method: which models run, at what
        resolution, over how many frames. Those decide the numbers a survey
        reports, so one diver's curiosity must not rewrite them for every dive
        after. Only MACHINE_OVERRIDABLE_KEYS describe the computer rather than
        the method, and only those are written back.

        A locked preset (an administrator named one) puts the rest of the form
        back to standard, because an authoritative configuration that the next
        run would silently ignore is not authoritative. An unlocked one leaves
        the edit in place for this session and says it will not be kept.
        """
        # With no readable organisation preset there is nothing to measure a
        # deviation against, so there is nothing meaningful to write either.
        if self._active_preset is None:
            return
        org = self._active_preset.org
        try:
            result = save_machine_override(self._collect_preset_from_form(), org)
        except OSError as exc:
            logger.warning("Could not save the settings for this machine: %s", exc)
            self._status_label.setText(f"Settings not saved: {exc}")
            return
        self._active_preset = ActivePreset(org=org, overrides=result.saved)
        if result.refused and org.locked:
            self._populate_form_from_preset(self._active_preset.settings)
        # What the run will use, which is the form: a session-only deviation the
        # organisation preset would not keep still reaches the pipeline.
        self._survey_preset = self._collect_preset_from_form()
        self._announce_preset_save(result, org)
        self._survey_preset_label.setText(self._survey_preset_summary())

    def _announce_preset_save(self, result: OverrideResult, org: OrgPreset) -> None:
        """Say what was kept and what was not, naming the settings in plain words."""
        if result.refused and org.locked:
            self._status_label.setText(
                f"{org.name} sets {describe_keys(result.refused)}, so that went back to standard."
            )
        elif result.refused:
            self._status_label.setText(
                f"{describe_keys(result.refused)} changed for now, but the standard"
                " settings come back next launch."
            )
        elif result.saved:
            self._status_label.setText(
                f"Saved for this machine: {describe_keys(result.saved)}."
            )

    def _survey_deviations(self) -> dict[str, Any]:
        """Form settings that differ from the organisation preset.

        Read from the form rather than the saved override, because a batch runs
        from _collect_run_settings(): a session-only edit changes the run just as
        much as a saved one, so it is named and recorded just the same.
        """
        if self._active_preset is None:
            return {}
        return deviations_from_org(
            self._collect_preset_from_form(), self._active_preset.org
        )

    def _reload_active_preset(self) -> None:
        """Re-resolve the organisation preset and this machine's changes.

        A malformed admin file is caught here rather than allowed to abort window
        construction: the gate then blocks with a reason the diver can read,
        instead of the app refusing to open on a field laptop.
        """
        try:
            self._active_preset = load_active_preset()
        except (OSError, ValueError, yaml.YAMLError) as exc:
            self._active_preset = None
            self._survey_preset = None
            logger.warning("Settings unavailable: %s", exc)
            return
        self._survey_preset = self._active_preset.settings

    def _restore_standard_settings(self) -> None:
        """Drop this machine's changes and go back to the organisation preset."""
        if self._survey_worker_running:
            self._status_label.setText("Unavailable while processing.")
            return
        # Ask before restoring, or the answer is always "nothing changed".
        deviated = bool(self._survey_deviations())
        had_override = clear_machine_override()
        self._reload_active_preset()
        if self._survey_preset is not None:
            self._populate_form_from_preset(self._survey_preset)
        else:
            # No readable preset to restore from, so the fresh-window values are
            # the only known-good state left to offer.
            self._reset_form_defaults()
        self._recompute_survey_start()
        if self._active_preset is None:
            self._status_label.setText(
                "Settings could not be read. The form has reverted to its defaults."
            )
        elif had_override or deviated:
            self._status_label.setText(
                f"Settings are back to {self._active_preset.org.label}."
            )
        else:
            self._status_label.setText(
                f"Already on {self._active_preset.org.label}; nothing to restore."
            )

    def _load_standard_into_form(self) -> None:
        """Load the organisation standard into the form without persisting.

        The run settings dialog borrows the live form, so its Restore button must
        only touch widgets: the caller's snapshot undoes it on Cancel, and OK
        persists the result, which clears the machine override once the form is
        back on the standard. The persistent restore is the Run page's own button.
        """
        if self._active_preset is not None:
            self._populate_form_from_preset(self._active_preset.org.settings)
        else:
            self._reset_form_defaults()

    def _activate_interface(self) -> None:
        """Bring the shell up once everything it reads has been built.

        There is one interface, so these refreshes belong to construction rather
        than to a mode change.
        """
        # The form is never shown outside the run settings dialog, but the batch
        # runs from _collect_run_settings(), which reads exactly these widgets.
        # Left on the constructor defaults, a run would ignore the preset.
        if self._survey_preset is not None:
            self._populate_form_from_preset(self._survey_preset)
        self._update_memory_profile_warning()
        if getattr(self, "_app_mode", "SETUP") == "SETUP":
            self._status_label.setText(self._idle_status_text())
        self._host_machine_panels()
        self._refresh_transect_list()
        self._refresh_survey_batch_tab()
        self._refresh_survey_analysis()
        self._refresh_data_manager()
        self._refresh_readiness_view()
        self._sync_system_gauges_running()
        self._update_work_area()

    def _build_simple_shell(self) -> QWidget:
        """Three destinations over one page stack.

        Transects are the lines you survey, Process is where a session's videos
        are queued and watched, and Browse is everything produced so far. You
        move between them freely: none is a step you finish, so none of them
        gates another.

        One band, one switch, one thing filled at a time. Utilities sit at the
        far end, bordered rather than filled, so "where am I working" and "what
        can I go and check" never look like the same kind of control.
        """
        shell = QWidget()
        layout = QVBoxLayout(shell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Built here so it exists before the first _sync_destination_chrome, but
        # added to the window's central column rather than to this shell: in View
        # mode the shell is squeezed to zero width, and a Back button inside it
        # would go with it.
        self._build_view_bar()

        header = QWidget()
        header.setObjectName("simpleBar")
        header.setStyleSheet(_BAR_QSS)
        self._simple_header = header
        nav = QHBoxLayout(header)
        nav.setContentsMargins(PAGE_MARGIN, SPACE_SM, PAGE_MARGIN, SPACE_SM)
        nav.setSpacing(SPACE_XS)

        self._simple_stack = QStackedWidget()
        self._simple_nav_buttons = {}
        self._section_state_cache = None

        # Pages are built before the buttons that select them: Process builds
        # the start button, and Transects builds the list its badge counts.
        pages = {
            "transects": self._build_plan_page(),
            "process": self._build_simple_run_page(),
            "browse": self._build_browse_page(),
            "machine": self._build_machine_page(),
            "view": self._build_view_info_page(),
        }

        nav_group = QButtonGroup(shell)
        nav_group.setExclusive(True)
        self._destination_group = nav_group
        for name in DESTINATIONS:
            btn = QToolButton()
            btn.setText(name.capitalize())
            btn.setCheckable(True)
            btn.setStyleSheet(_DESTINATION_QSS)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            btn.setIconSize(QSize(_DESTINATION_ICON_PX, _DESTINATION_ICON_PX))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(_DESTINATION_TIPS[name])
            nav_group.addButton(btn)
            nav.addWidget(btn)
            btn.toggled.connect(partial(self._on_simple_nav_toggled, name))
            self._simple_nav_buttons[name] = btn

        nav.addStretch(1)
        # What a destination holds is shown on it. What is wrong with one is
        # here, in a box that is empty unless something is.
        self._section_alert = HeaderAlert()
        self._section_alert.clicked.connect(self._on_section_alert_clicked)
        self._section_alert_target = ""
        nav.addWidget(self._section_alert)
        nav.addSpacing(SPACE_MD)
        # Utilities, at the far end from the work. Bordered rather than filled
        # (see utility_button_qss): these are places you visit and leave, not
        # where you are working.
        nav.addWidget(self._log_toggle_btn)
        nav.addWidget(self._build_machine_nav_button())
        layout.addWidget(header)

        for name in SIMPLE_SECTIONS:
            self._simple_stack.addWidget(pages[name])

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)
        body_layout.setSpacing(GUTTER)
        body_layout.addWidget(self._simple_stack, 1)
        layout.addWidget(body, 1)

        self._set_simple_section(self._initial_simple_section())
        self._refresh_section_state()
        return shell

    def _build_browse_page(self) -> QWidget:
        """Browse: the run archive, and the detail of whatever is selected in it.

        The whole page is the browse panel. Transect comparison is one page of
        its detail pane, shown when a transect is what is selected.
        """
        return self._build_data_panel()

    def _build_view_bar(self) -> QWidget:
        """The way out of View mode, and the switch for the metadata beside it.

        Full window width, so it survives the info column collapsing to nothing.
        """
        self._view_info_open = False
        bar = QWidget()
        bar.setObjectName("simpleBar")
        bar.setStyleSheet(_BAR_QSS)
        row = QHBoxLayout(bar)
        row.setContentsMargins(PAGE_MARGIN, SPACE_SM, PAGE_MARGIN, SPACE_SM)
        row.setSpacing(GUTTER)

        # A breadcrumb, not a way out. The header above stays put with Browse
        # lit, so an opened run reads as a place inside Browse rather than a
        # mode that has taken the window; the crumb says where inside.
        crumb = QPushButton("Browse")
        crumb.setProperty("quiet", "true")
        crumb.setCursor(Qt.CursorShape.PointingHandCursor)
        crumb.setToolTip("Back to the run list.")
        crumb.clicked.connect(lambda: self._set_simple_section("browse"))
        row.addWidget(crumb)
        row.addWidget(muted_label("›"))

        self._view_title = QLabel("")
        font = self._view_title.font()
        font.setWeight(QFont.Weight.DemiBold)
        self._view_title.setFont(font)
        row.addWidget(self._view_title)
        row.addStretch(1)

        self._view_info_btn = QToolButton()
        self._view_info_btn.setText("Info")
        self._view_info_btn.setCheckable(True)
        self._view_info_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._view_info_btn.setToolTip("Show what this run is, beside the cloud.")
        self._view_info_btn.setStyleSheet(_DESTINATION_QSS)
        self._view_info_btn.toggled.connect(self._on_view_info_toggled)
        row.addWidget(self._view_info_btn)

        bar.setVisible(False)
        self._view_bar = bar
        return bar

    def _set_view_bar_visible(self, visible: bool) -> None:
        if hasattr(self, "_view_bar"):
            self._view_bar.setVisible(visible)

    def _on_view_info_toggled(self, checked: bool) -> None:
        self._view_info_open = checked
        self._update_work_area()

    def _enter_view_mode(self, run_dir: Path) -> None:
        """Point View mode at the run now on screen, and go there.

        The catalogue entry carries what the info panel shows, but a run opened
        from outside the output root has none; the title still names it, and the
        panel stays on whatever it last described rather than showing another
        run's facts under this one's name.
        """
        entry = next(
            (e for e in getattr(self, "_data_entries", []) if e.run_dir == run_dir),
            None,
        )
        if entry is not None:
            self._view_title.setText(entry.display_name)
            self._view_detail.show_entry(entry)
        else:
            manifest = getattr(self, "_active_run_manifest", None) or {}
            self._view_title.setText(manifest.get("name") or run_dir.name)
            self._view_detail.clear()
        self._set_simple_section("view")

    def _build_view_info_page(self) -> QWidget:
        """What the run on screen is, and what it produced, beside the cloud.

        The whole point of View mode is the viewport, so this column is off by
        default and the Info button in the view bar brings it back. It reuses the
        Browse detail pane's filler, so the two cannot describe one run
        differently, and it takes the results panel: the ortho, the benthic
        cover, the transect crop and the exports all act on the run you are
        looking at, which is the only place they were ever about.

        Scrolls as one column rather than per section: the metadata is short and
        the results are long, and two scrollbars side by side in a 340px column
        is more chrome than content.
        """
        column = QWidget()
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(GUTTER)
        self._view_detail = RunDetailPanel()
        layout.addWidget(self._view_detail)
        layout.addWidget(self._results_page)
        layout.addStretch(1)

        page = QScrollArea()
        page.setWidgetResizable(True)
        page.setWidget(column)
        page.setFrameShape(QFrame.Shape.NoFrame)
        page.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return page

    def _current_section(self) -> str:
        index = self._simple_stack.currentIndex()
        if 0 <= index < len(SIMPLE_SECTIONS):
            return SIMPLE_SECTIONS[index]
        return DESTINATIONS[0]

    def _sync_destination_chrome(self) -> None:
        """Light the pill the live section belongs to, and recolour its glyph.

        The icon has to be repainted per state because a checked pill is filled
        with the accent and its ink is near-black, while an unchecked one is
        transparent over the dark shell: one colour cannot serve both.
        """
        section = self._current_section()
        # The header stays through View. An opened run is a page inside Browse,
        # so hiding the destination switch to show it made the app look like it
        # had changed mode, and left no way to Transects or Process without
        # first backing out of a cloud.
        self._simple_header.setVisible(True)
        self._set_view_bar_visible(section == "view")
        if section == "machine":
            # Not a destination, so no pill should own it.
            #
            # Exclusivity is lifted first: an exclusive QButtonGroup refuses to
            # let its one checked member be unchecked, so this silently did
            # nothing and Browse stayed lit while Setup was on screen.
            self._destination_group.setExclusive(False)
            for button in self._simple_nav_buttons.values():
                button.blockSignals(True)
                button.setChecked(False)
                button.blockSignals(False)
            self._destination_group.setExclusive(True)
        else:
            # View is a page inside Browse, so Browse is what it lights: the
            # cloud you are looking at came from the archive you are still in.
            lit = "browse" if section == "view" else section
            button = self._simple_nav_buttons[lit]
            button.blockSignals(True)
            button.setChecked(True)
            button.blockSignals(False)
        for name, button in self._simple_nav_buttons.items():
            ink = QColor(ON_ACCENT if button.isChecked() else WINDOW_TEXT)
            button.setIcon(_DESTINATION_ICONS[name](_DESTINATION_ICON_PX, ink))

    def _refresh_section_state(self) -> None:
        """Paint the header from the cached verdicts: tooltips, and the alert.

        A pure painter: it reads attributes other code has already computed and
        never touches the store or the filesystem, because it is called from
        paths that run on every keystroke.
        """
        if not hasattr(self, "_simple_nav_buttons"):
            return
        states = {
            "transects": getattr(self, "_plan_state", None) or transects_state(0, False),
            "process": getattr(self, "_survey_gate", None) or run_gate(
                pass_count=0,
                unassigned=0,
                remaining=0,
                failed=0,
                has_preset=True,
                missing_models=[],
            ),
            "browse": getattr(self, "_browse_state", None) or browse_state(0, 0),
        }
        key = tuple((name, s.state, s.count, s.reason) for name, s in states.items())
        if self._section_state_cache == key:
            return
        self._section_state_cache = key
        for name, verdict in states.items():
            self._simple_nav_buttons[name].setToolTip(
                "\n".join(filter(None, [_DESTINATION_TIPS[name], verdict.count, verdict.reason]))
            )
        urgent = most_urgent(states)
        if urgent is None:
            self._section_alert_target = ""
            self._section_alert.clear()
            return
        name, verdict = urgent
        self._section_alert_target = name
        icon = section_state_icon(verdict.state)
        self._section_alert.show_alert(
            f"{name.capitalize()}: {headline(verdict.reason)}",
            tooltip=f"{verdict.reason}\nGo to {name.capitalize()}.",
            pixmap=(
                icon.pixmap(_DESTINATION_ICON_PX, _DESTINATION_ICON_PX)
                if icon is not None
                else None
            ),
        )

    def _on_section_alert_clicked(self) -> None:
        if self._section_alert_target:
            self._go_to_section(self._section_alert_target)

    def _go_to_section(self, name: str) -> None:
        self._set_simple_section(name)

    def _set_navigation_enabled(self, enabled: bool) -> None:
        """A batch in flight owns the queue it is working, so editing it is out.

        Only Transects is locked. Process is where the batch reports itself, and
        Browse stays reachable throughout: a batch takes tens of minutes and
        looking at what finished earlier is exactly what you want to do while it
        runs. Opening a run from there is refused separately, so the live run
        keeps the viewer.
        """
        self._simple_nav_buttons["transects"].setEnabled(enabled)

    def _on_simple_nav_toggled(self, name: str, checked: bool) -> None:
        if checked:
            self._set_simple_section(name)

    def _set_simple_section(self, name: str) -> None:
        if name not in SIMPLE_SECTIONS:
            raise ValueError(f"Unknown simple section: {name!r}")
        if name in DESTINATIONS:
            button = self._simple_nav_buttons[name]
            if not button.isChecked():
                button.blockSignals(True)
                button.setChecked(True)
                button.blockSignals(False)
        if name == "machine":
            self._refresh_readiness_view()
        self._simple_stack.setCurrentIndex(SIMPLE_SECTIONS.index(name))
        self._sync_destination_chrome()
        # The gauges poll at 1 Hz, so they run only while they are on screen.
        self._sync_system_gauges_running()
        self._update_work_area()

    def _update_work_area(self) -> None:
        """The one place that decides whether the viewer pane shows and how the
        work splitter divides, from (app_mode, section)."""
        if not hasattr(self, "_work_hsplitter"):
            return
        section = (
            self._current_section() if hasattr(self, "_simple_stack") else DESTINATIONS[0]
        )
        # The point cloud gets a destination of its own rather than half of
        # Browse. Browsing wants the whole window for the table, and looking at
        # a cloud wants the whole window for the cloud; sharing served neither.
        show_viewer = section == "view"
        info_open = getattr(self, "_view_info_open", False)
        state = (show_viewer, section, info_open)
        if getattr(self, "_work_area_state", None) == state:
            return
        self._work_area_state = state
        self._viewer.setVisible(show_viewer)
        total = max(self._work_hsplitter.width(), 800)
        if not show_viewer:
            self._work_hsplitter.setSizes([total, 0])
        else:
            # View mode gives the viewport everything the info panel is not using.
            left = min(VIEW_INFO_WIDTH, total // 3) if info_open else 0
            self._work_hsplitter.setSizes([left, total - left])

    def _snapshot_form_settings(self) -> dict[str, Any]:
        """Every settings widget's current value, keyed by attribute name.

        Widget values rather than a preset dict: the preset drops the processing
        size when the resolution is not Custom, which is right for storage and
        wrong for an undo, since cancelling has to put back exactly what was
        there.
        """
        return {attr: _widget_value(getattr(self, attr)) for _, attr in _PRESET_FIELD_WIDGETS}

    def _restore_form_settings(self, snapshot: dict[str, Any]) -> None:
        for _, attr in _PRESET_FIELD_WIDGETS:
            _set_widget_value(getattr(self, attr), snapshot[attr])

    def _capture_form_defaults(self) -> None:
        """Snapshot fresh-window values of every settings field, so the run
        settings dialog can offer Reset defaults."""
        self._form_defaults = self._snapshot_form_settings()

    def _collect_preset_from_form(self) -> dict[str, Any]:
        preset: dict[str, Any] = {}
        custom_size = self._resolution_preset_combo.currentText() == "Custom"
        for key, attr in _PRESET_FIELD_WIDGETS:
            value = _widget_value(getattr(self, attr))
            if key == "transect_crop_width":
                value = value or None
            elif key in _NATIVE_SIZE_KEYS and not custom_size:
                value = None
            preset[key] = value
        return preset

    def _populate_form_from_preset(self, preset: dict[str, Any]) -> None:
        for key, attr in _PRESET_FIELD_WIDGETS:
            value = preset[key]
            if key == "transect_crop_width" and value is None:
                value = 0.0
            elif key in _NATIVE_SIZE_KEYS and value is None:
                continue
            _set_widget_value(getattr(self, attr), value)

    def _reset_form_defaults(self) -> None:
        self._restore_form_settings(self._form_defaults)

    def _survey_store(self) -> SurveyStore:
        """Store keyed to the current output root; reopened when the root changes.

        Held fixed for as long as a batch runs. The worker was handed this store
        and writes each pass status through it, and SurveyStore's connections are
        thread-local, so closing it here would not touch the worker's own
        connection: the batch would carry on writing to a database the window no
        longer reads. The batch owns the root it started under.
        """
        db_path = Path(self._out_root_input.text()).expanduser() / SURVEY_DB_NAME
        store = self._survey_store_obj
        if store is not None and store.path != db_path and self._survey_worker_running:
            logger.warning(
                "Output root changed to %s while a batch is running; keeping %s until it ends",
                db_path,
                store.path,
            )
            return store
        if store is None or store.path != db_path:
            if store is not None:
                store.close()
            store = SurveyStore(db_path)
            self._survey_store_obj = store
        self._survey_health = SurveyDbHealth(SurveyDbState.OK, db_path, len(_MIGRATIONS))
        return store

    def _try_survey_store(self) -> SurveyStore | None:
        """The store, or None with the reason recorded in ``_survey_health``.

        Opening happens while the window is still being built, so a database this
        build cannot read -- one left by a newer version after a rollback, a
        corrupt file, an output root on an unplugged drive -- would otherwise
        take the whole launch down before there is anything to show the error in.
        Every caller that runs during construction goes through here.

        An open store is its own verdict, so a root that has already been opened
        is not re-inspected: refreshes arrive by the handful whenever the output
        folder changes, and the answer can only change when the root changes or
        the store is dropped, both of which clear ``_survey_store_obj``.
        """
        db_path = Path(self._out_root_input.text()).expanduser() / SURVEY_DB_NAME
        open_store = self._survey_store_obj
        health = self._survey_health
        if (
            open_store is not None
            and open_store.path == db_path
            and health is not None
            and health.state is SurveyDbState.OK
        ):
            return open_store
        health = inspect_survey_db(db_path)
        if not health.openable:
            self._survey_health = health
            return None
        try:
            return self._survey_store()
        except Exception as exc:
            # inspect_survey_db opens read-only and cannot see everything that
            # can go wrong in a read-write open, so this is the backstop.
            logger.exception("Survey database unavailable at %s", db_path)
            self._survey_health = SurveyDbHealth(
                SurveyDbState.CORRUPT, db_path, len(_MIGRATIONS), detail=str(exc)
            )
            return None

    def check_survey_database(self) -> None:
        """Offer a way out if the survey database will not open. Called after show().

        After, not during, construction: the window has to exist and be visible
        before anything modal, and declining has to leave a usable app rather
        than an aborted launch.
        """
        health = self._survey_db_health()
        if health.openable:
            return
        from deepreefmap_gui.survey.recovery import RecoveryKind, apply_recovery
        from deepreefmap_gui.survey.recovery_dialog import SurveyRecoveryDialog

        out_root = Path(self._out_root_input.text()).expanduser()
        dialog = SurveyRecoveryDialog(health, out_root, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        option = dialog.selected()
        if option is None:
            return
        if option.kind is RecoveryKind.CHOOSE_FOLDER:
            self._browse_output_root()
            return
        try:
            message = apply_recovery(option, health, out_root)
        except Exception as exc:
            logger.exception("Survey recovery failed")
            QMessageBox.critical(
                self, "Survey database", f"That did not work: {exc}"
            )
            return
        # The store was never opened, so there is nothing to close; drop the
        # verdict so the next read re-inspects what recovery just put in place.
        self._survey_health = None
        self._survey_store_obj = None
        self._activate_interface()
        self._status_label.setText(message)

    def _survey_db_health(self) -> SurveyDbHealth:
        """The last verdict, asking the database if nothing has looked yet."""
        health = getattr(self, "_survey_health", None)
        if health is None:
            self._try_survey_store()
            health = getattr(self, "_survey_health", None)
        assert health is not None
        return health
