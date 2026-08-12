"""Applications-folder entry on macOS.

The PyApp asset is a bare executable, not a bundle, and macOS only lists bundles
in Launchpad and Spotlight. So this writes a small wrapper bundle in
``~/Applications`` -- the user's own folder, which needs no authorisation prompt
and is indexed the same way ``/Applications`` is.

The bundle's executable is a stub that ``exec``s the real binary rather than a
copy of it. That is deliberate: ``binary_swap.replace_binary`` updates in place
with ``os.rename``, which replaces the directory entry, so a copy *or* a hard
link would keep resolving to the old inode and silently run a stale version
forever. Only exec-by-path follows an update.

Users who installed from the disk image are already in Applications; a copy
running from inside a bundle reports that and offers nothing.
"""

from __future__ import annotations

import logging
import plistlib
import shlex
import subprocess
from pathlib import Path

from deepreefmap_gui.packaging.shortcuts import ShortcutError

logger = logging.getLogger(__name__)

_BUNDLE_NAME = "DeepReefMap.app"
_EXECUTABLE = "DeepReefMap"
_IDENTIFIER = "ch.epfl.eceo.deepreefmap-gui"
# Where the stub's target is recorded. Launch Services ignores keys it does not
# know, and reading a plist key back beats regexing a shell script.
_TARGET_KEY = "DRMTargetBinary"

_LSREGISTER = Path(
    "/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks"
    "/LaunchServices.framework/Versions/A/Support/lsregister"
)


def _bundle_path() -> Path:
    return Path.home() / "Applications" / _BUNDLE_NAME


def _plist_path() -> Path:
    return _bundle_path() / "Contents" / "Info.plist"


def _stub_path() -> Path:
    return _bundle_path() / "Contents" / "MacOS" / _EXECUTABLE


def _refresh_launch_services(unregister: bool = False) -> None:
    """Nudge Launch Services to re-read the bundle. Best-effort.

    Needed mainly when reinstalling over an existing bundle, where the icon and
    version are cached. Never a full database rebuild: that takes minutes and
    affects every app on the machine.

    ``unregister`` drops the bundle from the database instead, and has to run
    while the bundle still exists for lsregister to read.
    """
    if not _LSREGISTER.exists():
        return
    try:
        subprocess.run(
            [str(_LSREGISTER), "-u" if unregister else "-f", str(_bundle_path())],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("lsregister failed", exc_info=True)


def _stub_source(binary: Path) -> str:
    return (
        "#!/bin/sh\n"
        "# DeepReefMap launcher. Rewritten by the app's applications-menu control.\n"
        f'exec {shlex.quote(str(binary))} "$@"\n'
    )


def _target_from_stub(text: str) -> Path | None:
    for line in text.splitlines():
        if not line.startswith("exec "):
            continue
        parts = shlex.split(line[len("exec ") :])
        if parts:
            return Path(parts[0])
    return None


class MacShortcuts:
    name = "macos-app-bundle"

    def location(self) -> Path:
        return _bundle_path()

    def preinstalled(self, binary: Path) -> str:
        # Running from inside any bundle means the disk image (or a hand copy
        # into /Applications) already put it where this would.
        if ".app/Contents/MacOS" in str(binary):
            return "Installed in Applications, from the DeepReefMap disk image."
        return ""

    def read_target(self) -> Path | None:
        stub: Path | None = None
        try:
            stub = _target_from_stub(_stub_path().read_text(encoding="utf-8"))
        except OSError:
            pass
        recorded: Path | None = None
        try:
            with _plist_path().open("rb") as handle:
                raw = plistlib.load(handle).get(_TARGET_KEY)
            recorded = Path(raw) if raw else None
        except (OSError, plistlib.InvalidFileException, AttributeError):
            pass
        if stub is not None and recorded is not None and stub != recorded:
            logger.debug("Bundle stub launches %s but records %s", stub, recorded)
        # The stub wins: it is what actually runs.
        return stub or recorded

    def install(self, binary: Path) -> None:
        binary = binary.resolve()
        bundle = _bundle_path()
        try:
            (bundle / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
            (bundle / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)

            info: dict[str, object] = {
                "CFBundleName": "DeepReefMap",
                "CFBundleDisplayName": "DeepReefMap",
                "CFBundleExecutable": _EXECUTABLE,
                "CFBundleIdentifier": _IDENTIFIER,
                "CFBundlePackageType": "APPL",
                "CFBundleInfoDictionaryVersion": "6.0",
                "NSHighResolutionCapable": True,
                "LSMinimumSystemVersion": "12.0",
                _TARGET_KEY: str(binary),
            }
            from deepreefmap_gui.packaging.releases import current_version

            info["CFBundleShortVersionString"] = current_version()

            if self._write_icon(bundle):
                info["CFBundleIconFile"] = "icon"

            with _plist_path().open("wb") as handle:
                plistlib.dump(info, handle)

            stub = _stub_path()
            stub.write_text(_stub_source(binary), encoding="utf-8")
            stub.chmod(0o755)
        except OSError as exc:
            raise ShortcutError(str(exc)) from exc
        _refresh_launch_services()

    def _write_icon(self, bundle: Path) -> bool:
        """Copy the packaged .icns in. A missing icon is not a failed install."""
        from importlib import resources

        try:
            source = resources.files("deepreefmap_gui.resources") / "icon.icns"
            (bundle / "Contents" / "Resources" / "icon.icns").write_bytes(source.read_bytes())
        except (OSError, FileNotFoundError, ModuleNotFoundError):
            logger.debug("No icon.icns to install into the bundle", exc_info=True)
            return False
        return True

    def remove(self) -> None:
        import shutil

        bundle = _bundle_path()
        if bundle.exists():
            _refresh_launch_services(unregister=True)
        try:
            if bundle.exists():
                shutil.rmtree(bundle)
        except OSError as exc:
            raise ShortcutError(str(exc)) from exc
