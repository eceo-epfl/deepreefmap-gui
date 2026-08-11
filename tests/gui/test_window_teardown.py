"""What closing the main window releases.

Qt destroys child QObjects with their parent, so the timers and widgets look
after themselves. Three handles do not: the frame accessor, the survey database
and the run log file are held by plain attributes.

Only two of those hold an OS resource today -- the SQLite connection and the log
file. The accessor is closed on the same path because close() is part of the
FrameAccessor protocol, and the window is the only thing that would release an
implementation that did hold a handle. FakeAccessor stands in for exactly that:
RunDirFrameAccessor.close() is a no-op, so a real one would assert nothing.

The worker threads are deliberately signalled rather than joined -- see the
comment in closeEvent -- so these assert on the cancel events, not on the
threads having stopped.
"""

from __future__ import annotations

import threading

import pytest
from _factories import FakeAccessor
from PySide6.QtGui import QCloseEvent


class _FakeStore:
    def __init__(self):
        self.closed = False
        self.path = None

    def close(self):
        self.closed = True


def _close(window):
    event = QCloseEvent()
    window.closeEvent(event)
    return event


@pytest.fixture
def running_pipeline(window):
    """A window whose pipeline thread is genuinely alive, released on teardown."""
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    thread.start()
    window._pipeline_thread = thread
    yield window
    release.set()
    thread.join()


def test_closing_releases_the_frame_accessor_and_the_survey_store(window):
    accessor, store = FakeAccessor(), _FakeStore()
    window._scene_accessor = accessor
    window._survey_store_obj = store

    _close(window)

    assert accessor.closed, "an accessor holding a handle would never be released"
    assert store.closed, "the SQLite connection outlives the window otherwise"
    assert window._scene_accessor is None
    assert window._survey_store_obj is None


def test_closing_closes_the_run_log_file(window, tmp_path):
    from deepreefmap_gui.system.log_view import open_run_log_file

    window._run_log_file_handler = open_run_log_file(tmp_path)
    handler = window._run_log_file_handler

    _close(window)

    assert handler.stream is None or handler.stream.closed
    assert window._run_log_file_handler is None


def test_closing_stops_the_timers_this_window_owns(window):
    window._playback_timer.start(10)
    window._data_refresh_timer.start()

    _close(window)

    assert not window._playback_timer.isActive()
    assert not window._data_refresh_timer.isActive()


def test_closing_with_nothing_open_is_harmless(window):
    """Most closes happen with no run loaded and no survey store built."""
    _close(window)


def test_closing_signals_the_workers_to_stop(running_pipeline, monkeypatch):
    """A daemon thread parked on the pause gate cannot see a cancel until it is
    released, so the pause event is set rather than cleared."""
    from PySide6.QtWidgets import QMessageBox

    window = running_pipeline
    window._cancel_event = threading.Event()
    window._pause_event = threading.Event()
    window._pause_event.clear()
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )

    _close(window)

    assert window._cancel_event.is_set()
    assert window._pause_event.is_set()


@pytest.mark.parametrize(
    ("answer", "should_close"),
    [("Yes", True), ("No", False)],
)
def test_quitting_mid_run_asks_first(running_pipeline, monkeypatch, answer, should_close):
    from PySide6.QtWidgets import QMessageBox

    window = running_pipeline
    accessor = FakeAccessor()
    window._scene_accessor = accessor
    asked: list[str] = []

    def fake_question(_parent, title, *_a, **_k):
        asked.append(title)
        return getattr(QMessageBox.StandardButton, answer)

    monkeypatch.setattr(QMessageBox, "question", fake_question)

    event = _close(window)

    assert asked, "quitting mid-run must not be silent"
    assert event.isAccepted() is should_close
    assert accessor.closed is should_close, "a refused quit must leave the window usable"


def test_closing_with_no_run_in_flight_does_not_ask(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    def fail(*_a, **_k):
        raise AssertionError("prompted on an idle close")

    monkeypatch.setattr(QMessageBox, "question", fail)

    _close(window)


def test_a_finished_pipeline_thread_does_not_count_as_in_flight(window):
    thread = threading.Thread(target=lambda: None)
    thread.start()
    thread.join()
    window._pipeline_thread = thread

    assert not window._run_in_flight()


def test_a_survey_batch_is_seen_as_a_run_in_flight(running_pipeline):
    """The survey worker runs on _pipeline_thread like an ordinary run, so one
    predicate covers both; a separate _survey_worker_running check would add
    nothing."""
    running_pipeline._survey_worker_running = True

    assert running_pipeline._run_in_flight()


def test_closing_drops_the_qc_render_handlers(window, tmp_path):
    """They close over a QProgressDialog parented to this window, so a render
    still in flight would drive a widget Qt is about to destroy."""
    calls: list[tuple[int, int]] = []

    def on_progress(cur, total):
        calls.append((cur, total))

    def on_done(_ok, _error):
        pass

    window._sig_qc_render_progress.connect(on_progress)
    window._sig_qc_render_done.connect(on_done)
    window._qc_render_handlers = (on_progress, on_done)

    _close(window)

    window._sig_qc_render_progress.emit(1, 10)
    assert calls == []
    assert window._qc_render_handlers is None


def test_closing_without_a_qc_export_does_not_warn(window, recwarn):
    """PySide6 warns on a disconnect matching nothing, and the suite turns
    warnings into errors."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _close(window)

    assert not recwarn
