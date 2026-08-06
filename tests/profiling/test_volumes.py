"""Storage accounting against a described mount table, never the host's disks."""

from __future__ import annotations

import types

from deepreefmap_gui.profiling.volumes import VolumeUsage, group_by_volume, volume_root

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
