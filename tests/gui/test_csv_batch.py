"""Transport controls and cancellation for the CSV-driven batch.

Scenario: a CSV batch runs the same pipeline as a single reconstruction, on the
same thread attribute, but reached the form through its own path.

Expected behaviour: while it runs, start is out of reach so a second pipeline
cannot be launched over it, and the stop button has events to signal. The survey
runner in simple/batch.py already worked this way; this is the advanced-mode
half catching up.
"""

from __future__ import annotations

import threading

import pytest


@pytest.fixture
def batch_csv(tmp_path):
    """A three-row CSV whose videos exist, so no row is skipped as missing."""
    videos = []
    for name in ("alpha", "beta", "gamma"):
        video = tmp_path / f"{name}.mp4"
        video.write_bytes(b"not really a video")
        videos.append(video)
    csv_path = tmp_path / "jobs.csv"
    csv_path.write_text(
        "videos,timestamps,transect_length,crop_width\n"
        + "".join(f"{v},,10,2\n" for v in videos)
    )
    return csv_path


@pytest.fixture
def advanced_window(window, tmp_path):
    window._mode_buttons["advanced"].click()
    window._out_root_input.setText(str(tmp_path / "out"))
    return window


class _BlockingPipeline:
    """Stands in for instrumented_reconstruction, holding each job open.

    Records the kwargs it was called with so the test can assert the events
    reached the library, and honours them the way the real pipeline does.
    """

    def __init__(self):
        self.calls: list[dict] = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.entered.set()
        self.release.wait(timeout=5)
        cancel = kwargs.get("cancel_event")
        if cancel is not None and cancel.is_set():
            from deepreefmap.pipeline.artifacts import ReconstructionCancelled

            raise ReconstructionCancelled()


@pytest.fixture
def pipeline(monkeypatch):
    stub = _BlockingPipeline()
    monkeypatch.setattr(
        "deepreefmap_gui.profiling.instrumentation.instrumented_reconstruction", stub
    )
    yield stub
    stub.release.set()


def _start_batch(window, csv_path, monkeypatch):
    monkeypatch.setattr(
        "PySide6.QtWidgets.QFileDialog.getOpenFileName",
        lambda *a, **k: (str(csv_path), ""),
    )
    window._on_batch_clicked()


def test_a_running_batch_puts_start_out_of_reach(
    advanced_window, batch_csv, pipeline, monkeypatch
):
    """Clicking start mid-batch launched a second pipeline over the first,
    sharing the viewer and overwriting _pipeline_thread."""
    window = advanced_window
    assert not window._start_btn.isHidden()

    _start_batch(window, batch_csv, monkeypatch)
    assert pipeline.entered.wait(timeout=5)

    assert window._start_btn.isHidden()
    assert window._pause_btn.isVisibleTo(window._pause_btn.parentWidget())

    pipeline.release.set()
    window._pipeline_thread.join(timeout=5)


def test_stopping_a_batch_ends_it_before_the_next_job(
    advanced_window, batch_csv, pipeline, monkeypatch
):
    window = advanced_window
    _start_batch(window, batch_csv, monkeypatch)
    assert pipeline.entered.wait(timeout=5)

    window._on_stop_clicked()
    pipeline.release.set()
    window._pipeline_thread.join(timeout=5)

    assert not window._pipeline_thread.is_alive()
    assert len(pipeline.calls) == 1, "the remaining jobs ran after the stop"


def test_each_job_is_handed_the_cancel_and_pause_events(
    advanced_window, batch_csv, pipeline, monkeypatch
):
    window = advanced_window
    pipeline.release.set()

    _start_batch(window, batch_csv, monkeypatch)
    window._pipeline_thread.join(timeout=5)

    assert len(pipeline.calls) == 3
    for call in pipeline.calls:
        assert call["cancel_event"] is window._cancel_event
        assert call["pause_event"] is window._pause_event


def test_a_paused_batch_does_not_start_the_next_job(
    advanced_window, batch_csv, pipeline, monkeypatch
):
    window = advanced_window
    _start_batch(window, batch_csv, monkeypatch)
    assert pipeline.entered.wait(timeout=5)

    window._pause_btn.setChecked(True)
    window._on_pause_toggled(True)
    pipeline.entered.clear()
    pipeline.release.set()

    assert not pipeline.entered.wait(timeout=0.5), "the next job started while paused"

    window._on_pause_toggled(False)
    window._pipeline_thread.join(timeout=5)
    assert len(pipeline.calls) == 3


def test_a_stopped_batch_is_not_reported_as_failures(advanced_window):
    """Two of ten finishing reads identically whether the user stopped it or
    eight jobs blew up, so the status has to distinguish them."""
    window = advanced_window
    window._cancel_event = threading.Event()
    window._cancel_event.set()

    window._on_batch_done(2, 10, "")

    assert "stopped" in window._status_label.text().lower()


def test_finishing_a_batch_brings_start_back(advanced_window):
    window = advanced_window
    window._begin_run_controls()
    assert window._start_btn.isHidden()

    window._on_batch_done(3, 3, "")

    assert not window._start_btn.isHidden()
    assert window._batch_btn.isEnabled()
