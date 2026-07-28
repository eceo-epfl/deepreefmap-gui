"""Signal lifetime of the threaded QC-video export.

Scenario: the export renders on a worker thread and reports back through two
window signals, wired to handlers that close over that export's QProgressDialog
and destination path.

Expected behaviour: each export owns the signals for its duration and releases
them afterwards, so exporting twice neither accumulates handlers nor drives the
earlier export's dialog.
"""

from __future__ import annotations

import time
import warnings
from pathlib import Path

import pytest
from PySide6.QtWidgets import QFileDialog

PROGRESS_SIGNATURE = "_sig_qc_render_progress(int,int)"
DONE_SIGNATURE = "_sig_qc_render_done(bool,QString)"


def _fake_render(run_dir, *, transect_length_m=None, crop_width_m=None, progress_callback=None):
    for step in range(1, 4):
        if progress_callback is not None:
            progress_callback(step, 3)
    return Path(run_dir) / "videos" / "qc_render.mp4"


@pytest.fixture
def qc_run(window, tmp_path, monkeypatch) -> Path:
    """A window with a loaded run whose QC render is instant and offline."""
    run_dir = tmp_path / "qc_run"
    (run_dir / "videos").mkdir(parents=True)
    (run_dir / "videos" / "qc_render.mp4").write_bytes(b"")
    window._active_run_dir = run_dir
    monkeypatch.setattr("deepreefmap.postproc.reports.render_offline_video", _fake_render)
    return run_dir


def _receiver_count(window, signature: str) -> int:
    """Connections on a window signal. QObject.receivers takes the SIGNAL() string
    form, whose leading '2' is Qt's marker for a signal rather than a slot."""
    return int(window.receivers(f"2{signature}"))


def _start_export(window, target: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), ""))
    )
    window._on_export_qc_video()


def _drain_until_saved(window, qapp, target: Path) -> None:
    """Pump the event loop until the worker's queued done signal is delivered."""
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and str(target) not in window._status_label.text():
        qapp.processEvents()
        time.sleep(0.005)


def _export(window, qapp, target: Path, monkeypatch) -> None:
    _start_export(window, target, monkeypatch)
    _drain_until_saved(window, qapp, target)


def test_repeated_qc_export_keeps_one_handler_per_signal(window, qapp, qc_run, monkeypatch) -> None:
    first = qc_run / "first.mp4"
    second = qc_run / "second.mp4"
    _export(window, qapp, first, monkeypatch)

    _start_export(window, second, monkeypatch)
    # Counted before the loop is pumped: the worker's done signal is queued, so
    # the second export's handlers are still installed at this point.
    in_flight = (
        _receiver_count(window, PROGRESS_SIGNATURE),
        _receiver_count(window, DONE_SIGNATURE),
    )
    _drain_until_saved(window, qapp, second)

    assert in_flight == (1, 1)
    assert window._status_label.text() == f"QC video saved to {second}"


def test_finished_qc_export_releases_its_handlers(window, qapp, qc_run, monkeypatch) -> None:
    _export(window, qapp, qc_run / "only.mp4", monkeypatch)

    assert window._qc_render_handlers is None
    assert _receiver_count(window, PROGRESS_SIGNATURE) == 0
    assert _receiver_count(window, DONE_SIGNATURE) == 0


def test_disconnecting_qc_handlers_is_a_no_op_when_none_are_installed(window) -> None:
    """Teardown calls this unconditionally, and PySide6 raises on a disconnect
    that matches nothing once warnings are errors."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        window._disconnect_qc_render_handlers()
        window._disconnect_qc_render_handlers()

    assert window._qc_render_handlers is None
