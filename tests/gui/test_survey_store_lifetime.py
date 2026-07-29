"""When the survey store may be swapped for a different output root.

Scenario: the output root changes while a batch is running. The worker was handed
a SurveyStore and writes each pass status through it.

Expected behaviour: the store the batch is using survives until the batch ends.
SurveyStore.close() only closes the calling thread's connection, so a swap on the
GUI thread does not stop the worker writing -- it just stops the window reading
what the worker writes.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def rooted(simple_window, tmp_path):
    window = simple_window
    window._out_root_input.setText(str(tmp_path / "first"))
    return window, tmp_path


def test_a_new_root_swaps_the_store_when_nothing_is_running(rooted):
    window, tmp_path = rooted
    first = window._survey_store()

    window._out_root_input.setText(str(tmp_path / "second"))
    second = window._survey_store()

    assert second is not first
    assert second.path != first.path


def test_a_running_batch_keeps_the_store_it_was_handed(rooted, caplog):
    window, tmp_path = rooted
    running = window._survey_store()
    window._survey_worker_running = True

    window._out_root_input.setText(str(tmp_path / "second"))
    with caplog.at_level("WARNING"):
        during = window._survey_store()

    assert during is running, "the batch's pass statuses go to an orphaned database"
    assert "while a batch is running" in caplog.text


def test_the_swap_happens_once_the_batch_ends(rooted):
    window, tmp_path = rooted
    running = window._survey_store()
    window._survey_worker_running = True
    window._out_root_input.setText(str(tmp_path / "second"))
    window._survey_store()

    window._survey_worker_running = False
    after = window._survey_store()

    assert after is not running
    assert after.path.parent.name == "second"
