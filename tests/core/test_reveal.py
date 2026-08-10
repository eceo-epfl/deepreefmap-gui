"""Revealing a file in the file manager, on all three platforms.

Every branch is exercised on whatever host runs the suite: the platform is read
from `sys.platform` at call time, and the only seam is `subprocess.run`.
"""

from __future__ import annotations

import subprocess

import pytest

from deepreefmap_gui.core import reveal
from deepreefmap_gui.core.reveal import reveal_in_file_manager

# The real helpers are the subject here, so keep the conftest stub off them.
pytestmark = pytest.mark.real_reveal


class _FakeRun:
    """Stands in for `subprocess.run`, answering per command name."""

    def __init__(self, **returncodes: int):
        self.returncodes = returncodes
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        code = self.returncodes.get(argv[0], 0)
        return subprocess.CompletedProcess(argv, code, b"", b"")

    def argv_for(self, program: str) -> list[str] | None:
        for call in self.calls:
            if call[0] == program:
                return call
        return None

    @property
    def programs(self) -> list[str]:
        return [call[0] for call in self.calls]


@pytest.fixture
def target(tmp_path):
    """A path carrying the characters that break naive quoting."""
    path = tmp_path / "o'brien deep $reef.mp4"
    path.write_text("")
    return path.resolve()


@pytest.fixture
def fake(monkeypatch):
    def install(**returncodes):
        runner = _FakeRun(**returncodes)
        monkeypatch.setattr(reveal.subprocess, "run", runner)
        return runner

    return install


@pytest.fixture
def platform(monkeypatch):
    return lambda name: monkeypatch.setattr(reveal.sys, "platform", name)


def test_windows_asks_explorer_to_select_the_file(platform, fake, target):
    platform("win32")
    runner = fake()

    assert reveal_in_file_manager(target)
    assert runner.argv_for("explorer") == ["explorer", f"/select,{target}"]


def test_windows_ignores_the_exit_code(platform, fake, target):
    """Explorer exits non-zero on success, so a returncode check would reject
    the common case."""
    platform("win32")
    fake(explorer=1)

    assert reveal_in_file_manager(target)


def test_macos_asks_finder_to_reveal_the_file(platform, fake, target):
    platform("darwin")
    runner = fake()

    assert reveal_in_file_manager(target)
    assert runner.argv_for("open") == ["open", "-R", str(target)]


def test_macos_reports_a_failed_open(platform, fake, target):
    platform("darwin")
    fake(open=1)

    assert not reveal_in_file_manager(target)


def test_linux_selects_the_file_over_dbus_first(platform, fake, target):
    platform("linux")
    runner = fake()

    assert reveal_in_file_manager(target)
    assert runner.programs == ["gdbus"]

    argv = runner.argv_for("gdbus")
    assert "org.freedesktop.FileManager1.ShowItems" in argv
    # Percent-encoded, so the apostrophe cannot close the GVariant literal.
    assert argv[-2] == f"['{target.as_uri()}']"
    assert "'" not in target.as_uri()


def test_linux_falls_back_to_opening_the_parent_folder(platform, fake, target):
    platform("linux")
    runner = fake(gdbus=1)

    assert reveal_in_file_manager(target)
    assert runner.programs == ["gdbus", "xdg-open"]
    assert runner.argv_for("xdg-open") == ["xdg-open", str(target.parent)]


def test_linux_falls_back_to_qt_when_no_command_works(platform, fake, target, monkeypatch):
    platform("linux")
    runner = fake(gdbus=1, **{"xdg-open": 1})
    opened = []
    monkeypatch.setattr(reveal, "_qt_open", lambda parent: bool(opened.append(parent)) or True)

    assert reveal_in_file_manager(target)
    assert runner.programs == ["gdbus", "xdg-open"]
    assert opened == [target.parent]


def test_linux_reports_failure_when_every_strategy_fails(platform, fake, target, monkeypatch):
    platform("linux")
    fake(gdbus=1, **{"xdg-open": 1})
    monkeypatch.setattr(reveal, "_qt_open", lambda parent: False)

    assert not reveal_in_file_manager(target)


@pytest.mark.parametrize("name", ["win32", "darwin", "linux"])
@pytest.mark.parametrize(
    "boom",
    [
        FileNotFoundError("no such command"),
        PermissionError("denied"),
        subprocess.TimeoutExpired("gdbus", 2.0),
        subprocess.SubprocessError("broken"),
    ],
)
def test_a_broken_file_manager_returns_false_rather_than_raising(
    platform, monkeypatch, target, name, boom
):
    platform(name)

    def explode(*args, **kwargs):
        raise boom

    monkeypatch.setattr(reveal.subprocess, "run", explode)
    monkeypatch.setattr(reveal, "_qt_open", lambda parent: False)

    assert reveal_in_file_manager(target) is False


def test_every_subprocess_call_is_given_a_timeout(platform, monkeypatch, target):
    """A wedged file manager must not hang the GUI thread."""
    platform("linux")
    seen: list[float | None] = []

    def capture(argv, **kwargs):
        seen.append(kwargs.get("timeout"))
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    monkeypatch.setattr(reveal.subprocess, "run", capture)
    monkeypatch.setattr(reveal, "_qt_open", lambda parent: False)
    reveal_in_file_manager(target)

    assert seen and all(value == reveal._TIMEOUT_S for value in seen)
