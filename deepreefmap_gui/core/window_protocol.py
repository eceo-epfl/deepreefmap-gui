"""Declare the surface the DeepReefMapWindow mixins share, so mypy can resolve
cross-mixin `self._foo` references."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import logging
    import threading
    import uuid
    from pathlib import Path

    from deepreefmap.config.classes import ClassConfig
    from deepreefmap.pipeline.artifacts import SemanticPointCloud
    from deepreefmap.pointcloud.grid_ortho import OrthoGrid
    from PySide6.QtCore import QSettings, QTimer, Signal
    from PySide6.QtWidgets import (
        QCheckBox,
        QComboBox,
        QDoubleSpinBox,
        QGridLayout,
        QGroupBox,
        QLabel,
        QLineEdit,
        QProgressBar,
        QPushButton,
        QSlider,
        QSpinBox,
        QSplitter,
        QStackedWidget,
        QTableWidget,
        QTabWidget,
        QToolButton,
        QTreeWidget,
        QVBoxLayout,
        QWidget,
    )

    from deepreefmap_gui.core.spinner import SpinnerStopButton
    from deepreefmap_gui.core.widgets import NotReadyStrip
    from deepreefmap_gui.form.time_edit import TimeSecondsEdit
    from deepreefmap_gui.io.lazy_frames import FrameAccessor
    from deepreefmap_gui.map.widget import SlippyMapWidget
    from deepreefmap_gui.models.library_ui import PackProgressDialog
    from deepreefmap_gui.profiling.eta import RunEtaEstimator
    from deepreefmap_gui.runs.progress import ProgressModel
    from deepreefmap_gui.runs.run_detail import RunDetailPanel
    from deepreefmap_gui.runs.run_table import RunTable
    from deepreefmap_gui.runs.sunburst import SunburstWidget
    from deepreefmap_gui.runs.timing_popup import HoverColumn, TimingPopup
    from deepreefmap_gui.simple.charts import GroupedBarChart
    from deepreefmap_gui.simple.plan import NotesEdit
    from deepreefmap_gui.survey.models import SurveyBatch
    from deepreefmap_gui.survey.preset import ActivePreset
    from deepreefmap_gui.survey.store import SurveyStore
    from deepreefmap_gui.system.log_view import LogView
    from deepreefmap_gui.viewer.pick_tooltip import PickCard
    from deepreefmap_gui.viewer.widget import QtPointCloudViewer

    # QWidget, not QMainWindow: DeepReefMapWindow lists QMainWindow first among
    # its bases, so a QMainWindow base here breaks C3 linearisation.
    class MixinBase(QWidget):
        # --- plain state -------------------------------------------------
        _classes_config: ClassConfig
        _classes_path: Path | None
        _active_run_dir: Path | None
        _video_duration_s: float | None
        _settings: QSettings
        _playback_timer: QTimer
        _pipeline_thread: threading.Thread | None
        _active_progress_model: ProgressModel | None
        _status_tick_timer: QTimer
        _status_base_text: str
        _status_count_text: str
        _status_phase_key: str | None
        _status_phase_started: float
        _active_run_manifest: dict | None
        _results_output_dir: Path | None
        _TAB_RUN: int
        _TAB_RESULTS: int
        _TAB_DATA: int
        _TAB_SYSTEM: int
        _TAB_MODELS: int
        _ui_mode: str
        _app_mode: str
        _form_preferred_width: int
        _survey_store_obj: SurveyStore | None
        _transect_form_id: uuid.UUID | None
        _pick_stage: str | None
        _plan_map_fitted: bool
        _survey_rows: list
        _survey_table_index: list[int | None]
        _survey_transects: list
        _survey_batch: SurveyBatch | None
        _survey_preset: dict | None
        _active_preset: ActivePreset | None
        _survey_cancel_event: threading.Event | None
        _survey_worker_running: bool
        _settings_dialog_open: bool
        _analysis_covers: list
        _analysis_all_covers: list
        _downloading: set[str]
        _download_cancel_requested: set[str]
        _download_errors: dict[str, str]
        _delete_armed: dict[str, QPushButton]
        _model_actions: dict[str, QWidget]
        _model_rows: dict[str, QWidget]
        _last_model_states: list
        _pack_progress_dialog: PackProgressDialog | None
        _pack_cancel_event: threading.Event
        _run_warnings: list[str]

        # --- composite widgets / models ----------------------------------
        _viewer: QtPointCloudViewer
        _log_view: LogView
        _cover_sunburst: SunburstWidget
        _load_model: ProgressModel
        _recon_model: ProgressModel

        # --- ortho preview state -----------------------------------------
        _ortho_cloud: SemanticPointCloud | None
        _base_ortho_grid: OrthoGrid | None
        _current_ortho_grid: OrthoGrid | None
        _ortho_classes_config: ClassConfig | None

        # --- viewer / legend / pick state --------------------------------
        _pick_card: PickCard | None
        _last_pick_payload: dict | None
        _pick_card_pinned_pos: tuple[int, int] | None
        _legend_sort_mode: str
        _legend_sort_ascending: bool
        _legend_sort_connected: bool
        _legend_order_cache: list[int] | None
        _legend_solo_buttons: dict[int, QToolButton]
        _legend_toggles: dict[int, QCheckBox]
        _run_log_file_handler: logging.FileHandler | None
        _scene_accessor: FrameAccessor | None
        _available_releases: list[dict]
        _current_version_str: str

        # --- checkboxes --------------------------------------------------
        _refine_intrinsics_check: QCheckBox
        _require_gravity_check: QCheckBox
        _skip_seg_check: QCheckBox
        _tsdf_check: QCheckBox
        _update_show_all: QCheckBox

        # --- buttons -----------------------------------------------------
        _batch_btn: QPushButton
        _desktop_entry_btn: QPushButton
        _discover_btn: QPushButton
        _export_models_btn: QPushButton
        _hf_auth_btn: QPushButton
        _import_pack_btn: QPushButton
        _pause_btn: QPushButton
        _scrub_btn: QPushButton
        _spinner_stop: SpinnerStopButton
        _start_btn: QPushButton
        _update_btn: QPushButton

        # --- spin boxes --------------------------------------------------
        _batch_size_spin: QSpinBox
        _fps_spin: QSpinBox
        _grid_bins_spin: QSpinBox
        _proc_height_spin: QSpinBox
        _proc_width_spin: QSpinBox
        _rr_est_frames_spin: QSpinBox
        _scs_height_spin: QSpinBox
        _scs_width_spin: QSpinBox
        _begin_spin: TimeSecondsEdit
        _crop_width: QDoubleSpinBox
        _end_spin: TimeSecondsEdit
        _results_crop_width: QDoubleSpinBox
        _results_transect_length: QDoubleSpinBox
        _rr_factor_spin: QDoubleSpinBox
        _rr_override_spin: QDoubleSpinBox
        _transect_length: QDoubleSpinBox

        # --- sliders -----------------------------------------------------
        _frame_slider: QSlider
        _results_crop_slider: QSlider
        _results_transect_slider: QSlider

        # --- labels ------------------------------------------------------
        _cover_label: QLabel
        _hf_auth_icon: QLabel
        _hf_auth_label: QLabel
        _ortho_rgb_preview: QLabel
        _ortho_seg_preview: QLabel
        _run_meta_banner: QLabel
        _memory_notice: QLabel
        _memory_warn_icon: QLabel
        _recorded_runs_caption: QLabel
        _status_label: QLabel
        _update_status_label: QLabel
        _update_version_label: QLabel
        _warnings_label: QLabel
        _warnings_label_running: QLabel

        # --- run form, borrowed by the simple-mode settings dialog ----------
        _setup_page: QWidget
        _run_tab_layout: QVBoxLayout
        _video_row_widget: QWidget
        _range_row_widget: QWidget
        _run_name_widget: QWidget
        _transect_length_widget: QWidget

        # --- data section --------------------------------------------------
        _data_panel: QWidget
        _data_tab: QWidget
        _data_host_simple: QWidget
        _data_tree: QTreeWidget
        _data_run_table: RunTable
        _data_map: SlippyMapWidget
        _data_rail_split: QSplitter
        _run_detail: RunDetailPanel
        _data_run_stack: QStackedWidget
        _data_group_header: QLabel
        _data_disk_label: QLabel
        _data_open_btn: QPushButton
        _data_show_btn: QPushButton
        _data_refresh_timer: QTimer
        _data_facet_buttons: dict[str, QToolButton]
        _data_entries: list
        _data_groups: dict
        _data_facet: str
        _data_selected_key: tuple | None
        _data_rebuilt_root: Path | None
        _data_store_ok: bool
        _run_size_cache: dict[str, int]
        # (output root, measured bytes per footage minute); None until measured.
        _footage_rate_cache: tuple[Path, float | None] | None
        _data_sizes_scan_running: bool

        # --- survey mode -------------------------------------------------
        _mode_toggle_btn: QWidget
        _mode_buttons: dict[str, QToolButton]
        _plan_map: SlippyMapWidget
        _simple_header: QWidget
        _view_bar: QWidget
        _view_title: QLabel
        _view_info_btn: QToolButton
        _view_info_open: bool
        _view_detail: RunDetailPanel
        _survey_batch_name: QLineEdit
        _survey_preset_label: QLabel
        _survey_pass_table: QTableWidget
        _survey_start_btn: QPushButton
        _survey_settings_btn: QPushButton
        _survey_restore_btn: QPushButton
        _survey_audit_btn: QPushButton
        _survey_import_btn: QPushButton
        _survey_not_ready: NotReadyStrip
        _setup_memory_label: QLabel
        _hf_auth_user: str | None
        _analysis_transect_combo: QComboBox
        _analysis_level_combo: QComboBox
        _analysis_chart: GroupedBarChart
        _analysis_stats_table: QTableWidget
        _analysis_repro_label: QLabel
        _analysis_estimate_label: QLabel
        _transect_list: QTreeWidget
        _tr_name_input: QLineEdit
        _tr_start_coord: QLineEdit
        _tr_end_coord: QLineEdit
        _tr_geometry: QLabel
        _pick_both_btn: QToolButton
        _plan_view_timer: QTimer
        _tr_length: QDoubleSpinBox
        _tr_depth: QDoubleSpinBox
        _tr_description: NotesEdit

        # --- combos / line edits -----------------------------------------
        _map_combo: QComboBox
        _profile_combo: QComboBox
        _resolution_preset_combo: QComboBox
        _seg_combo: QComboBox
        _update_version_combo: QComboBox
        _out_root_input: QLineEdit
        _run_name_input: QLineEdit
        _scs_checkpoint_input: QLineEdit
        _video_input: QLineEdit

        # --- containers / layouts ----------------------------------------
        _crop_box: QGroupBox
        _results_group: QGroupBox
        _models_grid: QGridLayout
        _sidebar_tabs: QTabWidget
        _left_stack: QStackedWidget
        _simple_stack: QStackedWidget
        _wizard_back_buttons: dict[str, QPushButton]
        _wizard_next_buttons: dict[str, QPushButton]
        _work_hsplitter: QSplitter
        _new_run_btn: QPushButton
        _progress_bar: QProgressBar
        _total_progress_bar: QProgressBar
        _bottom_progress_bar: QProgressBar
        _bottom_bar: QWidget
        _progress_stack: HoverColumn
        _eta_total_label: QLabel
        _eta: RunEtaEstimator | None
        _timing_popup: TimingPopup

        # --- signals (defined as class attrs on DeepReefMapWindow) --------
        _sig_update_check_done = Signal(str, object, object)
        _sig_model_status_done = Signal(object, object)
        _sig_pipeline_error = Signal(str)
        _sig_pipeline_cancelled = Signal()
        _sig_status_text = Signal(str)
        _sig_hf_auth_done = Signal(object, str)
        _sig_download_progress = Signal(str, int)
        _sig_pack_progress = Signal(str, str, "qint64", "qint64")  # type: ignore[arg-type]
        _sig_pack_done = Signal(bool, str)
        _sig_run_loaded = Signal(object, str, str, int)
        _sig_load_progress = Signal(str, int, int)
        _sig_scene_file_done = Signal()
        _sig_batch_progress = Signal(int, int, str)
        _sig_batch_done = Signal(int, int, str)
        _sig_qc_render_progress = Signal(int, int)
        _sig_qc_render_done = Signal(bool, str)
        _sig_discovery_done = Signal(object, object)
        _sig_survey_progress = Signal(int, int, str)
        _sig_survey_done = Signal(int, int, str)
        _sig_run_sizes_done = Signal(object)
        _sig_videos_probed = Signal(object)

        # --- cross-mixin methods -----------------------------------------
        def _add_run_warning(self, message: str) -> None: ...
        def _add_video_path(self, path: str, probed: tuple[float, float] | None = None) -> bool: ...
        def _add_video_paths(self, paths: list[str]) -> None: ...
        def _on_videos_probed(self, probed: list) -> None: ...
        def _on_survey_import_csv(self) -> None: ...
        def _apply_progress(
            self,
            phase_key: str,
            label: str,
            current: int = 0,
            total: int = 0,
            flush: bool = False,
        ) -> None: ...
        def _auto_load_run(self, run_dir: Path) -> None: ...
        def _begin_progress(self, model: ProgressModel) -> None: ...
        def _progress_sinks(self) -> list: ...
        def _build_legend(self) -> None: ...
        def _build_model_status_button(self, combo: QComboBox) -> QPushButton: ...
        def _build_system_panel(self, layout: object) -> None: ...
        def _on_sidebar_tab_changed(self, index: int) -> None: ...
        def _refresh_recorded_runs(self) -> None: ...
        def _update_memory_profile_warning(self) -> None: ...
        def _cancel_load(self) -> None: ...
        def _check_for_update(self) -> None: ...
        def _clear_run_warnings(self) -> None: ...
        def _collect_loger_options(self, mapping_name: str) -> dict | None: ...
        def _collect_run_settings(self) -> dict: ...
        def _estimate_frame_count(self, fps: int) -> int | None: ...
        def _recompute_submit_state(self) -> None: ...
        def _gpu_only_mapper(self) -> str: ...
        def _gpu_available(self) -> bool: ...
        def _refresh_desktop_entry_button(self) -> None: ...
        def _refresh_model_status(self) -> None: ...
        def _build_data_panel(self) -> QWidget: ...
        def _build_simple_data_host(self) -> QWidget: ...
        def _build_browse_page(self) -> QWidget: ...
        def _refresh_section_state(self) -> None: ...
        def _refresh_browse_state(self) -> None: ...
        def _update_data_actions(self) -> None: ...
        def _set_scope_transect(self, transect_id: uuid.UUID | None) -> None: ...
        def _on_survey_pass_activated(self, row_index: int, column: int) -> None: ...
        def _on_analysis_transect_changed(self) -> None: ...
        def _host_data_panel(self, simple: bool) -> None: ...
        def _refresh_data_manager(self) -> None: ...
        def _focus_data_on_transect(self, transect_id: uuid.UUID) -> None: ...
        def _request_data_refresh(self) -> None: ...
        def _apply_run_sizes(self, sizes: dict) -> None: ...
        def _hide_run_meta_banner(self) -> None: ...
        def _refresh_run_warnings_view(self) -> None: ...
        def _required_model_names(self) -> set[str]: ...
        def _reset_progress_bars(self) -> None: ...
        def _reset_form_defaults(self) -> None: ...
        def _snapshot_form_settings(self) -> dict[str, Any]: ...
        def _restore_form_settings(self, snapshot: dict[str, Any]) -> None: ...
        def _adopt_form_as_preset(self) -> None: ...
        def _reload_active_preset(self) -> None: ...
        def _restore_standard_settings(self) -> None: ...
        def _survey_deviations(self) -> dict: ...
        def _idle_status_text(self) -> str: ...
        def _on_edit_run_settings(self) -> None: ...
        def _set_progress_widgets_visible(self, visible: bool) -> None: ...
        def _build_bottom_bar(self) -> QWidget: ...
        def _render_status(self) -> None: ...
        def _render_eta(self) -> None: ...
        def _end_run_controls(self) -> None: ...
        def _begin_run_controls(self) -> None: ...
        def _run_in_flight(self) -> bool: ...
        def _new_run_estimator(self) -> RunEtaEstimator: ...
        def _reveal_legend_overlay(self) -> None: ...
        def _set_app_mode(self, mode: str) -> None: ...
        def _set_form_enabled(self, enabled: bool) -> None: ...
        def _build_mode_toggle(self) -> QWidget: ...
        def _build_plan_page(self) -> QWidget: ...
        def _build_simple_run_page(self) -> QWidget: ...
        def _build_analysis_page(self) -> QWidget: ...
        def _build_simple_shell(self) -> QWidget: ...
        def _build_setup_page(self) -> QWidget: ...
        def _build_videos_page(self) -> QWidget: ...
        def _refresh_videos_page(self) -> None: ...
        def _queue_video_path(self, path: str | None) -> None: ...
        def _build_setup_nav_button(self) -> QToolButton: ...
        def _refresh_setup_page(self) -> None: ...
        def _current_setup_checks(self) -> list: ...
        def _initial_simple_section(self) -> str: ...
        def _reveal_memory_detail(self) -> None: ...
        def _simple_peak_frames(self, fps: int) -> int | None: ...
        def _survey_missing_models(self) -> list[str]: ...
        def _download_model(self, model_name: str) -> None: ...
        def _set_simple_section(self, name: str) -> None: ...

        def _enter_view_mode(self, run_dir: Path) -> None: ...
        def _go_to_step(self, name: str) -> None: ...
        def _set_wizard_navigation_enabled(self, enabled: bool) -> None: ...
        def _wrap_wizard_page(self, name: str, page: QWidget) -> QWidget: ...
        def _update_work_area(self) -> None: ...
        def _init_ui_mode(self) -> None: ...
        def _request_ui_mode(self, mode: str) -> None: ...
        def _set_ui_mode(self, mode: str) -> None: ...
        def _survey_store(self) -> SurveyStore: ...
        def _survey_data_changed(self) -> None: ...
        def _refresh_transect_list(self, select_id: uuid.UUID | None = None) -> None: ...
        def _refresh_survey_analysis(self) -> None: ...
        def _refresh_survey_batch_tab(self) -> None: ...
        def _refresh_survey_transect_combos(self) -> None: ...
        def _refresh_survey_pass_statuses(self) -> None: ...
        def _recompute_survey_start(self) -> None: ...
        def _survey_preset_summary(self) -> str: ...
        def _on_survey_progress(self, index: int, total: int, name: str) -> None: ...
        def _on_survey_done(self, ok: int, total: int, last_error: str) -> None: ...
        def _set_log_panel_visible(self, visible: bool) -> None: ...
        def _set_ortho_sources(
            self,
            cloud: SemanticPointCloud | None,
            base_grid: OrthoGrid | None,
            classes_config: ClassConfig | None,
        ) -> None: ...
        def _set_semantic_only_controls_visible(self, visible: bool) -> None: ...
        def _show_results(self, output_dir: str) -> None: ...
        def _show_run_meta_banner(
            self, manifest: dict, run_dir: Path, *, include_disk_size: bool
        ) -> None: ...
        def _show_viewer_controls(self) -> None: ...
        def _set_overlay_controls_visible(self, visible: bool) -> None: ...
        def _update_effective_dir_label(self) -> None: ...

        # event handlers invoked across mixins
        def _on_batch_clicked(self) -> None: ...
        def _on_discover_clicked(self) -> None: ...
        def _on_export_cover_csv(self) -> None: ...
        def _on_export_current_frame(self) -> None: ...
        def _on_export_ortho_npz(self) -> None: ...
        def _on_export_ortho_png(self) -> None: ...
        def _on_export_qc_video(self) -> None: ...
        def _on_export_zip(self) -> None: ...
        def _on_follow_camera_changed(self) -> None: ...
        def _on_hf_auth_button(self) -> None: ...
        def _on_new_reconstruction(self) -> None: ...
        def _open_model_library(self) -> None: ...
        def _on_export_models(self) -> None: ...
        def _on_import_model_pack(self) -> None: ...
        def _on_pack_progress(
            self, phase: str, label: str, current: int, total: int
        ) -> None: ...
        def _on_pack_done(self, ok: bool, message: str) -> None: ...
        def _on_pause_toggled(self, paused: bool) -> None: ...
        def _on_play_fps_changed(self) -> None: ...
        def _on_play_toggled(self, playing: bool) -> None: ...
        def _on_required_models_changed(self, _value: object = "") -> None: ...
        def _on_results_crop_slider_changed(self, value: int) -> None: ...
        def _on_results_crop_width_changed(self, value: float) -> None: ...
        def _on_results_transect_length_changed(self, value: float) -> None: ...
        def _on_results_transect_slider_changed(self, value: int) -> None: ...
        def _on_stop_clicked(self) -> None: ...
        def _on_submit(self) -> None: ...
        def _on_sunburst_selection(self, class_ids: list) -> None: ...
        def _on_toggle_desktop_entry(self) -> None: ...
        def _on_toggle_show_all_versions(self, _checked: bool) -> None: ...
        def _on_update(self) -> None: ...
        def _on_viewer_control_changed(self) -> None: ...
        def _on_view_from_camera(self) -> None: ...

        @staticmethod
        def _format_cover_html(cover: dict) -> str: ...
        @staticmethod
        def _sanitize_run_name(name: str) -> str: ...

else:
    MixinBase = object
