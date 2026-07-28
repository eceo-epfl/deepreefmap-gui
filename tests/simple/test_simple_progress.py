"""Per-step verdicts. Pure and Qt-free, so these need no window."""

from __future__ import annotations

import pytest

from deepreefmap_gui.simple.progress import (
    ATTENTION,
    BLOCKED,
    OK,
    TODO,
    SectionState,
    browse_state,
    plan_state,
    run_gate,
)


def gate(**overrides):
    kwargs = {
        "pass_count": 1,
        "unassigned": 0,
        "remaining": 1,
        "failed": 0,
        "has_preset": True,
        "missing_models": [],
    }
    kwargs.update(overrides)
    return run_gate(**kwargs)


def test_plan_needs_one_saved_transect():
    assert plan_state(0, False).state == TODO
    assert plan_state(2, False).state == OK
    assert plan_state(2, False).count == "2 transects"
    assert plan_state(1, False).count == "1 transect"


def test_half_entered_transect_is_flagged():
    """Nothing else in the UI mentions a draft again, so the step has to."""
    state = plan_state(1, True)
    assert state.state == ATTENTION
    assert "endpoints" in state.reason


def test_empty_run_step_is_todo_not_blocked():
    """No videos yet is the normal starting state, not a problem to solve."""
    state = gate(pass_count=0, remaining=0)
    assert state.state == TODO


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"unassigned": 2}, "need a transect"),
        ({"has_preset": False}, "run settings"),
        ({"missing_models": ["coralscapes-vit-b-dpt"]}, "coralscapes-vit-b-dpt"),
    ],
)
def test_blockers_name_themselves(overrides, fragment):
    state = gate(pass_count=3, **overrides)
    assert state.state == BLOCKED
    assert fragment in state.reason


def test_unassigned_outranks_the_other_blockers():
    """Only one reason is shown, so it must be the one the user can act on first."""
    state = gate(pass_count=3, unassigned=1, has_preset=False, missing_models=["x"])
    assert "need a transect" in state.reason


def test_failed_passes_warn_without_blocking():
    state = gate(pass_count=4, failed=2, remaining=2)
    assert state.state == ATTENTION
    assert "2 failed" in state.count


def test_a_finished_batch_is_ok():
    state = gate(pass_count=2, remaining=0)
    assert state.state == OK
    assert state.count == "2 passes · all processed"
    assert state.reason == ""


def test_browse_never_blocks_and_chases_unfiled_runs():
    assert browse_state(0, 0).state == TODO
    assert browse_state(5, 0).state == OK
    unfiled = browse_state(5, 2)
    assert unfiled.state == ATTENTION
    assert "2 unfiled" in unfiled.count


def test_unknown_state_is_rejected():
    with pytest.raises(ValueError):
        SectionState("nearly", "1 transect")


def test_badge_vocabulary_matches_the_verdicts():
    """core/icons.py spells the states out rather than importing upwards, so
    the two lists have to be checked against each other."""
    from deepreefmap_gui.core.icons import STEP_STATES
    from deepreefmap_gui.simple.progress import SECTION_STATES

    assert set(STEP_STATES) == set(SECTION_STATES)
