"""Which cached-run load gets to apply when two overlap.

Scenario: a run is opened, then a second before the first finishes. Order of
completion does not follow order of request -- a run taking the library slow path
runs for minutes where one with a scene file returns in well under a second.

Expected behaviour: only the load the window is currently waiting on applies,
and a superseded one releases what it opened without disturbing the bars the
live load is driving.
"""

from __future__ import annotations

import pytest


class _FakeAccessor:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeResult:
    """Enough of a GuiLoadedRun for _apply_loaded_run's early exits."""

    def __init__(self):
        self.scene_accessor = _FakeAccessor()


@pytest.fixture
def loading_window(window, monkeypatch):
    """A window whose loads never start a thread, so generations are explicit."""
    monkeypatch.setattr(window, "_load_run_worker", lambda *a, **k: None)
    return window


def _generation_of(window, tmp_path):
    window._auto_load_run(tmp_path)
    return window._load_generation


def test_each_load_takes_a_new_generation(loading_window, tmp_path):
    first = _generation_of(loading_window, tmp_path)
    second = _generation_of(loading_window, tmp_path)

    assert second != first


def test_a_superseded_load_does_not_apply(loading_window, tmp_path, monkeypatch):
    window = loading_window
    applied: list[str] = []
    monkeypatch.setattr(window, "_reset_progress_bars", lambda: applied.append("reset"))

    stale = _generation_of(window, tmp_path)
    _generation_of(window, tmp_path)
    result = _FakeResult()

    window._apply_loaded_run(result, str(tmp_path), "", stale)

    assert result.scene_accessor.closed, "the superseded load's handles leak"
    assert applied == [], "resetting here blanks the bars of the load still running"
    # Without the guard the stale result reaches the apply path, where the first
    # thing it does is hand its accessor to the window.
    assert getattr(window, "_scene_accessor", None) is None


def test_the_current_load_still_applies(loading_window, tmp_path, monkeypatch):
    """The guard must not reject the load the window is actually waiting on."""
    window = loading_window
    reached: list[str] = []
    monkeypatch.setattr(window, "_reset_progress_bars", lambda: reached.append("reset"))

    generation = _generation_of(window, tmp_path)
    window._load_cancelled = True

    window._apply_loaded_run(_FakeResult(), str(tmp_path), "", generation)

    assert reached == ["reset"], "the live load was dropped as if superseded"


def test_a_cancelled_load_stays_cancelled_when_another_starts(loading_window, tmp_path):
    """_load_cancelled is reset by the next _auto_load_run, so the flag alone
    cannot keep a cancelled result out once a second load begins."""
    window = loading_window
    cancelled = _generation_of(window, tmp_path)
    window._cancel_load()
    assert window._load_cancelled

    _generation_of(window, tmp_path)
    assert not window._load_cancelled

    result = _FakeResult()
    window._apply_loaded_run(result, str(tmp_path), "", cancelled)

    assert result.scene_accessor.closed
