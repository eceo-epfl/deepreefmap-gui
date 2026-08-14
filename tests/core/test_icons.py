"""The icon set on a scaled screen.

Expected behaviour: a glyph carries a bitmap per screen scale, so a 150% or
200% display draws it from pixels it has rather than from an upscaled 16px one.
"""

from __future__ import annotations

from PySide6.QtCore import QSize

from deepreefmap_gui.core.icons import (
    ICON_SM,
    cog_icon,
    icon_pixmap,
    status_dot_icon,
)


def test_glyph_carries_a_bitmap_per_scale(qapp):
    sizes = {size.width() for size in cog_icon().availableSizes()}
    assert sizes == {ICON_SM, ICON_SM * 2, ICON_SM * 3}


def test_dot_carries_a_bitmap_per_scale(qapp):
    sizes = {size.width() for size in status_dot_icon("#ff0000").availableSizes()}
    assert sizes == {ICON_SM, ICON_SM * 2, ICON_SM * 3}


def test_scaled_screen_gets_more_pixels(qapp):
    for ratio in (1.5, 2.0, 3.0):
        pixmap = cog_icon().pixmap(QSize(ICON_SM, ICON_SM), ratio)
        assert pixmap.width() == int(ICON_SM * ratio)


def test_label_pixmap_follows_the_ratio_it_is_given(qapp):
    pixmap = icon_pixmap(status_dot_icon("#ff0000"), ICON_SM, 2.0)
    assert pixmap.width() == ICON_SM * 2
    assert pixmap.devicePixelRatio() == 2.0
