"""The in-app updater's progress dialog.

`_worker_main` is the only entry point the Install button reaches, and it is
what decides between a real swap, the mock script, and the two failure paths.
Calling `_run_real` directly (as the wiring test below does) skips that
decision, so the dispatch is covered here on its own.

A failure that never reaches `_on_done` is the shape that matters: the Close
button starts disabled, so a swallowed exception leaves a modal dialog the user
cannot dismiss.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deepreefmap_gui.packaging.binary_swap import BinarySwapError
from deepreefmap_gui.update import dialog as update_dialog


@pytest.fixture
def dialog(qapp, tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPREEFMAP_MOCK_PYAPP", raising=False)
    binary = tmp_path / "deepreefmap-gui"
    binary.write_bytes(b"OLD")
    return update_dialog.UpdateProgressDialog(
        target_version="1.2.0",
        release={"tag_name": "v1.2.0", "assets": []},
        binary_path=binary,
    )


def _outcomes(dialog) -> list[tuple[bool, str]]:
    seen: list[tuple[bool, str]] = []
    dialog._sig_done.connect(lambda ok, msg: seen.append((ok, msg)))
    return seen


def _lines(dialog) -> list[str]:
    seen: list[str] = []
    dialog._sig_line.connect(seen.append)
    return seen


# --- what the worker thread dispatches to -------------------------------


def test_the_worker_runs_the_real_update(qapp, tmp_path, monkeypatch) -> None:
    """Guarantee the Install button's worker is wired to perform_update(), and that
    it threads current_version through -- that argument is what retains the outgoing
    binary for rollback, so a dropped one silently breaks rollback (see the e2e)."""
    monkeypatch.delenv("DEEPREEFMAP_MOCK_PYAPP", raising=False)
    binary = tmp_path / "deepreefmap-gui"
    binary.write_bytes(b"OLD")
    dialog = update_dialog.UpdateProgressDialog(
        target_version="1.2.0",
        release={"tag_name": "v1.2.0", "assets": []},
        binary_path=binary,
        current_version="1.1.0",
    )
    calls: dict[str, object] = {}

    def fake_perform_update(
        release, binary_path, target_version, current_version=None, progress_cb=None, line_cb=None
    ):
        calls["args"] = (release, binary_path, target_version)
        calls["current_version"] = current_version
        if line_cb is not None:
            line_cb("working")

    monkeypatch.setattr(update_dialog, "perform_update", fake_perform_update)
    outcomes = _outcomes(dialog)

    dialog._worker_main()

    assert calls["args"][1] == dialog._binary_path
    assert calls["args"][2] == "1.2.0"
    assert calls["current_version"] == "1.1.0"
    assert outcomes == [(True, "Installed 1.2.0. Restart to apply.")]


def test_a_rollback_uses_the_kept_binary_and_never_downloads(qapp, tmp_path, monkeypatch) -> None:
    """The rollback path swaps in a locally kept binary; it must not call
    perform_update (which downloads)."""
    binary = tmp_path / "deepreefmap-gui"
    binary.write_bytes(b"NEW")
    d = update_dialog.UpdateProgressDialog(
        target_version="1.0.0",
        release={"tag_name": "v1.0.0", "assets": []},
        binary_path=binary,
        current_version="1.2.0",
        rollback=True,
    )
    monkeypatch.setattr(
        update_dialog, "perform_update", lambda *a, **k: pytest.fail("rollback must not download")
    )
    rolled: dict[str, object] = {}

    def fake_rollback(binary_path, target_version, current_version=None, line_cb=None):
        rolled["to"] = target_version
        rolled["from"] = current_version

    monkeypatch.setattr(update_dialog, "perform_rollback", fake_rollback)
    outcomes = _outcomes(d)

    d._worker_main()

    assert rolled == {"to": "1.0.0", "from": "1.2.0"}
    assert outcomes == [(True, "Rolled back to 1.0.0. Restart to apply.")]


def test_the_mock_environment_never_touches_the_real_binary(dialog, monkeypatch) -> None:
    """DEEPREEFMAP_MOCK_PYAPP is how the update flow is demonstrated and tested
    end to end; a leak into perform_update would overwrite a developer's binary."""
    monkeypatch.setenv("DEEPREEFMAP_MOCK_PYAPP", "1")
    monkeypatch.setattr(update_dialog.time, "sleep", lambda _s: None)

    def fail(*_a, **_k):
        raise AssertionError("perform_update called under DEEPREEFMAP_MOCK_PYAPP")

    monkeypatch.setattr(update_dialog, "perform_update", fail)
    outcomes, lines = _outcomes(dialog), _lines(dialog)

    dialog._worker_main()

    assert outcomes == [(True, "Mock install of 1.2.0 complete.")]
    assert any("simulated" in line for line in lines)
    assert dialog._binary_path.read_bytes() == b"OLD"


def test_a_failed_swap_is_reported_with_its_reason(dialog, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise BinarySwapError("no asset for linux-x64")

    monkeypatch.setattr(update_dialog, "perform_update", boom)
    outcomes, lines = _outcomes(dialog), _lines(dialog)

    dialog._worker_main()

    assert outcomes == [(False, "no asset for linux-x64")]
    assert lines == ["Error: no asset for linux-x64"]


def test_an_unexpected_failure_still_lets_the_dialog_be_closed(dialog, monkeypatch) -> None:
    """Anything perform_update did not anticipate -- a disk error, a bad
    response -- must still land on _on_done, which is what enables Close."""

    def boom(*_a, **_k):
        raise OSError("No space left on device")

    monkeypatch.setattr(update_dialog, "perform_update", boom)
    outcomes = _outcomes(dialog)

    dialog._worker_main()

    assert len(outcomes) == 1
    success, message = outcomes[0]
    assert success is False
    assert "No space left on device" in message
    assert dialog._close_btn.isEnabled()


# --- what the user sees -------------------------------------------------


def test_success_offers_the_relaunch_and_fills_the_bar(dialog) -> None:
    dialog._on_done(True, "Installed 1.2.0. Click Relaunch.")

    assert not dialog._relaunch_btn.isHidden()
    assert dialog._close_btn.isEnabled()
    assert dialog._progress.value() == dialog._progress.maximum()
    assert dialog._status_label.text() == "Installed 1.2.0. Click Relaunch."


def test_failure_leaves_the_bar_empty_and_hides_the_relaunch(dialog) -> None:
    """Relaunch after a failed swap would start a binary that was never replaced."""
    dialog._on_done(False, "download interrupted")

    assert dialog._relaunch_btn.isHidden()
    assert dialog._close_btn.isEnabled()
    assert dialog._progress.value() == 0
    assert dialog._status_label.text() == "download interrupted"


def test_the_bar_switches_from_indeterminate_once_a_total_is_known(dialog) -> None:
    assert dialog._progress.maximum() == 0  # spinning: no content-length yet

    dialog._on_progress(30, 100)

    assert (dialog._progress.maximum(), dialog._progress.value()) == (100, 30)


def test_a_download_longer_than_advertised_does_not_overrun_the_bar(dialog) -> None:
    """Content-length is a claim by the server, not a guarantee."""
    dialog._on_progress(10, 100)
    dialog._on_progress(140, 100)

    assert dialog._progress.value() == 100


def test_the_total_is_taken_from_the_first_report_only(dialog) -> None:
    """perform_update reports running totals; re-ranging mid-download would make
    the bar jump backwards."""
    dialog._on_progress(50, 100)
    dialog._on_progress(60, 999)

    assert dialog._progress.maximum() == 100


def test_progress_before_any_total_leaves_the_bar_spinning(dialog) -> None:
    dialog._on_progress(5, 0)

    assert dialog._progress.maximum() == 0


def test_each_line_is_logged_and_the_latest_becomes_the_status(dialog) -> None:
    dialog._on_line("Downloading deepreefmap-gui-linux-x64…")
    dialog._on_line("Replacing binary…")

    assert dialog._status_label.text() == "Replacing binary…"
    assert "Downloading deepreefmap-gui-linux-x64…" in dialog._log.toPlainText()


def test_a_blank_line_does_not_wipe_the_status(dialog) -> None:
    dialog._on_line("Verifying download…")
    dialog._on_line("")

    assert dialog._status_label.text() == "Verifying download…"


# --- relaunch -----------------------------------------------------------


def test_relaunch_starts_the_new_binary_and_quits(dialog, monkeypatch) -> None:
    spawned: list[list[str]] = []
    monkeypatch.setattr(update_dialog.subprocess, "Popen", spawned.append)
    quits: list[bool] = []
    monkeypatch.setattr(
        update_dialog.QApplication, "instance", staticmethod(type("A", (), {"quit": lambda _s: quits.append(True)}))
    )

    dialog._on_relaunch()

    assert spawned == [[str(dialog._binary_path)]]
    assert quits == [True]


def test_relaunch_under_the_mock_environment_spawns_nothing(dialog, monkeypatch) -> None:
    monkeypatch.setenv("DEEPREEFMAP_MOCK_PYAPP", "1")

    def fail(_cmd):
        raise AssertionError("mock relaunch spawned a process")

    monkeypatch.setattr(update_dialog.subprocess, "Popen", fail)
    monkeypatch.setattr(update_dialog.QApplication, "instance", staticmethod(lambda: None))

    dialog._on_relaunch()


def test_a_binary_that_will_not_start_does_not_take_the_dialog_down(
    dialog, monkeypatch
) -> None:
    """The swap already succeeded at this point, so the install must not be
    reported as failed because the relaunch could not spawn."""

    def boom(_cmd):
        raise OSError("Exec format error")

    monkeypatch.setattr(update_dialog.subprocess, "Popen", boom)
    monkeypatch.setattr(update_dialog.QApplication, "instance", staticmethod(lambda: None))

    dialog._on_relaunch()

    assert dialog.result() == update_dialog.QDialog.DialogCode.Accepted


def test_the_binary_path_is_normalised(qapp, tmp_path) -> None:
    """app.py hands over whatever sys.executable reported, string or Path."""
    d = update_dialog.UpdateProgressDialog(
        target_version="1.2.0",
        release={},
        binary_path=str(tmp_path / "bin"),
    )

    assert d._binary_path == Path(tmp_path / "bin")
