from __future__ import annotations

import importlib.util
import os

import pytest


def require_torch() -> None:
    """Skip locally when torch is absent, but fail where it must be installed.

    Building a window pulls in torch, so a torchless machine would skip most of
    this directory. That is a reasonable local convenience and a useless CI run,
    so DEEPREEFMAP_REQUIRE_TORCH turns the skip into a failure.
    """
    if importlib.util.find_spec("torch") is not None:
        return
    message = "torch not loadable on this machine"
    if os.environ.get("DEEPREEFMAP_REQUIRE_TORCH"):
        raise AssertionError(
            f"{message}, and DEEPREEFMAP_REQUIRE_TORCH is set: the GUI suite would "
            "have been skipped rather than run"
        )
    pytest.skip(message, allow_module_level=True)


@pytest.fixture(autouse=True)
def _reset_ui_mode(qapp):
    """Keep the persisted mode and preview toggles from leaking between tests."""
    from PySide6.QtCore import QSettings

    settings = QSettings("ECEO", "deepreefmap")
    for key in ("ui_mode", "preview_3d"):
        settings.remove(key)
    yield
    for key in ("ui_mode", "preview_3d"):
        settings.remove(key)


@pytest.fixture(autouse=True)
def _tmp_output_root(qapp, tmp_path):
    """Point the persisted output root at a temp dir: windows open in simple
    mode by default and create survey.db under the root at construction."""
    from PySide6.QtCore import QSettings

    settings = QSettings("ECEO", "deepreefmap")
    old = settings.value("output_root_dir")
    settings.setValue("output_root_dir", str(tmp_path / "out"))
    yield
    if old is None:
        settings.remove("output_root_dir")
    else:
        settings.setValue("output_root_dir", old)


@pytest.fixture(autouse=True)
def _offline_tiles(qapp):
    """Map widgets must never fetch tiles during tests."""
    from deepreefmap_gui.map.tile_cache import shared_tile_cache

    cache = shared_tile_cache()
    cache.network_enabled = False
    yield


@pytest.fixture
def make_window(qapp):
    """Factory building a fresh DeepReefMapWindow with the built-in classes.

    A factory rather than an instance so tests can set env vars (mock PyApp,
    timing-profile path) before construction.

    Every window it builds is drained and stopped on teardown, in that order.
    Nothing closes these windows, so anything they left pending outlives the test
    and lands inside whichever later test happens to run the event loop -- an
    order-dependent failure a long way from its cause.

    The order is the whole point. Some work is queued rather than armed: deleting
    a run directory fires QFileSystemWatcher.directoryChanged asynchronously, and
    a batch worker emits its done signal across threads. Neither has been
    delivered by teardown, so stopping timers first misses them entirely -- the
    delivery happens later and arms a timer on a window nothing is watching any
    more. Draining first lets them land, then the stop catches what they armed.

    closeEvent is deliberately not called: it prompts when a run is in flight.
    """
    require_torch()
    built = []

    def _make():
        from deepreefmap.config.classes import load_classes
        from deepreefmap_gui.app import DeepReefMapWindow

        win = DeepReefMapWindow(load_classes(), None)
        built.append(win)
        return win

    yield _make

    if built:
        from PySide6.QtWidgets import QApplication

        QApplication.processEvents()
    for win in built:
        win._stop_window_timers()


@pytest.fixture
def window(make_window):
    """A fresh DeepReefMapWindow. Function-scoped: tests mutate form state,
    legend toggles and signals, so sharing an instance would leak state."""
    return make_window()


@pytest.fixture
def simple_window(window, tmp_path):
    """A window in simple mode with its output root under tmp_path.

    The starting point for the plan, batch, analysis and settings-dialog suites,
    which each used to carry an identical copy of these two lines.
    """
    window._out_root_input.setText(str(tmp_path))
    window._set_ui_mode("simple")
    return window
