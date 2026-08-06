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


def _tree_column0(tree) -> list[str]:
    return [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]


def test_sortable_tree_item_orders_by_value_not_text(qapp) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidget

    from deepreefmap_gui.core.widgets import SortableTreeItem, enable_sorting

    tree = QTreeWidget()
    tree.setColumnCount(2)
    SortableTreeItem(tree, ["short", "9 m"], {0: "short", 1: 9.0})
    SortableTreeItem(tree, ["long", "12 m"], {0: "long", 1: 12.0})
    # No values at all: the row a draft uses, pinned to the foot either way.
    SortableTreeItem(tree, ["draft", ""])

    enable_sorting(tree, 1)
    assert _tree_column0(tree) == ["short", "long", "draft"]

    tree.sortByColumn(1, Qt.SortOrder.DescendingOrder)
    assert _tree_column0(tree) == ["long", "short", "draft"]


def test_enable_sorting_flags_the_header_for_the_theme(qapp) -> None:
    from PySide6.QtWidgets import QTableWidget

    from deepreefmap_gui.core.widgets import enable_sorting

    table = QTableWidget(0, 2)
    header = table.horizontalHeader()
    assert header.property("sortable") is None

    enable_sorting(table)
    assert header.property("sortable") == "true"
    assert header.isSortIndicatorShown()
    assert table.isSortingEnabled()
