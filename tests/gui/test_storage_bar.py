"""The per-drive storage bars at the foot of the window.

A standalone widget, so these take the root `qapp` fixture rather than a window.
"""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtGui import QPixmap

from deepreefmap_gui.core.storage_bar import MAX_BARS, StorageBars, VolumeBar, alert_colour
from deepreefmap_gui.core.theme import BLOCK, WARNING
from deepreefmap_gui.core.volume_card import VolumeCard
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
    assert not bars.overflow_button.isVisible()


def test_extra_drives_collapse_into_an_overflow_button(qapp) -> None:
    volumes = [make_volume(name) for name in ("a", "b", "c", "d", "e")]
    bars = StorageBars()
    bars.set_volumes(volumes)
    bars.show()

    assert len(bars.bars) == MAX_BARS
    assert bars.overflow_button.isVisible()
    assert bars.overflow_button.text() == "+2 more"
    tooltip = bars.overflow_button.toolTip()
    assert "d" in tooltip and "e" in tooltip


def test_compact_mode_shows_only_the_first_drive(qapp) -> None:
    volumes = [make_volume(name) for name in ("a", "b", "c")]
    bars = StorageBars()
    bars.set_volumes(volumes)
    bars.show()
    bars.set_compact(True)

    assert len(bars.bars) == 1
    assert bars.bars[0].usage() is volumes[0]
    assert bars.overflow_button.text() == "+2 more"

    bars.set_compact(False)
    assert len(bars.bars) == 3
    assert not bars.overflow_button.isVisible()


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


def test_a_press_asks_rather_than_lighting_itself(qapp) -> None:
    """The window decides where a press lands, so the button waits to be told."""
    bars = StorageBars()
    volumes = [make_volume("a"), make_volume("b")]
    bars.set_volumes(volumes)
    bars.show()
    asked: list[str] = []
    bars.volume_clicked.connect(asked.append)

    bars.buttons[0].click()
    assert asked == [volumes[0].root]
    assert not bars.buttons[0].isChecked()


def test_the_lit_button_follows_the_selected_root(qapp) -> None:
    bars = StorageBars()
    volumes = [make_volume("a"), make_volume("b")]
    bars.set_volumes(volumes)
    bars.show()

    bars.set_selected_root(volumes[1].root)
    assert [b.isChecked() for b in bars.buttons] == [False, True]

    bars.set_selected_root(None)
    assert not any(b.isChecked() for b in bars.buttons)


def test_compact_mode_keeps_the_drive_whose_page_is_open(qapp) -> None:
    """Starting a batch must not take the button out from under the open page."""
    bars = StorageBars()
    volumes = [make_volume(name) for name in ("a", "b", "c")]
    bars.set_volumes(volumes)
    bars.show()
    bars.set_selected_root(volumes[2].root)
    bars.set_compact(True)

    assert [b.usage() for b in bars.buttons] == [volumes[2]]
    assert bars.buttons[0].isChecked()


def test_a_tight_drive_is_banded_and_a_full_one_paints(qapp) -> None:
    tight = VolumeUsage(root="/mnt/t", label="t", total_bytes=100 * GB,
                        free_bytes=11 * GB, video_bytes=40 * GB, output_bytes=40 * GB)
    full = VolumeUsage(root="/mnt/f", label="f", total_bytes=100 * GB,
                       free_bytes=2 * GB, video_bytes=50 * GB, output_bytes=40 * GB)
    assert alert_colour(tight) == WARNING
    assert alert_colour(full) == BLOCK
    assert alert_colour(make_volume("a")) is None

    bar = VolumeBar()
    bar.set_usage(full)
    paint(bar)


def test_the_card_carries_the_figures_and_raises_no_tooltip_of_its_own(qapp) -> None:
    card = VolumeCard()
    volume = make_volume("a")
    card.set_volume(volume)

    inner = card.findChildren(VolumeBar)
    assert len(inner) == 1
    assert inner[0].toolTip() == ""


def raise_card(bars: StorageBars, index: int = 0) -> None:
    """Show the card the way an outlasted hover delay does."""
    bars._card_for = bars.buttons[index]
    bars._show_card()


def test_the_card_is_not_a_child_of_the_bars(qapp) -> None:
    """test_repeated_refreshes_reuse_the_same_bars counts VolumeBar children."""
    bars = StorageBars()
    bars.set_volumes([make_volume("a")])
    bars.show()
    raise_card(bars)

    assert bars._card is not None and bars._card.isVisible()
    assert len(bars.findChildren(VolumeBar)) == 1


def test_the_card_paints_its_own_background(qapp) -> None:
    """A plain QWidget draws no stylesheet box model without WA_StyledBackground."""
    from PySide6.QtCore import Qt

    card = VolumeCard()
    assert card.testAttribute(Qt.WidgetAttribute.WA_StyledBackground)


def test_a_pointer_that_moved_on_before_the_delay_raises_nothing(qapp) -> None:
    """Leaving cancels the clock, so the card never opens behind the pointer."""
    bars = StorageBars()
    bars.set_volumes([make_volume("a")])
    bars.show()
    button = bars.buttons[0]

    button.hovered.emit(True)
    assert bars._hover_timer.isActive()
    button.hovered.emit(False)

    assert not bars._hover_timer.isActive()
    assert bars._card_for is None


def test_pressing_a_drive_leaves_the_card_down(qapp) -> None:
    """The press opens the drive's page, which the card would then cover."""
    bars = StorageBars()
    bars.set_volumes([make_volume("a")])
    bars.show()
    button = bars.buttons[0]
    raise_card(bars)

    button.clicked.emit()
    assert bars._card is not None and not bars._card.isVisible()

    # Still down while the pointer stays on the button it just pressed.
    button.hovered.emit(True)
    assert not bars._hover_timer.isActive()

    # And available again once the pointer has been somewhere else.
    button.hovered.emit(False)
    button.hovered.emit(True)
    assert bars._hover_timer.isActive()


def test_a_card_the_cursor_has_left_is_taken_down_by_the_guard(qapp) -> None:
    """A tooltip window under the pointer can swallow the button's leave event."""
    bars = StorageBars()
    bars.set_volumes([make_volume("a")])
    bars.show()
    raise_card(bars)
    assert bars._guard_timer.isActive()

    bars._guard_card()

    assert not bars._card.isVisible()
    assert not bars._guard_timer.isActive()
