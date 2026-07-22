"""Theme application and token validity."""

from __future__ import annotations


def test_apply_theme_sets_dark_palette(qapp) -> None:
    # apply_theme mutates the shared app, so snapshot and restore to keep other
    # tests isolated. We assert on the palette (not style().objectName(), which
    # the global stylesheet wraps in an empty-named QStyleSheetStyle proxy).
    from PySide6.QtGui import QPalette

    from deepreefmap_gui.core.theme import apply_theme

    prev_style = qapp.style().objectName()
    prev_palette = QPalette(qapp.palette())
    prev_qss = qapp.styleSheet()
    try:
        apply_theme(qapp)
        win = qapp.palette().color(QPalette.ColorRole.Window)
        button = qapp.palette().color(QPalette.ColorRole.Button)
        assert win.red() < 80 and win.green() < 80 and win.blue() < 80
        # Window is the app shell, the bottom of the elevation ramp, so the
        # controls that sit on top of it are lighter.
        assert win.lightness() < button.lightness()
    finally:
        qapp.setStyleSheet(prev_qss)
        qapp.setPalette(prev_palette)
        if prev_style:
            qapp.setStyle(prev_style)


def test_theme_semantic_constants_are_valid_hex() -> None:
    from PySide6.QtGui import QColor

    from deepreefmap_gui.core import theme

    for name in ("SUCCESS", "WARNING", "ERROR", "PRIMARY", "LINK", "UPDATE", "DANGER_BG"):
        assert QColor(getattr(theme, name)).isValid()


def test_elevation_ramp_is_ordered() -> None:
    """Each surface is visibly lighter than the one it sits on.

    Panels that fall within a few greys of the shell are what made the app read
    flat, so the ramp is asserted rather than left to drift.
    """
    from PySide6.QtGui import QColor

    from deepreefmap_gui.core import theme

    ramp = [theme.WINDOW, theme.BASE, theme.CARD_BG, theme.BUTTON, theme.SURFACE_HI]
    lightness = [QColor(value).lightness() for value in ramp]
    assert lightness == sorted(lightness)
    assert lightness[-1] - lightness[0] >= 20

    # A hairline catches light off the panel it edges, but never outshines the
    # topmost fill: a border brighter than its own hover state reads as a frame
    # drawn on the page rather than an edge between two surfaces.
    border = QColor(theme.BORDER).lightness()
    assert QColor(theme.CARD_BG).lightness() < border < QColor(theme.SURFACE_HI).lightness()

