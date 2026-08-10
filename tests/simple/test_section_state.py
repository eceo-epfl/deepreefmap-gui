"""Per-step verdicts. Pure and Qt-free, so these need no window."""

from __future__ import annotations

import pytest

from deepreefmap_gui.simple.section_state import (
    ATTENTION,
    BLOCKED,
    FIX_HERE,
    FIX_MACHINE,
    FIX_SETTINGS,
    OK,
    TODO,
    SectionState,
    browse_state,
    headline,
    machine_state,
    run_gate,
    transects_state,
    videos_state,
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
    assert transects_state(0, False).state == TODO
    assert transects_state(2, False).state == OK
    assert transects_state(2, False).count == "2 transects"
    assert transects_state(1, False).count == "1 transect"


def test_half_entered_transect_is_flagged():
    """Nothing else in the UI mentions a draft again, so the step has to."""
    state = transects_state(1, True)
    assert state.state == ATTENTION
    assert "endpoints" in state.reason


def test_empty_run_step_is_todo_not_blocked():
    """No videos yet is the normal starting state, not a problem to solve."""
    state = gate(pass_count=0, remaining=0)
    assert state.state == TODO


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"has_preset": False}, "run settings"),
        ({"missing_models": ["coralscapes-vit-b-dpt"]}, "coralscapes-vit-b-dpt"),
        ({"gpu_only_mapper": "loger_star"}, "loger_star"),
    ],
)
def test_blockers_name_themselves(overrides, fragment):
    state = gate(pass_count=3, **overrides)
    assert state.state == BLOCKED
    assert fragment in state.reason


def test_a_skipped_transect_is_reported_but_never_blocks():
    """Scenario: a clip is queued with the transect deliberately skipped.

    Expected behaviour: the batch runs. The step says what was given up -- the
    pass cannot be set beside repeat passes of the same place -- and says it
    below every real blocker, because it is information rather than a fault.
    """
    state = gate(pass_count=3, unassigned=1, remaining=3)
    assert state.state == OK
    assert "without a transect" in state.count
    assert "repeat passes" in state.reason


def test_an_unscaled_transect_is_reported_but_never_blocks():
    """Scenario: a pass sits on a transect whose tape length was never entered.

    Expected behaviour: the batch runs, and the step says the outputs will be
    unscaled while there is still time to enter the length. Below unassigned,
    because a missing transect swallows a missing tape reading.
    """
    state = gate(pass_count=2, remaining=2, unscaled=1)
    assert state.state == OK
    assert "unscaled" in state.count
    assert "tape length" in state.reason

    both = gate(pass_count=2, remaining=2, unassigned=1, unscaled=1)
    assert "without a transect" in both.count


def test_a_real_blocker_outranks_a_skipped_transect():
    """Only one reason is shown, so it must be the one that stops the batch."""
    state = gate(
        pass_count=3,
        unassigned=1,
        has_preset=False,
        missing_models=["x"],
        gpu_only_mapper="loger",
    )
    assert state.state == BLOCKED
    assert "run settings" in state.reason


def test_a_gpu_only_method_on_a_cpu_laptop_blocks():
    """Without this the batch enables Process and every pass fails in turn."""
    state = gate(pass_count=2, gpu_only_mapper="loger_star")
    assert state.state == BLOCKED
    assert "graphics card" in state.reason


def test_the_missing_card_outranks_the_missing_models():
    """Changing the method changes what to download, so it is asked about first."""
    state = gate(pass_count=2, gpu_only_mapper="loger", missing_models=["LoGeR"])
    assert "graphics card" in state.reason


@pytest.mark.parametrize(
    "overrides, destination",
    [
        ({"unassigned": 1}, FIX_HERE),
        ({"has_preset": False}, FIX_SETTINGS),
        ({"gpu_only_mapper": "loger"}, FIX_MACHINE),
        ({"missing_models": ["x"]}, FIX_MACHINE),
    ],
)
def test_each_blocker_says_where_it_is_fixed(overrides, destination):
    """The strip's button reads this, so a blocker with nowhere to go says so."""
    assert gate(pass_count=2, **overrides).fix == destination


def test_no_blocker_tells_the_user_to_change_modes():
    """Simple mode cannot follow directions into the advanced sidebar."""
    for overrides in (
        {"has_preset": False},
        {"gpu_only_mapper": "loger"},
        {"missing_models": ["coralscapes-vit-b-dpt"]},
    ):
        reason = gate(pass_count=2, **overrides).reason.lower()
        assert "advanced" not in reason
        assert "tab" not in reason


def test_an_unknown_fix_destination_is_rejected():
    with pytest.raises(ValueError):
        SectionState(BLOCKED, "2 passes", "nowhere to go", fix="somewhere")


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


def test_an_unmet_requirement_blocks_the_machine_and_counts_itself():
    one = machine_state(unmet=1)
    assert one.state == BLOCKED
    assert one.count == "1 requirement not met"
    assert machine_state(unmet=3).count == "3 requirements not met"


def test_a_memory_advisory_alone_warns_rather_than_blocking():
    """The batch still runs on a machine that may run low, so nothing is blocked."""
    state = machine_state(unmet=0, advisory="This session may exhaust memory on this machine.")
    assert state.state == ATTENTION
    assert "exhaust memory" in state.reason


def test_an_available_update_is_named_without_raising_the_state():
    """An update is a chore rather than a blocker, so it says so and nothing more."""
    state = machine_state(unmet=0, update_version="2.1.0")
    assert state.state == OK
    assert "2.1.0" in state.reason


def test_an_update_alongside_a_blocker_still_leaves_the_machine_blocked():
    state = machine_state(unmet=2, update_version="2.1.0")
    assert state.state == BLOCKED
    assert "2 requirements" in state.reason
    assert "2.1.0" in state.reason


def test_an_update_alongside_an_advisory_stays_at_attention():
    state = machine_state(unmet=0, advisory="Memory may run low.", update_version="2.1.0")
    assert state.state == ATTENTION
    assert "Memory may run low." in state.reason
    assert "2.1.0" in state.reason


def test_a_machine_with_nothing_to_report_is_ok():
    state = machine_state(unmet=0)
    assert state.state == OK
    assert state.count == "Ready"


def test_the_machine_never_sends_the_user_anywhere_else():
    """Setup is where its own blockers are fixed, so it names no destination."""
    for state in (machine_state(unmet=2), machine_state(unmet=0, advisory="low memory")):
        assert state.fix == FIX_HERE


def test_unknown_state_is_rejected():
    with pytest.raises(ValueError):
        SectionState("nearly", "1 transect")


def test_unknown_cause_is_rejected():
    with pytest.raises(ValueError):
        SectionState(ATTENTION, "1 clip", cause="videos.eaten_by_a_shark")


def _speaking_verdicts():
    """Every verdict any of the five functions can return with something to say."""
    return [
        transects_state(1, True),
        browse_state(19, 17),
        videos_state(10, 10),
        gate(missing_files=2),
        gate(has_preset=False),
        gate(gpu_only_mapper="loger"),
        gate(missing_models=["dinov3"]),
        gate(failed=3),
        gate(unassigned=3),
        gate(unscaled=3),
        machine_state(unmet=2),
        machine_state(unmet=0, advisory="low memory"),
    ]


def test_every_verdict_worth_raising_names_its_cause():
    """The notification centre fingerprints on the cause, so a verdict with
    advice and no cause would be a message it could never track or silence."""
    for verdict in _speaking_verdicts():
        assert verdict.cause, verdict.reason


def test_a_cause_survives_a_reworded_reason():
    """Rewording must not read as a different problem, or every reader's
    decision to never hear this one again is silently voided."""
    assert videos_state(10, 10).cause == videos_state(10, 4).cause


def test_a_count_verdict_carries_the_number_behind_its_words():
    assert browse_state(19, 17).n == 17
    assert videos_state(10, 4).n == 4
    assert gate(failed=3).n == 3
    assert gate(missing_models=["a", "b"]).n == 2


def test_badge_vocabulary_matches_the_verdicts():
    """core/icons.py spells the states out rather than importing upwards, so
    the two lists have to be checked against each other."""
    from deepreefmap_gui.core.icons import STEP_STATES
    from deepreefmap_gui.simple.section_state import SECTION_STATES

    assert set(STEP_STATES) == set(SECTION_STATES)


def test_a_headline_drops_the_advice_and_keeps_the_fault():
    """A one-line surface has room for the fault, not the advice after it."""
    reason = browse_state(19, 17).reason
    assert headline(reason) == "17 runs belong to no transect"
    assert "Assign them" in reason


def test_a_one_sentence_reason_survives_whole():
    assert headline("Add a transect, or import a CSV or GPX file.") == (
        "Add a transect, or import a CSV or GPX file"
    )
