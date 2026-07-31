"""Cursor-anchored zoom, and the viewer frame strip's click-to-open popups."""

from __future__ import annotations

import numpy as np
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
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


def _viewer_with_frames(qapp):
    from deepreefmap_gui.viewer.widget import QtPointCloudViewer

    viewer = QtPointCloudViewer()
    for kind in ("rgb", "seg", "depth"):
        viewer._frame_label(kind).setVisible(True)
        viewer._paint_label(kind, np.full((90, 160, 3), 40, dtype=np.uint8))
    return viewer


def test_clicking_a_frame_opens_it_at_full_resolution(qapp) -> None:
    viewer = _viewer_with_frames(qapp)

    for kind in ("rgb", "seg", "depth"):
        viewer._frame_label(kind).clicked.emit()
        dialog = viewer._frame_dialogs[kind]
        # The strip label holds a downscale; the popup holds the frame.
        assert dialog.view.pixmap().size() == viewer._frame_pixmaps[kind].size()
        assert dialog.view.pixmap().width() == 160


def test_repainting_a_frame_updates_an_open_popup_without_losing_zoom(qapp) -> None:
    viewer = _viewer_with_frames(qapp)
    viewer._frame_label("rgb").clicked.emit()
    dialog = viewer._frame_dialogs["rgb"]
    dialog.resize(400, 300)
    qapp.processEvents()
    _wheel(dialog.view, 2, QPointF(200, 150))
    zoomed = dialog.view.zoom_factor()
    assert zoomed > 1.0

    viewer._paint_label("rgb", np.full((90, 160, 3), 200, dtype=np.uint8))

    assert dialog.view.zoom_factor() == zoomed
    assert dialog.view.pixmap().toImage().pixelColor(0, 0).red() == 200


def test_a_hidden_frame_neither_opens_nor_stays_open(qapp) -> None:
    viewer = _viewer_with_frames(qapp)
    viewer._frame_label("seg").clicked.emit()
    assert "seg" in viewer._frame_dialogs

    viewer._set_frame_label_visible("seg", False)
    assert "seg" not in viewer._frame_dialogs

    viewer._frame_label("seg").clicked.emit()
    assert "seg" not in viewer._frame_dialogs


def test_the_strip_fills_out_once_the_panel_has_its_width(qapp) -> None:
    """The first frame is painted before the splitter has sized the strip.

    Without a rescale on resize the thumbnails stay at that initial narrow size
    until the next scrub repaints them.
    """
    from deepreefmap_gui.viewer.widget import QtPointCloudViewer

    viewer = QtPointCloudViewer()
    viewer.resize(240, 600)
    viewer.show()
    qapp.processEvents()
    viewer._paint_label("rgb", np.full((90, 160, 3), 40, dtype=np.uint8))
    assert viewer._rgb_label.pixmap().width() < 160

    viewer.resize(1200, 600)
    qapp.processEvents()

    # Filled out on the resize alone, without a repaint, capped at the frame's
    # own resolution.
    assert viewer._rgb_label.pixmap().width() == 160


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
