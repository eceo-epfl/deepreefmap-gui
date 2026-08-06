"""The per-drive storage bars at the foot of the window.

A standalone widget, so these take the root `qapp` fixture rather than a window.
"""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtGui import QPixmap

from deepreefmap_gui.core.storage_bar import MAX_BARS, StorageBars, VolumeBar
from deepreefmap_gui.profiling.system_probe import format_bytes
from deepreefmap_gui.profiling.volumes import VolumeUsage

GB = 1024**3


def make_volume(label: str, *, total: int = 500 * GB, unmeasured: int = 0) -> VolumeUsage:
    return VolumeUsage(
        root=f"/mnt/{label}",
        label=label,
        total_bytes=total,
        free_bytes=total // 2,
        video_bytes=total // 5,
        output_bytes=total // 10,
        unmeasured_items=unmeasured,
    )


def paint(widget) -> None:
    """Force a real paintEvent, which is where the arithmetic lives."""
    widget.resize(QSize(200, 40))
    widget.render(QPixmap(widget.size()))


def test_no_volumes_hides_the_widget(qapp) -> None:
    bars = StorageBars()
    bars.set_volumes([])
    assert not bars.isVisible()
    assert bars.bars == []


def test_a_bar_per_drive(qapp) -> None:
    bars = StorageBars()
    bars.set_volumes([make_volume(name) for name in ("a", "b", "c")])
    bars.show()
    assert len(bars.bars) == 3
    assert not bars.overflow_label.isVisible()


def test_extra_drives_collapse_into_an_overflow_label(qapp) -> None:
    volumes = [make_volume(name) for name in ("a", "b", "c", "d", "e")]
    bars = StorageBars()
    bars.set_volumes(volumes)
    bars.show()

    assert len(bars.bars) == MAX_BARS
    assert bars.overflow_label.isVisible()
    assert bars.overflow_label.text() == "+2 more"
    tooltip = bars.overflow_label.toolTip()
    assert "d" in tooltip and "e" in tooltip


def test_compact_mode_shows_only_the_first_drive(qapp) -> None:
    volumes = [make_volume(name) for name in ("a", "b", "c")]
    bars = StorageBars()
    bars.set_volumes(volumes)
    bars.show()
    bars.set_compact(True)

    assert len(bars.bars) == 1
    assert bars.bars[0].usage() is volumes[0]
    assert bars.overflow_label.text() == "+2 more"

    bars.set_compact(False)
    assert len(bars.bars) == 3
    assert not bars.overflow_label.isVisible()


def test_repeated_refreshes_reuse_the_same_bars(qapp) -> None:
    """It is refreshed on a timer, so a rebuild must not stack up child widgets."""
    bars = StorageBars()
    volumes = [make_volume(name) for name in ("a", "b")]
    bars.set_volumes(volumes)
    bars.show()
    first = bars.bars

    for _ in range(5):
        bars.set_volumes(volumes)
    assert bars.bars == first
    assert len(bars.findChildren(VolumeBar)) == 2


def test_the_tooltip_names_the_drive_and_every_figure(qapp) -> None:
    volume = make_volume("a")
    bar = VolumeBar()
    bar.set_usage(volume)

    tooltip = bar.toolTip()
    assert volume.root in tooltip
    for value in (
        volume.video_bytes,
        volume.output_bytes,
        volume.other_used_bytes,
        volume.free_bytes,
    ):
        assert format_bytes(value) in tooltip


def test_the_tooltip_says_when_some_items_went_unmeasured(qapp) -> None:
    """Those bytes land in "other used", so the bar looks wrong without a word."""
    bar = VolumeBar()
    bar.set_usage(make_volume("a", unmeasured=3))
    assert "3" in bar.toolTip()


def test_an_empty_drive_paints_without_dividing_by_zero(qapp) -> None:
    bar = VolumeBar()
    bar.set_usage(
        VolumeUsage(root="/mnt/x", label="x", total_bytes=0, free_bytes=0,
                    video_bytes=0, output_bytes=0)
    )
    paint(bar)


def test_the_widget_paints_with_volumes(qapp) -> None:
    bars = StorageBars()
    bars.set_volumes([make_volume(name) for name in ("a", "b", "c", "d")])
    paint(bars)
