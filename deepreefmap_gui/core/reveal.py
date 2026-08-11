"""Show a file in the operating system's file manager, selected.

Opening the parent folder is not the same thing: a run directory holds dozens of
similarly named files, and the point of revealing one is that the user can see
which. Windows and macOS both have a first-class call for it. Linux has no
single one, so three strategies are tried in turn.

Nothing here raises. Revealing a file is a convenience, and a wedged or absent
file manager must never take the GUI thread down with it.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Short, because this runs on the GUI thread. A file manager that has not
# answered by now is hung, and waiting longer only freezes the window.
_TIMEOUT_S = 2.0

_FILE_MANAGER1 = (
    "--dest",
    "org.freedesktop.FileManager1",
    "--object-path",
    "/org/freedesktop/FileManager1",
    "--method",
    "org.freedesktop.FileManager1.ShowItems",
)


def reveal_in_file_manager(path: Path) -> bool:
    """Open the file manager with `path` selected. True if a strategy launched."""
    try:
        target = Path(path).expanduser().resolve()
    except OSError:
        logger.debug("Could not resolve %s", path, exc_info=True)
        return False

    if sys.platform == "win32":
        return _reveal_windows(target)
    if sys.platform == "darwin":
        return _reveal_macos(target)
    return _reveal_linux(target)


def _reveal_windows(target: Path) -> bool:
    try:
        subprocess.run(
            ["explorer", f"/select,{target}"],
            timeout=_TIMEOUT_S,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("Could not reveal %s in Explorer", target, exc_info=True)
        return False
    # Explorer exits 1 on success as often as not, so its return code says
    # nothing about whether the window opened. Reading it would reject the
    # common case. Do not "fix" this into a returncode check.
    return True


def _reveal_macos(target: Path) -> bool:
    try:
        done = subprocess.run(
            ["open", "-R", str(target)],
            timeout=_TIMEOUT_S,
            check=False,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("Could not reveal %s in Finder", target, exc_info=True)
        return False
    return done.returncode == 0


def _reveal_linux(target: Path) -> bool:
    """Freedesktop first, then the parent folder, giving up selection to open at all."""
    if _show_items(target):
        return True
    parent = target.parent
    if _xdg_open(parent):
        return True
    return _qt_open(parent)


def _show_items(target: Path) -> bool:
    """The one interface that selects rather than merely opens.

    Nautilus, Dolphin, Nemo and Thunar all export it. `as_uri` percent-encodes
    the quote and dollar, so the URI is safe inside the GVariant literal.
    """
    try:
        done = subprocess.run(
            ["gdbus", "call", "--session", *_FILE_MANAGER1, f"['{target.as_uri()}']", ""],
            timeout=_TIMEOUT_S,
            check=False,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        logger.debug("FileManager1.ShowItems unavailable", exc_info=True)
        return False
    return done.returncode == 0


def _xdg_open(parent: Path) -> bool:
    try:
        done = subprocess.run(
            ["xdg-open", str(parent)],
            timeout=_TIMEOUT_S,
            check=False,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("xdg-open failed for %s", parent, exc_info=True)
        return False
    return done.returncode == 0


def _qt_open(parent: Path) -> bool:
    """Last resort, so a sandboxed build with no portal binaries still opens something."""
    try:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        if QDesktopServices.openUrl(QUrl.fromLocalFile(str(parent))):
            return True
    except Exception:
        logger.debug("QDesktopServices could not open %s", parent, exc_info=True)
        return False
    logger.warning("No file manager would open %s", parent)
    return False
