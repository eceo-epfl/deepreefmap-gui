"""The interface shell: workspaces, steps, and the shared survey store accessor.

There used to be two interfaces here, Simple and Advanced, and the toggle
between them was never progressive disclosure: both ran from the same widgets,
and simple mode did not show a reduced form, it showed all of it in a dialog
that borrowed the live one. What the toggle actually selected was whether a
third of the application was reachable at all, since Results, Models and System
had no route outside the advanced sidebar.

One interface now. The panels that used to live in that sidebar are still single
widgets rather than second builds, lent to the destination that shows them:
Browse into the shell, the model library and system panel into This machine, the
results panel into View mode, and the run form into the settings dialog. Two
copies of any of them would disagree the moment a download or a path edit landed
against only one.
"""

from __future__ import annotations

import logging
from functools import partial
from pathlib import Path
from typing import Any

import yaml
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.icons import step_badge_icon
from deepreefmap_gui.core.theme import (
    BORDER,
    CARD_BG,
    CONTROL_HEIGHT,
    DISABLED_FG,
    GUTTER,
    ON_ACCENT,
    PAGE_MARGIN,
    PRIMARY,
    RADIUS_SM,
    SPACE_MD,
    SPACE_SM,
    SPACE_XL,
    SPACE_XS,
    SURFACE_HI,
    TEXT_MUTED,
    WEIGHT_SEMIBOLD,
    WINDOW_TEXT,
)
from deepreefmap_gui.core.widgets import segmented_qss
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.runs.run_detail import RunDetailPanel
from deepreefmap_gui.simple.progress import browse_state, plan_state, run_gate
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
from deepreefmap_gui.survey.store import SURVEY_DB_NAME, SurveyStore

logger = logging.getLogger(__name__)

# Doing the work is a short sequence; looking at what came out is not a step you
# finish but a place you return to, so the two are different kinds of thing.
#
# Two, not three. The clip library used to be a workspace of its own, but Browse
# already groups the same runs By video, so the two answered the same question
# from opposite ends and neither said so. The library now lives inside Browse as
# that grouping's rail and detail pane.
WORKSPACES = ("survey", "browse")

# Inside Survey: plan your transects, then process the day's videos.
SURVEY_STEPS = ("plan", "run")

# One line per workspace, said in the terms of the work rather than the widget.
_WORKSPACE_TIPS = {
    "survey": "Plan transects and process this session's videos.",
    "browse": "Every run and clip so far, and how repeat passes compare.",
}

# Every destination the stack can show, in stack order. Machine and view are
# appended last so the Plan/Run/Browse stack indices other code and tests rely on
# stay put; neither is a numbered survey step. Machine is a utility you visit and
# leave, and view is where an opened run goes, reached by opening one.
SIMPLE_SECTIONS = (*SURVEY_STEPS, "browse", "machine", "view")

# What the info panel takes when it is open. Wide enough for the metadata block
# without eating into the cloud, which is what View mode is for.
VIEW_INFO_WIDTH = 340

_STEP_BADGE_PX = 20

# The numbered badge carries the emphasis, so the label itself stays at body
# weight and only the current step is filled.
_STEP_QSS = (
    f"QToolButton {{ font-weight: {WEIGHT_SEMIBOLD};"
    f" padding: {SPACE_XS}px {SPACE_MD}px {SPACE_XS}px {SPACE_SM}px;"
    f" min-height: {CONTROL_HEIGHT - 2 * SPACE_XS}px;"
    f" border: 1px solid transparent; border-radius: {RADIUS_SM}px;"
    f" background: transparent; color: {TEXT_MUTED}; }}"
    f" QToolButton:hover {{ background: {SURFACE_HI}; color: {WINDOW_TEXT}; }}"
    f" QToolButton:focus {{ border-color: {PRIMARY}; color: {WINDOW_TEXT}; }}"
    f" QToolButton:checked {{ background: {PRIMARY}; color: {ON_ACCENT}; }}"
    f" QToolButton:disabled {{ color: {DISABLED_FG}; background: transparent; }}"
)

# Bars that top a page and are separated from it by a hairline. Object-name
# scoped: an unscoped `border-bottom` on the container is inherited by every
# child, which drew a stray underline beneath each label and button in the row.
_BAR_QSS = (
    f"QWidget {{ background-color: {CARD_BG}; }}"
    f" QWidget#simpleBar {{ border-bottom: 1px solid {BORDER}; }}"
)


def _step_connector() -> QFrame:
    """Hairline joining two step badges, in place of an arrow glyph."""
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedWidth(SPACE_XL)
    line.setFixedHeight(1)
    line.setStyleSheet(f"background-color: {BORDER}; border: none;")
    return line

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
    # The configuration as persisted: organisation preset plus this machine's
    # allow-listed changes. _survey_preset is what the run will use, which can
    # additionally hold a session-only edit the override would not keep.
    _active_preset: ActivePreset | None = None
    _form_defaults: dict[str, Any]
    _simple_nav_buttons: dict[str, QToolButton]
    _workspace_buttons: dict[str, QToolButton]
    _workspace_group: QButtonGroup
    _section_counts: dict[str, QLabel]
    _step_widgets: list[QWidget]
    _section_state_cache: tuple | None = None
    _last_survey_step: str = "plan"
    _work_area_state: tuple[bool, str, bool] | None = None

    def _reveal_memory_detail(self) -> None:
        """Where the memory warning sends you: the readiness rows, which carry
        the same sentence without the system panel's jargon."""
        self._set_simple_section("machine")
        self._set_machine_view("readiness")

    def _idle_status_text(self) -> str:
        return "Ready. Plan your transects, then add videos on the Run step."

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

        These refreshes used to fire on a mode flip, which meant the interface
        was only ever fully populated by switching away from it and back. There
        is one interface now, so they belong to construction.
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
        """Two workspaces over one page stack.

        Survey is the work you do in order: plan the transects, then process the
        videos. Browse is where everything you have ever produced lives, which
        is not a step you finish but a place you come back to, so it sits
        beside the flow rather than at the end of it.

        Both live in one header band, so the band answers "where am I in simple
        mode" while the top bar keeps answering "which interface am I in".
        """
        shell = QWidget()
        layout = QVBoxLayout(shell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Built here so it exists before the first _sync_workspace_chrome, but
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
        self._workspace_buttons = {}
        self._section_counts = {}
        self._wizard_back_buttons = {}
        self._wizard_next_buttons = {}
        self._section_state_cache = None

        workspace_group = QButtonGroup(shell)
        workspace_group.setExclusive(True)
        self._workspace_group = workspace_group
        for index, workspace in enumerate(WORKSPACES):
            btn = QToolButton()
            btn.setText(workspace.capitalize())
            btn.setCheckable(True)
            btn.setStyleSheet(
                segmented_qss(first=index == 0, last=index == len(WORKSPACES) - 1)
            )
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(_WORKSPACE_TIPS[workspace])
            btn.clicked.connect(partial(self._on_workspace_clicked, workspace))
            workspace_group.addButton(btn)
            nav.addWidget(btn)
            self._workspace_buttons[workspace] = btn
        nav.addSpacing(GUTTER * 2)

        # Pages are built before the step buttons that select them, because the
        # Run page constructs the button the Plan footer forwards to.
        pages = {
            "plan": self._build_plan_page(),
            "run": self._build_simple_run_page(),
            "browse": self._build_browse_page(),
            "machine": self._build_machine_page(),
            "view": self._build_view_info_page(),
        }

        step_group = QButtonGroup(shell)
        step_group.setExclusive(True)
        self._step_widgets = []
        for number, name in enumerate(SURVEY_STEPS, start=1):
            btn = QToolButton()
            btn.setText(name.capitalize())
            btn.setCheckable(True)
            btn.setStyleSheet(_STEP_QSS)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            btn.setIconSize(QSize(_STEP_BADGE_PX, _STEP_BADGE_PX))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            step_group.addButton(btn)
            nav.addWidget(btn)
            self._step_widgets.append(btn)
            # The count sits outside the pill so it does not fight the fill when
            # the step is selected.
            count = QLabel("")
            count.setStyleSheet(f"color: {TEXT_MUTED};")
            nav.addWidget(count)
            self._step_widgets.append(count)
            self._section_counts[name] = count
            if number < len(SURVEY_STEPS):
                connector = _step_connector()
                nav.addWidget(connector)
                self._step_widgets.append(connector)
            btn.toggled.connect(partial(self._on_simple_nav_toggled, name))
            self._simple_nav_buttons[name] = btn

        browse_count = QLabel("")
        browse_count.setStyleSheet(f"color: {TEXT_MUTED};")
        nav.addWidget(browse_count)
        self._section_counts["browse"] = browse_count

        nav.addStretch(1)
        # Utilities, at the far end from the work. The log toggle came here off
        # the old top bar, which held it and nothing else once the mode switch
        # went; This machine is the one destination that is not a workspace.
        nav.addWidget(self._log_toggle_btn)
        nav.addWidget(self._build_machine_nav_button())
        layout.addWidget(header)

        for name in SIMPLE_SECTIONS:
            self._simple_stack.addWidget(self._wrap_wizard_page(name, pages[name]))

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

        The whole page is the browse panel. Transect comparison used to sit under
        it permanently, which meant a transect chart while you were grouped by
        video; it is now one page of that panel's detail pane, shown when a
        transect is what you have selected.
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

        back = QPushButton("← Back to Browse")
        back.setProperty("quiet", "true")
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.clicked.connect(lambda: self._set_simple_section("browse"))
        row.addWidget(back)

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
        self._view_info_btn.setStyleSheet(_STEP_QSS)
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
        return "plan"

    def _on_workspace_clicked(self, workspace: str) -> None:
        if workspace == "survey":
            # Coming back to Survey lands on the step you left, not always the
            # first one.
            self._set_simple_section(getattr(self, "_last_survey_step", "plan"))
        else:
            self._set_simple_section(workspace)

    def _sync_workspace_chrome(self) -> None:
        """Reflect which workspace the live section belongs to, and hide the
        step controls entirely in Browse, where they steer nothing."""
        section = self._current_section()
        in_survey = section in SURVEY_STEPS
        # The header band is the workspace switch; View replaces it with its own
        # bar, so nothing here should read as the live destination while it shows.
        self._simple_header.setVisible(section != "view")
        self._set_view_bar_visible(section == "view")
        if section in ("machine", "view"):
            # Neither sits inside a workspace, so no pill should own them.
            #
            # Exclusivity is lifted first: an exclusive QButtonGroup refuses to
            # let its one checked member be unchecked, so this silently did
            # nothing and Browse stayed lit while This machine was on screen.
            self._workspace_group.setExclusive(False)
            for button in self._workspace_buttons.values():
                button.blockSignals(True)
                button.setChecked(False)
                button.blockSignals(False)
            self._workspace_group.setExclusive(True)
        else:
            button = self._workspace_buttons["survey" if in_survey else section]
            button.blockSignals(True)
            button.setChecked(True)
            button.blockSignals(False)
        for widget in self._step_widgets:
            widget.setVisible(in_survey)
        # The run count describes Browse, so it goes with Browse rather than
        # sitting over the clip library counting something else's rows.
        self._section_counts["browse"].setVisible(section == "browse")

    def _refresh_section_state(self) -> None:
        """Paint each step's badge and count from the cached verdicts.

        A pure painter: it reads attributes other code has already computed and
        never touches the store or the filesystem, because it is called from
        paths that run on every keystroke.
        """
        if not hasattr(self, "_simple_nav_buttons"):
            return
        states = {
            "plan": getattr(self, "_plan_state", None) or plan_state(0, False),
            "run": getattr(self, "_survey_gate", None) or run_gate(
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
        for number, name in enumerate(SURVEY_STEPS, start=1):
            verdict = states[name]
            button = self._simple_nav_buttons[name]
            button.setIcon(step_badge_icon(number, verdict.state, _STEP_BADGE_PX))
            button.setToolTip(verdict.reason or f"{name.capitalize()}: {verdict.count}")
        for name, verdict in states.items():
            self._section_counts[name].setText(verdict.count)
            self._section_counts[name].setToolTip(verdict.reason)
        self._workspace_buttons["browse"].setToolTip(
            states["browse"].reason
            or "Everything processed so far, and how repeat passes compare."
        )

    def _wrap_wizard_page(self, name: str, page: QWidget) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(page, 1)
        layout.addLayout(self._build_wizard_footer(name))
        return container

    def _build_wizard_footer(self, name: str) -> QHBoxLayout:
        """Back on the left, the step's forward action on the right.

        The Run step's forward action is the run button itself, so there is one
        obvious thing to press rather than a Next beside a Start. Browse has no
        footer: it is a destination, not a step on the way to one.
        """
        row = QHBoxLayout()
        row.setContentsMargins(0, 6, 0, 0)
        if name not in SURVEY_STEPS:
            return row
        index = SURVEY_STEPS.index(name)
        if index > 0:
            back = QPushButton("← Back")
            back.setProperty("quiet", "true")
            back.clicked.connect(partial(self._go_to_step, SURVEY_STEPS[index - 1]))
            self._wizard_back_buttons[name] = back
            row.addWidget(back)
        row.addStretch(1)
        if name == "run":
            row.addWidget(self._survey_start_btn)
        elif index < len(SURVEY_STEPS) - 1:
            nxt = SURVEY_STEPS[index + 1]
            button = QPushButton(f"Next: {nxt.capitalize()} →")
            button.setProperty("cta", "true")
            button.clicked.connect(partial(self._go_to_step, nxt))
            self._wizard_next_buttons[name] = button
            row.addWidget(button)
        return row

    def _go_to_step(self, name: str) -> None:
        self._set_simple_section(name)

    def _set_wizard_navigation_enabled(self, enabled: bool) -> None:
        """A run in flight owns the Survey steps, so stepping away is blocked.

        Browse stays reachable throughout: a batch takes tens of minutes and
        looking at what finished earlier is exactly what you want to do while it
        runs. Opening a run from there is refused separately, so the live run
        keeps the viewer.
        """
        steps: list[QAbstractButton] = list(self._simple_nav_buttons.values())
        steps.extend(self._wizard_back_buttons.values())
        steps.extend(self._wizard_next_buttons.values())
        steps.append(self._workspace_buttons["survey"])
        for button in steps:
            button.setEnabled(enabled)

    def _on_simple_nav_toggled(self, name: str, checked: bool) -> None:
        if checked:
            self._set_simple_section(name)

    def _set_simple_section(self, name: str) -> None:
        if name not in SIMPLE_SECTIONS:
            raise ValueError(f"Unknown simple section: {name!r}")
        if name in SURVEY_STEPS:
            self._last_survey_step = name
            button = self._simple_nav_buttons[name]
            if not button.isChecked():
                button.blockSignals(True)
                button.setChecked(True)
                button.blockSignals(False)
        if name == "machine":
            self._refresh_readiness_view()
        self._simple_stack.setCurrentIndex(SIMPLE_SECTIONS.index(name))
        self._sync_workspace_chrome()
        # The gauges poll at 1 Hz, so they run only while they are on screen.
        self._sync_system_gauges_running()
        self._update_work_area()

    def _update_work_area(self) -> None:
        """The one place that decides whether the viewer pane shows and how the
        work splitter divides, from (app_mode, section)."""
        if not hasattr(self, "_work_hsplitter"):
            return
        section = self._current_section() if hasattr(self, "_simple_stack") else "plan"
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
        return store
