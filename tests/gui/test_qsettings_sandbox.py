"""Regression test for the test-session QSettings sandbox in conftest.py.

If this test ever fails, ad-hoc tests that write to QSettings will start
polluting the developer's real ~/.config/ECEO/deepreefmap.conf.
"""

import sys
import tempfile
from pathlib import Path

import pytest


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="QSettings NativeFormat is the registry on Windows, where setPath is a no-op",
)
def test_qsettings_writes_go_to_tempdir() -> None:
    from PySide6.QtCore import QSettings

    s = QSettings("ECEO", "deepreefmap")
    s.setValue("__sandbox_probe", "should-not-leak")
    s.sync()

    file_path = Path(s.fileName()).resolve()
    # The sandbox in conftest reroutes both formats; whichever this instance
    # uses, the resulting file must live under the temp dir, not the user's real
    # home config dir. Compared against gettempdir() rather than a literal
    # "/tmp": macOS uses /var/folders/..., and --basetemp moves it anywhere.
    temp_root = Path(tempfile.gettempdir()).resolve()
    assert temp_root in file_path.parents, (
        f"QSettings sandbox failed: wrote to {file_path}, outside {temp_root}. "
        "This means real config files would be polluted by tests."
    )

    # Clean the probe key so re-running doesn't accumulate state.
    s.remove("__sandbox_probe")
    s.sync()
