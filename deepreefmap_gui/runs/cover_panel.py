"""What a run found, as a stacked bar of benthic cover.

The detail pane's facts say which run you are looking at and what it cost. None
of them says what it saw, which is the reason the run was made. A bar reads that
in one glance, where the same numbers as a list would be another eight rows to
scan past.

Cover is read from the run's own `benthic_cover.json` rather than the manifest:
`run_record.py` keeps the two apart deliberately, so the file is the only place
the numbers live. Class *names* and fractions come from that file, which is what
lets a run processed under a different taxonomy still read correctly. Only the
colours come from the window's class table, so at worst an unusual run is
correct and oddly coloured.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from deepreefmap_gui.core.theme import (
    FONT_SM,
    GROOVE,
    RADIUS_SM,
    SPACE_XS,
    TEXT_MUTED,
)
from deepreefmap_gui.core.widgets import muted_label

logger = logging.getLogger(__name__)

# The bar itself. Thin, because it is a readout rather than a chart: it sits
# under a column of facts and should not out-weigh them.
_BAR_HEIGHT = 14

# Classes named beneath the bar, two to a line. Everything smaller is in the bar
# but not in the legend: past four the names are shorter than the segments they
# point at, and the pane has a fixed height to keep.
_LEGEND_ENTRIES = 4
_LEGEND_PER_LINE = 2

# Below this a class is not a segment anyone can see, so it is rolled into the
# bar without a name of its own.
_LEGEND_FLOOR = 0.001

_SWATCH = "■"


def load_cover(run_dir: Path) -> dict | None:
    """The cover report the pipeline wrote for a run, or None if it wrote none."""
    path = run_dir / "benthic_cover.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.debug("Unreadable benthic cover report: %s", path, exc_info=True)
        return None


def cover_shares(cover: dict | None) -> list[tuple[str, float, int | None]]:
    """Name, fraction and class id per class, largest first.

    The id rides along because it is what the class table colours by; the name
    is the run's own and may not appear in that table at all.
    """
    classes = cover.get("classes") if isinstance(cover, dict) else None
    if not isinstance(classes, dict):
        return []
    shares: list[tuple[str, float, int | None]] = []
    for raw_id, info in classes.items():
        if not isinstance(info, dict):
            continue
        try:
            fraction = float(info.get("fraction", 0.0))
        except (TypeError, ValueError):
            continue
        if fraction <= 0:
            continue
        try:
            class_id: int | None = int(raw_id)
        except (TypeError, ValueError):
            class_id = None
        shares.append((str(info.get("name", raw_id)), fraction, class_id))
    shares.sort(key=lambda item: -item[1])
    return shares


class _CoverBar(QWidget):
    """The stacked bar. Its own widget so the legend below it lays out normally."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._segments: list[tuple[QColor, float]] = []
        self.setFixedHeight(_BAR_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def set_segments(self, segments: list[tuple[QColor, float]]) -> None:
        self._segments = segments
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0, 0, self.width(), _BAR_HEIGHT)
        clip = QPainterPath()
        clip.addRoundedRect(rect, RADIUS_SM, RADIUS_SM)
        painter.setClipPath(clip)
        # The groove shows through wherever the classes do not reach 100%, and is
        # the whole bar for a run that produced no cover at all.
        painter.fillRect(rect, QColor(GROOVE))
        offset = 0.0
        for colour, fraction in self._segments:
            width = self.width() * fraction
            # Half a pixel of overlap: abutting fills leave hairlines of groove
            # between the segments at fractional widths.
            painter.fillRect(QRectF(offset, 0, width + 0.5, _BAR_HEIGHT), colour)
            offset += width


class CoverPanel(QWidget):
    """Benthic cover for one run: a heading, a stacked bar, a named legend.

    Fixed height whatever it is showing, including showing nothing. The pane
    around it is arrowed through one run at a time, and a block that collapses
    for a run with no cover would shuffle everything above it on every keypress.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._colours: dict[int, str] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_XS)

        self._heading = muted_label("Benthic cover")
        layout.addWidget(self._heading)

        self._bar = _CoverBar()
        layout.addWidget(self._bar)

        self._legend: list[QLabel] = []
        for _ in range(_LEGEND_ENTRIES // _LEGEND_PER_LINE):
            line = QLabel("")
            line.setTextFormat(Qt.TextFormat.RichText)
            line.setStyleSheet(f"color: {TEXT_MUTED}; font-size: {FONT_SM};")
            # Never wrap: a wrapped legend is a second line, and a second line is
            # the height jump this widget exists to avoid.
            line.setWordWrap(False)
            layout.addWidget(line)
            self._legend.append(line)

    def set_classes_config(self, classes_config) -> None:
        """The colour table. Names and fractions are the run's own, not this."""
        self._colours = {
            cls.id: QColor(*cls.color).name() for cls in classes_config.classes
        }

    def show_cover(self, cover: dict | None) -> None:
        shares = cover_shares(cover)
        self._bar.set_segments(
            [(QColor(self._colour_for(class_id)), fraction) for _, fraction, class_id in shares]
        )
        named = [item for item in shares if item[1] >= _LEGEND_FLOOR][:_LEGEND_ENTRIES]
        for index, line in enumerate(self._legend):
            chunk = named[index * _LEGEND_PER_LINE : (index + 1) * _LEGEND_PER_LINE]
            line.setText("&nbsp; ".join(self._entry_html(item) for item in chunk))
        self._heading.setVisible(True)

    def _entry_html(self, item: tuple[str, float, int | None]) -> str:
        name, fraction, class_id = item
        colour = self._colour_for(class_id)
        return (
            f'<span style="color: {colour}">{_SWATCH}</span> {name} {fraction * 100:.0f}%'
        )

    def _colour_for(self, class_id: int | None) -> str:
        """The class table's colour, or the groove for a class it has never seen."""
        if class_id is None:
            return GROOVE
        return self._colours.get(class_id, GROOVE)
