"""The drive figures at reading size, raised by hovering a drive button.

The bar at the foot of the window is eight pixels tall and has to share a row
with the status text, so it can show the shape of a drive and not the numbers.
This is where the numbers go: the same segments in the same order, at a size
somebody can read, with the bytes and the share each one takes.

A tooltip window rather than a popup: Qt.Popup grabs the mouse, and this sits
directly over the button somebody is about to click. It never takes focus, it
appears only on hover or focus, and it goes the moment either ends, so there is
nothing here to dismiss.
"""

from __future__ import annotations

from html import escape

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from deepreefmap_gui.core.hover_card import apply_hover_card_flags, place_near_widget
from deepreefmap_gui.core.storage_bar import (
    TALL_BAR_HEIGHT,
    VolumeBar,
    alert_colour,
    volume_headline,
    volume_rows,
)
from deepreefmap_gui.core.theme import (
    BORDER_STRONG,
    CARD_BG,
    FONT_LG,
    FONT_SM,
    RADIUS,
    SPACE_MD,
    SPACE_SM,
    SPACE_XS,
    TEXT_MUTED,
    WEIGHT_SEMIBOLD,
    WINDOW_TEXT,
)
from deepreefmap_gui.profiling.system_probe import format_bytes

# Wide enough for a mount path and a four-column table, narrow enough that it
# does not cover the run somebody is watching in the row above.
CARD_WIDTH = 320


class VolumeCard(QWidget):
    """One drive, spelled out: the headline, the bar, and every figure under it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        apply_hover_card_flags(self)
        self.setObjectName("volumeCard")
        self.setFixedWidth(CARD_WIDTH)
        self.setStyleSheet(
            f"QWidget#volumeCard {{ background-color: {CARD_BG};"
            f" border: 1px solid {BORDER_STRONG}; border-radius: {RADIUS}px; }}"
            f" QLabel {{ color: {WINDOW_TEXT}; }}"
        )

        column = QVBoxLayout(self)
        column.setContentsMargins(SPACE_MD, SPACE_SM, SPACE_MD, SPACE_SM)
        column.setSpacing(SPACE_XS)

        self._heading = QLabel()
        self._heading.setTextFormat(Qt.TextFormat.RichText)
        column.addWidget(self._heading)

        self._path = QLabel()
        self._path.setStyleSheet(f"color: {TEXT_MUTED}; font-size: {FONT_SM};")
        column.addWidget(self._path)

        column.addSpacing(SPACE_XS)
        self._bar = VolumeBar(height=TALL_BAR_HEIGHT, describe=False)
        column.addWidget(self._bar)
        column.addSpacing(SPACE_XS)

        self._figures = QLabel()
        self._figures.setTextFormat(Qt.TextFormat.RichText)
        column.addWidget(self._figures)

        self._footnote = QLabel()
        self._footnote.setWordWrap(True)
        self._footnote.setStyleSheet(f"color: {TEXT_MUTED}; font-size: {FONT_SM};")
        column.addWidget(self._footnote)

    def set_volume(self, volume, *, footage: str = "") -> None:
        headline_colour = alert_colour(volume) or WINDOW_TEXT
        self._heading.setText(
            f"<span style='font-size:{FONT_LG};font-weight:{WEIGHT_SEMIBOLD}'>{volume.label}</span>"
            f"&nbsp;&nbsp;<span style='color:{headline_colour}'>{volume_headline(volume)}</span>"
        )
        # A mount whose label is its path says it once. On POSIX the system
        # drive is "/" both ways, and two identical lines read as a bug.
        self._path.setText("" if volume.root == volume.label else volume.root)
        self._path.setVisible(volume.root != volume.label)
        self._bar.set_usage(volume)
        self._figures.setText(_figures_table(volume))
        # Free bytes are a number; how many more dives fit is the answer. Only
        # the drive the runs land on can be asked that, so only it says it.
        self._footnote.setText(footage)
        self._footnote.setVisible(bool(footage))
        self.adjustSize()

    def show_for(self, volume, anchor: QWidget, *, footage: str = "") -> None:
        """Sit above the button, or below it on a screen with no room above."""
        self.set_volume(volume, footage=footage)
        place_near_widget(self, anchor, prefer="above")
        self.show()


def _figures_table(volume) -> str:
    """A swatch, a name, the bytes and the share, in the bar's own paint order.

    Free gets a hollow swatch, because that is what it is on the bar: the groove
    with nothing painted over it. A filled one made it a fourth kind of content.
    """
    rows = []
    figures = volume_rows(volume)
    for index, (colour, label, size, percent) in enumerate(figures):
        glyph = "&#9633;" if index == len(figures) - 1 else "&#9632;"
        rows.append(
            f"<tr><td style='color:{colour};padding-right:6px'>{glyph}</td>"
            f"<td style='padding-right:12px'>{label}</td>"
            f"<td align='right' style='padding-right:10px;font-family:monospace'>{size}</td>"
            # Escaped: a share under one percent reads "<1%", which rich text
            # would otherwise swallow as the start of a tag.
            f"<td align='right' style='color:{TEXT_MUTED};font-family:monospace'>{escape(percent)}</td>"
            f"</tr>"
        )
    total = f"of {format_bytes(volume.total_bytes)}"
    rows.append(
        f"<tr><td></td><td colspan='3' style='color:{TEXT_MUTED};padding-top:4px'>{total}</td></tr>"
    )
    return f"<table cellspacing='0' width='100%'>{''.join(rows)}</table>"
