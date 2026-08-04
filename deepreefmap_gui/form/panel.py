"""The run form, the progress bar and the status strip.

The form is never on screen as part of the window: it is built into a hidden
holder and lent to the run settings dialog. Every setting a batch runs under
lives in these widgets, and _collect_run_settings() reads exactly them, so the
batch runs from what the dialog edited whether or not the dialog is open. What
varies per pass (the clips, the trim, the transect) comes from the pass table on
the Run step instead.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import cast

from PySide6.QtCore import (
    QFileSystemWatcher,
    QSettings,
    QSize,
    QStandardPaths,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QDesktopServices,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QStyle,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.spinner import SpinnerStopButton
from deepreefmap_gui.core.theme import (
    BAR_HEIGHT,
    BLOCK,
    BORDER,
    BUTTON,
    CARD_BG,
    FONT_SM,
    FONT_XL,
    GUTTER,
    PAGE_MARGIN,
    PREVIEW_BG,
    PRIMARY,
    PRIMARY_DARK,
    RADIUS_SM,
    SPACE_XS,
    SURFACE_HI,
    TEXT_MUTED,
    TEXT_SECONDARY,
    UPDATE,
    WARN_TEXT,
    WEIGHT_BOLD,
    bar_qss,
)
from deepreefmap_gui.core.widgets import EmptyState, warning_banner_qss
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.packaging.releases import current_version, pyapp_binary_path
from deepreefmap_gui.runs.progress import (
    _LOAD_PHASES,
    _RECON_PHASES,
    ProgressModel,
)
from deepreefmap_gui.runs.sunburst import SunburstWidget
from deepreefmap_gui.runs.timing_popup import HoverColumn
from deepreefmap_gui.system.log_view import LogView, install_qt_log_handler


class _InstantTipLabel(QLabel):
    """A QLabel whose tooltip appears the instant the cursor enters, no delay."""

    # Lets the whole label act as a button, so the glyph can be a color-honouring
    # span: Qt paints anchor links in the palette color and ignores inline color.
    clicked = Signal()

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self.toolTip():
            QToolTip.showText(event.globalPosition().toPoint(), self.toolTip(), self)
        super().enterEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.clicked.emit()
        super().mousePressEvent(event)


# Transport controls sit in the bottom bar where they are the primary run
# affordance, so they are larger than the old 40px top-bar cluster.
_TRANSPORT_SIZE = 34
_TRANSPORT_ICON = 28


def _separator() -> QWidget:
    line = QWidget()
    line.setFixedHeight(1)
    line.setStyleSheet(f"background-color: {BORDER};")
    return line


class FormPanelMixin(MixinBase):
    """DeepReefMapWindow methods that build and drive the run form and the status bars."""

    def _build_form_widgets(self) -> None:
        from deepreefmap.camera.intrinsics import available_profile_names
        from deepreefmap.mapping.registry import list_mapping_backends
        from deepreefmap.segmentation.registry import list_segmentation_models

        profiles = available_profile_names() or ["gopro_hero_10"]
        seg_models = list_segmentation_models()
        map_backends = list_mapping_backends()
        documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        default_root = str(Path(documents or str(Path.home())) / "DeepReefMap")

        (
            setup_layout,
            viewer_layout,
            models_layout,
            system_layout,
        ) = self._build_panel_homes()

        self._build_deferred_top_bar_widgets(setup_layout)
        self._build_input_group(setup_layout, profiles)
        self._build_model_selection_group(setup_layout, seg_models, map_backends)
        self._build_output_group(setup_layout, default_root)
        self._build_advanced_toggle_and_notices(setup_layout)
        self._build_advanced_panel(setup_layout)
        self._build_run_control_buttons()
        self._build_gated_warning(setup_layout)
        self._build_run_warnings_and_log(viewer_layout)
        self._build_progress_widgets()
        self._build_results_group(viewer_layout)
        self._build_models_panel(models_layout)
        self._build_updates_section(system_layout)

        # Start in SETUP, no run loaded yet. The mode flips to RUNNING when a
        # batch starts and to VIEWING when a past run is selected or a
        # reconstruction completes.
        self._set_app_mode("SETUP")

    def _build_panel_homes(self) -> tuple[QVBoxLayout, ...]:
        """A permanent home for each panel that is shown somewhere else.

        None of these are on screen as built. The run form is lent to the run
        settings dialog; the results, model and system panels are lent to View
        mode and to This machine. Every one gets a holder of its own rather than
        sharing a layout, because a panel handed back into a shared layout lands
        wherever that layout happens to append it.

        Parented to the window and hidden: a parentless widget made visible maps
        itself as a top-level window, which flashes an empty titlebar on screen.
        """
        # The run form. RunSettingsDialog takes this page, wraps it in a scroll
        # area and puts it back on close, by both exit paths. Without a holder
        # that outlives the dialog the form would be left parented to something
        # being destroyed, and every later _collect_run_settings() would raise
        # on a deleted C++ object.
        self._form_home = QWidget(self)
        self._form_home.setVisible(False)
        self._form_home_layout = QVBoxLayout(self._form_home)
        self._form_home_layout.setContentsMargins(0, 0, 0, 0)
        self._setup_page = QWidget()
        setup_layout = QVBoxLayout(self._setup_page)
        setup_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        setup_layout.setContentsMargins(0, 0, 0, 0)
        self._form_home_layout.addWidget(self._setup_page)

        # What a finished run produced: quality warnings, the ortho previews,
        # benthic cover, the transect crop and the exports. Shown in View mode's
        # info panel, beside the cloud they all describe.
        self._results_page = QWidget()
        results_layout = QVBoxLayout(self._results_page)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._models_home = QWidget(self)
        self._models_home.setVisible(False)
        models_layout = QVBoxLayout(self._models_home)
        models_layout.setContentsMargins(0, 0, 0, 0)
        models_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        from deepreefmap_gui.system.panel import build_system_home

        self._system_home, self._system_page, system_layout = build_system_home(self)
        self._build_system_panel(system_layout)
        return (
            setup_layout,
            results_layout,
            models_layout,
            system_layout,
        )

    def _build_deferred_top_bar_widgets(self, setup_layout: QVBoxLayout) -> None:
        # These widgets are owned by the top toolbar but constructed here so
        # initialisation code can reference them before the toolbar is laid out.

        # Log toggle button, checkable so the pressed state mirrors panel
        # visibility. The panel is opened on request only: a batch reports
        # itself on the Run step, and the log is for looking closer.
        self._log_toggle_btn = QPushButton("Log")
        self._log_toggle_btn.setToolTip("Show or hide the live log panel")
        self._log_toggle_btn.setCheckable(True)
        self._log_toggle_btn.setFixedHeight(28)
        self._log_toggle_btn.toggled.connect(self._set_log_panel_visible)

        self._warnings_label_running = QLabel("")
        self._warnings_label_running.setWordWrap(True)
        self._warnings_label_running.setTextFormat(Qt.TextFormat.RichText)
        self._warnings_label_running.setStyleSheet(
            warning_banner_qss()
        )
        self._warnings_label_running.setVisible(False)
        setup_layout.addWidget(self._warnings_label_running)

    def _build_input_group(self, setup_layout: QVBoxLayout, profiles: list[str]) -> None:
        """How the footage is read: which lens it was shot through, and how
        densely it is sampled. Both are settings the whole batch shares; the
        clips themselves come from the pass table on the Run step."""
        input_group = QGroupBox("Input")
        ig = QVBoxLayout(input_group)

        profile_fps_row = QHBoxLayout()
        profile_fps_row.setContentsMargins(0, 0, 0, 0)
        profile_col = QVBoxLayout()
        profile_col.setContentsMargins(0, 0, 0, 0)
        profile_col.addWidget(QLabel("Camera profile"))
        self._profile_combo = QComboBox()
        self._profile_combo.addItems(profiles)
        profile_col.addWidget(self._profile_combo)
        profile_fps_row.addLayout(profile_col, 1)

        fps_col = QVBoxLayout()
        fps_col.setContentsMargins(0, 0, 0, 0)
        fps_col.addWidget(QLabel("FPS"))
        self._fps_spin = QSpinBox()
        self._fps_spin.setRange(1, 60)
        self._fps_spin.setValue(5)
        self._fps_spin.setMinimumWidth(64)
        fps_col.addWidget(self._fps_spin)
        profile_fps_row.addLayout(fps_col)
        ig.addLayout(profile_fps_row)
        setup_layout.addWidget(input_group)

    def _build_model_selection_group(
        self, setup_layout: QVBoxLayout, seg_models: list[str], map_backends: list[str]
    ) -> None:
        from deepreefmap.mapping.registry import loger_available

        from deepreefmap_gui.models.loger_hint import LOGER_INSTALL_HINT

        models_group = QGroupBox("Models")
        mg = QVBoxLayout(models_group)

        mg.addWidget(QLabel("Segmentation"))
        seg_row = QHBoxLayout()
        seg_row.setContentsMargins(0, 0, 0, 0)
        seg_row.setSpacing(4)
        self._seg_combo = QComboBox()
        self._seg_combo.addItems(seg_models)
        idx = self._seg_combo.findText("coralscapes-vit-b-dpt")
        if idx >= 0:
            self._seg_combo.setCurrentIndex(idx)
        seg_row.addWidget(self._seg_combo, 1)
        self._seg_status_btn = self._build_model_status_button(self._seg_combo)
        seg_row.addWidget(self._seg_status_btn)
        mg.addLayout(seg_row)

        mg.addWidget(QLabel("Mapping"))
        map_row = QHBoxLayout()
        map_row.setContentsMargins(0, 0, 0, 0)
        map_row.setSpacing(4)
        self._map_combo = QComboBox()
        self._map_combo.addItems(map_backends)
        default_map = "loger_star" if loger_available() else "scsfmlearner"
        idx = self._map_combo.findText(default_map)
        if idx >= 0:
            self._map_combo.setCurrentIndex(idx)
        if not loger_available():
            map_model = self._map_combo.model()
            if isinstance(map_model, QStandardItemModel):
                for i in range(self._map_combo.count()):
                    if self._map_combo.itemText(i) in ("loger", "loger_star"):
                        item = map_model.item(i)
                        item.setEnabled(False)
                        item.setData(LOGER_INSTALL_HINT, Qt.ItemDataRole.ToolTipRole)
        map_row.addWidget(self._map_combo, 1)
        self._map_status_btn = self._build_model_status_button(self._map_combo)
        map_row.addWidget(self._map_status_btn)
        mg.addLayout(map_row)

        setup_layout.addWidget(models_group)

    def _build_output_group(self, setup_layout: QVBoxLayout, default_root: str) -> None:
        output_group = QGroupBox("Output")
        # Not a setting: it is the root a whole survey lands under, which is a
        # property of this computer. The run settings dialog hides the group
        # whole for that reason, which also keeps it from rendering as an empty
        # titled frame once This machine has borrowed the controls out of it.
        self._output_group = output_group
        og = QVBoxLayout(output_group)

        style = self.style()
        # Wrapped in one widget so it can be lent to This machine, which is
        # where the output root is edited. Offering a second place to change it
        # would invite two answers to one question.
        self._out_root_widget = QWidget()
        out_root_layout = QVBoxLayout(self._out_root_widget)
        out_root_layout.setContentsMargins(0, 0, 0, 0)
        out_root_layout.setSpacing(SPACE_XS)
        label_row = QHBoxLayout()
        label_row.setContentsMargins(0, 0, 0, 0)
        label_row.setSpacing(4)
        label_row.addWidget(QLabel("Output root"))
        from deepreefmap_gui.core.icons import arrow_right_icon

        root_open_btn = QPushButton()
        root_open_btn.setIcon(arrow_right_icon(18))
        root_open_btn.setFixedSize(26, 24)
        root_open_btn.setToolTip("Open output root in file manager")
        root_open_btn.setAccessibleName("Open output root in file manager")
        root_open_btn.clicked.connect(self._open_output_root)
        label_row.addWidget(root_open_btn)
        label_row.addStretch(1)
        out_root_layout.addLayout(label_row)

        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(4)
        self._out_root_input = QLineEdit(default_root)
        input_row.addWidget(self._out_root_input, 1)
        root_browse_btn = QPushButton()
        root_browse_btn.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        root_browse_btn.setIconSize(QSize(18, 18))
        root_browse_btn.setFixedSize(28, 28)
        root_browse_btn.setToolTip("Browse for output root folder…")
        root_browse_btn.setAccessibleName("Browse for output root folder")
        root_browse_btn.clicked.connect(self._browse_output_root)
        input_row.addWidget(root_browse_btn)
        root_default_btn = QPushButton()
        root_default_btn.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DialogResetButton))
        root_default_btn.setIconSize(QSize(18, 18))
        root_default_btn.setFixedSize(28, 28)
        root_default_btn.setToolTip("Reset to <Documents>/DeepReefMap")
        root_default_btn.setAccessibleName("Reset the output root to its default")
        root_default_btn.clicked.connect(self._reset_output_root_to_default)
        input_row.addWidget(root_default_btn)
        out_root_layout.addLayout(input_row)
        # An empty holder rather than the group itself, so the controls can be
        # lent to This machine and handed back to the same place. The advanced
        # form and the machine page describe one folder, so they share one set
        # of widgets rather than each carrying their own.
        self._out_root_home = QWidget()
        out_root_home_layout = QVBoxLayout(self._out_root_home)
        out_root_home_layout.setContentsMargins(0, 0, 0, 0)
        out_root_home_layout.addWidget(self._out_root_widget)
        og.addWidget(self._out_root_home)

        setup_layout.addWidget(output_group)

    def _build_advanced_toggle_and_notices(self, setup_layout: QVBoxLayout) -> None:
        self._advanced_toggle = QCheckBox("Advanced settings")
        self._advanced_toggle.toggled.connect(self._on_advanced_toggled)
        setup_layout.addWidget(self._advanced_toggle)
        self._vram_notice = QLabel()
        self._vram_notice.setWordWrap(True)
        self._vram_notice.setStyleSheet(f"color: {UPDATE}; font-size: {FONT_SM}; margin: 2px 0 4px 0;")
        self._vram_notice.setVisible(False)
        setup_layout.addWidget(self._vram_notice)

        # System-RAM grade, shown inline like the VRAM notice above, and linked
        # through to the readiness rows that repeat it in plainer words. Anything
        # that changes the projected frame count re-grades the run.
        self._memory_notice = QLabel()
        self._memory_notice.setWordWrap(True)
        self._memory_notice.setStyleSheet(f"color: {UPDATE}; font-size: {FONT_SM}; margin: 2px 0 4px 0;")
        self._memory_notice.setVisible(False)
        self._memory_notice.linkActivated.connect(lambda _: self._reveal_memory_detail())
        setup_layout.addWidget(self._memory_notice)
        self._fps_spin.valueChanged.connect(self._update_memory_profile_warning)

    def _build_advanced_panel(self, setup_layout: QVBoxLayout) -> None:
        self._advanced_panel = QWidget()
        adv_layout = QVBoxLayout(self._advanced_panel)
        adv_layout.setContentsMargins(12, 0, 0, 0)
        self._build_advanced_transect_crop(adv_layout)
        self._build_advanced_resolution(adv_layout)
        self._build_advanced_batch_and_radius(adv_layout)
        self._build_scs_panel(adv_layout)
        self._build_loger_panel(adv_layout)
        self._advanced_panel.setVisible(False)
        setup_layout.addWidget(self._advanced_panel)

    def _build_advanced_transect_crop(self, adv_layout: QVBoxLayout) -> None:
        # Width only. A pass takes its transect length from the transect it was
        # assigned to on the Plan step, so there is nothing here to set it with.
        adv_layout.addWidget(QLabel("Crop width (m), 0 disables"))
        self._crop_width = QDoubleSpinBox()
        self._crop_width.setRange(0.0, 50.0)
        self._crop_width.setDecimals(2)
        self._crop_width.setSingleStep(0.1)
        self._crop_width.setValue(0.0)
        self._crop_width.setSuffix(" m")
        adv_layout.addWidget(self._crop_width)

    def _build_advanced_resolution(self, adv_layout: QVBoxLayout) -> None:
        from deepreefmap.segmentation.registry import model_processing_size

        default_seg = self._seg_combo.currentText()
        self._native_resolution = model_processing_size(default_seg) or (1376, 768)
        self._is_dpt_model = "dpt" in default_seg

        adv_layout.addWidget(QLabel("Processing resolution"))
        self._resolution_preset_combo = QComboBox()
        self._resolution_preset_combo.addItems(["Native", "Half", "Quarter", "Custom"])
        self._resolution_preset_combo.setToolTip(
            "Resolution preset relative to the segmentation model's training resolution."
        )
        adv_layout.addWidget(self._resolution_preset_combo)

        res_row = QHBoxLayout()
        res_row.setContentsMargins(0, 0, 0, 0)
        res_row.setSpacing(4)
        w_col = QVBoxLayout()
        w_col.setContentsMargins(0, 0, 0, 0)
        w_col.addWidget(QLabel("Width"))
        self._proc_width_spin = QSpinBox()
        self._proc_width_spin.setRange(256, 3840)
        self._proc_width_spin.setSingleStep(32)
        self._proc_width_spin.setValue(self._native_resolution[0])
        self._proc_width_spin.setEnabled(False)
        w_col.addWidget(self._proc_width_spin)
        res_row.addLayout(w_col, 1)
        h_col = QVBoxLayout()
        h_col.setContentsMargins(0, 0, 0, 0)
        h_col.addWidget(QLabel("Height"))
        self._proc_height_spin = QSpinBox()
        self._proc_height_spin.setRange(256, 2160)
        self._proc_height_spin.setSingleStep(32)
        self._proc_height_spin.setValue(self._native_resolution[1])
        self._proc_height_spin.setEnabled(False)
        h_col.addWidget(self._proc_height_spin)
        res_row.addLayout(h_col, 1)
        adv_layout.addLayout(res_row)

        self._dpt_resolution_warning = QLabel(
            "DPT models have no internal resize, so non-native resolution may reduce accuracy."
        )
        self._dpt_resolution_warning.setWordWrap(True)
        self._dpt_resolution_warning.setStyleSheet(f"color: {UPDATE}; font-size: {FONT_SM};")
        self._dpt_resolution_warning.setVisible(False)
        adv_layout.addWidget(self._dpt_resolution_warning)

        self._resolution_preset_combo.currentTextChanged.connect(
            self._on_resolution_preset_changed
        )
        self._proc_width_spin.valueChanged.connect(self._update_vram_warning)
        self._proc_height_spin.valueChanged.connect(self._update_vram_warning)

    def _build_advanced_batch_and_radius(self, adv_layout: QVBoxLayout) -> None:
        adv_layout.addWidget(QLabel("Segmentation batch size"))
        self._batch_size_spin = QSpinBox()
        self._batch_size_spin.setRange(1, 16)
        self._batch_size_spin.setValue(4)
        self._batch_size_spin.setToolTip(
            "Frames segmented per GPU batch. Lower values use less VRAM."
        )
        self._batch_size_spin.valueChanged.connect(self._update_vram_warning)
        adv_layout.addWidget(self._batch_size_spin)
        self._vram_auto_label = QLabel()
        self._vram_auto_label.setWordWrap(True)
        self._vram_auto_label.setStyleSheet(f"color: {UPDATE}; font-size: {FONT_SM};")
        adv_layout.addWidget(self._vram_auto_label)
        self._reset_defaults_btn = QPushButton("Reset to defaults")
        self._reset_defaults_btn.clicked.connect(self._reset_advanced_defaults)
        adv_layout.addWidget(self._reset_defaults_btn)
        self._update_vram_warning()
        adv_layout.addWidget(QLabel("Grid bins (ortho resolution)"))
        self._grid_bins_spin = QSpinBox()
        self._grid_bins_spin.setRange(100, 10000)
        self._grid_bins_spin.setSingleStep(100)
        self._grid_bins_spin.setValue(2000)
        self._grid_bins_spin.setToolTip("Number of bins for the ortho projection grid.")
        adv_layout.addWidget(self._grid_bins_spin)
        adv_layout.addWidget(QLabel("Replacement radius factor, 0 = auto"))
        self._rr_factor_spin = QDoubleSpinBox()
        self._rr_factor_spin.setRange(0.0, 10.0)
        self._rr_factor_spin.setDecimals(2)
        self._rr_factor_spin.setSingleStep(0.1)
        self._rr_factor_spin.setValue(0.0)
        self._rr_factor_spin.setToolTip("Multiplier on the auto replacement radius. 0 = use auto estimate.")
        adv_layout.addWidget(self._rr_factor_spin)
        adv_layout.addWidget(QLabel("Replacement radius estimation frames"))
        self._rr_est_frames_spin = QSpinBox()
        self._rr_est_frames_spin.setRange(1, 200)
        self._rr_est_frames_spin.setValue(30)
        self._rr_est_frames_spin.setToolTip(
            "Number of leading depth maps used to estimate the default replacement radius."
        )
        adv_layout.addWidget(self._rr_est_frames_spin)
        adv_layout.addWidget(QLabel("Replacement radius override (m), 0 = auto"))
        self._rr_override_spin = QDoubleSpinBox()
        self._rr_override_spin.setRange(0.0, 10.0)
        self._rr_override_spin.setDecimals(4)
        self._rr_override_spin.setSingleStep(0.001)
        self._rr_override_spin.setValue(0.0)
        self._rr_override_spin.setToolTip("Absolute replacement voxel size in meters. 0 = use auto estimate.")
        adv_layout.addWidget(self._rr_override_spin)
        self._tsdf_check = QCheckBox("Enable TSDF")
        adv_layout.addWidget(self._tsdf_check)
        self._require_gravity_check = QCheckBox("Require gravity telemetry")
        self._require_gravity_check.setToolTip("Fail if GoPro gravity telemetry cannot be loaded.")
        adv_layout.addWidget(self._require_gravity_check)
        self._skip_seg_check = QCheckBox("Skip segmentation")
        adv_layout.addWidget(self._skip_seg_check)

    def _build_scs_panel(self, adv_layout: QVBoxLayout) -> None:
        # SCSfMLearner-specific knobs, only shown when scsfmlearner is selected.
        style = self.style()
        self._scs_panel = QWidget()
        scs_layout = QVBoxLayout(self._scs_panel)
        scs_layout.setContentsMargins(0, 6, 0, 0)
        scs_layout.setSpacing(2)
        scs_layout.addWidget(QLabel("SCSfMLearner width"))
        self._scs_width_spin = QSpinBox()
        self._scs_width_spin.setRange(64, 2048)
        self._scs_width_spin.setSingleStep(32)
        self._scs_width_spin.setValue(512)
        scs_layout.addWidget(self._scs_width_spin)
        scs_layout.addWidget(QLabel("SCSfMLearner height"))
        self._scs_height_spin = QSpinBox()
        self._scs_height_spin.setRange(64, 2048)
        self._scs_height_spin.setSingleStep(32)
        self._scs_height_spin.setValue(256)
        scs_layout.addWidget(self._scs_height_spin)
        scs_layout.addWidget(QLabel("SCSfMLearner checkpoint (optional override)"))
        scs_ckpt_row = QHBoxLayout()
        scs_ckpt_row.setContentsMargins(0, 0, 0, 0)
        scs_ckpt_row.setSpacing(4)
        self._scs_checkpoint_input = QLineEdit()
        self._scs_checkpoint_input.setPlaceholderText("Default: EPFL-ECEO/deepreefmap-sfm-net")
        scs_ckpt_row.addWidget(self._scs_checkpoint_input, 1)
        scs_ckpt_btn = QPushButton()
        scs_ckpt_btn.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        scs_ckpt_btn.setIconSize(QSize(18, 18))
        scs_ckpt_btn.setFixedSize(28, 28)
        scs_ckpt_btn.setToolTip("Browse for a SCSfMLearner .pt checkpoint…")
        scs_ckpt_btn.setAccessibleName("Browse for a SCSfMLearner checkpoint")
        scs_ckpt_btn.clicked.connect(self._browse_scs_checkpoint)
        scs_ckpt_row.addWidget(scs_ckpt_btn)
        scs_layout.addLayout(scs_ckpt_row)
        self._scs_panel.setVisible(False)
        adv_layout.addWidget(self._scs_panel)

    def _build_loger_panel(self, adv_layout: QVBoxLayout) -> None:
        # LoGeR-specific knobs, only shown when a LoGeR backend is selected.
        style = self.style()
        self._loger_panel = QWidget()
        loger_layout = QVBoxLayout(self._loger_panel)
        loger_layout.setContentsMargins(0, 6, 0, 0)
        loger_layout.setSpacing(2)
        loger_layout.addWidget(QLabel("LoGeR window size"))
        self._loger_window_spin = QSpinBox()
        self._loger_window_spin.setRange(1, 256)
        self._loger_window_spin.setValue(32)
        loger_layout.addWidget(self._loger_window_spin)
        loger_layout.addWidget(QLabel("LoGeR overlap size"))
        self._loger_overlap_spin = QSpinBox()
        self._loger_overlap_spin.setRange(0, 64)
        self._loger_overlap_spin.setValue(3)
        loger_layout.addWidget(self._loger_overlap_spin)
        loger_layout.addWidget(QLabel("LoGeR checkpoint (optional override)"))
        loger_ckpt_row = QHBoxLayout()
        loger_ckpt_row.setContentsMargins(0, 0, 0, 0)
        loger_ckpt_row.setSpacing(4)
        self._loger_model_path_input = QLineEdit()
        self._loger_model_path_input.setPlaceholderText("Default: vendored checkpoint")
        loger_ckpt_row.addWidget(self._loger_model_path_input, 1)
        loger_ckpt_btn = QPushButton()
        loger_ckpt_btn.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        loger_ckpt_btn.setIconSize(QSize(18, 18))
        loger_ckpt_btn.setFixedSize(28, 28)
        loger_ckpt_btn.setToolTip("Browse for a LoGeR .pt checkpoint…")
        loger_ckpt_btn.setAccessibleName("Browse for a LoGeR checkpoint")
        loger_ckpt_btn.clicked.connect(self._browse_loger_checkpoint)
        loger_ckpt_row.addWidget(loger_ckpt_btn)
        loger_layout.addLayout(loger_ckpt_row)
        self._refine_intrinsics_check = QCheckBox("Refine intrinsics from mapper")
        loger_layout.addWidget(self._refine_intrinsics_check)
        self._loger_panel.setVisible(False)
        adv_layout.addWidget(self._loger_panel)

    def _build_gated_warning(self, setup_layout: QVBoxLayout) -> None:
        self._gated_warning = QLabel()
        self._gated_warning.setWordWrap(True)
        self._gated_warning.setTextFormat(Qt.TextFormat.RichText)
        self._gated_warning.setOpenExternalLinks(True)
        self._gated_warning.setStyleSheet(warning_banner_qss() + f" font-size: {FONT_SM};")
        self._gated_warning.setVisible(False)
        setup_layout.addWidget(self._gated_warning)

    def _build_run_warnings_and_log(self, viewer_layout: QVBoxLayout) -> None:
        # Sticky banner for non-fatal quality warnings emitted during a run
        # (preprocess detected mostly-background frames, missing transect line,
        # etc.). Cleared at the start of each new run. Lives on the Results
        # tab so it surfaces alongside the results it warns about, plus is
        # mirrored on the running page so the user sees them as they happen.
        self._warnings_label = QLabel("")
        self._warnings_label.setWordWrap(True)
        self._warnings_label.setTextFormat(Qt.TextFormat.RichText)
        self._warnings_label.setStyleSheet(
            warning_banner_qss()
        )
        self._warnings_label.setVisible(False)
        self._run_warnings: list[str] = []
        viewer_layout.addWidget(self._warnings_label)

        # Live log view: lives in a bottom panel built by _build_log_panel and
        # assembled into the main window's vertical splitter in app.py.
        # Construction is here so the panel is ready to receive log lines as
        # soon as the qt log handler installs (next block).
        self._log_view = LogView()

        # The log handler streams every deepreefmap.* log line into the panel.
        # A per-run FileHandler, when one is open, is closed on the run's
        # completion paths and again on window teardown.
        self._qt_log_handler = install_qt_log_handler()
        self._qt_log_handler.line_signal.connect(self._log_view.append_line)

    def _build_progress_widgets(self) -> None:
        # Status label and progress bar are owned by the top toolbar but
        # constructed here so they exist before anything reports into them.
        self._status_label = QLabel("Ready.")
        self._status_label.setWordWrap(True)

        # Stage bar (top) + total bar (bottom) stacked in one compact hover column
        # so the two percentages read as a unit at half the width. Bar text is
        # hidden to avoid cramped labels: the numbers live in the status text, the
        # overall-estimate label, and the hover breakdown. Empty when idle.
        _STAGE_CHUNK = PRIMARY
        _TOTAL_CHUNK = PRIMARY_DARK  # darker PRIMARY, unique to the stacked total bar
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setFixedHeight(BAR_HEIGHT)
        self._progress_bar.setStyleSheet(bar_qss(_STAGE_CHUNK))

        self._total_progress_bar = QProgressBar()
        self._total_progress_bar.setRange(0, 100)
        self._total_progress_bar.setValue(0)
        self._total_progress_bar.setTextVisible(False)
        self._total_progress_bar.setFixedHeight(BAR_HEIGHT)
        self._total_progress_bar.setStyleSheet(bar_qss(_TOTAL_CHUNK))

        self._progress_stack = HoverColumn()
        self._progress_stack.setFixedWidth(150)
        _stack_layout = QVBoxLayout(self._progress_stack)
        _stack_layout.setContentsMargins(0, 0, 0, 0)
        _stack_layout.setSpacing(2)
        _stack_layout.addWidget(self._progress_bar)
        _stack_layout.addWidget(self._total_progress_bar)
        # Bars pass hover through to the column so the breakdown follows the mouse.
        self._progress_bar.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._total_progress_bar.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        # Overall (all-stages) remaining estimate, kept visible rather than buried
        # in the hover breakdown.
        self._eta_total_label = QLabel("")
        self._eta_total_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self._eta_total_label.setMinimumWidth(78)

        self._recon_model = ProgressModel(_RECON_PHASES)
        self._load_model = ProgressModel(_LOAD_PHASES)
        self._active_progress_model: ProgressModel | None = None

    def _build_run_control_buttons(self) -> None:
        # Transport controls live at the bottom-right of the window, next to the
        # progress bar they drive. They belong with the progress bar they
        # interrupt, and only exist while a run is in flight. Starting one is not
        # here: the Run step's own button launches the batch.
        from deepreefmap_gui.core.icons import pause_icon

        self._pause_btn = QPushButton()
        self._pause_btn.setProperty("pad", "none")
        self._pause_btn.setIcon(pause_icon(_TRANSPORT_ICON))
        self._pause_btn.setToolTip(
            "Pause at the next safe checkpoint. Long mapping passes may take "
            "time to respond."
        )
        self._pause_btn.setAccessibleName("Pause the reconstruction")
        self._pause_btn.setCheckable(True)
        self._pause_btn.setFixedSize(_TRANSPORT_SIZE, _TRANSPORT_SIZE)
        self._pause_btn.setVisible(False)
        self._pause_btn.toggled.connect(self._on_pause_toggled)

        self._spinner_stop = SpinnerStopButton(size=_TRANSPORT_SIZE)
        self._spinner_stop.setVisible(False)
        self._spinner_stop.clicked.connect(self._on_stop_clicked)

    def _build_results_group(self, viewer_layout: QVBoxLayout) -> None:
        # The legend lives as a floating overlay on the 3D canvas; this dict
        # is populated by _build_legend and queried by _enabled_class_set.
        self._legend_toggles: dict[int, QCheckBox] = {}
        self._legend_solo_buttons: dict[int, QToolButton] = {}
        # Legend sort state: column "selected" (visibility), "name", or "size",
        # plus a direction. _legend_order_cache lets _apply_legend_sort skip
        # reflows when the resulting order is unchanged.
        self._legend_sort_mode: str = "selected"
        self._legend_sort_ascending: bool = False
        self._legend_order_cache: list[int] | None = None
        self._legend_sort_connected: bool = False

        self._results_group = QGroupBox("Results")
        self._results_group.setVisible(False)
        res_layout = QVBoxLayout(self._results_group)

        ortho_row = QHBoxLayout()
        ortho_row.setSpacing(4)
        self._ortho_rgb_preview = QLabel("RGB ortho")
        self._ortho_rgb_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._ortho_rgb_preview.setMinimumSize(160, 100)
        self._ortho_rgb_preview.setStyleSheet(f"background-color: {PREVIEW_BG}; color: {TEXT_MUTED};")
        ortho_row.addWidget(self._ortho_rgb_preview, 1)
        self._ortho_seg_preview = QLabel("Seg ortho")
        self._ortho_seg_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._ortho_seg_preview.setMinimumSize(160, 100)
        self._ortho_seg_preview.setStyleSheet(f"background-color: {PREVIEW_BG}; color: {TEXT_MUTED};")
        ortho_row.addWidget(self._ortho_seg_preview, 1)
        res_layout.addLayout(ortho_row)

        crop_box = QGroupBox("Transect crop (live)")
        crop_box.setVisible(False)
        crop_layout = QGridLayout(crop_box)
        crop_layout.addWidget(QLabel("Transect length (m)"), 0, 0)
        self._results_transect_length = QDoubleSpinBox()
        self._results_transect_length.setRange(0.0, 100.0)
        self._results_transect_length.setDecimals(2)
        self._results_transect_length.setSingleStep(0.1)
        self._results_transect_length.setValue(0.0)
        crop_layout.addWidget(self._results_transect_length, 0, 1)
        self._results_transect_slider = QSlider(Qt.Orientation.Horizontal)
        self._results_transect_slider.setRange(0, 10000)
        crop_layout.addWidget(self._results_transect_slider, 1, 0, 1, 2)
        crop_layout.addWidget(QLabel("Crop width (m)"), 2, 0)
        self._results_crop_width = QDoubleSpinBox()
        self._results_crop_width.setRange(0.0, 50.0)
        self._results_crop_width.setDecimals(2)
        self._results_crop_width.setSingleStep(0.1)
        self._results_crop_width.setValue(0.0)
        crop_layout.addWidget(self._results_crop_width, 2, 1)
        self._results_crop_slider = QSlider(Qt.Orientation.Horizontal)
        self._results_crop_slider.setRange(0, 5000)
        crop_layout.addWidget(self._results_crop_slider, 3, 0, 1, 2)
        self._crop_box = crop_box
        res_layout.addWidget(crop_box)

        self._results_transect_length.valueChanged.connect(self._on_results_transect_length_changed)
        self._results_crop_width.valueChanged.connect(self._on_results_crop_width_changed)
        self._results_transect_slider.valueChanged.connect(self._on_results_transect_slider_changed)
        self._results_crop_slider.valueChanged.connect(self._on_results_crop_slider_changed)

        # Two-ring sunburst (outer = fine classes, inner = coarse groups) docks
        # above the legend rows in the canvas overlay so the user can show/hide
        # classes from either the pie or the rows. Updates live with the crop.
        self._cover_sunburst = SunburstWidget()
        self._cover_sunburst.selection_clicked.connect(self._on_sunburst_selection)
        self._cover_sunburst.setVisible(False)
        self._viewer.legend_overlay.set_sunburst(self._cover_sunburst)

        self._cover_label = QLabel()
        self._cover_label.setWordWrap(True)
        res_layout.addWidget(self._cover_label)

        self._open_dir_btn = QPushButton("Open output directory")
        self._open_dir_btn.clicked.connect(self._open_output_dir)
        res_layout.addWidget(self._open_dir_btn)

        exports_grid = QGridLayout()
        exports_grid.setHorizontalSpacing(6)
        exports_grid.setVerticalSpacing(4)
        self._export_ortho_npz_btn = QPushButton("Save ortho (NPZ)")
        self._export_ortho_npz_btn.clicked.connect(self._on_export_ortho_npz)
        exports_grid.addWidget(self._export_ortho_npz_btn, 0, 0)
        self._export_ortho_png_btn = QPushButton("Save ortho preview (PNG)")
        self._export_ortho_png_btn.clicked.connect(self._on_export_ortho_png)
        exports_grid.addWidget(self._export_ortho_png_btn, 0, 1)
        self._export_cover_btn = QPushButton("Save benthic cover (CSV)")
        self._export_cover_btn.clicked.connect(self._on_export_cover_csv)
        exports_grid.addWidget(self._export_cover_btn, 1, 0)
        self._export_zip_btn = QPushButton("Zip output directory")
        self._export_zip_btn.clicked.connect(self._on_export_zip)
        exports_grid.addWidget(self._export_zip_btn, 1, 1)
        self._export_qc_video_btn = QPushButton("Render QC video (MP4)")
        self._export_qc_video_btn.clicked.connect(self._on_export_qc_video)
        exports_grid.addWidget(self._export_qc_video_btn, 2, 0)
        self._export_frame_btn = QPushButton("Save current frame (PNG)")
        self._export_frame_btn.clicked.connect(self._on_export_current_frame)
        exports_grid.addWidget(self._export_frame_btn, 2, 1)
        res_layout.addLayout(exports_grid)
        viewer_layout.addWidget(self._results_group)

        # The group above is hidden until a run loads, which left this tab a
        # blank rectangle -- the one panel in the app that said nothing at all
        # about why it was empty.
        self._results_empty = EmptyState(
            "No run loaded",
            "Start a run, or open a finished one from Browse, to see its ortho, "
            "benthic cover and exports here.",
        )
        viewer_layout.addWidget(self._results_empty, 1)
        viewer_layout.addStretch()

    def _build_models_panel(self, models_layout: QVBoxLayout) -> None:
        # Untitled: This machine's view switch already says Models above it,
        # and a frame whose caption repeats the control that reached it says
        # nothing twice.
        models_group = QGroupBox()
        self._models_layout = QVBoxLayout(models_group)

        auth_row = QHBoxLayout()
        self._hf_auth_label = QLabel("Checking Hugging Face login...")
        self._hf_auth_label.setWordWrap(True)
        auth_row.addWidget(self._hf_auth_label, 1)

        self._hf_auth_icon = QLabel()
        self._hf_auth_icon.setFixedWidth(14)
        self._hf_auth_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        auth_row.addWidget(self._hf_auth_icon)
        auth_row.addSpacing(6)

        self._hf_auth_btn = QPushButton("Log in...")
        self._hf_auth_btn.setFixedWidth(110)
        self._hf_auth_btn.clicked.connect(self._on_hf_auth_button)
        auth_row.addWidget(self._hf_auth_btn)
        self._models_layout.addLayout(auth_row)

        self._models_layout.addWidget(_separator())

        # Model library: reveal the cache folder, and export/import portable model
        # packs so offline field laptops can be provisioned from a USB stick without
        # re-downloading. Logic lives in models/library.py + models/library_ui.py.
        lib_row = QHBoxLayout()
        lib_row.setContentsMargins(0, 0, 0, 0)
        lib_row.setSpacing(6)
        open_lib_btn = QPushButton("Open model folder")
        open_lib_btn.setToolTip("Open the folder where downloaded models are stored")
        open_lib_btn.clicked.connect(self._open_model_library)
        lib_row.addWidget(open_lib_btn)
        self._export_models_btn = QPushButton("Export…")
        self._export_models_btn.setToolTip(
            "Copy downloaded models to a folder or USB drive as a portable pack"
        )
        self._export_models_btn.clicked.connect(self._on_export_models)
        lib_row.addWidget(self._export_models_btn)
        self._import_pack_btn = QPushButton("Import…")
        self._import_pack_btn.setToolTip(
            "Install models from a pack folder or USB drive (no internet needed)"
        )
        self._import_pack_btn.clicked.connect(self._on_import_model_pack)
        lib_row.addWidget(self._import_pack_btn)
        self._models_layout.addLayout(lib_row)

        self._models_layout.addWidget(_separator())

        self._models_grid_host = QWidget()
        self._models_grid = QGridLayout(self._models_grid_host)
        self._models_grid.setContentsMargins(0, 4, 0, 0)
        self._models_grid.setHorizontalSpacing(10)
        self._models_grid.setVerticalSpacing(4)
        self._models_grid.setColumnStretch(0, 1)
        self._models_grid.setColumnStretch(1, 0)
        self._models_layout.addWidget(self._models_grid_host)

        self._discover_btn = QPushButton("Check Hugging Face for new models")
        self._discover_btn.setToolTip(
            "Query the EPFL-ECEO organisation on Hugging Face for newly published "
            "models. Requires an internet connection."
        )
        self._discover_btn.clicked.connect(self._on_discover_clicked)
        self._models_layout.addWidget(self._discover_btn)

        self._hf_auth_user: str | None = None
        self._model_rows: dict[str, QWidget] = {}
        self._model_actions: dict[str, QWidget] = {}
        self._downloading: set[str] = set()
        self._download_cancel_requested: set[str] = set()
        self._delete_armed: dict[str, QPushButton] = {}
        self._last_model_states: list = []
        self._download_errors: dict[str, str] = {}
        self._pack_progress_dialog = None

        self._seg_combo.currentTextChanged.connect(self._on_required_models_changed)
        self._seg_combo.currentTextChanged.connect(self._on_seg_model_changed)
        self._map_combo.currentTextChanged.connect(self._on_required_models_changed)
        self._map_combo.currentTextChanged.connect(self._on_mapping_backend_changed)
        self._skip_seg_check.toggled.connect(self._on_required_models_changed)
        self._out_root_input.textChanged.connect(self._on_output_root_changed)

        # Editing the output root is debounced because the expensive half of
        # reacting to it walks the tree and opens a SurveyStore, and SurveyStore
        # creates its database under whatever path it is handed. Run per
        # keystroke, that leaves a survey.db under every prefix of what the user
        # typed on the way to the path they meant.
        self._out_root_commit_timer = QTimer(self)
        self._out_root_commit_timer.setSingleShot(True)
        self._out_root_commit_timer.setInterval(300)
        self._out_root_commit_timer.timeout.connect(self._commit_output_root)

        # Watch the output root directory so Browse reflects new manifests
        # appearing on disk (e.g. a sibling process completes a run) in addition
        # to user edits of the path text.
        self._out_root_watcher = QFileSystemWatcher(self)
        self._out_root_watcher.directoryChanged.connect(self._on_out_root_dir_changed)

        self._active_run_dir: Path | None = None
        self._active_run_manifest: dict | None = None
        self._load_cancelled = False

        self._base_ortho_grid = None
        self._ortho_cloud = None
        self._ortho_classes_config = None
        self._current_ortho_grid = None
        self._results_output_dir: Path | None = None
        self._ortho_crop_refresh_pending = False

        self._settings = QSettings("ECEO", "deepreefmap")
        saved_root = cast(str, self._settings.value("output_root_dir", "", type=str))
        if saved_root:
            self._out_root_input.setText(saved_root)
        self._refresh_data_manager()
        self._update_out_root_watch()

        # The inline status icons next to the seg/mapping dropdowns surface
        # state without forcing the user to leave the form for common cases.
        #
        # The group is the only child of the tab, which is what lets
        # _host_machine_panels lend it to This machine and take it back: the tab
        # top-aligns its contents already, so there is no trailing stretch for a
        # returning widget to land underneath.
        self._models_group = models_group
        self._models_page = models_group
        models_layout.addWidget(models_group)
        threading.Thread(target=self._refresh_model_status, daemon=True).start()

    def _build_updates_section(self, updates_layout: QVBoxLayout) -> None:
        self._update_version_label = QLabel(f"Version: <b>{current_version()}</b>")
        self._update_version_label.setWordWrap(True)
        updates_layout.addWidget(self._update_version_label)
        self._update_status_label = QLabel("Checking for updates…")
        self._update_status_label.setWordWrap(True)
        self._update_status_label.setStyleSheet(f"color: {TEXT_MUTED};")
        updates_layout.addWidget(self._update_status_label)
        update_row = QHBoxLayout()
        self._update_version_combo = QComboBox()
        self._update_version_combo.setVisible(False)
        update_row.addWidget(self._update_version_combo, 1)
        self._update_btn = QPushButton("Install")
        self._update_btn.setVisible(False)
        self._update_btn.clicked.connect(self._on_update)
        update_row.addWidget(self._update_btn)
        updates_layout.addLayout(update_row)
        self._update_show_all = QCheckBox("Show older versions (rollback)")
        self._update_show_all.setVisible(False)
        self._update_show_all.toggled.connect(self._on_toggle_show_all_versions)
        updates_layout.addWidget(self._update_show_all)
        self._available_releases: list[dict] = []
        self._current_version_str = current_version()

        # Linux menu integration. Windows/macOS get shortcuts from their
        # installers; on Linux the bare binary registers itself on demand. The
        # entry points at the current binary path, which the in-app updater
        # swaps in place, so it survives updates and rollbacks.
        from deepreefmap_gui.packaging.desktop_entry import desktop_entry_supported

        self._desktop_entry_btn = QPushButton()
        self._desktop_entry_btn.clicked.connect(self._on_toggle_desktop_entry)
        # Parented before shown: setVisible on a parentless widget maps it as a
        # top-level window, which flashes an empty titlebar box on screen.
        updates_layout.addWidget(self._desktop_entry_btn)
        self._desktop_entry_btn.setVisible(
            desktop_entry_supported() and pyapp_binary_path() is not None
        )
        self._refresh_desktop_entry_button()

        threading.Thread(target=self._check_for_update, daemon=True).start()

        updates_layout.addStretch()

    def _build_top_bar(self) -> QWidget:
        bar = QWidget()
        # The hairline on the inner edge is what makes the darker work area
        # between the two bars read as recessed rather than as more chrome.
        bar.setStyleSheet(
            f"QWidget {{ background-color: {CARD_BG}; }}"
            f" QWidget#topBar {{ border-bottom: 1px solid {BORDER}; }}"
        )
        bar.setObjectName("topBar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(PAGE_MARGIN, 6, PAGE_MARGIN, 6)
        h.setSpacing(GUTTER)
        h.addStretch(1)

        # The stage/total bars, and nothing else. Everything that used to share
        # this band now lives where it belongs: the log toggle and This machine
        # in the workspace header, the memory advisory on This machine's button,
        # and the "+" nowhere, because a batch of passes has no single loaded run
        # to clear. A whole band of chrome for one button was not worth its
        # height, so the bar goes with them and comes back only while a run is
        # in flight, which _set_progress_widgets_visible drives.
        self._progress_stack.setVisible(False)
        h.addWidget(self._progress_stack)
        bar.setVisible(False)
        self._top_bar = bar
        return bar

    def _build_bottom_bar(self) -> QWidget:
        """Full-width status and progress strip, the way a desktop app reports work.

        The progress bar spans the window above a row of status text, remaining
        estimate, and the transport controls at the right.
        """
        bar = QWidget()
        self._bottom_bar = bar
        bar.setObjectName("bottomBar")
        bar.setStyleSheet(
            f"QWidget {{ background-color: {CARD_BG}; }}"
            f" QWidget#bottomBar {{ border-top: 1px solid {BORDER}; }}"
        )
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._bottom_progress_bar = QProgressBar()
        self._bottom_progress_bar.setRange(0, 100)
        self._bottom_progress_bar.setValue(0)
        self._bottom_progress_bar.setTextVisible(False)
        self._bottom_progress_bar.setFixedHeight(BAR_HEIGHT)
        self._bottom_progress_bar.setStyleSheet(bar_qss(PRIMARY))
        self._bottom_progress_bar.setVisible(False)
        outer.addWidget(self._bottom_progress_bar)

        row = QHBoxLayout()
        row.setContentsMargins(PAGE_MARGIN, 4, PAGE_MARGIN, 4)
        row.setSpacing(GUTTER)
        self._status_label.setStyleSheet(f"color: {TEXT_SECONDARY};")
        row.addWidget(self._status_label, 1)
        self._eta_total_label.setVisible(False)
        row.addWidget(self._eta_total_label)
        # No prospective command here. Runs are launched as a batch of passes
        # off the survey plan, so a command naming one video and one output
        # directory describes a run nobody is about to start. Browse keeps the
        # retrospective one, built from each run's own manifest.
        row.addWidget(self._pause_btn)
        row.addWidget(self._spinner_stop)
        outer.addLayout(row)
        return bar

    def _build_log_panel(self) -> QWidget:
        """Bottom-of-window panel hosting the live log."""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 4, 8, 6)
        layout.setSpacing(4)
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.addWidget(QLabel("<b>Live log</b>"))
        header_row.addStretch(1)
        close_btn = QPushButton("×")
        close_btn.setFixedSize(20, 20)
        close_btn.setToolTip("Hide log panel")
        close_btn.setProperty("pad", "none")
        close_btn.setStyleSheet(
            f"QPushButton {{ font-size: {FONT_XL}; font-weight: {WEIGHT_BOLD}; padding: 0;"
            f" border: 1px solid {BORDER}; border-radius: {RADIUS_SM}px;"
            f" background: {BUTTON}; }}"
            f" QPushButton:hover {{ background: {SURFACE_HI}; }}"
        )
        close_btn.clicked.connect(lambda: self._set_log_panel_visible(False))
        header_row.addWidget(close_btn)
        layout.addLayout(header_row)
        layout.addWidget(self._log_view, 1)
        panel.setVisible(False)
        self._log_panel = panel
        return panel

    def _set_log_panel_visible(self, visible: bool) -> None:
        if not hasattr(self, "_log_panel"):
            return
        self._log_panel.setVisible(visible)
        # Keep the top-bar toggle in sync without re-triggering this slot.
        if self._log_toggle_btn.isChecked() != visible:
            self._log_toggle_btn.blockSignals(True)
            self._log_toggle_btn.setChecked(visible)
            self._log_toggle_btn.blockSignals(False)
        # When the user re-opens the panel after collapsing the splitter all
        # the way down, give it a reasonable default height again.
        if visible and hasattr(self, "_central_vsplitter"):
            sizes = self._central_vsplitter.sizes()
            if len(sizes) == 2 and sizes[1] < 40:
                total = sum(sizes) or 800
                bottom = max(200, total // 4)
                self._central_vsplitter.setSizes([total - bottom, bottom])
    def _on_advanced_toggled(self, checked: bool) -> None:
        self._advanced_panel.setVisible(checked)

    def _on_mapping_backend_changed(self, _value: object = "") -> None:
        backend = self._map_combo.currentText()
        self._loger_panel.setVisible(backend in ("loger", "loger_star"))
        self._scs_panel.setVisible(backend == "scsfmlearner")

    def _collect_run_settings(self) -> dict:
        """Every run_reconstruction kwarg the form controls, minus the per-run
        inputs (video, output dir, run name, trim, transect length).

        Every pass in a survey batch builds its run from this, so a setting
        edited in the dialog reaches all of them or none.
        """
        settings: dict = {
            "fps": self._fps_spin.value(),
            "segmentation_name": self._seg_combo.currentText(),
            "mapping_name": self._map_combo.currentText(),
            "camera_profile_name": self._profile_combo.currentText(),
            "transect_crop_width": self._crop_width.value() or None,
            "enable_tsdf": self._tsdf_check.isChecked(),
            "skip_segmentation": self._skip_seg_check.isChecked(),
            "classes_path": self._classes_path,
            "processing_width": self._proc_width_spin.value(),
            "processing_height": self._proc_height_spin.value(),
            "preprocess_batch_size": self._batch_size_spin.value(),
            "grid_bins": self._grid_bins_spin.value(),
            "require_gravity_telemetry": self._require_gravity_check.isChecked(),
            "replacement_radius_factor": self._rr_factor_spin.value() or None,
            "replacement_radius_estimation_frames": self._rr_est_frames_spin.value(),
            "replacement_radius_override": self._rr_override_spin.value() or None,
        }
        mapping_name = str(settings["mapping_name"])
        loger_options = self._collect_loger_options(mapping_name)
        if loger_options is not None:
            settings["mapping_options"] = loger_options
            settings["refine_intrinsics_from_mapper"] = self._refine_intrinsics_check.isChecked()
        elif mapping_name == "scsfmlearner":
            scs_opts: dict[str, object] = {
                "target_width": self._scs_width_spin.value(),
                "target_height": self._scs_height_spin.value(),
            }
            scs_ckpt = self._scs_checkpoint_input.text().strip()
            if scs_ckpt:
                scs_opts["checkpoint_path"] = scs_ckpt
            settings["mapping_options"] = scs_opts
        return settings

    def _collect_loger_options(self, mapping_name: str) -> dict | None:
        """Build the LoGeR mapping_options dict from the form, or None for other backends."""
        if mapping_name not in ("loger", "loger_star"):
            return None
        model_path = self._loger_model_path_input.text().strip()
        return {
            "window_size": self._loger_window_spin.value(),
            "overlap_size": self._loger_overlap_spin.value(),
            "model_path": model_path or None,
        }

    def _browse_loger_checkpoint(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select LoGeR checkpoint", "", "Checkpoints (*.pt *.pth);;All files (*)"
        )
        if path:
            self._loger_model_path_input.setText(path)

    def _browse_scs_checkpoint(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select SCSfMLearner checkpoint", "", "Checkpoints (*.pt *.pth);;All files (*)"
        )
        if path:
            self._scs_checkpoint_input.setText(path)

    def _on_seg_model_changed(self, name: str) -> None:
        from deepreefmap.segmentation.registry import model_processing_size

        self._native_resolution = model_processing_size(name) or (1376, 768)
        self._is_dpt_model = "dpt" in name
        preset = self._resolution_preset_combo.currentText()
        if preset != "Custom":
            self._apply_resolution_preset(preset)
        self._update_dpt_warning()
        self._update_vram_warning()

    def _on_resolution_preset_changed(self, preset: str) -> None:
        is_custom = preset == "Custom"
        self._proc_width_spin.setEnabled(is_custom)
        self._proc_height_spin.setEnabled(is_custom)
        if not is_custom:
            self._apply_resolution_preset(preset)
        self._update_dpt_warning()
        self._update_vram_warning()

    def _apply_resolution_preset(self, preset: str) -> None:
        nw, nh = self._native_resolution
        divisors = {"Native": 1, "Half": 2, "Quarter": 4}
        d = divisors.get(preset, 1)
        self._proc_width_spin.blockSignals(True)
        self._proc_height_spin.blockSignals(True)
        self._proc_width_spin.setValue(nw // d)
        self._proc_height_spin.setValue(nh // d)
        self._proc_width_spin.blockSignals(False)
        self._proc_height_spin.blockSignals(False)

    def _update_dpt_warning(self) -> None:
        nw, nh = self._native_resolution
        current_w = self._proc_width_spin.value()
        current_h = self._proc_height_spin.value()
        show = self._is_dpt_model and (current_w != nw or current_h != nh)
        self._dpt_resolution_warning.setVisible(show)

    def _update_vram_warning(self) -> None:
        w = self._proc_width_spin.value()
        h = self._proc_height_spin.value()
        batch = self._batch_size_spin.value()
        try:
            from deepreefmap.device import estimate_segmentation_batch_size, resolve_device

            suggested = estimate_segmentation_batch_size(resolve_device(), w, h)
        except Exception:
            suggested = 4
        if batch > suggested:
            self._vram_auto_label.setText(
                f"Batch size {batch} may exceed available VRAM at {w}×{h}. "
                f"Suggested for your GPU: {suggested}."
            )
            self._vram_auto_label.setVisible(True)
            self._vram_notice.setText(
                "VRAM warning: check batch size in advanced settings."
            )
            self._vram_notice.setVisible(True)
        else:
            self._vram_auto_label.setVisible(False)
            self._vram_notice.setVisible(False)
        self._update_dpt_warning()
        self._update_memory_profile_warning()

    def _memory_grade_frames(self, fps: int) -> int | None:
        """Frames the memory grade is computed over.

        There is no single video to grade, so this grades the longest clip in
        the queued batch: that is the pass most likely to run the machine low,
        and every other pass fits under it.
        """
        return self._simple_peak_frames(fps)

    def _update_memory_profile_warning(self) -> None:
        """Grade the queued batch and report it, inline and on This machine."""
        # Advisory only: a warn or block grade never gates the run.
        notice = getattr(self, "_memory_notice", None)
        if notice is None:  # form not built yet
            return
        setup_label = getattr(self, "_setup_memory_label", None)

        def hide() -> None:
            notice.setVisible(False)
            if setup_label is not None:
                setup_label.setVisible(False)
            self._memory_advisory = ""
            self._refresh_machine_button()

        try:
            from deepreefmap_gui.profiling.memory_estimate import estimate_peak_bytes, preflight_check
            from deepreefmap_gui.profiling.run_history import history_key, load_expected_peaks
            from deepreefmap_gui.profiling.system_probe import format_bytes, probe_system

            fps = self._fps_spin.value()
            frames = self._memory_grade_frames(fps)
            if not frames:
                hide()
                return
            w, h = self._proc_width_spin.value(), self._proc_height_spin.value()
            mapping = self._map_combo.currentText()
            seg = self._seg_combo.currentText()
            est = estimate_peak_bytes(
                frames, w, h, mapping, seg,
                recorded=load_expected_peaks(history_key(mapping, seg, w, h, fps)),
            )
            profile = probe_system()
            verdict = preflight_check(profile, est)
        except Exception:
            hide()
            return
        if verdict.level == "ok":
            hide()
            return

        color = BLOCK if verdict.level == "block" else UPDATE
        headline = f"Crash risk: {verdict.risk}"
        cap_word = "RAM + swap" if profile.free_swap_bytes else "RAM"

        # Inline notice: concise, always-visible indicator in the form.
        notice.setStyleSheet(f"color: {color}; font-size: {FONT_SM}; margin: 2px 0 4px 0;")
        notice.setText(
            f"{headline}: ~{format_bytes(verdict.ram_need_bytes)} "
            f"({verdict.percent:.0f}% of {cap_word}). "
            f'<a href="#system" style="color:{color};">This machine</a>'
        )
        notice.setVisible(True)

        # Readiness view: the same warning without the numbers, for the diver
        # who never reads them. The header button reports it too, which is why
        # the sentence is kept rather than only painted: the button's verdict is
        # computed, not read back off a label.
        self._memory_advisory = (
            "This batch may exhaust memory on this machine. Processing will "
            "proceed; a pass may stop."
        )
        if setup_label is not None:
            setup_label.setStyleSheet(f"color: {color}; font-size: {FONT_SM};")
            setup_label.setText(self._memory_advisory)
            setup_label.setVisible(True)
        self._refresh_machine_button()

    def _update_gated_warning(self) -> None:
        seg_name = self._seg_combo.currentText()
        if self._skip_seg_check.isChecked():
            self._gated_warning.setVisible(False)
            return
        from deepreefmap_gui.models.manager import DPT_BACKBONE_MAP

        backbone_name = DPT_BACKBONE_MAP.get(seg_name)
        if not backbone_name:
            self._gated_warning.setVisible(False)
            return
        states = {info.name: (info, cached) for info, cached in self._last_model_states}
        missing_repos: list[str] = []
        for name in (seg_name, backbone_name):
            entry = states.get(name)
            if entry and not entry[1]:
                missing_repos.extend(entry[0].hf_repos)
        if not missing_repos:
            self._gated_warning.setVisible(False)
            return
        links = " ".join(
            f'<a href="https://huggingface.co/{repo}" style="color:{WARN_TEXT}">{repo}</a>'
            for repo in missing_repos
        )
        logged_in = self._hf_auth_user is not None
        can_gated = getattr(self, "_can_read_gated", True)
        if logged_in and not can_gated:
            msg = (
                f"<b>{seg_name}</b> needs gated repos, but your token does not have "
                f"permission to download them. Edit your token at "
                f'<a href="https://huggingface.co/settings/tokens" style="color:{WARN_TEXT}">'
                f"huggingface.co/settings/tokens</a> and enable "
                f"<i>Read access to contents of all public gated repos</i>."
            )
        elif logged_in:
            msg = (
                f"<b>{seg_name}</b> uses gated repos that must be downloaded first. "
                f"Accept the license on each repo page and download under "
                f"Models: {links}"
            )
        else:
            msg = (
                f"<b>{seg_name}</b> requires Hugging Face login. "
                f"Log in under Models, then accept each repo's license "
                f"and download: {links}"
            )
        self._gated_warning.setText(msg)
        self._gated_warning.setVisible(True)

    def _reset_advanced_defaults(self) -> None:
        self._resolution_preset_combo.setCurrentText("Native")
        self._batch_size_spin.setValue(4)
        self._grid_bins_spin.setValue(2000)
        self._rr_factor_spin.setValue(0.0)
        self._rr_est_frames_spin.setValue(30)
        self._rr_override_spin.setValue(0.0)

    def _gpu_available(self) -> bool:
        cached = getattr(self, "_gpu_available_cache", None)
        if cached is None:
            try:
                from deepreefmap.device import resolve_device

                cached = resolve_device().type != "cpu"
            except Exception:
                cached = False
            self._gpu_available_cache = cached
        return cached

    def _gpu_only_mapper(self) -> str:
        """The chosen mapping method when it needs a card this machine lacks.

        The Run step's gate and the setup step's readiness row both read this,
        so a CPU-only laptop cannot be told it is fine by one and blocked by
        the other.
        """
        from deepreefmap_gui.models.manager import GPU_ONLY_BACKENDS

        mapping = self._map_combo.currentText()
        if mapping in GPU_ONLY_BACKENDS and not self._gpu_available():
            return mapping
        return ""

    def _open_output_dir(self) -> None:
        d = getattr(self, "_results_output_dir", None) or self._viewer._output_dir
        if d and Path(d).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))


    def _browse_output_root(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select output root directory", self._out_root_input.text()
        )
        if path:
            self._out_root_input.setText(path)

    def _open_output_root(self) -> None:
        root = Path(self._out_root_input.text()).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))

    def _reset_output_root_to_default(self) -> None:
        documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        default = str(Path(documents or str(Path.home())) / "DeepReefMap")
        self._out_root_input.setText(default)

    @staticmethod
    def _sanitize_run_name(name: str) -> str:
        import re
        from datetime import datetime

        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
        return cleaned.strip("._-") or datetime.now().strftime("%Y%m%d-%H%M%S")  # noqa: DTZ005 (local time is intended: this is a user-facing default name)

    def _on_output_root_changed(self, _text: str = "") -> None:
        self._out_root_commit_timer.start()

    def _commit_output_root(self) -> None:
        """Persist the root and rescan it, once the path has stopped changing."""
        self._settings.setValue("output_root_dir", self._out_root_input.text())
        self._refresh_data_manager()
        self._update_out_root_watch()

    def _on_out_root_dir_changed(self, _path: str = "") -> None:
        # Manifests from other processes appear unprompted; the Data refresh is
        # debounced because runs touch the root continuously while writing.
        self._request_data_refresh()

    def _update_out_root_watch(self) -> None:
        """Re-point the watcher so manifests from other processes appear unprompted."""
        if not hasattr(self, "_out_root_watcher"):
            return
        existing = self._out_root_watcher.directories()
        if existing:
            self._out_root_watcher.removePaths(existing)
        root = Path(self._out_root_input.text()).expanduser()
        if root.exists() and root.is_dir():
            self._out_root_watcher.addPath(str(root))
