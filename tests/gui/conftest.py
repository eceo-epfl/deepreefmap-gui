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
    for key in ("ui_mode", "preview_3d", "setup_complete"):
        settings.remove(key)
    yield
    for key in ("ui_mode", "preview_3d", "setup_complete"):
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


def _machine_preset_path(tmp_path):
    return tmp_path / "machine-settings" / "survey_preset.yaml"


@pytest.fixture(autouse=True)
def _isolate_survey_preset(tmp_path, monkeypatch):
    """Keep GUI tests off the developer's real survey settings.

    Windows build in simple mode and load the settings at construction, and
    _adopt_form_as_preset writes the machine override back on the return to
    simple mode, so an unguarded test would both read and overwrite the file at
    ~/.local/share/deepreefmap/survey_preset.yaml. Point it at a tmp path that
    does not exist and clear the admin override so the bundled preset loads.
    Follows the pattern in tests/survey/conftest.py.
    """
    monkeypatch.delenv("DEEPREEFMAP_SURVEY_PRESET", raising=False)
    monkeypatch.setattr(
        "deepreefmap_gui.survey.preset.survey_preset_path",
        lambda: _machine_preset_path(tmp_path),
    )


@pytest.fixture
def machine_preset_path(tmp_path):
    """Where the isolation fixture sends this machine's override."""
    return _machine_preset_path(tmp_path)


@pytest.fixture(autouse=True)
def _offline_tiles(qapp):
    """Map widgets must never fetch tiles during tests."""
    from deepreefmap_gui.map.tile_cache import shared_tile_cache

    cache = shared_tile_cache()
    cache.network_enabled = False
    yield


@pytest.fixture(autouse=True)
def _assume_gpu(monkeypatch):
    """Assume a graphics card so the run gate is deterministic off CI hardware.

    The bundled preset defaults to a GPU-only mapper, so on a CPU-only runner the
    gate blocks and every ready-state test fails. Tests for the no-GPU path
    override _gpu_available or _gpu_only_mapper themselves.
    """
    from deepreefmap_gui.app import DeepReefMapWindow

    monkeypatch.setattr(DeepReefMapWindow, "_gpu_available", lambda self: True)


@pytest.fixture
def make_window(qapp):
    """Factory building a fresh DeepReefMapWindow with the built-in classes.

    A factory rather than an instance so tests can set env vars (mock PyApp,
    timing-profile path) before construction. Windows are destroyed on teardown:
    each installs event filters on its widgets, and a leaked window makes a later
    global setStyleSheet re-polish it through those filters, which hangs the suite.
    deleteLater rather than close() so the quit-confirmation modal never fires.
    """
    from PySide6.QtCore import QEvent

    require_torch()
    created = []

    def _make():
        from deepreefmap.config.classes import load_classes
        from deepreefmap_gui.app import DeepReefMapWindow

        window = DeepReefMapWindow(load_classes(), None)
        created.append(window)
        return window

    yield _make
    # Stop the periodic timers and drain queued signals before deleting, so a
    # tick does not fire against a half-deleted window ("signal source deleted").
    for window in created:
        window._stop_window_timers()
        window.hide()
    qapp.processEvents()
    for window in created:
        window.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


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
