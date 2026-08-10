import atexit
import os
import shutil
import subprocess
import tempfile
import time

import pytest


def _headless() -> None:
    """Keep the suite off the developer's screen: `Xvfb` if there is one, else `offscreen`.

    Xvfb first because the viewer tests need a GL context, and `offscreen`
    segfaults them on a machine with no software GL. An explicit
    `QT_QPA_PLATFORM` wins, and must be `xcb`: VTK under Wayland hangs.
    """
    if os.environ.get("QT_QPA_PLATFORM"):
        return
    if (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and _start_xvfb():
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        return
    os.environ["QT_QPA_PLATFORM"] = "offscreen"


def _start_xvfb() -> bool:
    """Point DISPLAY at a throwaway X server of our own. True if one came up."""
    if not shutil.which("Xvfb"):
        return False
    for number in range(99, 110):
        if os.path.exists(f"/tmp/.X11-unix/X{number}"):
            continue
        try:
            server = subprocess.Popen(
                ["Xvfb", f":{number}", "-screen", "0", "1920x1200x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return False
        # Poll for the socket: Qt fails to connect if it starts first.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if os.path.exists(f"/tmp/.X11-unix/X{number}"):
                atexit.register(_stop_xvfb, server)
                os.environ["DISPLAY"] = f":{number}"
                os.environ.pop("WAYLAND_DISPLAY", None)
                return True
            if server.poll() is not None:
                break  # Another server took the number, or Xvfb is unusable.
            time.sleep(0.05)
        server.kill()
    return False


def _stop_xvfb(server: subprocess.Popen) -> None:
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()


_headless()

# Keep the suite off the developer's real run history: measured peaks in
# ~/.local/share/deepreefmap/run_timings.json would otherwise shift memory
# grades and ETA priors, making tests fail on machines that have done runs.
os.environ.setdefault(
    "DEEPREEFMAP_RUN_TIMINGS",
    os.path.join(tempfile.mkdtemp(prefix="deepreefmap-test-timings-"), "run_timings.json"),
)

# Keep window construction off the network: an empty mock reads as a fetch
# failure, so no update badge appears and GitHub is never contacted.
os.environ.setdefault("DEEPREEFMAP_MOCK_VERSIONS", "")


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _no_file_manager(request, monkeypatch):
    """Stop a click reaching a real file manager on `tmp_path`.

    The effect functions rather than `reveal_in_file_manager`: callers import
    that name into their own module, and its dispatch stays real.
    `tests/core/test_reveal.py` is the exception, marked `real_reveal`.
    """
    if request.node.get_closest_marker("real_reveal"):
        yield
        return

    from deepreefmap_gui.core import reveal

    for name in ("_show_items", "_xdg_open", "_qt_open", "_reveal_windows", "_reveal_macos"):
        monkeypatch.setattr(reveal, name, lambda _path: False)

    try:
        from PySide6.QtGui import QDesktopServices
    except ImportError:
        yield
        return
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda _url: True))
    yield


@pytest.fixture(autouse=True, scope="session")
def _isolate_qsettings():
    """Redirect all QSettings storage to a tempdir for the whole test session.

    setDefaultFormat(IniFormat) does not flip QSettings(org, app) instances under
    PySide6, so redirect the NativeFormat path itself. On Windows that path is the
    registry, where setPath has no effect.

    A missing PySide6 is raised, not swallowed: yielding without isolation would
    let the rest of the session write to the developer's real config.
    """
    from PySide6.QtCore import QSettings

    tmp = tempfile.mkdtemp(prefix="deepreefmap-test-qsettings-")
    for fmt in (QSettings.Format.NativeFormat, QSettings.Format.IniFormat):
        QSettings.setPath(fmt, QSettings.Scope.UserScope, tmp)
        QSettings.setPath(fmt, QSettings.Scope.SystemScope, tmp)
    yield
