"""Covers deepreefmap_gui/survey/overrides.py: the settings one cart row departs
from its session on.

Scenario: a session runs at 5 fps, and one long pass in it needs 3.

Expected behaviour: only the frame rate is stored, as a whole value, so every
other setting still follows the session when it changes, and an override that
comes to agree with the session stops counting as one.
"""

from __future__ import annotations

from deepreefmap_gui.survey.overrides import (
    effective,
    live_overrides,
    override_diff,
    override_summary,
    override_tooltip,
)

SESSION = {"fps": 5, "mapping_name": "loger", "preprocess_batch_size": 4}


def test_only_the_changed_settings_are_kept():
    edited = {**SESSION, "fps": 3}
    assert override_diff(edited, SESSION) == {"fps": 3}


def test_a_key_the_session_does_not_carry_is_not_an_override():
    """The two dicts come from one form, so a key in one alone is a vocabulary
    that has moved on, not something anybody changed."""
    assert override_diff({**SESSION, "invented": 1}, SESSION) == {}


def test_the_pass_runs_on_the_session_plus_its_own():
    assert effective(SESSION, {"fps": 3}) == {**SESSION, "fps": 3}


def test_a_session_change_reaches_a_setting_nobody_overrode():
    changed = {**SESSION, "preprocess_batch_size": 8}
    assert effective(changed, {"fps": 3})["preprocess_batch_size"] == 8


def test_an_override_that_agrees_with_the_session_stops_counting():
    assert live_overrides({"fps": 3}, SESSION) == {"fps": 3}
    assert live_overrides({"fps": 3}, {**SESSION, "fps": 3}) == {}


def test_the_button_says_how_many():
    assert override_summary({}) == "Default settings"
    assert override_summary({"fps": 3}) == "1 override"
    assert override_summary({"fps": 3, "mapping_name": "scsfmlearner"}) == "2 overrides"


def test_the_tooltip_names_the_settings_in_plain_words():
    assert "frames per second" in override_tooltip({"fps": 3})
    assert "session" in override_tooltip({})
