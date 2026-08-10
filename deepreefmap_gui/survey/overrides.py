"""Run settings one cart row departs from its session on.

A session is processed under one set of settings. One pass in it sometimes needs
different ones: a long swim that will not fit this machine's memory at the
session's frame rate, a clip shot on the other camera. Rather than split the
session, the cart row carries the handful of settings it differs on.

Only the differing keys are stored, and they are stored as whole values. That
way a session setting nobody overrode still reaches every pass when it changes,
and an override that comes to agree with the session stops counting as one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from deepreefmap_gui.survey.preset import describe_keys


def override_diff(edited: Mapping[str, Any], session: Mapping[str, Any]) -> dict[str, Any]:
    """The settings ``edited`` changes about ``session``.

    Keys the session does not carry are dropped rather than kept as additions:
    the two dicts come from the same form, so a key in one and not the other is
    a settings vocabulary that has moved on, not an override anybody made.
    """
    return {
        key: value
        for key, value in edited.items()
        if key in session and value != session[key]
    }


def effective(session: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    """What this pass will actually run under."""
    return {**session, **overrides}


def live_overrides(
    overrides: Mapping[str, Any], session: Mapping[str, Any]
) -> dict[str, Any]:
    """The stored overrides that still differ from the session.

    Editing the session towards a row's override leaves the row claiming a
    difference that no longer exists, so the count is re-derived rather than
    trusted.
    """
    return override_diff(overrides, session)


def override_summary(keys: Mapping[str, Any] | list[str]) -> str:
    """The label for the row's settings button."""
    count = len(keys)
    if not count:
        return "Default settings"
    return "1 override" if count == 1 else f"{count} overrides"


def override_tooltip(overrides: Mapping[str, Any]) -> str:
    """What the row changes, named in the words the settings dialog uses."""
    if not overrides:
        return "This pass runs under the session's settings. Click to change them for it alone."
    return f"Changed for this pass only: {describe_keys(sorted(overrides))}."
