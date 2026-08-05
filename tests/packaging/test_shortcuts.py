"""Applications-menu integration across Linux, Windows and macOS.

All three backends are exercised here, on whatever host runs the suite: the
backend is chosen from a platform string rather than at import, and only Windows
needs a faked seam (one subprocess call).

What no test here can tell us is whether macOS honours the wrapper bundle -- see
the note on the bundle-identity caveat in the module.
"""

from __future__ import annotations

import base64
import plistlib
import subprocess
import sys

import pytest

from deepreefmap_gui.packaging import shortcuts as sc
from deepreefmap_gui.packaging.shortcuts import (
    ShortcutError,
    ShortcutState,
    install_shortcut,
    remove_shortcut,
    repair_owned_shortcut,
    shortcut_status,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPREEFMAP_SHORTCUT_MANIFEST", str(tmp_path / "shortcut.json"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setattr(
        "deepreefmap_gui.packaging.shortcuts._linux._refresh_menu_database", lambda: None
    )


@pytest.fixture
def binary(tmp_path):
    """A path carrying the characters that break naive quoting."""
    path = tmp_path / "o'brien deep reef.bin"
    path.write_text("#!/bin/sh\n")
    return path


# --- Dispatch ---


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("linux", "linux-desktop-entry"),
        ("linux2", "linux-desktop-entry"),
        ("win32", "windows-start-menu"),
        ("darwin", "macos-app-bundle"),
        ("freebsd14", None),
        ("emscripten", None),
    ],
)
def test_the_backend_is_chosen_from_the_platform_string(platform, expected):
    backend = sc._backend_for(platform)
    assert (backend.name if backend else None) == expected


@pytest.mark.parametrize("platform", ["linux", "win32", "darwin"])
def test_every_backend_implements_the_whole_protocol(platform):
    """A new platform cannot land half-written: the row calls all of these."""
    backend = sc._backend_for(platform)
    for method in ("location", "read_target", "install", "remove", "preinstalled"):
        assert callable(getattr(backend, method, None)), f"{platform} lacks {method}"
    assert isinstance(backend.name, str) and backend.name


def test_an_unsupported_platform_says_so_rather_than_failing(monkeypatch):
    monkeypatch.setattr(sc.sys, "platform", "freebsd14")
    assert not sc.shortcut_supported()
    status = shortcut_status()
    assert status.state is ShortcutState.UNSUPPORTED
    assert "freebsd14" in status.detail


def test_a_source_checkout_offers_nothing_to_install(monkeypatch):
    """Scenario: `uv run deepreefmap-gui` from a checkout, where PYAPP is unset.

    Expected behaviour: say why rather than offering an Add that cannot work.
    The console script lives inside .venv, so an entry pointing at it would
    break the next time the environment is rebuilt.
    """
    monkeypatch.setattr(sc.sys, "platform", "linux")
    monkeypatch.delenv("DEEPREEFMAP_MOCK_PYAPP", raising=False)
    monkeypatch.delenv("PYAPP", raising=False)

    status = shortcut_status()
    assert status.state is ShortcutState.UNSUPPORTED
    assert "source checkout" in status.detail

    result = install_shortcut()
    assert not result.ok
    assert result.message


# --- Linux ---


def test_the_exec_line_survives_a_path_with_a_space_and_a_dollar(tmp_path):
    from deepreefmap_gui.packaging.shortcuts._linux import parse_exec, quote_exec

    awkward = tmp_path / 'deep reef $HOME "quoted".bin'
    assert parse_exec(quote_exec(awkward)) == awkward


def test_an_unquoted_exec_line_drops_its_field_codes():
    from deepreefmap_gui.packaging.shortcuts._linux import parse_exec

    assert str(parse_exec("/usr/bin/deepreefmap %U")) == "/usr/bin/deepreefmap"


def test_linux_installs_reads_back_and_removes(monkeypatch, binary):
    monkeypatch.setattr(sc.sys, "platform", "linux")
    assert shortcut_status(binary).state is ShortcutState.ABSENT

    result = install_shortcut(binary)
    assert result.ok
    assert result.status.state is ShortcutState.CURRENT
    assert result.status.owned

    result = remove_shortcut()
    assert result.ok
    assert shortcut_status(binary).state is ShortcutState.ABSENT


def test_linux_install_is_idempotent(monkeypatch, binary):
    monkeypatch.setattr(sc.sys, "platform", "linux")
    install_shortcut(binary)
    install_shortcut(binary)
    entries = list((sc._backend().location().parent).glob("*.desktop"))
    assert len(entries) == 1


def test_removing_twice_is_not_an_error(monkeypatch, binary):
    monkeypatch.setattr(sc.sys, "platform", "linux")
    install_shortcut(binary)
    assert remove_shortcut().ok
    assert remove_shortcut().ok


# --- Staleness and ownership ---


def test_a_moved_binary_makes_the_entry_stale(monkeypatch, binary, tmp_path):
    """Scenario: our own updater swapped the binary to a different path."""
    monkeypatch.setattr(sc.sys, "platform", "linux")
    install_shortcut(binary)

    moved = tmp_path / "moved-elsewhere.bin"
    binary.rename(moved)
    status = shortcut_status(moved)
    assert status.state is ShortcutState.STALE
    assert status.owned


def test_a_stale_entry_we_own_is_repaired(monkeypatch, binary, tmp_path):
    monkeypatch.setattr(sc.sys, "platform", "linux")
    install_shortcut(binary)
    moved = tmp_path / "moved-elsewhere.bin"
    binary.rename(moved)

    result = repair_owned_shortcut(moved)
    assert result is not None and result.ok
    assert shortcut_status(moved).state is ShortcutState.CURRENT


def test_repair_never_creates_an_entry_nobody_asked_for(monkeypatch, binary):
    """Being in the applications menu is consented to once, not re-decided
    silently on the app's behalf."""
    monkeypatch.setattr(sc.sys, "platform", "linux")
    assert repair_owned_shortcut(binary) is None
    assert shortcut_status(binary).state is ShortcutState.ABSENT


def test_an_entry_deleted_by_hand_reads_as_absent(monkeypatch, binary):
    """Existence is a filesystem question; the record must never answer it."""
    monkeypatch.setattr(sc.sys, "platform", "linux")
    install_shortcut(binary)
    sc._backend().location().unlink()
    assert shortcut_status(binary).state is ShortcutState.ABSENT


def test_an_entry_we_did_not_create_is_left_alone(monkeypatch, binary):
    """Scenario: the app was installed by the Windows installer or the macOS disk
    image, which write their own entry at the same place.

    Expected behaviour: removing it would orphan the uninstaller, so it is
    refused and the entry stays.
    """
    monkeypatch.setattr(sc.sys, "platform", "linux")
    install_shortcut(binary)
    sc.clear_record()  # as if someone else's installer had written it

    status = shortcut_status(binary)
    assert status.state is ShortcutState.CURRENT
    assert not status.owned

    result = remove_shortcut()
    assert not result.ok
    assert "installer" in result.message
    assert sc._backend().location().exists()


def test_the_ownership_guard_holds_without_a_resolvable_binary(monkeypatch, binary):
    """Ownership is a fact about the record and the location, not about which
    binary is running. Deriving it from the binary let the guard lapse whenever
    the binary could not be resolved, which is every source checkout."""
    monkeypatch.setattr(sc.sys, "platform", "linux")
    install_shortcut(binary)
    sc.clear_record()
    monkeypatch.delenv("DEEPREEFMAP_MOCK_PYAPP", raising=False)
    monkeypatch.delenv("PYAPP", raising=False)

    result = remove_shortcut()
    assert not result.ok
    assert sc._backend().location().exists()


def test_the_windows_shortcut_name_matches_the_installers(monkeypatch):
    """Both write to %APPDATA%\\...\\Start Menu\\Programs, so a rename on either
    side would silently produce two entries for the same app."""
    from deepreefmap_gui.packaging.shortcuts import _windows

    iss = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "scripts"
        / "installer.iss"
    )
    if not iss.exists():
        pytest.skip("installer.iss not present")
    icons = [ln for ln in iss.read_text().splitlines() if "userprograms" in ln.lower()]
    assert icons, "installer.iss no longer declares a Start Menu icon"
    stem = _windows._LNK_NAME.removesuffix(".lnk")
    assert any(stem in line for line in icons)


# --- macOS ---


@pytest.fixture
def mac(monkeypatch, tmp_path):
    monkeypatch.setattr(sc.sys, "platform", "darwin")
    from deepreefmap_gui.packaging.shortcuts import _macos

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(_macos.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(_macos, "_refresh_launch_services", lambda: None)
    return home / "Applications" / "DeepReefMap.app"


def test_macos_writes_a_bundle_that_execs_the_binary(mac, binary):
    assert install_shortcut(binary).ok

    info = plistlib.loads((mac / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == "DeepReefMap"
    assert info["CFBundleIdentifier"] == "ch.epfl.eceo.deepreefmap-gui"
    assert info["DRMTargetBinary"] == str(binary.resolve())

    from deepreefmap_gui.packaging.shortcuts._macos import _target_from_stub

    stub = mac / "Contents" / "MacOS" / "DeepReefMap"
    assert stub.stat().st_mode & 0o777 == 0o755
    # exec by path, not a copy: binary_swap renames over the binary in place, so
    # a copied executable would keep running the version it was copied from.
    # Read back through the parser, since the apostrophe in the fixture path is
    # shell-escaped in the file.
    assert stub.read_text().count("exec ") == 1
    assert _target_from_stub(stub.read_text()) == binary.resolve()


def test_macos_ships_its_icon(mac, binary):
    install_shortcut(binary)
    icon = mac / "Contents" / "Resources" / "icon.icns"
    assert icon.exists() and icon.stat().st_size > 0
    info = plistlib.loads((mac / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleIconFile"] == "icon"


def test_macos_installs_without_an_icon_rather_than_failing(mac, binary, monkeypatch):
    from deepreefmap_gui.packaging.shortcuts import _macos

    monkeypatch.setattr(_macos.MacShortcuts, "_write_icon", lambda self, bundle: False)
    assert install_shortcut(binary).ok
    info = plistlib.loads((mac / "Contents" / "Info.plist").read_bytes())
    assert "CFBundleIconFile" not in info


def test_macos_reads_its_target_back_and_notices_a_move(mac, binary, tmp_path):
    install_shortcut(binary)
    assert shortcut_status(binary).state is ShortcutState.CURRENT
    moved = tmp_path / "somewhere else.bin"
    binary.rename(moved)
    assert shortcut_status(moved).state is ShortcutState.STALE


def test_macos_removes_the_whole_bundle_idempotently(mac, binary):
    install_shortcut(binary)
    assert remove_shortcut().ok
    assert not mac.exists()
    assert remove_shortcut().ok


def test_running_from_inside_a_bundle_offers_nothing(mac, tmp_path):
    """Installed from the disk image: it is already in Applications."""
    inside = tmp_path / "Volumes" / "DeepReefMap.app" / "Contents" / "MacOS" / "deepreefmap-gui"
    inside.parent.mkdir(parents=True)
    inside.write_text("")
    status = shortcut_status(inside)
    assert status.state is ShortcutState.CURRENT
    assert not status.owned
    assert "disk image" in status.detail


def test_lsregister_absence_is_not_a_failure(monkeypatch, tmp_path):
    """The lsregister path is unversioned private tooling that has moved between
    macOS releases, so a missing one must never fail an install."""
    from deepreefmap_gui.packaging.shortcuts import _macos

    monkeypatch.setattr(_macos, "_LSREGISTER", tmp_path / "definitely-not-here")
    _macos._refresh_launch_services()


# --- Windows ---


class _FakePowerShell:
    """Stands in for the one subprocess seam the Windows backend has."""

    def __init__(self, returncode=0, stdout="", fail=False):
        self.returncode, self.stdout, self.fail = returncode, stdout, fail
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, script, env):
        self.calls.append((script, env))
        if self.fail:
            raise ShortcutError("PowerShell was not found.")
        from pathlib import Path

        if "TargetPath)" in script:
            return subprocess.CompletedProcess([], self.returncode, self.stdout, "")
        if self.returncode == 0:
            lnk = Path(env["DRM_LNK"])
            lnk.parent.mkdir(parents=True, exist_ok=True)
            lnk.write_text("fake lnk")
        return subprocess.CompletedProcess([], self.returncode, "", "constrained language mode")


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(sc.sys, "platform", "win32")
    from deepreefmap_gui.packaging.shortcuts import _windows

    return _windows


def test_windows_writes_a_lnk_and_reads_its_target_back(win, monkeypatch, binary):
    fake = _FakePowerShell(stdout=f"{binary}  \n")  # trailing whitespace on purpose
    monkeypatch.setattr(win, "_run_powershell", fake)

    assert install_shortcut(binary).ok
    assert sc._backend().location().name == "DeepReefMap.lnk"
    assert sc._backend().read_target() == binary


def test_no_path_ever_reaches_the_command_line(monkeypatch, binary):
    """Every path travels in the child's environment instead, which removes the
    whole class of quoting bugs -- including the apostrophe in this fixture."""
    from deepreefmap_gui.packaging.shortcuts import _windows

    seen = {}

    def capture(argv, **kwargs):
        seen["argv"], seen["env"] = argv, kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(_windows.shutil, "which", lambda name: "/fake/powershell")
    monkeypatch.setattr(_windows.subprocess, "run", capture)
    _windows._run_powershell(_windows._WRITE_SCRIPT, {"DRM_LNK": "C:\\a b.lnk"})

    joined = " ".join(seen["argv"])
    assert str(binary) not in joined
    assert "a b.lnk" not in joined
    assert seen["env"]["DRM_LNK"] == "C:\\a b.lnk"


def test_the_powershell_payload_is_bom_free_utf16le(monkeypatch):
    from deepreefmap_gui.packaging.shortcuts import _windows

    seen = {}

    def capture(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(_windows.shutil, "which", lambda name: "/fake/powershell")
    monkeypatch.setattr(_windows.subprocess, "run", capture)
    _windows._run_powershell(_windows._WRITE_SCRIPT, {})

    raw = base64.b64decode(seen["argv"][-1])
    assert raw[:2] != b"\xff\xfe", "a BOM makes -EncodedCommand reject the payload"
    assert raw.decode("utf-16-le") == _windows._WRITE_SCRIPT
    for flag in ("-NoProfile", "-NonInteractive", "-EncodedCommand"):
        assert flag in seen["argv"]


def test_windows_falls_back_to_a_url_when_powershell_is_blocked(win, monkeypatch, binary):
    """Scenario: AppLocker Constrained Language Mode, where New-Object -ComObject
    is refused outright -- a real configuration on managed laptops."""
    monkeypatch.setattr(win, "_run_powershell", _FakePowerShell(fail=True))

    result = install_shortcut(binary)
    assert result.ok
    location = sc._backend().location()
    assert location.name == "DeepReefMap.url"
    assert "[InternetShortcut]" in location.read_text()
    # The apostrophe and spaces survive the URL round trip.
    assert sc._backend().read_target() == binary


def test_a_hung_powershell_still_leaves_a_start_menu_entry(win, monkeypatch, binary):
    """Antivirus scanning can hold a PowerShell cold start for a long time.
    Timing out must fall back like any other failure rather than leaving the
    user with nothing."""

    def timeout(script, env):
        raise subprocess.TimeoutExpired("powershell", 30)

    monkeypatch.setattr(win, "_run_powershell", timeout)
    result = install_shortcut(binary)
    assert result.ok
    assert sc._backend().location().name == "DeepReefMap.url"


def test_windows_reports_when_nothing_can_be_written(win, monkeypatch, binary):
    monkeypatch.setattr(win, "_run_powershell", _FakePowerShell(fail=True))

    def refuse(*args, **kwargs):
        raise OSError("access is denied")

    monkeypatch.setattr(win, "_write_url_shortcut", refuse)
    result = install_shortcut(binary)
    assert not result.ok
    assert "Programs" in result.message


# --- Never raises ---


@pytest.mark.parametrize(
    "boom",
    [PermissionError("denied"), OSError("io"), subprocess.TimeoutExpired("x", 1), RuntimeError("?")],
)
@pytest.mark.parametrize("method", ["install", "remove"])
def test_a_backend_blowing_up_still_returns_a_message(monkeypatch, binary, boom, method):
    monkeypatch.setattr(sc.sys, "platform", "linux")
    backend = sc._backend_for("linux")

    def explode(*args, **kwargs):
        raise boom

    monkeypatch.setattr(type(backend), method, explode)
    monkeypatch.setattr(sc, "_backend", lambda: backend)

    result = install_shortcut(binary) if method == "install" else remove_shortcut()
    assert not result.ok
    assert result.message
    assert result.error


def test_an_unreadable_target_reads_as_unknown_not_a_crash(monkeypatch, binary):
    monkeypatch.setattr(sc.sys, "platform", "linux")
    install_shortcut(binary)
    backend = sc._backend_for("linux")
    monkeypatch.setattr(type(backend), "read_target", lambda self: None)
    monkeypatch.setattr(sc, "_backend", lambda: backend)
    sc.clear_record()

    assert shortcut_status(binary).state is ShortcutState.UNKNOWN


@pytest.mark.skipif(sys.platform != "linux", reason="uses a real read-only directory")
def test_an_unwritable_data_home_is_reported(monkeypatch, binary, tmp_path):
    monkeypatch.setattr(sc.sys, "platform", "linux")
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    monkeypatch.setenv("XDG_DATA_HOME", str(blocked))
    try:
        result = install_shortcut(binary)
        assert not result.ok
        assert result.message
    finally:
        blocked.chmod(0o700)
