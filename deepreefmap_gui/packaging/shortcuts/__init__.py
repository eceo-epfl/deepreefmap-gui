"""Putting DeepReefMap in the computer's applications menu, on any platform.

Linux has no installer -- the user downloads a bare binary -- so the app has to
register itself. Windows and macOS normally get their shortcut from the Inno
installer or the disk image, but the raw GitHub asset run out of a Downloads
folder gets nothing, which is who this is for there.

That makes **ownership** the safety property. On Windows the installer's Start
Menu entry lives at the very path this module would write, so removing "the
shortcut" from inside an installed copy would delete the entry the uninstaller
expects to find. A shortcut this module did not create is reported and left
alone.

Nothing here raises. Every entry point returns a value the GUI can render,
including the reason it failed.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol

from deepreefmap_gui.packaging.shortcuts._manifest import (
    clear_record,
    read_record,
    write_record,
)

logger = logging.getLogger(__name__)


class ShortcutError(Exception):
    """A backend could not do what was asked; the message reaches the user."""


class ShortcutState(str, Enum):
    UNSUPPORTED = "unsupported"
    ABSENT = "absent"
    CURRENT = "current"
    # Present, but launching a binary that is no longer the running one. Our own
    # updater causes this whenever it moves the binary.
    STALE = "stale"
    # Present, target unreadable. Says nothing about whether it works.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ShortcutStatus:
    state: ShortcutState
    location: Path | None = None
    target: Path | None = None
    owned: bool = False
    detail: str = ""


@dataclass(frozen=True)
class ShortcutResult:
    ok: bool
    action: Literal["install", "remove"]
    status: ShortcutStatus
    message: str
    error: str | None = None


class ShortcutBackend(Protocol):
    name: str

    def location(self) -> Path: ...

    def read_target(self) -> Path | None:
        """What the shortcut launches, or None if that cannot be read. Never raises."""

    def install(self, binary: Path) -> None:
        """Create or replace the shortcut. Raises ShortcutError on failure."""

    def remove(self) -> None:
        """Delete the shortcut. Idempotent."""

    def preinstalled(self, binary: Path) -> str:
        """Why this binary is already installed, or "" if it is not.

        The macOS disk image ships a bundle whose executable *is* the binary, so
        a copy running from inside it is already in Applications.
        """


def _backend_for(platform: str) -> ShortcutBackend | None:
    """Chosen from the platform string alone, so every backend is testable anywhere."""
    if platform.startswith("linux"):
        from deepreefmap_gui.packaging.shortcuts._linux import LinuxShortcuts

        return LinuxShortcuts()
    if platform == "win32":
        from deepreefmap_gui.packaging.shortcuts._windows import WindowsShortcuts

        return WindowsShortcuts()
    if platform == "darwin":
        from deepreefmap_gui.packaging.shortcuts._macos import MacShortcuts

        return MacShortcuts()
    return None


def _backend() -> ShortcutBackend | None:
    # sys.platform read here rather than at import, so a test can swap it.
    return _backend_for(sys.platform)


def shortcut_supported() -> bool:
    return _backend() is not None


def _running_binary(binary_path: str | Path | None) -> Path | None:
    if binary_path is not None:
        return Path(binary_path)
    from deepreefmap_gui.packaging.releases import pyapp_binary_path

    found = pyapp_binary_path()
    return Path(found) if found else None


def _placement(backend: ShortcutBackend) -> tuple[Path, bool, bool]:
    """Where the entry goes, whether one is there, and whether it is ours.

    Deliberately independent of which binary is running: ownership is a fact
    about the record and the location. Deciding it from the running binary would
    make the guard in remove_shortcut stop protecting the installer's entry
    whenever the binary cannot be resolved.
    """
    location = backend.location()
    # Existence is always a filesystem question. Letting the record answer it
    # would report a shortcut the user deleted by hand as still present.
    exists = False
    try:
        exists = location.exists()
    except OSError:
        pass
    record = read_record()
    owned = bool(record and record.get("location") == str(location) and exists)
    return location, exists, owned


def shortcut_status(binary_path: str | Path | None = None) -> ShortcutStatus:
    """Whether the applications menu holds an entry, and whether it still works."""
    backend = _backend()
    if backend is None:
        return ShortcutStatus(
            ShortcutState.UNSUPPORTED,
            detail=f"Adding to the applications menu is not supported on {sys.platform}.",
        )
    binary = _running_binary(binary_path)
    if binary is None:
        # A source checkout launches through a console script inside .venv,
        # which is rebuilt and moved often enough that a menu entry pointing at
        # it would break. Only the packaged binary has a path worth recording.
        return ShortcutStatus(
            ShortcutState.UNSUPPORTED,
            detail=(
                "Only the installed application can add itself to the "
                "applications menu. This copy is running from a source checkout."
            ),
        )
    already = backend.preinstalled(binary)
    if already:
        return ShortcutStatus(ShortcutState.CURRENT, owned=False, detail=already)

    try:
        location, exists, owned = _placement(backend)
    except Exception as exc:
        return ShortcutStatus(ShortcutState.UNKNOWN, detail=str(exc))
    record = read_record()

    if not exists:
        if record:
            clear_record()
        return ShortcutStatus(ShortcutState.ABSENT, location=location, owned=False)

    target = backend.read_target()
    if target is None and record:
        raw = record.get("target")
        target = Path(raw) if raw else None
    if target is None:
        return ShortcutStatus(
            ShortcutState.UNKNOWN,
            location=location,
            owned=owned,
            detail="An entry is present, but what it launches could not be read.",
        )
    if _same_path(target, binary):
        return ShortcutStatus(ShortcutState.CURRENT, location=location, target=target, owned=owned)
    return ShortcutStatus(
        ShortcutState.STALE,
        location=location,
        target=target,
        owned=owned,
        detail=f"The entry launches {target}, but DeepReefMap is running from {binary}.",
    )


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return str(left) == str(right)


def install_shortcut(binary_path: str | Path | None = None) -> ShortcutResult:
    """Add or refresh the applications-menu entry for the running binary."""
    backend = _backend()
    binary = _running_binary(binary_path)
    if backend is None or binary is None:
        status = shortcut_status(binary_path)
        return ShortcutResult(
            False, "install", status, "DeepReefMap cannot add itself to this applications menu."
        )
    try:
        backend.install(binary)
    except Exception as exc:
        logger.warning("Could not add the applications-menu entry", exc_info=True)
        return ShortcutResult(
            False,
            "install",
            shortcut_status(binary_path),
            f"DeepReefMap could not be added to the applications menu: {exc}",
            error=str(exc),
        )
    try:
        write_record(str(backend.location()), str(binary), backend.name)
    except OSError:
        logger.debug("Could not record the shortcut", exc_info=True)
    # Re-read rather than assume: a backend can report success and still have
    # written something the platform will not honour.
    status = shortcut_status(binary_path)
    return ShortcutResult(True, "install", status, "DeepReefMap was added to the applications menu.")


def remove_shortcut() -> ShortcutResult:
    """Delete the entry this app created. Refuses to touch anyone else's."""
    backend = _backend()
    if backend is None:
        status = shortcut_status()
        return ShortcutResult(False, "remove", status, "There is nothing to remove.")
    # Asked of the location and the record, not of shortcut_status: the guard
    # below has to hold even when the running binary cannot be resolved.
    try:
        _location, exists, owned = _placement(backend)
    except Exception as exc:
        return ShortcutResult(
            False, "remove", shortcut_status(), f"The entry could not be read: {exc}", error=str(exc)
        )
    if exists and not owned:
        return ShortcutResult(
            False,
            "remove",
            shortcut_status(),
            "This entry was created by the installer, so DeepReefMap will not remove it.",
        )
    try:
        backend.remove()
    except Exception as exc:
        logger.warning("Could not remove the applications-menu entry", exc_info=True)
        return ShortcutResult(
            False,
            "remove",
            shortcut_status(),
            f"The applications-menu entry could not be removed: {exc}",
            error=str(exc),
        )
    clear_record()
    return ShortcutResult(
        True, "remove", shortcut_status(), "DeepReefMap was removed from the applications menu."
    )


def repair_owned_shortcut(binary_path: str | Path | None = None) -> ShortcutResult | None:
    """Re-point an entry we created after our own updater moved the binary.

    Only ever repairs; never creates. Being in the applications menu is
    something the user asked for once, and it is not re-asked for silently.
    """
    status = shortcut_status(binary_path)
    if status.state is not ShortcutState.STALE or not status.owned:
        return None
    logger.info("Repairing the applications-menu entry after the binary moved")
    return install_shortcut(binary_path)


__all__ = [
    "ShortcutBackend",
    "ShortcutError",
    "ShortcutResult",
    "ShortcutState",
    "ShortcutStatus",
    "install_shortcut",
    "remove_shortcut",
    "repair_owned_shortcut",
    "shortcut_status",
    "shortcut_supported",
]
