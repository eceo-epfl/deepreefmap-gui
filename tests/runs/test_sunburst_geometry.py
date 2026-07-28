"""Sunburst slice geometry, and the cover report it is built from.

The ring is clickable, so the angle maths is behaviour, not decoration: a slice
that paints in one place and hit-tests in another silently selects the wrong
class. These cover the slice builders and the pure helpers; painting itself is
left alone.

The widget is a QWidget but needs no main window, so this stays out of
tests/gui/ and its full DeepReefMapWindow fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from deepreefmap.config.classes import ClassConfig, SemanticClass

from deepreefmap_gui.runs.sunburst import (
    SunburstWidget,
    _angles_from_items,
    _centered_square,
    _slice_at_angle,
)

RED, GREEN, BLUE = (255, 0, 0), (0, 255, 0), (0, 0, 255)

# Real coralscapes ids so the bundled class_groups.yaml drives the roll-up:
# 22 and 25 are both "coral alive" at the coarse level, 5 is sand.
BRANCHING, ACROPORA, SAND = 22, 25, 5


def _classes() -> ClassConfig:
    return ClassConfig(
        classes=(
            SemanticClass(BRANCHING, "branching alive", RED, frozenset()),
            SemanticClass(ACROPORA, "acropora alive", GREEN, frozenset()),
            SemanticClass(SAND, "sand", BLUE, frozenset()),
        ),
        path=Path("test"),
    )


def _cover(fractions: dict[int, float] | None = None) -> dict[str, object]:
    values = {BRANCHING: 0.3, ACROPORA: 0.5, SAND: 0.2}
    values.update(fractions or {})
    names = {BRANCHING: "branching alive", ACROPORA: "acropora alive", SAND: "sand"}
    return {
        "classes": {
            str(cid): {"name": names[cid], "count": frac * 100, "fraction": frac}
            for cid, frac in values.items()
        },
        "denominator": 100.0,
    }


def _items(*fractions):
    names = "abcdefg"
    colors = [RED, GREEN, BLUE] * 3
    return [(names[i], f, colors[i], (i + 1,)) for i, f in enumerate(fractions)]


def test_slices_tile_the_full_circle_clockwise_from_noon():
    slices = _angles_from_items(_items(0.5, 0.25, 0.25))

    assert [s.name for s in slices] == ["a", "b", "c"]
    assert slices[0].start_deg == 90.0  # 12 o'clock
    # Negative spans: QPainter angles grow counter-clockwise, the ring reads clockwise.
    assert all(s.span_deg < 0 for s in slices)
    assert sum(s.span_deg for s in slices) == pytest.approx(-360.0)
    # Each slice starts where the previous one ended.
    for prev, nxt in zip(slices, slices[1:]):
        assert nxt.start_deg == pytest.approx(prev.start_deg + prev.span_deg)


def test_fractions_are_normalised_not_taken_at_face_value():
    """Cover dicts need not sum to 1: a crop or a filter leaves a remainder."""
    slices = _angles_from_items(_items(1.0, 3.0))
    assert [s.fraction for s in slices] == pytest.approx([0.25, 0.75])
    assert sum(s.span_deg for s in slices) == pytest.approx(-360.0)


def test_non_positive_fractions_are_dropped_not_drawn_backwards():
    slices = _angles_from_items(_items(0.5, 0.0, -0.2, 0.5))
    assert [s.name for s in slices] == ["a", "d"]
    assert sum(s.span_deg for s in slices) == pytest.approx(-360.0)


@pytest.mark.parametrize("fractions", [(), (0.0,), (-1.0, -2.0)])
def test_an_empty_or_empty_valued_ring_produces_no_slices(fractions):
    assert _angles_from_items(_items(*fractions)) == ()


def test_class_ids_ride_along_so_a_click_can_select_them():
    (slc,) = _angles_from_items([("coral", 1.0, RED, (22, 25))])
    assert slc.class_ids == (22, 25)


def test_hit_test_finds_the_slice_that_was_drawn_there():
    slices = _angles_from_items(_items(0.5, 0.25, 0.25))
    for slc in slices:
        midpoint = (slc.start_deg + slc.span_deg / 2.0) % 360.0
        assert _slice_at_angle(slices, midpoint) is slc


def test_hit_test_handles_the_slice_that_wraps_past_zero():
    """The first slice starts at 90 and runs clockwise through 0, so its
    normalised range is inverted -- the branch a naive lo<=x<hi test gets wrong."""
    slices = _angles_from_items(_items(0.5, 0.5))
    first = slices[0]
    assert _slice_at_angle(slices, 80.0) is first    # just after the start
    assert _slice_at_angle(slices, 350.0) is first   # wrapped past zero


def test_hit_test_covers_every_angle_of_a_full_ring():
    slices = _angles_from_items(_items(0.3, 0.3, 0.4))
    for degrees in range(0, 360):
        assert _slice_at_angle(slices, float(degrees)) is not None


def test_hit_test_on_an_empty_ring_is_a_miss():
    assert _slice_at_angle((), 45.0) is None


def test_centered_square_is_centred():
    rect = _centered_square(100.0, 50.0, 20.0)
    assert (rect.center().x(), rect.center().y()) == (100.0, 50.0)
    assert (rect.width(), rect.height()) == (20.0, 20.0)


# --- turning a cover report into the two rings --------------------------


def test_the_outer_ring_is_one_slice_per_class_biggest_first():
    slices = SunburstWidget._build_fine_slices(_cover(), _classes())

    assert [s.name for s in slices] == ["acropora alive", "branching alive", "sand"]
    assert [s.class_ids for s in slices] == [(ACROPORA,), (BRANCHING,), (SAND,)]
    assert slices[0].color.getRgb()[:3] == GREEN


def test_a_class_absent_from_the_run_is_not_drawn():
    """Cover reports carry every class the model can emit, most of them zero."""
    slices = SunburstWidget._build_fine_slices(_cover({SAND: 0.0}), _classes())

    assert [s.name for s in slices] == ["acropora alive", "branching alive"]
    assert sum(s.span_deg for s in slices) == pytest.approx(-360.0)


def test_a_non_numeric_class_key_is_skipped_rather_than_crashing_the_panel():
    """`classes` is read straight from run_manifest.json, so its keys are
    whatever was written there."""
    cover = _cover()
    cover["classes"]["background"] = {"name": "background", "fraction": 0.9}

    slices = SunburstWidget._build_fine_slices(cover, _classes())

    assert [s.name for s in slices] == ["acropora alive", "branching alive", "sand"]


def test_a_cover_report_with_no_classes_block_draws_nothing():
    assert SunburstWidget._build_fine_slices({"denominator": 0.0}, _classes()) == ()


def test_the_inner_ring_rolls_the_classes_up_into_groups():
    slices = SunburstWidget._build_coarse_slices(_cover(), _classes())

    assert [s.name for s in slices] == ["coral alive", "sand"]
    assert sum(s.span_deg for s in slices) == pytest.approx(-360.0)


def test_a_group_slice_selects_every_class_it_covers():
    """Clicking the inner ring filters the cloud to the whole group, so a group
    that forgets a member silently hides points the user asked to see."""
    (coral, sand) = SunburstWidget._build_coarse_slices(_cover(), _classes())

    assert set(coral.class_ids) == {BRANCHING, ACROPORA}
    assert sand.class_ids == (SAND,)


def test_a_group_is_exactly_as_wide_as_the_classes_inside_it():
    """The rings are concentric, so the inner arc has to account for the same
    share of the circle as the outer arcs it summarises."""
    classes_config = _classes()
    fine = {s.name: s for s in SunburstWidget._build_fine_slices(_cover(), classes_config)}
    coarse = {s.name: s for s in SunburstWidget._build_coarse_slices(_cover(), classes_config)}

    members = fine["branching alive"].span_deg + fine["acropora alive"].span_deg
    assert coarse["coral alive"].span_deg == pytest.approx(members)


def test_setting_a_cover_report_populates_both_rings(qapp):
    widget = SunburstWidget()
    assert not widget.has_data()

    widget.set_cover(_cover(), _classes())

    assert widget.has_data()
    assert len(widget._fine_slices) == 3
    assert len(widget._coarse_slices) == 2


@pytest.mark.parametrize("cover", [None, {}, "not a dict"])
def test_a_run_without_cover_data_empties_the_rings(qapp, cover):
    """Geometry-only runs have no cover block at all."""
    widget = SunburstWidget()
    widget.set_cover(_cover(), _classes())

    widget.set_cover(cover, _classes())

    assert not widget.has_data()
    assert widget._coarse_slices == ()


def test_selecting_the_same_classes_twice_does_not_repaint(qapp):
    """set_selection is driven by the legend, which re-announces its state on
    every click; repainting the ring each time makes the panel flicker."""
    widget = SunburstWidget()
    widget.set_cover(_cover(), _classes())
    widget.set_selection(frozenset({BRANCHING}), True)
    repaints: list[bool] = []
    widget.update = lambda: repaints.append(True)

    widget.set_selection(frozenset({BRANCHING}), True)
    assert not repaints

    widget.set_selection(frozenset({SAND}), True)
    assert repaints


# --- export -------------------------------------------------------------


def test_the_ring_renders_to_a_pixmap_for_export(qapp):
    """The PNG export path, which paints off-screen rather than to the widget."""
    widget = SunburstWidget()
    widget.set_cover(_cover(), _classes())
    widget.set_title("T1 forward")

    pixmap = widget.render_pixmap(256)

    assert (pixmap.width(), pixmap.height()) == (256, 256)
    image = pixmap.toImage()
    # Painted, not left transparent: the slice colours have to reach the file.
    assert any(
        image.pixelColor(x, y).alpha() > 0
        for x in range(0, 256, 8)
        for y in range(0, 256, 8)
    )


def test_a_pixmap_too_small_to_draw_a_ring_is_left_blank(qapp):
    """_paint bails below a minimum side rather than drawing a degenerate ring."""
    widget = SunburstWidget()
    widget.set_cover(_cover(), _classes())

    pixmap = widget.render_pixmap(12)

    image = pixmap.toImage()
    assert all(
        image.pixelColor(x, y).alpha() == 0 for x in range(12) for y in range(12)
    )
