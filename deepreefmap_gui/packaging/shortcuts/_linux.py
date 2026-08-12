"""Freedesktop menu entry for the packaged binary.

The entry points at the running PyApp binary. The updater swaps the binary at
that same path, so the entry stays valid across updates and rollbacks.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from deepreefmap_gui.packaging.shortcuts import ShortcutError

logger = logging.getLogger(__name__)

_ENTRY_NAME = "deepreefmap-gui.desktop"
_ICON_NAME = "deepreefmap-gui.png"

# Characters that make an Exec= value ambiguous unless it is quoted. The spec
# reserves these for the shell-like syntax a launcher applies to the value.
_NEEDS_QUOTING = set(' \t"\'\\><~|&;$*?#()`')


def _data_home() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) if xdg else Path.home() / ".local" / "share"


def _entry_path() -> Path:
    return _data_home() / "applications" / _ENTRY_NAME


def _theme_root() -> Path:
    return _data_home() / "icons" / "hicolor"


def _icon_path() -> Path:
    return _theme_root() / "512x512" / "apps" / _ICON_NAME


def _pixmap_path() -> Path:
    """Pre-theme lookup path, still consulted by LXDE, LXQt and older panels."""
    return _data_home() / "pixmaps" / _ICON_NAME


def _run(args: list[str]) -> None:
    try:
        subprocess.run(args, check=False, capture_output=True)
    except OSError:
        logger.debug("%s failed", args[0], exc_info=True)


def _refresh_desktop_caches() -> None:
    """Rebuild the menu and icon caches a desktop reads instead of scanning.

    Best-effort: each tool is optional and a failure is not a failed install.
    """
    exe = shutil.which("update-desktop-database")
    if exe is not None:
        _run([exe, str(_data_home() / "applications")])

    # A per-user overlay of the system hicolor theme, which carries the
    # index.theme the two merge under, hence --ignore-theme-index. Only refresh
    # a cache that exists: without one the desktop scans the directory.
    exe = shutil.which("gtk-update-icon-cache")
    if exe is not None and (_theme_root() / "icon-theme.cache").exists():
        _run([exe, "--ignore-theme-index", "--quiet", "--force", str(_theme_root())])

    for name in ("kbuildsycoca6", "kbuildsycoca5"):
        exe = shutil.which(name)
        if exe is not None:
            _run([exe])
            break


def quote_exec(path: Path) -> str:
    """An Exec= value a launcher will read back as this exact path.

    The spec quotes with double quotes and escapes backslash, double quote,
    backtick and dollar inside them. A path with a space left bare would be read
    as a command plus an argument.
    """
    text = str(path)
    if not any(char in _NEEDS_QUOTING for char in text):
        return text
    escaped = text
    for char in ("\\", '"', "`", "$"):
        escaped = escaped.replace(char, "\\" + char)
    return f'"{escaped}"'


def parse_exec(value: str) -> Path | None:
    """The program an Exec= line launches, quoted or not, field codes dropped."""
    value = value.strip()
    if not value:
        return None
    if value.startswith('"'):
        out: list[str] = []
        index = 1
        while index < len(value):
            char = value[index]
            if char == "\\" and index + 1 < len(value):
                out.append(value[index + 1])
                index += 2
                continue
            if char == '"':
                return Path("".join(out))
            out.append(char)
            index += 1
        return Path("".join(out)) if out else None
    # Unquoted: the program is the first token, and %f/%U/%i and friends are
    # arguments the launcher substitutes, not part of the path.
    first = value.split()[0]
    return None if first.startswith("%") else Path(first)


class LinuxShortcuts:
    name = "linux-desktop-entry"

    def location(self) -> Path:
        return _entry_path()

    def preinstalled(self, binary: Path) -> str:
        return ""

    def read_target(self) -> Path | None:
        try:
            text = _entry_path().read_text(encoding="utf-8")
        except OSError:
            return None
        for line in text.splitlines():
            if line.startswith("Exec="):
                return parse_exec(line[len("Exec=") :])
        return None

    def install(self, binary: Path) -> None:
        binary = binary.resolve()
        try:
            from importlib import resources

            icon_src = resources.files("deepreefmap_gui.resources") / "icon.png"
            payload = icon_src.read_bytes()
            for icon_dest in (_icon_path(), _pixmap_path()):
                icon_dest.parent.mkdir(parents=True, exist_ok=True)
                if icon_dest.exists() and icon_dest.read_bytes() == payload:
                    continue
                icon_dest.write_bytes(payload)

            entry = _entry_path()
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.write_text(
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=DeepReefMap\n"
                "Comment=Underwater 3D reconstruction and benthic cover mapping\n"
                f"Exec={quote_exec(binary)}\n"
                # Theme-name lookup against the hicolor icon installed above; KDE's
                # launcher does not reliably render absolute Icon= paths.
                "Icon=deepreefmap-gui\n"
                "Terminal=false\n"
                "Categories=Science;\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise ShortcutError(str(exc)) from exc
        _refresh_desktop_caches()

    def remove(self) -> None:
        try:
            _entry_path().unlink(missing_ok=True)
            _icon_path().unlink(missing_ok=True)
            _pixmap_path().unlink(missing_ok=True)
        except OSError as exc:
            raise ShortcutError(str(exc)) from exc
        # The caches would otherwise keep naming an icon that is gone.
        _refresh_desktop_caches()
