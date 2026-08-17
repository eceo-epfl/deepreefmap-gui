"""Bar geometry in the cover chart, and the y-axis it is scaled against.

The bars and the legend are clickable, so the geometry is behaviour: a bar that
paints in one place and hit-tests in another opens the wrong pass. Everything
here is recomputed from the widget's size rather than cached from a paint, which
is what lets these run without ever painting.

The widget needs no main window, so this stays out of tests/gui/ and its full
DeepReefMapWindow fixture.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor

from deepreefmap_gui.simple.charts import GroupedBarChart, pass_color

LABELS = ["sand", "rubble", "coral"]


@pytest.fixture
def chart(qapp) -> GroupedBarChart:
    widget = GroupedBarChart()
    widget.resize(600, 300)
    return widget


def per_pass(chart: GroupedBarChart) -> None:
    chart.set_data(
        LABELS,
        [
            ("1 →", {"sand": 0.4, "rubble": 0.2, "coral": 0.1}, QColor("#46c2b4")),
            ("2 ←", {"sand": 0.5, "rubble": 0.1, "coral": 0.2}, QColor("#e58fd6")),
        ],
        keys=["pass-a", "pass-b"],
    )


def pooled(chart: GroupedBarChart, spread=None) -> None:
    chart.set_aggregate(
        LABELS,
        {"sand": 0.45, "rubble": 0.15, "coral": 0.15},
        spread=spread if spread is not None else {"sand": (0.4, 0.5), "coral": (0.1, 0.2)},
        colours={label: QColor("#888888") for label in LABELS},
        passes=2,
    )


# --- pooled mode ------------------------------------------------------------

def test_pooled_draws_one_bar_per_class(chart) -> None:
    pooled(chart)

    bars = chart._bars()
    assert len(bars) == len(LABELS)
    assert [bar.label for bar in bars] == LABELS


def test_the_axis_leaves_room_for_a_whisker_taller_than_its_bar(chart) -> None:
    """Scaling the axis off the bars alone silently clips the top cap off the
    whisker, which is the half of it that carries the disagreement."""
    chart.set_aggregate(
        LABELS,
        {"sand": 0.2, "rubble": 0.1, "coral": 0.05},
        spread={"sand": (0.1, 0.62)},
        colours={label: QColor("#888888") for label in LABELS},
        passes=2,
    )

    assert chart._axis_top() >= 0.62


def test_a_pooled_bar_is_inert_because_it_is_every_pass_at_once(chart) -> None:
    pooled(chart)

    assert chart._legend_keys() == []
    assert all(bar.key == "" for bar in chart._bars())


def test_a_single_pass_is_given_no_whisker_to_read(chart) -> None:
    pooled(chart, spread={})

    assert chart._bars()
    assert chart._spread == {}


# --- per-pass mode ----------------------------------------------------------

def test_per_pass_draws_every_pass_in_each_class_group(chart) -> None:
    per_pass(chart)

    bars = chart._bars()
    assert len(bars) == len(LABELS) * 2
    # Grouped by class, so the two passes of one class sit side by side.
    assert [bar.label for bar in bars[:2]] == ["sand", "sand"]
    assert {bar.key for bar in bars} == {"pass-a", "pass-b"}


def test_a_click_inside_a_bar_names_the_pass_it_belongs_to(chart) -> None:
    per_pass(chart)
    bars = chart._bars()

    for bar in bars:
        hit = chart._at(bar.rect.center())
        assert hit is not None
        assert (hit.key, hit.label) == (bar.key, bar.label)


def test_a_click_in_the_margin_hits_nothing(chart) -> None:
    per_pass(chart)

    assert chart._at(QPointF(2, 2)) is None
    assert chart._at(QPointF(chart.width() - 2, chart.height() - 2)) is None


def test_a_legend_key_is_clickable_as_well_as_its_bars(chart) -> None:
    """A crowded chart has bars a few pixels wide, so the key is the reliable
    way to reach a pass."""
    per_pass(chart)

    keys = chart._legend_keys()
    assert [key for _rect, key, _name, _colour in keys] == ["pass-a", "pass-b"]
    for rect, key, _name, _colour in keys:
        assert chart._key_at(rect.center()) == key
    assert chart._key_at(QPointF(chart.width() - 2, chart.height() - 2)) is None


def test_bars_follow_a_resize_rather_than_the_last_paint(chart) -> None:
    """Recomputed from size(), so a window resized between a paint and a click
    cannot leave the hit test behind, and a headless test never paints at all."""
    per_pass(chart)
    narrow = chart._bars()[0].rect.width()

    chart.resize(1200, 300)
    wide = chart._bars()[0].rect.width()

    assert wide > narrow


# --- colour -----------------------------------------------------------------

def test_a_repeat_of_a_pass_is_a_darker_shade_of_its_direction(qapp) -> None:
    first = pass_color("forward", 0)
    second = pass_color("forward", 1)
    reverse = pass_color("reverse", 0)

    assert first.hue() == second.hue()
    assert second.value() < first.value()
    assert reverse.hue() != first.hue()
