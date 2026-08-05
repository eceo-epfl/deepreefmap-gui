"""What the Run step says when this laptop cannot process the queue.

Scenario: a CPU-only machine with a graphics-card-only method, or models that
never made it onto the disk.

Expected behaviour: the batch blocks with a plain reason and a button that goes
where the blocker is fixed, rather than prose describing the fix and leaving the
diver to find the place it happens.
"""

from __future__ import annotations

import pytest
from _factories import make_transect
from _qt_wait import wait_until

from deepreefmap_gui.simple.section_state import BLOCKED, OK


@pytest.fixture
def queued_window(window, tmp_path, monkeypatch):
    """One transect, one queued pass, nothing else blocking."""
    window._survey_store().add_transect(make_transect())
    window._refresh_survey_batch_tab()
    monkeypatch.setattr(window, "_survey_missing_models", list)

    video = tmp_path / "GX010001.MP4"
    video.write_bytes(b"x" * 4096)
    monkeypatch.setattr("deepreefmap_gui.simple.batch._probe_video", lambda _p: (60.0, 30.0))
    monkeypatch.setattr(
        "deepreefmap_gui.simple.batch.QFileDialog.getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(video)], "")),
    )
    window._on_survey_add_videos()
    # Adding videos probes them on a worker thread and appends the row from a
    # queued signal, so wait for it rather than racing the worker (and so the
    # signal lands while the window is still alive).
    wait_until(lambda: len(window._survey_rows) == 1)
    return window


def strip_showing(window):
    # isHidden(), not isVisibleTo(): the Run page is not the current stack page
    # in most of these tests, which would hide the strip either way.
    return not window._survey_not_ready.isHidden()


def strip_reason(window):
    return window._survey_not_ready._reason.text()


def strip_action(window):
    return window._survey_not_ready._action.text()


def test_a_ready_queue_shows_no_strip(queued_window):
    assert queued_window._survey_start_btn.isEnabled()
    assert not strip_showing(queued_window)


def test_a_gpu_only_method_blocks_the_batch(queued_window, monkeypatch):
    window = queued_window
    monkeypatch.setattr(window, "_gpu_only_mapper", lambda: "loger_star")
    window._recompute_survey_start()

    assert window._survey_gate.state == BLOCKED
    assert not window._survey_start_btn.isEnabled()
    assert "graphics card" in strip_reason(window)


def test_the_strip_goes_to_this_machine(queued_window, monkeypatch):
    window = queued_window
    monkeypatch.setattr(window, "_gpu_only_mapper", lambda: "loger")
    window._recompute_survey_start()

    assert strip_action(window) == "Open Setup"
    window._survey_not_ready._action.click()
    assert window._current_section() == "machine"


def test_missing_models_send_the_diver_to_this_machine(queued_window, monkeypatch):
    window = queued_window
    monkeypatch.setattr(window, "_survey_missing_models", lambda: ["coralscapes-vit-b-dpt"])
    window._recompute_survey_start()

    assert "coralscapes-vit-b-dpt" in strip_reason(window)
    assert strip_action(window) == "Open Setup"


def test_a_broken_preset_sends_the_diver_to_the_settings(queued_window, monkeypatch):
    window = queued_window
    window._survey_preset = None
    window._recompute_survey_start()

    assert strip_action(window) == "Edit settings…"
    opened = []
    monkeypatch.setattr(window, "_on_edit_run_settings", lambda: opened.append(True))
    window._survey_not_ready._action.click()
    assert opened == [True]


def test_a_skipped_transect_neither_blocks_nor_raises_a_strip(queued_window):
    """Scenario: a clip is queued with the transect deliberately skipped.

    Expected behaviour: it processes. The strip is for a blocker that has to be
    fixed somewhere else, and this is neither a blocker nor elsewhere.
    """
    window = queued_window
    window._survey_rows[0].transect_id = None
    window._recompute_survey_start()

    assert window._survey_gate.state == OK
    assert window._survey_start_btn.isEnabled()
    assert not strip_showing(window)
    assert "without a transect" in window._survey_gate.count


def test_clearing_the_blocker_hides_the_strip(queued_window, monkeypatch):
    window = queued_window
    monkeypatch.setattr(window, "_gpu_only_mapper", lambda: "loger")
    window._recompute_survey_start()
    assert strip_showing(window)

    monkeypatch.setattr(window, "_gpu_only_mapper", str)
    window._recompute_survey_start()
    assert not strip_showing(window)


def test_the_real_probe_reaches_the_gate(queued_window, monkeypatch):
    """The other blocker tests stub _gpu_only_mapper, so one of them has to drive
    the card probe itself and confirm the answer arrives at the Run button."""
    window = queued_window
    monkeypatch.setattr(window, "_gpu_available", lambda: False)
    window._map_combo.setCurrentText("loger")
    assert window._gpu_only_mapper() == "loger"

    window._recompute_survey_start()
    assert window._survey_gate.state == BLOCKED
    assert not window._survey_start_btn.isEnabled()
    assert "graphics card" in strip_reason(window)
