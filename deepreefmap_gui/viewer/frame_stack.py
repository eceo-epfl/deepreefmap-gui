"""The frame, segmentation and depth images as one stack of blended layers.

The three are the same pixel grid for a given frame, so stacking rather than
tiling them puts a class boundary directly over the reef it was drawn from, and
gives each three times the width.

Opacity is what makes the stack readable rather than a mess: sliding
segmentation up dissolves it over the frame, and a layer soloed to itself is the
old single-image view.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.theme import (
    BORDER,
    GROOVE,
    PREVIEW_BG,
    PRIMARY,
    PRIMARY_DARK,
    SLIDER_HANDLE,
    TEXT_SECONDARY,
)

# A handle big enough to grab without aiming, on a groove tall enough to be an
# obvious click target in its own right.
_SLIDER_QSS = f"""
QSlider::groove:horizontal {{
    height: 8px;
    background: {GROOVE};
    border: 1px solid {BORDER};
    border-radius: 4px;
}}
QSlider::sub-page:horizontal {{
    background: {PRIMARY};
    border: 1px solid {PRIMARY_DARK};
    border-radius: 4px;
}}
QSlider::handle:horizontal {{
    background: {SLIDER_HANDLE};
    border: 2px solid {PRIMARY_DARK};
    width: 14px;
    height: 18px;
    margin: -6px 0;
    border-radius: 4px;
}}
QSlider::handle:horizontal:hover {{ background: #ffffff; }}
QSlider:hover::groove:horizontal {{ border-color: {PRIMARY}; }}
"""

# Bottom to top: the frame is the ground truth everything else is read against,
# depth sits on top because it is the one usually looked at on its own.
FRAME_LAYERS = ("rgb", "seg", "depth")

FRAME_TITLES = {
    "rgb": "Frame",
    "seg": "Segmentation",
    "depth": "Depth",
}

# Segmentation half-dissolved over the frame is the view that earns the stack;
# depth starts out of the way, one click from being soloed.
DEFAULT_OPACITY = {"rgb": 1.0, "seg": 0.45, "depth": 0.0}

_SWATCH_SIZE = QSize(28, 12)
# Rows sit beside the image rather than under it, so height is no longer taken
# from the frame: they can afford to be a comfortable size to hit.
_ROW_HEIGHT = 28
# The controls take a fixed strip and the image gets the rest of the width.
_CONTROLS_WIDTH = 330


class ScrubSlider(QSlider):
    """A slider that jumps to where it is clicked and drags on from there.

    Qt's default is a page-step towards the click, so placing a value means
    finding the handle first and nudging it, which is most of the work when
    three of these are being compared against each other.
    """

    def __init__(self, orientation, parent: QWidget | None = None) -> None:
        super().__init__(orientation, parent)
        self._hover_x: float | None = None
        # Tracking without a button held is what makes the click preview possible.
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _geometry(self) -> tuple[QRectF, float]:
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        style = self.style()
        groove = style.subControlRect(
            QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderGroove, self
        )
        handle = style.subControlRect(
            QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderHandle, self
        )
        return QRectF(groove), float(handle.width())

    def _value_at(self, x: float) -> int:
        groove, handle_w = self._geometry()
        span = groove.width() - handle_w
        position = x - groove.x() - handle_w / 2
        return int(
            QStyle.sliderValueFromPosition(
                self.minimum(), self.maximum(), int(position), max(1, int(span))
            )
        )

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._hover_x is None or self.isSliderDown():
            return
        # A faint fill up to the pointer: the bar is clickable anywhere, and this
        # says so by showing the level a click would set before it sets it.
        groove, _ = self._geometry()
        edge = min(max(self._hover_x, groove.left()), groove.right())
        preview = QRectF(groove.left(), groove.top(), edge - groove.left(), groove.height())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        color = QColor(PRIMARY)
        color.setAlpha(70)
        painter.setBrush(color)
        painter.drawRoundedRect(preview, groove.height() / 2, groove.height() / 2)

    def leaveEvent(self, event) -> None:
        self._hover_x = None
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setSliderDown(True)
            self.setValue(self._value_at(event.position().x()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        self._hover_x = event.position().x()
        if self.isSliderDown():
            self.setValue(self._value_at(self._hover_x))
            event.accept()
            return
        self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            self.setSliderDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)


def _ramp_pixmap(colors: np.ndarray) -> QPixmap:
    """A horizontal strip of ``colors`` (N, 3) at the swatch size."""
    row = np.ascontiguousarray(colors.astype(np.uint8)).reshape(1, -1, 3)
    image = QImage(
        row.data, row.shape[1], 1, 3 * row.shape[1], QImage.Format.Format_RGB888
    ).copy()
    return QPixmap.fromImage(image).scaled(
        _SWATCH_SIZE,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def layer_swatch(kind: str, class_colors: dict[int, tuple[int, int, int]]) -> QPixmap:
    """The colours a layer speaks in, as a chip for its row in the controls.

    This is the legend: depth carries its colour map, segmentation the classes
    actually present in the run, and the frame a grey ramp standing for the
    photograph itself.
    """
    if kind == "depth":
        from deepreefmap_gui.viewer.render import _colorize_depth

        ramp = np.linspace(0.0, 1.0, 64, dtype=np.float32).reshape(1, 64)
        return _ramp_pixmap(_colorize_depth(ramp).reshape(-1, 3))
    if kind == "seg" and class_colors:
        colors = [class_colors[key] for key in sorted(class_colors)]
        return _ramp_pixmap(np.asarray(colors, dtype=np.uint8))
    grey = np.linspace(40, 235, 64, dtype=np.uint8)
    return _ramp_pixmap(np.repeat(grey.reshape(-1, 1), 3, axis=1))


class CompositeFrameView(QWidget):
    """The available layers drawn over one another, each at its own opacity."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmaps: dict[str, QPixmap] = {}
        self._opacity: dict[str, float] = dict(DEFAULT_OPACITY)
        self._available: dict[str, bool] = dict.fromkeys(FRAME_LAYERS, True)
        # The frame is wider than it is tall, so height is what limits it: in a
        # short pane no amount of free width makes the image any bigger.
        self.setMinimumHeight(240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet(f"background-color: {PREVIEW_BG};")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to open the stack at full size")

    # --- layer state ---

    def set_layer(self, kind: str, pixmap: QPixmap) -> None:
        self._pixmaps[kind] = pixmap
        self.update()

    def clear_layer(self, kind: str) -> None:
        self._pixmaps.pop(kind, None)
        self.update()

    def clear_layers(self) -> None:
        """Drop every layer, so the next run starts from an empty pane.

        Stacking makes a stale layer dangerous in a way three separate
        thumbnails were not: one run's depth left under the next run's frame
        blends into something that looks like a plausible result.
        """
        self._pixmaps.clear()
        self.update()

    def layer_pixmap(self, kind: str) -> QPixmap | None:
        return self._pixmaps.get(kind)

    def set_opacity(self, kind: str, value: float) -> None:
        self._opacity[kind] = max(0.0, min(1.0, float(value)))
        self.update()

    def opacity(self, kind: str) -> float:
        return self._opacity.get(kind, 0.0)

    def set_layer_available(self, kind: str, available: bool) -> None:
        """A layer the run does not produce is not drawn and not offered."""
        self._available[kind] = available
        self.update()

    def is_layer_available(self, kind: str) -> bool:
        return self._available.get(kind, False)

    def visible_layers(self) -> list[str]:
        """Layers that would actually put ink on the pane, bottom first."""
        return [
            kind
            for kind in FRAME_LAYERS
            if self._available.get(kind)
            and self._opacity.get(kind, 0.0) > 0.0
            and not (self._pixmaps.get(kind) or QPixmap()).isNull()
        ]

    def has_content(self) -> bool:
        return any(not pixmap.isNull() for pixmap in self._pixmaps.values())

    # --- painting ---

    def _source_size(self) -> QSize | None:
        """The frame's own pixel size.

        Taken from the frame in preference to the layers drawn over it, and
        deliberately not from the set that happens to be visible: a composite
        that changed size when a layer was soloed would reset the popup's zoom
        to fit on every slider move, since ZoomableImageView only holds the zoom
        across a same-sized image.
        """
        for kind in ("rgb", *FRAME_LAYERS):
            pixmap = self._pixmaps.get(kind)
            if pixmap is not None and not pixmap.isNull():
                return pixmap.size()
        return None

    def _target_rect(self) -> QRectF | None:
        """Where the stack lands: fitted to the pane, never blown up past native."""
        source = self._source_size()
        if source is None or source.width() <= 0 or source.height() <= 0:
            return None
        scale = min(
            self.width() / source.width(),
            self.height() / source.height(),
            1.0,
        )
        width = source.width() * scale
        height = source.height() * scale
        return QRectF(
            (self.width() - width) / 2, (self.height() - height) / 2, width, height
        )

    def _draw_layers(self, painter: QPainter, target: QRectF) -> None:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        for kind in self.visible_layers():
            pixmap = self._pixmaps[kind]
            painter.setOpacity(self._opacity[kind])
            painter.drawPixmap(target, pixmap, QRectF(pixmap.rect()))
        painter.setOpacity(1.0)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().window())
        target = self._target_rect()
        if target is None:
            return
        self._draw_layers(painter, target)

    def composite_pixmap(self) -> QPixmap | None:
        """The stack as it stands, at the frame's own resolution.

        The popup shows what the pane shows rather than a chosen layer, so the
        blend being read is the blend that opens.
        """
        source = self._source_size()
        if source is None:
            return None
        out = QPixmap(source)
        out.fill(Qt.GlobalColor.black)
        painter = QPainter(out)
        self._draw_layers(painter, QRectF(0, 0, source.width(), source.height()))
        painter.end()
        return out

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


@dataclass
class _LayerRow:
    row: QWidget
    swatch: QLabel
    name: QToolButton
    slider: QSlider
    percent: QLabel


class FrameLayerControls(QWidget):
    """One row per layer: what it looks like, how much of it, and solo."""

    opacity_changed = Signal(str, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: dict[str, _LayerRow] = {}
        self._solo: str | None = None
        # The mix a solo interrupted, so leaving solo puts the blend back rather
        # than whatever the muted sliders were left holding.
        self._saved_mix: dict[str, float] | None = None
        self._applying = False

        # A fixed strip beside the image rather than a band under it: stacked
        # under, three rows of controls came straight out of the height the
        # frame and the 3D cloud were sharing.
        self.setFixedWidth(_CONTROLS_WIDTH)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 4, 4)
        layout.setSpacing(2)
        for kind in FRAME_LAYERS:
            layout.addWidget(self._build_row(kind))
        layout.addStretch(1)

    def _build_row(self, kind: str) -> QWidget:
        row = QWidget()
        row.setFixedHeight(_ROW_HEIGHT)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)

        swatch = QLabel()
        swatch.setFixedSize(_SWATCH_SIZE)
        swatch.setStyleSheet(f"border: 1px solid {BORDER};")

        name = QToolButton()
        name.setText(FRAME_TITLES[kind])
        name.setCheckable(True)
        # Wide enough for the longest name: an elided "Segm...tion" in a row of
        # three is exactly the label that needed reading.
        name.setFixedWidth(
            max(
                name.fontMetrics().horizontalAdvance(title) for title in FRAME_TITLES.values()
            )
            + 26
        )
        name.setToolTip(
            f"Show {FRAME_TITLES[kind].lower()} on its own; press again for the blend"
        )
        name.setFixedHeight(_ROW_HEIGHT)
        name.setStyleSheet("padding: 1px 6px;")
        name.toggled.connect(lambda on, k=kind: self._on_solo_toggled(k, on))

        slider = ScrubSlider(Qt.Orientation.Horizontal)
        slider.setFixedHeight(_ROW_HEIGHT)
        slider.setStyleSheet(_SLIDER_QSS)
        slider.setRange(0, 100)
        slider.setPageStep(10)
        slider.setValue(int(round(DEFAULT_OPACITY[kind] * 100)))
        slider.setToolTip(f"How much {FRAME_TITLES[kind].lower()} shows through")
        slider.valueChanged.connect(lambda value, k=kind: self._on_slider(k, value))

        percent = QLabel(f"{slider.value()}%")
        percent.setFixedWidth(38)
        percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        percent.setStyleSheet(f"color: {TEXT_SECONDARY};")

        row_layout.addWidget(swatch)
        row_layout.addWidget(name)
        row_layout.addWidget(slider, 1)
        row_layout.addWidget(percent)
        self._rows[kind] = _LayerRow(
            row=row, swatch=swatch, name=name, slider=slider, percent=percent
        )
        return row

    # --- state ---

    def set_swatches(self, class_colors: dict[int, tuple[int, int, int]]) -> None:
        for kind, widgets in self._rows.items():
            widgets.swatch.setPixmap(layer_swatch(kind, class_colors))

    def set_layer_available(self, kind: str, available: bool) -> None:
        self._rows[kind].row.setVisible(available)
        if not available and self._solo == kind:
            self._rows[kind].name.setChecked(False)

    def opacities(self) -> dict[str, float]:
        return {
            kind: self._slider(kind).value() / 100.0 for kind in FRAME_LAYERS
        }

    def _slider(self, kind: str) -> QSlider:
        return self._rows[kind].slider

    def _set_value(self, kind: str, value: int) -> None:
        """Move a slider and report it, without letting one move drive another."""
        slider = self._slider(kind)
        if slider.value() == value:
            return
        slider.setValue(value)

    def _on_slider(self, kind: str, value: int) -> None:
        self._rows[kind].percent.setText(f"{value}%")
        self.opacity_changed.emit(kind, value / 100.0)
        # Reaching for a slider is a request for a blend, so it drops the solo
        # without disturbing the values the other layers are sitting at.
        if not self._applying and self._solo is not None:
            self._applying = True
            self._rows[self._solo].name.setChecked(False)
            self._solo = None
            self._saved_mix = None
            self._applying = False

    def _on_solo_toggled(self, kind: str, on: bool) -> None:
        if self._applying:
            return
        self._applying = True
        try:
            if on:
                if self._saved_mix is None:
                    self._saved_mix = self.opacities()
                for other in FRAME_LAYERS:
                    if other != kind:
                        self._rows[other].name.setChecked(False)
                    self._set_value(other, 100 if other == kind else 0)
                self._solo = kind
            else:
                mix, self._saved_mix = self._saved_mix, None
                self._solo = None
                for other, value in (mix or DEFAULT_OPACITY).items():
                    self._set_value(other, int(round(value * 100)))
        finally:
            self._applying = False

    def solo(self) -> str | None:
        return self._solo
