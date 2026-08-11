"""Start Menu entry on Windows.

Two mechanisms, because the first is not always allowed to run:

A ``.lnk`` is what Windows itself uses, and it is written and read back through
PowerShell's ``WScript.Shell`` COM object -- no pywin32, which the app does not
depend on. Under AppLocker or WDAC **Constrained Language Mode**, though,
``New-Object -ComObject`` is blocked outright, and that is a real configuration
on managed institutional laptops.

So the fallback is a ``.url`` internet shortcut: plain INI text, written and
read with no subprocess at all. It shows up in the Start Menu and in search. It
carries no working directory, and some endpoint-protection products look twice
at ``.url`` files pointing at an executable, which is why it is the fallback and
not the default.

Every path reaches PowerShell through the child process's environment, never on
the command line. That removes the whole class of quoting bugs at once --
including a username containing an apostrophe.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse

from deepreefmap_gui.packaging.shortcuts import ShortcutError

logger = logging.getLogger(__name__)

# Deliberately the name scripts/installer.iss uses, so the two can never produce
# two entries for the same app.
_LNK_NAME = "DeepReefMap.lnk"
_URL_NAME = "DeepReefMap.url"

_TIMEOUT_S = 30

_WRITE_SCRIPT = """\
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($env:DRM_LNK)
$link.TargetPath = $env:DRM_TARGET
$link.WorkingDirectory = $env:DRM_WORKDIR
$link.IconLocation = $env:DRM_ICON
$link.Description = 'DeepReefMap'
$link.Save()
"""

# [Console]::Out.Write, not Write-Output: the formatter wraps host output at the
# console buffer width, which puts a newline in the middle of a long
# C:\\Users\\... path.
_READ_SCRIPT = """\
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
[Console]::Out.Write($shell.CreateShortcut($env:DRM_LNK).TargetPath)
"""


def _powershell() -> str | None:
    """Windows PowerShell 5.1 first: it is guaranteed present, 7 is not."""
    found = shutil.which("powershell")
    if found:
        return found
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    builtin = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if builtin.exists():
        return str(builtin)
    return shutil.which("pwsh")


def _programs_dir() -> Path:
    """The Start Menu's Programs folder, asked of Windows rather than assumed.

    OneDrive's Known Folder Move relocates the Start Menu, so a hardcoded
    %APPDATA% path can write a shortcut that never appears.
    """
    try:
        import ctypes
        import ctypes.wintypes

        # FOLDERID_Programs
        guid = "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}"
        buffer = ctypes.c_wchar_p()
        klass = ctypes.windll.ole32  # type: ignore[attr-defined]
        result = ctypes.windll.shell32.SHGetKnownFolderPath(  # type: ignore[attr-defined]
            ctypes.byref(_guid_struct(guid)), 0, None, ctypes.byref(buffer)
        )
        if result == 0 and buffer.value:
            path = Path(buffer.value)
            klass.CoTaskMemFree(buffer)
            return path
    except Exception:
        logger.debug("SHGetKnownFolderPath unavailable; falling back to APPDATA", exc_info=True)
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def _guid_struct(guid: str):  # type: ignore[no-untyped-def]
    import ctypes
    import uuid as _uuid

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    parsed = _uuid.UUID(guid)
    out = GUID()
    out.Data1, out.Data2, out.Data3 = parsed.fields[0], parsed.fields[1], parsed.fields[2]
    rest = parsed.bytes[8:]
    out.Data4 = (ctypes.c_ubyte * 8)(*rest)
    return out


def _run_powershell(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """One seam, so every PowerShell path can be exercised without Windows."""
    exe = _powershell()
    if exe is None:
        raise ShortcutError("Windows PowerShell was not found.")
    # UTF-16LE without a BOM is what -EncodedCommand expects.
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        [
            exe,
            "-NoProfile",
            "-NonInteractive",
            # Policy governs script files rather than -EncodedCommand, but this
            # costs nothing and pre-empts an AllSigned group policy.
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
        # The caller reads returncode and falls back to a .url, so a non-zero
        # exit is a branch rather than an exception.
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _write_url_shortcut(path: Path, binary: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[InternetShortcut]\n"
        f"URL={binary.as_uri()}\n"
        "IconIndex=0\n"
        f"IconFile={binary}\n",
        encoding="utf-8",
    )


def _read_url_shortcut(path: Path) -> Path | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("URL="):
            parsed = urlparse(line[len("URL=") :].strip())
            if not parsed.path:
                return None
            # file:///C:/x -> /C:/x, so drop the leading separator Windows drives
            # do not have.
            raw = unquote(parsed.path)
            if len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
                raw = raw[1:]
            return Path(raw)
    return None


class WindowsShortcuts:
    name = "windows-start-menu"

    def location(self) -> Path:
        programs = _programs_dir()
        lnk = programs / _LNK_NAME
        if lnk.exists():
            return lnk
        url = programs / _URL_NAME
        return url if url.exists() else lnk

    def preinstalled(self, binary: Path) -> str:
        return ""

    def read_target(self) -> Path | None:
        location = self.location()
        if location.suffix.lower() == ".url":
            return _read_url_shortcut(location)
        if not location.exists():
            return None
        try:
            done = _run_powershell(_READ_SCRIPT, {"DRM_LNK": str(location)})
        except (ShortcutError, OSError, subprocess.SubprocessError):
            logger.debug("Could not read the shortcut target", exc_info=True)
            return None
        if done.returncode != 0:
            return None
        # CreateShortcut on a missing .lnk yields "" rather than an error, so an
        # empty target means unreadable, not "points at nothing".
        text = (done.stdout or "").strip()
        return Path(text) if text else None

    def install(self, binary: Path) -> None:
        programs = _programs_dir()
        programs.mkdir(parents=True, exist_ok=True)
        lnk = programs / _LNK_NAME
        try:
            done = _run_powershell(
                _WRITE_SCRIPT,
                {
                    "DRM_LNK": str(lnk),
                    "DRM_TARGET": str(binary),
                    "DRM_WORKDIR": str(binary.parent),
                    # build.ps1 embeds the icon in the exe with rcedit, so no
                    # separate .ico has to ship or stay in step with updates.
                    "DRM_ICON": f"{binary},0",
                },
            )
            if done.returncode == 0:
                (programs / _URL_NAME).unlink(missing_ok=True)
                return
            reason = (done.stderr or "").strip() or f"exit code {done.returncode}"
        except subprocess.TimeoutExpired:
            # Falls through to the .url like any other failure: a hung
            # PowerShell should still leave the user with a Start Menu entry,
            # which is the whole point of having a second mechanism.
            reason = "PowerShell did not respond in time"
        except (ShortcutError, OSError, subprocess.SubprocessError) as exc:
            reason = str(exc)

        logger.info("Start Menu .lnk unavailable (%s); writing a .url instead", reason)
        try:
            _write_url_shortcut(programs / _URL_NAME, binary)
            lnk.unlink(missing_ok=True)
        except OSError as exc:
            raise ShortcutError(
                f"{reason}. Writing a simple shortcut also failed ({exc}). "
                f"You can add one by hand in {programs}."
            ) from exc

    def remove(self) -> None:
        programs = _programs_dir()
        try:
            (programs / _LNK_NAME).unlink(missing_ok=True)
            (programs / _URL_NAME).unlink(missing_ok=True)
        except OSError as exc:
            raise ShortcutError(str(exc)) from exc
