"""Storage accounting against a described mount table, never the host's disks."""

from __future__ import annotations

import types

from deepreefmap_gui.profiling.volumes import (
    FULLNESS_FULL,
    FULLNESS_OK,
    FULLNESS_TIGHT,
    MIN_FREE_BYTES,
    VolumeUsage,
    fullness,
    group_by_volume,
    used_percent,
    volume_root,
)

GB = 1024**3

MOUNTS = {"/", "/media/field"}


def ismount(path: str) -> bool:
    return path in MOUNTS


def usage_table(**by_root: tuple[int, int]):
    def usage(root: str):
        total, free = by_root[root]
        return types.SimpleNamespace(total=total, free=free)

    return usage


def test_clips_and_outputs_land_on_their_own_volumes() -> None:
    volumes = group_by_volume(
        [("/media/field/a.mp4", 4 * GB), ("/media/field/b.mp4", 2 * GB)],
        [("/home/evan/runs/dive1", 3 * GB)],
        usage=usage_table(**{"/": (500 * GB, 100 * GB), "/media/field": (64 * GB, 50 * GB)}),
        ismount=ismount,
    )
    by_root = {v.root: v for v in volumes}
    assert [v.root for v in volumes] == ["/", "/media/field"]
    assert (by_root["/media/field"].video_bytes, by_root["/media/field"].output_bytes) == (6 * GB, 0)
    assert (by_root["/"].video_bytes, by_root["/"].output_bytes) == (0, 3 * GB)
    assert by_root["/media/field"].label == "field"
    assert by_root["/"].label == "/"


def test_other_used_space_is_the_remainder() -> None:
    (volume,) = group_by_volume(
        [("/media/field/a.mp4", 4 * GB)],
        [("/media/field/out", 1 * GB)],
        usage=usage_table(**{"/media/field": (64 * GB, 40 * GB)}),
        ismount=ismount,
    )
    assert volume.other_used_bytes == 19 * GB


def test_recorded_sizes_beyond_the_volume_clamp_at_zero() -> None:
    """A row outliving the file it describes can oversubscribe the drive, and a
    negative segment would paint backwards."""
    (volume,) = group_by_volume(
        [("/media/field/a.mp4", 900 * GB)],
        [],
        usage=usage_table(**{"/media/field": (64 * GB, 40 * GB)}),
        ismount=ismount,
    )
    assert volume.other_used_bytes == 0


def test_the_same_clip_listed_twice_is_counted_once() -> None:
    (volume,) = group_by_volume(
        [("/media/field/a.mp4", 4 * GB), ("/media/field/../field/a.mp4", 4 * GB)],
        [],
        usage=usage_table(**{"/media/field": (64 * GB, 40 * GB)}),
        ismount=ismount,
    )
    assert volume.video_bytes == 4 * GB


def test_a_path_on_no_known_volume_is_dropped() -> None:
    assert volume_root("/nowhere/a.mp4", ismount=lambda p: False) is None
    assert (
        group_by_volume(
            [("/nowhere/a.mp4", 4 * GB)],
            [],
            usage=usage_table(),
            ismount=lambda p: False,
        )
        == []
    )


def test_a_volume_that_cannot_be_read_is_dropped() -> None:
    """Expected behaviour: a dead network mount leaves the other bars intact."""

    def usage(root: str):
        if root == "/media/field":
            raise OSError("transport endpoint is not connected")
        return types.SimpleNamespace(total=500 * GB, free=100 * GB)

    volumes = group_by_volume(
        [("/media/field/a.mp4", 4 * GB)],
        [("/home/evan/runs/dive1", 3 * GB)],
        usage=usage,
        ismount=ismount,
    )
    assert [v.root for v in volumes] == ["/"]


def test_unmeasured_sizes_count_as_zero_but_are_reported() -> None:
    (volume,) = group_by_volume(
        [("/media/field/a.mp4", None), ("/media/field/b.mp4", 2 * GB)],
        [],
        usage=usage_table(**{"/media/field": (64 * GB, 40 * GB)}),
        ismount=ismount,
    )
    assert (volume.video_bytes, volume.unmeasured_items) == (2 * GB, 1)


def test_nothing_referenced_means_no_bars() -> None:
    assert group_by_volume([], [], usage=usage_table(), ismount=ismount) == []


def test_an_empty_path_names_no_volume() -> None:
    assert volume_root("", ismount=ismount) is None


def test_usage_is_a_frozen_record() -> None:
    volume = VolumeUsage("/", "/", 10, 4, 3, 2)
    assert volume.other_used_bytes == 1


def volume_at(*, total: int, free: int) -> VolumeUsage:
    return VolumeUsage("/", "/", total, free, 0, 0)


def test_a_comfortable_drive_is_not_banded() -> None:
    assert fullness(volume_at(total=4000 * GB, free=1600 * GB)) == FULLNESS_OK


def test_the_percentage_bands_a_drive_with_room_to_spare() -> None:
    """A 4 TB external at 96% is still full, however many bytes are left."""
    assert fullness(volume_at(total=4000 * GB, free=160 * GB)) == FULLNESS_FULL
    assert fullness(volume_at(total=4000 * GB, free=520 * GB)) == FULLNESS_TIGHT


def test_headroom_bands_a_drive_the_percentage_calls_fine() -> None:
    """80% of a 60 GB card is 12 GB, which is four passes and no margin."""
    assert fullness(volume_at(total=60 * GB, free=9 * GB)) == FULLNESS_FULL
    assert fullness(volume_at(total=60 * GB, free=11 * GB)) == FULLNESS_TIGHT


def test_the_worse_of_the_two_readings_wins() -> None:
    """Half empty by percentage, under the download floor by headroom."""
    assert fullness(volume_at(total=16 * GB, free=8 * GB)) == FULLNESS_FULL


def test_the_download_floor_is_the_one_models_cache_refuses_under() -> None:
    from deepreefmap_gui.models.cache import _MIN_FREE_BYTES

    assert MIN_FREE_BYTES == _MIN_FREE_BYTES


def test_a_drive_reporting_no_size_is_not_divided_by() -> None:
    assert used_percent(volume_at(total=0, free=0)) == 0.0
