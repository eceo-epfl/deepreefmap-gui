"""Cursor-anchored zoom, and the viewer frame strip's click-to-open popups."""

from __future__ import annotations

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QImage, QPixmap, QWheelEvent


def _pixmap(width: int, height: int) -> QPixmap:
    return QPixmap.fromImage(QImage(width, height, QImage.Format.Format_RGB32))


def _wheel(view, notches: int, pos: QPointF) -> None:
    event = QWheelEvent(
        pos,
        view.mapToGlobal(pos.toPoint()),
        QPoint(0, 0),
        QPoint(0, 120 * notches),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    view.wheelEvent(event)


@pytest.fixture()
def view(qapp):
    from deepreefmap_gui.core.image_view import ZoomableImageView

    widget = ZoomableImageView()
    widget.resize(400, 300)
    widget.set_pixmap(_pixmap(1600, 400))
    widget.show()
    qapp.processEvents()
    return widget


def test_opens_fitted_to_width(view) -> None:
    assert view.is_fitted()
    assert view.zoom_factor() == 1.0
    assert view.transform().m11() == pytest.approx(view.viewport().width() / 1600)


def test_wheel_zooms_in_and_out(view) -> None:
    _wheel(view, 1, QPointF(200, 150))
    zoomed = view.zoom_factor()
    assert zoomed > 1.0

    _wheel(view, -1, QPointF(200, 150))
    assert view.zoom_factor() < zoomed


def test_zoom_keeps_the_point_under_the_cursor(view) -> None:
    """Expected behaviour: scrolling magnifies towards the mouse, not the centre.

    Zoomed in far enough to fill the viewport first: an axis with no scroll
    room left stays centred, so there is nothing to pin along it.
    """
    cursor = QPointF(320, 140)
    _wheel(view, 3, cursor)
    _wheel(view, 3, cursor)
    before = view.mapToScene(cursor.toPoint())

    _wheel(view, 2, cursor)

    after = view.mapToScene(cursor.toPoint())
    assert after.x() == pytest.approx(before.x(), abs=2.0)
    assert after.y() == pytest.approx(before.y(), abs=2.0)


def test_zoom_clamps_at_fit_and_at_the_ceiling(view) -> None:
    for _ in range(20):
        _wheel(view, 3, QPointF(200, 150))
    assert view.zoom_factor() == pytest.approx(view.max_zoom())

    for _ in range(40):
        _wheel(view, -3, QPointF(200, 150))
    assert view.zoom_factor() == 1.0
    assert view.is_fitted()


def test_double_click_returns_to_fit(view) -> None:
    _wheel(view, 3, QPointF(200, 150))
    assert not view.is_fitted()

    view.reset_zoom()

    assert view.is_fitted()


def test_same_size_image_keeps_the_zoom(view) -> None:
    _wheel(view, 2, QPointF(200, 150))
    zoomed = view.zoom_factor()

    view.set_pixmap(_pixmap(1600, 400))
    assert view.zoom_factor() == zoomed

    # A differently shaped image is a different image; the zoom means nothing.
    view.set_pixmap(_pixmap(800, 400))
    assert view.is_fitted()


# --- Viewer frame strip ---


def _solid(r, g, b, h=90, w=160):
    return np.dstack([
        np.full((h, w), value, dtype=np.uint8) for value in (r, g, b)
    ])


def _qimage(array):
    h, w, _ = array.shape
    return QImage(
        np.ascontiguousarray(array).data, w, h, 3 * w, QImage.Format.Format_RGB888
    ).copy()


def _settle_popup(viewer):
    """Let the coalescing timer behind the popup fire."""
    from PySide6.QtTest import QTest

    QTest.qWait(viewer._popup_refresh_timer.interval() * 3)


def _viewer_with_frames(qapp, colors=None):
    from deepreefmap_gui.viewer.widget import QtPointCloudViewer

    colors = colors or dict.fromkeys(("rgb", "seg", "depth"), (40, 40, 40))
    viewer = QtPointCloudViewer()
    for kind, color in colors.items():
        viewer._paint_label(kind, _solid(*color))
    return viewer


def test_clicking_the_stack_opens_the_blend_at_full_resolution(qapp) -> None:
    viewer = _viewer_with_frames(qapp)
    viewer.frame_stack.clicked.emit()

    dialog = viewer._frame_dialogs["stack"]
    # The pane holds a downscale fitted to it; the popup holds the frame.
    assert dialog.view.pixmap().size() == viewer._frame_pixmaps["rgb"].size()
    assert dialog.view.pixmap().width() == 160


def test_the_layers_blend_at_their_opacities(qapp) -> None:
    """Expected behaviour: segmentation half over the frame reads as half of
    each, not as one replacing the other."""
    viewer = _viewer_with_frames(qapp, {"rgb": (200, 0, 0), "seg": (0, 0, 200)})
    viewer._on_layer_opacity_changed("seg", 0.5)
    viewer._on_layer_opacity_changed("depth", 0.0)

    blended = viewer.frame_stack.composite_pixmap().toImage().pixelColor(0, 0)
    assert blended.red() == pytest.approx(100, abs=2)
    assert blended.blue() == pytest.approx(100, abs=2)

    viewer._on_layer_opacity_changed("seg", 0.0)
    frame_only = viewer.frame_stack.composite_pixmap().toImage().pixelColor(0, 0)
    assert (frame_only.red(), frame_only.blue()) == (200, 0)


def test_a_layer_at_zero_is_not_drawn(qapp) -> None:
    viewer = _viewer_with_frames(qapp)
    viewer._on_layer_opacity_changed("seg", 0.0)
    assert "seg" not in viewer.frame_stack.visible_layers()
    viewer._on_layer_opacity_changed("seg", 0.6)
    assert "seg" in viewer.frame_stack.visible_layers()


def test_repainting_a_frame_updates_an_open_popup_without_losing_zoom(qapp) -> None:
    viewer = _viewer_with_frames(qapp)
    viewer._on_layer_opacity_changed("seg", 0.0)
    viewer._on_layer_opacity_changed("depth", 0.0)
    viewer.frame_stack.clicked.emit()
    dialog = viewer._frame_dialogs["stack"]
    dialog.resize(400, 300)
    qapp.processEvents()
    _wheel(dialog.view, 2, QPointF(200, 150))
    zoomed = dialog.view.zoom_factor()
    assert zoomed > 1.0

    viewer._paint_label("rgb", _solid(200, 200, 200))
    _settle_popup(viewer)

    assert dialog.view.zoom_factor() == zoomed
    assert dialog.view.pixmap().toImage().pixelColor(0, 0).red() == 200


def test_moving_a_slider_shows_in_the_open_popup(qapp) -> None:
    viewer = _viewer_with_frames(qapp, {"rgb": (200, 0, 0), "seg": (0, 0, 200)})
    viewer._on_layer_opacity_changed("seg", 0.0)
    viewer._on_layer_opacity_changed("depth", 0.0)
    viewer.frame_stack.clicked.emit()
    dialog = viewer._frame_dialogs["stack"]

    viewer._on_layer_opacity_changed("seg", 1.0)
    _settle_popup(viewer)

    assert dialog.view.pixmap().toImage().pixelColor(0, 0).blue() == 200


def test_a_withdrawn_layer_is_neither_drawn_nor_offered(qapp) -> None:
    """A geometry-only run has no labels, so segmentation is not a layer it can
    show however far its slider is pushed."""
    viewer = _viewer_with_frames(qapp)
    viewer._on_layer_opacity_changed("seg", 1.0)
    assert "seg" in viewer.frame_stack.visible_layers()

    viewer._set_frame_label_visible("seg", False)

    assert "seg" not in viewer.frame_stack.visible_layers()
    assert not viewer.frame_layers._rows["seg"].row.isVisibleTo(viewer.frame_layers)


def test_the_stack_fills_the_pane_without_being_blown_up(qapp) -> None:
    """The first frame is painted before the splitter has sized the panel, so the
    pane has to follow its own width rather than the one it was born with."""
    from deepreefmap_gui.viewer.frame_stack import CompositeFrameView

    view = CompositeFrameView()
    view.resize(80, 300)
    view.set_layer("rgb", QPixmap.fromImage(_qimage(_solid(40, 40, 40))))
    assert view._target_rect().width() == 80

    view.resize(1200, 300)

    # Filled out on the resize alone, and capped at the frame's own resolution
    # rather than smeared across the pane.
    assert view._target_rect().width() == 160
    assert view._target_rect().height() == 90


def test_a_new_frame_keeps_where_you_were_looking(qapp) -> None:
    """Scrubbing while zoomed in should hold position, not jump to the corner."""
    from deepreefmap_gui.core.image_view import ZoomableImageView

    view = ZoomableImageView()
    view.resize(400, 300)
    view.show()
    view.set_pixmap(_pixmap(1600, 400))
    qapp.processEvents()

    _wheel(view, 3, QPointF(360, 150))
    _wheel(view, 3, QPointF(360, 150))
    before = view.mapToScene(view.viewport().rect().center())

    view.set_pixmap(_pixmap(1600, 400))

    after = view.mapToScene(view.viewport().rect().center())
    assert after.x() == pytest.approx(before.x(), abs=2.0)
    assert after.y() == pytest.approx(before.y(), abs=2.0)


# --- Layer controls ---


def _controls(qapp):
    from deepreefmap_gui.viewer.frame_stack import FrameLayerControls

    return FrameLayerControls()


def test_solo_isolates_a_layer_and_gives_the_mix_back(qapp) -> None:
    controls = _controls(qapp)
    controls._set_value("seg", 60)
    controls._set_value("depth", 20)

    controls._rows["depth"].name.setChecked(True)
    assert controls.opacities() == {"rgb": 0.0, "seg": 0.0, "depth": 1.0}
    assert controls.solo() == "depth"

    controls._rows["depth"].name.setChecked(False)
    assert controls.opacities() == {"rgb": 1.0, "seg": 0.6, "depth": 0.2}
    assert controls.solo() is None


def test_only_one_layer_is_soloed_at_a_time(qapp) -> None:
    controls = _controls(qapp)
    controls._rows["seg"].name.setChecked(True)
    controls._rows["depth"].name.setChecked(True)

    assert controls.solo() == "depth"
    assert not controls._rows["seg"].name.isChecked()
    assert controls.opacities() == {"rgb": 0.0, "seg": 0.0, "depth": 1.0}


def test_reaching_for_a_slider_leaves_solo(qapp) -> None:
    """Asking for some of another layer is asking for a blend."""
    controls = _controls(qapp)
    controls._rows["depth"].name.setChecked(True)
    controls._set_value("rgb", 70)

    assert controls.solo() is None
    assert not controls._rows["depth"].name.isChecked()
    # The values stay where the solo and the drag left them, rather than
    # springing back to a mix that was never asked for.
    assert controls.opacities() == {"rgb": 0.7, "seg": 0.0, "depth": 1.0}


def test_each_layer_carries_its_own_colours_as_a_legend(qapp) -> None:
    controls = _controls(qapp)
    controls.set_swatches({1: (255, 0, 0), 2: (0, 255, 0)})

    for kind in ("rgb", "seg", "depth"):
        swatch = controls._rows[kind].swatch.pixmap()
        assert not swatch.isNull()
    # The segmentation chip is drawn from the run's own classes, so it differs
    # from the neutral ramp standing in for the photograph.
    seg = controls._rows["seg"].swatch.pixmap().toImage()
    assert seg.pixelColor(1, 6).red() > seg.pixelColor(1, 6).blue()


# --- The panel across a run's lifetime ---


def test_a_new_run_does_not_inherit_the_last_one(qapp) -> None:
    """Scenario: run A finishes with all three layers, run B starts.

    Expected behaviour: nothing of A survives. Stacked layers make this matter in
    a way three thumbnails did not: A's depth left under B's frame blends into a
    picture that looks like a result rather than like a bug.
    """
    viewer = _viewer_with_frames(qapp, {"rgb": (200, 0, 0), "seg": (0, 200, 0), "depth": (0, 0, 200)})
    viewer.frame_stack.clicked.emit()
    assert "stack" in viewer._frame_dialogs

    viewer._on_start_run("run B", "/tmp/run-b")

    assert viewer.frame_stack.visible_layers() == []
    assert viewer.frame_stack.composite_pixmap() is None
    assert viewer._frame_pixmaps == {}
    assert "stack" not in viewer._frame_dialogs


def test_clearing_the_scene_empties_the_panel(qapp) -> None:
    viewer = _viewer_with_frames(qapp)
    viewer._clear_scene_data()
    assert viewer.frame_stack.visible_layers() == []


def test_a_run_starts_with_only_the_frame_offered(qapp) -> None:
    """During preprocessing there are no depth maps yet, and labels only once
    segmentation has run: neither layer is offered before it exists."""
    from deepreefmap_gui.viewer.widget import QtPointCloudViewer

    viewer = QtPointCloudViewer()
    viewer._on_start_run("run", "/tmp/run")

    assert viewer.frame_stack.is_layer_available("rgb")
    assert not viewer.frame_stack.is_layer_available("seg")
    assert not viewer.frame_stack.is_layer_available("depth")

    # Labels landing mid-preprocess bring the layer back with them.
    viewer._set_frame_label_visible("seg", True)
    viewer._paint_label("seg", _solid(0, 200, 0))
    assert "seg" in viewer.frame_stack.visible_layers()


def test_a_withdrawn_layer_leaves_no_pixels_behind(qapp) -> None:
    """Withdrawing has to drop the image, not just stop drawing it: the layer
    coming back must show the new run's pixels, not the old run's."""
    viewer = _viewer_with_frames(qapp)
    viewer._set_frame_label_visible("seg", False)
    assert viewer.frame_stack.layer_pixmap("seg") is None

    viewer._set_frame_label_visible("seg", True)
    assert "seg" not in viewer.frame_stack.visible_layers()


def test_the_composite_size_follows_the_frame_not_the_layer_set(qapp) -> None:
    """A composite that resized when a layer was toggled would slam the popup
    back to fit-on-corner on every slider move."""
    viewer = _viewer_with_frames(qapp)
    full = viewer.frame_stack.composite_pixmap().size()

    for kind in ("seg", "depth"):
        viewer._on_layer_opacity_changed(kind, 0.0)
        assert viewer.frame_stack.composite_pixmap().size() == full
    viewer._set_frame_label_visible("seg", False)
    assert viewer.frame_stack.composite_pixmap().size() == full
    viewer.frame_layers._rows["rgb"].name.setChecked(True)
    assert viewer.frame_stack.composite_pixmap().size() == full


def test_the_popup_opens_while_the_viewer_is_unshown(qapp) -> None:
    """The panel is legitimately not visible until the viewer is shown, so only
    an explicit hide should block the popup."""
    viewer = _viewer_with_frames(qapp)
    assert not viewer.frame_stack.isVisible()

    viewer.frame_stack.clicked.emit()
    assert "stack" in viewer._frame_dialogs

    viewer._close_frame_popup()
    viewer.frame_stack.hide()
    viewer.frame_stack.clicked.emit()
    assert "stack" not in viewer._frame_dialogs


def test_the_popup_is_not_rebuilt_per_slider_tick(qapp) -> None:
    """Compositing costs a full-resolution pixmap, so a drag has to coalesce."""
    viewer = _viewer_with_frames(qapp)
    viewer.frame_stack.clicked.emit()
    _settle_popup(viewer)

    builds = []
    original = viewer.frame_stack.composite_pixmap
    viewer.frame_stack.composite_pixmap = lambda: (builds.append(1), original())[1]
    for value in range(0, 100, 5):
        viewer._on_layer_opacity_changed("seg", value / 100.0)
    assert builds == []

    _settle_popup(viewer)
    assert len(builds) == 1


# --- Slider handling ---


def _scrub_slider(qapp, width=200):
    from deepreefmap_gui.viewer.frame_stack import ScrubSlider

    slider = ScrubSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, 100)
    slider.setValue(0)
    slider.resize(width, 28)
    slider.show()
    qapp.processEvents()
    return slider


def _press(slider, x):
    from PySide6.QtGui import QMouseEvent

    for kind, handler in (
        (QEvent.Type.MouseButtonPress, slider.mousePressEvent),
        (QEvent.Type.MouseButtonRelease, slider.mouseReleaseEvent),
    ):
        handler(QMouseEvent(
            kind, QPointF(x, 14), Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        ))


def test_clicking_the_bar_jumps_to_that_value(qapp) -> None:
    """Qt's default is a page-step towards the click, which means hunting for the
    handle before a value can be placed."""
    slider = _scrub_slider(qapp)
    _press(slider, slider.width() * 0.75)
    assert slider.value() == pytest.approx(75, abs=8)

    _press(slider, 0)
    assert slider.value() == 0
    _press(slider, slider.width())
    assert slider.value() == 100


def test_hovering_the_bar_previews_where_a_click_lands(qapp) -> None:
    from PySide6.QtGui import QMouseEvent

    slider = _scrub_slider(qapp)
    assert slider._hover_x is None

    slider.mouseMoveEvent(QMouseEvent(
        QEvent.Type.MouseMove, QPointF(120, 14), Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
    ))
    assert slider._hover_x == 120
    # Hovering shows the level, it does not set it.
    assert slider.value() == 0

    slider.leaveEvent(QEvent(QEvent.Type.Leave))
    assert slider._hover_x is None


def test_the_layer_rows_sit_beside_the_image_not_under_it(qapp) -> None:
    """Stacked under the frame, the rows came out of the height the frame and the
    3D cloud were sharing."""
    viewer = _viewer_with_frames(qapp)
    viewer.resize(1400, 800)
    viewer.show()
    qapp.processEvents()

    controls = viewer.frame_layers.geometry()
    image = viewer.frame_stack.geometry()
    assert image.left() >= controls.right()
    assert viewer.frame_stack.height() > viewer.frame_layers.height() * 0.5
