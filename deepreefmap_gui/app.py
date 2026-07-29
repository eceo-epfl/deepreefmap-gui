from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import cast

from deepreefmap.config.classes import ClassConfig
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QSurfaceFormat
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.theme import BANNER_BG, BANNER_BORDER, BANNER_TEXT
from deepreefmap_gui.form.batch import BatchMixin
from deepreefmap_gui.form.panel import FormPanelMixin
from deepreefmap_gui.models.library_ui import ModelLibraryMixin
from deepreefmap_gui.models.management import ModelManagementMixin
from deepreefmap_gui.runs.data_manager import DataManagerMixin
from deepreefmap_gui.runs.loading import RunLoadingMixin
from deepreefmap_gui.runs.past_runs import PastRunsMixin
from deepreefmap_gui.runs.progress import ProgressBarsMixin
from deepreefmap_gui.runs.progress_panel import ProgressPanel
from deepreefmap_gui.runs.results import ResultsMixin
from deepreefmap_gui.simple.analysis import SimpleAnalysisMixin
from deepreefmap_gui.simple.batch import SimpleBatchMixin
from deepreefmap_gui.simple.mode import UiModeMixin
from deepreefmap_gui.simple.plan import SimplePlanMixin
from deepreefmap_gui.system.panel import SystemPanelMixin
from deepreefmap_gui.update.version import VersionCheckMixin
from deepreefmap_gui.viewer.controls import ViewerControlsMixin

logger = logging.getLogger(__name__)


class DeepReefMapWindow(
    QMainWindow,
    BatchMixin,
    DataManagerMixin,
    FormPanelMixin,
    ModelLibraryMixin,
    ModelManagementMixin,
    PastRunsMixin,
    ProgressBarsMixin,
    ResultsMixin,
    RunLoadingMixin,
    SimpleAnalysisMixin,
    SimpleBatchMixin,
    SimplePlanMixin,
    SystemPanelMixin,
    UiModeMixin,
    ViewerControlsMixin,
    VersionCheckMixin,
):
    _sig_update_check_done = Signal(str, object, object)
    _sig_model_status_done = Signal(object, object)
    _sig_pipeline_error = Signal(str)
    _sig_pipeline_cancelled = Signal()
    _sig_status_text = Signal(str)
    _sig_hf_auth_done = Signal(object, str)
    _sig_download_progress = Signal(str, int)
    _sig_pack_progress = Signal(str, int, int)
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

    def __init__(self, classes_config: ClassConfig, classes_path: Path | None) -> None:
        super().__init__()
        self._classes_config = classes_config
        self._classes_path = classes_path
        self._pipeline_thread: threading.Thread | None = None
        self._playback_timer = QTimer(self)
        self._playback_timer.timeout.connect(self._on_playback_tick)

        self._sig_update_check_done.connect(self._apply_update_check)
        self._sig_model_status_done.connect(self._apply_model_status)
        self._sig_pipeline_error.connect(self._on_pipeline_error)
        self._sig_pipeline_cancelled.connect(self._on_pipeline_cancelled)
        # The lambda is load-bearing, not noise: _status_label is built later, by
        # _build_form_panel(). Binding self._status_label.setText here would
        # resolve the attribute at connect time and raise AttributeError.
        self._sig_status_text.connect(lambda t: self._status_label.setText(t))  # noqa: PLW0108
        self._sig_hf_auth_done.connect(self._on_hf_auth_done)
        self._sig_download_progress.connect(self._on_download_progress)
        self._sig_pack_progress.connect(self._on_pack_progress)
        self._sig_pack_done.connect(self._on_pack_done)
        self._sig_run_loaded.connect(self._apply_loaded_run)
        self._sig_load_progress.connect(self._on_load_progress)
        self._sig_scene_file_done.connect(self._on_scene_file_done)
        self._sig_batch_progress.connect(self._on_batch_progress)
        self._sig_batch_done.connect(self._on_batch_done)
        self._sig_discovery_done.connect(self._on_discovery_done)
        self._sig_survey_progress.connect(self._on_survey_progress)
        self._sig_survey_done.connect(self._on_survey_done)
        self._sig_run_sizes_done.connect(self._apply_run_sizes)

        self.setWindowTitle("DeepReefMap")
        # Open at ~90% of the available screen, capped at the comfortable
        # 1400x900 target. Small laptops / low resolutions then get a window
        # that fits their screen instead of one clipped by the panels or the
        # taskbar. availableGeometry excludes docks and taskbars.
        screen = QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            init_w = min(1400, int(avail.width() * 0.9))
            init_h = min(900, int(avail.height() * 0.9))
        else:
            init_w, init_h = 1400, 900
        self.resize(init_w, init_h)
        # Explicit floor so the window can always be shrunk back after the user
        # enlarges it. Without this, Qt's computed minimumSize follows whichever
        # child currently sizes the widest, and the window gets stuck at that
        # width.
        self.setMinimumSize(720, 480)

        from deepreefmap_gui.viewer.widget import QtPointCloudViewer

        self._viewer = QtPointCloudViewer(
            class_colors=classes_config.id_to_color,
            class_names=classes_config.id_to_name,
        )
        self._viewer.set_status_callback(self._on_viewer_status)
        self._progress_panel = ProgressPanel()
        self._viewer.set_placeholder_widget(self._progress_panel)
        self._viewer.point_picked.connect(self._on_point_picked)
        self._viewer.point_picked_clear.connect(self._on_point_picked_clear)
        self._viewer.canvas_resized.connect(self._on_canvas_resized)
        self._viewer.frustum_picked.connect(self._on_frustum_picked)
        self._pick_card = None
        self._last_pick_payload = None
        self._pick_card_pinned_pos: tuple[int, int] | None = None
        self._build_pick_mode_overlay()

        # Build the form first so widgets it references (status_label, etc.)
        # are constructed before we wire them into the top toolbar.
        form_panel = self._build_form_panel()
        self._capture_form_defaults()
        top_bar = self._build_top_bar()
        # The preview toggle gates the viewer canvas, so it docks onto the
        # viewer's header rather than the global toolbar.
        self._viewer.add_header_widget(self._build_preview_toggle())
        log_panel = self._build_log_panel()

        # The left pane is either the advanced sidebar (page 0) or the simple
        # full-page shell (page 1); _set_ui_mode flips the stack.
        self._left_stack = QStackedWidget()
        self._left_stack.addWidget(form_panel)
        self._left_stack.addWidget(self._build_simple_shell())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._left_stack)
        splitter.addWidget(self._viewer)
        # Size the form pane to its DPI-aware content width, clamped to at most
        # half the window so the 3D viewport keeps the majority. Stretch factor
        # 0 then pins that width when the window grows, so the viewer absorbs the
        # extra space rather than the form. _update_work_area re-divides the
        # splitter per mode.
        form_w = getattr(self, "_form_preferred_width", 440)
        form_w = max(360, min(form_w, self.width() // 2))
        splitter.setSizes([form_w, max(400, self.width() - form_w)])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setChildrenCollapsible(True)
        splitter.setHandleWidth(6)
        self._work_hsplitter = splitter

        # Vertical splitter places the live log as a togglable section at the
        # bottom of the window, alongside the form + 3D viewer above it. Hidden
        # initially; the top-bar Log button drives visibility, and _on_submit
        # auto-opens it when a run starts.
        self._central_vsplitter = QSplitter(Qt.Orientation.Vertical)
        self._central_vsplitter.addWidget(splitter)
        self._central_vsplitter.addWidget(log_panel)
        # Log takes ~a quarter of the height when shown, proportional to the
        # window so it doesn't dwarf a short window or vanish in a tall one.
        _log_h = max(180, self.height() // 4)
        self._central_vsplitter.setSizes([self.height() - _log_h, _log_h])
        self._central_vsplitter.setStretchFactor(0, 1)
        self._central_vsplitter.setStretchFactor(1, 0)
        self._central_vsplitter.setChildrenCollapsible(True)
        self._central_vsplitter.setHandleWidth(6)

        # Banner below the toolbar that pops up the instant a past run is
        # clicked, with the manifest metadata. Hidden until populated.
        self._run_meta_banner = QLabel("")
        self._run_meta_banner.setWordWrap(True)
        self._run_meta_banner.setTextFormat(Qt.TextFormat.RichText)
        self._run_meta_banner.setStyleSheet(
            f"background-color: {BANNER_BG}; color: {BANNER_TEXT};"
            f" padding: 4px 12px; border-bottom: 1px solid {BANNER_BORDER};"
        )
        # Compact single-row format means we only need ~2 lines of height.
        self._run_meta_banner.setMaximumHeight(56)
        self._run_meta_banner.setVisible(False)

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(top_bar)
        central_layout.addWidget(self._run_meta_banner)
        central_layout.addWidget(self._central_vsplitter, 1)
        # Status and progress span the whole window under everything else, so
        # they read the same in both modes and survive log-panel resizing.
        central_layout.addWidget(self._build_bottom_bar())
        self.setCentralWidget(central)

        # Apply the saved mode last: it flips the left stack and re-divides the
        # splitter, so everything above must exist first.
        self._init_ui_mode()

    # --- teardown -----------------------------------------------------------

    def _stop_window_timers(self) -> None:
        """Stop the timers this window owns.

        Qt destroys them with their parent, but destruction happens after the
        close returns; a tick landing in between runs a slot against widgets
        that are already going away.
        """
        for attr in (
            "_playback_timer",
            "_sys_timer",
            "_status_tick_timer",
            "_data_refresh_timer",
            "_out_root_commit_timer",
        ):
            timer = getattr(self, attr, None)
            if timer is not None:
                try:
                    timer.stop()
                except RuntimeError:
                    logger.debug("Timer %s was already destroyed", attr, exc_info=True)

    def _release_handles(self) -> None:
        """Close what Qt's parent-child ownership does not cover.

        Three handles are held by plain Python attributes rather than QObjects,
        so nothing releases them when the window is destroyed. Two hold an OS
        resource outright: the survey store is a live SQLite connection and the
        run log is an open file.

        The frame accessor holds none, now that the scene file is closed before
        load_scene_file returns and the pixels are read per-frame from the run
        directory. It is still closed here because close() is part of the
        FrameAccessor protocol and this is the only place a window-held accessor
        would be released; an implementation backed by an archive rather than a
        directory would need it.
        """
        accessor = getattr(self, "_scene_accessor", None)
        if accessor is not None:
            accessor.close()
            self._scene_accessor = None

        store = getattr(self, "_survey_store_obj", None)
        if store is not None:
            try:
                store.close()
            except Exception:
                logger.debug("Survey store did not close cleanly", exc_info=True)
            self._survey_store_obj = None

        handler = getattr(self, "_run_log_file_handler", None)
        if handler is not None:
            from deepreefmap_gui.system.log_view import close_run_log_file

            close_run_log_file(handler)
            self._run_log_file_handler = None

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        # RunLoadingMixin._run_in_flight: a survey batch runs on _pipeline_thread
        # too, so this one predicate covers both kinds of work.
        if self._run_in_flight():
            answer = QMessageBox.question(
                self,
                "Quit DeepReefMap",
                "A reconstruction is still running. Quitting stops it and the "
                "run will be incomplete.\n\nQuit anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            # Signalled, not joined. The worker threads are daemons, and a join
            # on a mid-pipeline reconstruction would freeze the UI for minutes
            # at the point the user has just asked to leave. They are left to
            # die with the process, which is why the warning is logged.
            logger.warning("Quitting with a reconstruction in flight; it will be abandoned")

        for attr in ("_cancel_event", "_survey_cancel_event"):
            cancel = getattr(self, attr, None)
            if cancel is not None:
                cancel.set()
        # Set, not cleared: a worker parked on the pause gate has to be released
        # before it can observe the cancel it was just given.
        pause = getattr(self, "_pause_event", None)
        if pause is not None:
            pause.set()

        self._stop_window_timers()
        # Before the handles go: these close over a QProgressDialog parented to
        # this window, so a render still in flight would drive a widget Qt is
        # about to destroy.
        self._disconnect_qc_render_handlers()
        self._release_handles()
        super().closeEvent(event)



def launch(classes_path: Path | None = None, view_run_dir: Path | None = None) -> None:
    from deepreefmap.config.classes import load_classes

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # After basicConfig: the root StreamHandler keeps the real stderr, so
    # redirected writes can't loop back through it.
    from deepreefmap_gui.system.log_view import redirect_std_streams_to_logging

    redirect_std_streams_to_logging()
    if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("QT_QPA_PLATFORM"):
        os.environ["QT_QPA_PLATFORM"] = "xcb"
    os.environ.setdefault("QT_OPENGL", "desktop")
    fmt = QSurfaceFormat()
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    # VTK 9's OpenGL2 backend needs >=3.2 core. macOS only exposes >2.1 through a
    # forward-compatible core profile, which Qt sets for a core-profile request.
    fmt.setVersion(3, 2)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    QSurfaceFormat.setDefaultFormat(fmt)
    QApplication.setApplicationName("DeepReefMap")
    QApplication.setApplicationDisplayName("DeepReefMap")
    qt_app = cast(QApplication, QApplication.instance() or QApplication(sys.argv))
    from deepreefmap_gui.core.fonts import apply_app_fonts
    from deepreefmap_gui.core.theme import apply_theme

    apply_app_fonts(qt_app)
    apply_theme(qt_app)
    from importlib import resources
    icon_path = resources.files("deepreefmap_gui.resources").joinpath("icon.png")
    qt_app.setWindowIcon(QIcon(str(icon_path)))
    classes_config = load_classes(classes_path)
    window = DeepReefMapWindow(classes_config, classes_path)
    window.show()
    if view_run_dir is not None:
        QTimer.singleShot(100, lambda: window._auto_load_run(view_run_dir))

    # Qt's exec() blocks in C++, so Python's SIGINT handler can't fire until
    # the event loop yields. A no-op timer wakes the interpreter every 200 ms.
    # Parent it to qt_app so PySide6 won't garbage-collect the C++ side.
    _sigint_count = 0

    def _on_sigint(*_: object) -> None:
        nonlocal _sigint_count
        _sigint_count += 1
        if _sigint_count >= 2:
            os._exit(1)
        qt_app.closeAllWindows()
        qt_app.quit()

    signal.signal(signal.SIGINT, _on_sigint)
    sigint_heartbeat = QTimer(qt_app)
    sigint_heartbeat.start(200)
    sigint_heartbeat.timeout.connect(lambda: None)

    sys.exit(qt_app.exec())
