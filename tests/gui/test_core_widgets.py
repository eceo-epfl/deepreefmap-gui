"""Shared widgets from core/widgets.py.

Scenario: a widget that is made visible before a layout adopts it has no parent
at that moment, so Qt maps it as a real top-level window -- an empty titlebar
box that flashes on screen and disappears once the layout reparents it. The
window build constructs several EmptyStates, so one misordered line flashes
several times per launch.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QWidget


@pytest.fixture
def toplevel_shows(monkeypatch):
    """Record every widget made visible while it is still parentless."""
    seen: list[str] = []
    original = QWidget.setVisible

    def record(self, visible=True, *args, **kwargs):
        if visible and self.isWindow():
            seen.append(f"{type(self).__name__}: {self.text()!r}" if hasattr(self, "text") else type(self).__name__)
        return original(self, visible, *args, **kwargs)

    monkeypatch.setattr(QWidget, "setVisible", record)
    return seen


@pytest.mark.parametrize("hint", ["a hint", ""])
def test_empty_state_never_flashes_a_toplevel_window(qapp, toplevel_shows, hint) -> None:
    from deepreefmap_gui.core.widgets import EmptyState

    EmptyState("No runs here yet", hint)

    assert toplevel_shows == []


def test_empty_state_hint_visibility_follows_the_text(qapp) -> None:
    from deepreefmap_gui.core.widgets import EmptyState

    state = EmptyState("message", "")
    assert not state._hint.isVisibleTo(state)

    state.set_text("message", "now there is a hint")
    assert state._hint.isVisibleTo(state)

    state.set_text("message", "")
    assert not state._hint.isVisibleTo(state)
