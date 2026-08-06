"""Where a survey's disk space went, as one thin bar per drive.

The bars sit at the foot of the window for the whole session, so a diver filling
a laptop in the field sees it happening rather than finding out when a run dies.
Only the drives the survey names are drawn: see profiling/volumes.py, which does
all the accounting. This module paints, it does not measure.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from deepreefmap_gui.core.theme import (
    BAR_HEIGHT,
    GROOVE,
    PRIMARY,
    SPACE_SM,
    SPACE_XS,
    SUCCESS,
    SURFACE_HI,
)
from deepreefmap_gui.core.widgets import muted_label, secondary_label
from deepreefmap_gui.profiling.system_probe import format_bytes
from deepreefmap_gui.profiling.volumes import VolumeUsage

# Narrower than this and the three segments stop being separable from each
# other, which is the only thing the bar is for.
BAR_MIN_WIDTH = 72

# How many drives get a bar of their own. A field laptop has a system disk plus
# a card reader plus an external, and past that the row is wider than the status
# it shares the foot of the window with.
MAX_BARS = 3


class VolumeBar(QWidget):
    """One drive: clips in, our outputs, everything else, and what is left.

    Painted rather than assembled from styled child widgets, because the four
    parts have to add up to one continuous groove with no seams between them.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._usage: VolumeUsage | None = None
        self.setFixedHeight(BAR_HEIGHT)
        self.setMinimumWidth(BAR_MIN_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def usage(self) -> VolumeUsage | None:
        return self._usage

    def set_usage(self, volume: VolumeUsage) -> None:
        self._usage = volume
        self.setToolTip(volume_tooltip(volume))
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        track = QRectF(self.rect())
        radius = track.height() / 2.0
        path = QPainterPath()
        path.addRoundedRect(track, radius, radius)
        painter.fillPath(path, QColor(GROOVE))
        # Segments are square-cornered rectangles, so the groove's own rounding
        # is what shapes the ends of the bar.
        painter.setClipPath(path)

        volume = self._usage
        total = volume.total_bytes if volume is not None else 0
        if volume is not None and total > 0:
            segments = (
                (PRIMARY, volume.video_bytes),
                (SUCCESS, volume.output_bytes),
                (SURFACE_HI, volume.other_used_bytes),
            )
            # Offsets accumulate in fractions of the track rather than in pixels,
            # so rounding cannot open a groove-coloured seam between two segments.
            start = 0.0
            for colour, size in segments:
                end = min(1.0, start + size / total)
                if end > start:
                    left = track.left() + track.width() * start
                    width = track.width() * (end - start)
                    painter.fillRect(
                        QRectF(left, track.top(), width, track.height()), QColor(colour)
                    )
                start = end
        painter.end()


def volume_tooltip(volume: VolumeUsage) -> str:
    """The drive, and every figure the bar draws, spelled out."""
    lines = [
        volume.root,
        f"Videos: {format_bytes(volume.video_bytes)}",
        f"Outputs: {format_bytes(volume.output_bytes)}",
        f"Other used: {format_bytes(volume.other_used_bytes)}",
        f"Free: {format_bytes(volume.free_bytes)} of {format_bytes(volume.total_bytes)}",
    ]
    if volume.unmeasured_items:
        # Those bytes are counted, just not as ours: they sit in "other used",
        # which otherwise reads as though the survey has taken no room at all.
        lines.append(
            f"{volume.unmeasured_items} item(s) of unknown size, counted under other used"
        )
    return "\n".join(lines)


def _caption(volume: VolumeUsage, *, compact: bool) -> str:
    free = format_bytes(volume.free_bytes)
    return f"{volume.label} {free}" if compact else f"{volume.label}  {free} free"


class _VolumeRow(QWidget):
    """A caption and its bar, kept together so a refresh can reuse the pair."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QVBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE_XS)
        self.caption = muted_label()
        row.addWidget(self.caption)
        self.bar = VolumeBar()
        row.addWidget(self.bar)

    def show_volume(self, volume: VolumeUsage, *, compact: bool) -> None:
        self.caption.setText(_caption(volume, compact=compact))
        self.caption.setToolTip(volume_tooltip(volume))
        self.bar.set_usage(volume)


class StorageBars(QWidget):
    """The drives this survey uses, side by side.

    Rows are pooled rather than rebuilt: this is refreshed on a timer, and
    deleting and recreating widgets every tick both churns and makes the foot of
    the window flicker as the layout settles.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._volumes: list[VolumeUsage] = []
        self._compact = False
        self._rows: list[_VolumeRow] = []

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(SPACE_SM)
        self._overflow = secondary_label()
        self._overflow.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self._layout.addWidget(self._overflow)
        self._overflow.setVisible(False)
        self.setVisible(False)

    @property
    def bars(self) -> list[VolumeBar]:
        """The bars currently on screen, in the order they are drawn."""
        # isVisibleTo, not isVisible: a row is only truly visible once the whole
        # window is, and this has to answer before then.
        return [row.bar for row in self._rows if row.isVisibleTo(self)]

    @property
    def overflow_label(self) -> QLabel:
        """The "+2 more" stand-in for the drives with no bar of their own."""
        return self._overflow

    def set_volumes(self, volumes: list[VolumeUsage]) -> None:
        self._volumes = list(volumes)
        self._refresh()

    def set_compact(self, compact: bool) -> None:
        """During a run the foot of the window also carries progress and an ETA."""
        self._compact = compact
        self._refresh()

    def _row(self, index: int) -> _VolumeRow:
        while len(self._rows) <= index:
            row = _VolumeRow(self)
            # Inserted before the overflow label, which stays last in the row.
            self._layout.insertWidget(len(self._rows), row)
            self._rows.append(row)
        return self._rows[index]

    def _refresh(self) -> None:
        limit = 1 if self._compact else MAX_BARS
        shown = self._volumes[:limit]
        for index, volume in enumerate(shown):
            row = self._row(index)
            row.show_volume(volume, compact=self._compact)
            row.setVisible(True)
        for row in self._rows[len(shown) :]:
            row.setVisible(False)

        hidden = self._volumes[len(shown) :]
        self._overflow.setText(f"+{len(hidden)} more" if hidden else "")
        self._overflow.setToolTip(
            "\n".join(f"{v.label}  {format_bytes(v.free_bytes)} free" for v in hidden)
        )
        self._overflow.setVisible(bool(hidden))
        self.setVisible(bool(self._volumes))
