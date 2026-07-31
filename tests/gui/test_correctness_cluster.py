"""Three unrelated correctness bugs that share a landing.

Each is a case the surrounding code handles for every input except one: a chart
with a single slice, a map panned past the antimeridian, and a timestamp written
without a UTC offset.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


class TestSingleSliceSunburst:
    """A run where one class covers everything."""

    def _slices(self, fractions):
        from deepreefmap_gui.runs.sunburst import _angles_from_items

        return _angles_from_items(
            [(f"class_{i}", frac, (10 * i, 20, 30), (i,)) for i, frac in enumerate(fractions, start=1)]
        )

    def test_a_lone_slice_is_hit_at_every_angle(self):
        """It used to reduce to lo == hi and be skipped as if it were empty, so
        the chart had no tooltip and no click-to-filter anywhere."""
        from deepreefmap_gui.runs.sunburst import _slice_at_angle

        slices = self._slices([1.0])
        assert slices, "the fixture built no slices"

        for angle in (0.0, 89.0, 90.0, 91.0, 180.0, 270.0, 359.9):
            assert _slice_at_angle(slices, angle) is not None, f"no slice at {angle}"

    def test_several_slices_still_partition_the_circle(self):
        from deepreefmap_gui.runs.sunburst import _slice_at_angle

        slices = self._slices([0.5, 0.3, 0.2])

        hits = {id(_slice_at_angle(slices, a)) for a in range(0, 360, 5)}
        assert None not in hits
        assert len(hits) == 3, "angles did not spread across all three slices"


class TestLongitudeWrap:
    def test_a_longitude_past_the_antimeridian_wraps(self):
        from deepreefmap_gui.map.tile_math import normalise_longitude

        assert normalise_longitude(214.3) == pytest.approx(-145.7)

    def test_the_western_bound_is_kept_and_the_eastern_wraps(self):
        from deepreefmap_gui.map.tile_math import normalise_longitude

        assert normalise_longitude(-180.0) == pytest.approx(-180.0)
        assert normalise_longitude(180.0) == pytest.approx(-180.0)

    def test_ordinary_longitudes_are_untouched(self):
        from deepreefmap_gui.map.tile_math import normalise_longitude

        for lon in (-179.9, -90.0, 0.0, 6.6, 179.9):
            assert normalise_longitude(lon) == pytest.approx(lon)

    def test_panning_past_the_antimeridian_yields_a_storable_coordinate(self):
        """This is the coordinate that reaches the survey database."""
        from deepreefmap_gui.map.tile_math import tile2deg

        zoom = 4
        n = 2.0**zoom
        _, lon = tile2deg(n * 1.1, n / 2, zoom)

        assert -180.0 <= lon < 180.0


class TestRunTimestamps:
    def test_a_naive_timestamp_is_read_as_utc(self):
        """Everything that writes one writes UTC; reading it as local puts it
        hours out, in a direction that depends on the machine."""
        from deepreefmap_gui.survey.catalogue import parse_run_timestamp

        parsed = parse_run_timestamp("2026-07-28T10:00:00")

        assert parsed == datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc)

    def test_an_offset_timestamp_keeps_its_offset(self):
        from deepreefmap_gui.survey.catalogue import parse_run_timestamp

        parsed = parse_run_timestamp("2026-07-28T10:00:00+02:00")

        assert parsed == datetime(
            2026, 7, 28, 10, 0, tzinfo=timezone(timedelta(hours=2))
        )

    def test_naive_and_offset_timestamps_order_against_each_other(self):
        from deepreefmap_gui.survey.catalogue import run_sort_key

        earlier = run_sort_key({"run_timestamp": "2026-07-28T10:00:00"}, 0.0)
        later = run_sort_key({"run_timestamp": "2026-07-28T11:00:00+00:00"}, 0.0)

        assert earlier < later
        assert later - earlier == pytest.approx(3600.0)

    def test_an_unusable_timestamp_falls_back_to_the_mtime(self):
        from deepreefmap_gui.survey.catalogue import parse_run_timestamp, run_sort_key

        assert parse_run_timestamp(None) is None
        assert parse_run_timestamp("not a date") is None
        assert run_sort_key({"run_timestamp": "not a date"}, 1234.0) == 1234.0
