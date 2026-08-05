import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QWheelEvent
from PySide6.QtTest import QTest

from deepreefmap_gui.map.layers import OSM_LAYER
from deepreefmap_gui.map.overlays import OverlayTransect
from deepreefmap_gui.map.slippy_map import SlippyMapWidget
from deepreefmap_gui.map.tile_cache import TileCache


@pytest.fixture
def map_widget(qapp):
    cache = TileCache(OSM_LAYER)
    cache.network_enabled = False
    widget = SlippyMapWidget(cache=cache)
    widget.resize(400, 300)
    return widget


def make_overlay(selected=False):
    return OverlayTransect(
        id="11111111-1111-1111-1111-111111111111",
        start=(-17.5, 177.1),
        end=(-17.5005, 177.1005),
        color=QColor(0, 170, 170),
        selected=selected,
    )


def test_latlon_at_center_matches_view(map_widget):
    map_widget.set_view(-17.5, 177.1, 15)
    lat, lon = map_widget.latlon_at(QPointF(200, 150))
    assert lat == pytest.approx(-17.5, abs=1e-4)
    assert lon == pytest.approx(177.1, abs=1e-4)


def test_px_and_latlon_are_inverse(map_widget):
    map_widget.set_view(-17.5, 177.1, 16)
    point = map_widget._px_of(-17.5002, 177.1003)
    lat, lon = map_widget.latlon_at(point)
    assert lat == pytest.approx(-17.5002, abs=1e-6)
    assert lon == pytest.approx(177.1003, abs=1e-6)


def test_fit_transects_centers_on_overlay(map_widget):
    map_widget.set_transects([make_overlay()])
    map_widget.fit_transects()
    lat, lon = map_widget.latlon_at(QPointF(200, 150))
    assert lat == pytest.approx(-17.50025, abs=1e-3)
    assert lon == pytest.approx(177.10025, abs=1e-3)


def test_click_on_empty_map_emits_coordinates(map_widget):
    map_widget.set_view(-17.5, 177.1, 15)
    clicks = []
    map_widget.map_clicked.connect(lambda lat, lon: clicks.append((lat, lon)))
    QTest.mouseClick(map_widget, Qt.MouseButton.LeftButton, pos=QPoint(200, 150))
    assert len(clicks) == 1
    assert clicks[0][0] == pytest.approx(-17.5, abs=1e-3)


def test_click_on_transect_emits_id(map_widget):
    overlay = make_overlay()
    map_widget.set_transects([overlay])
    map_widget.fit_transects()
    center = map_widget._px_of(
        (overlay.start[0] + overlay.end[0]) / 2, (overlay.start[1] + overlay.end[1]) / 2
    )
    hits = []
    map_widget.transect_clicked.connect(hits.append)
    QTest.mouseClick(
        map_widget, Qt.MouseButton.LeftButton, pos=QPoint(int(center.x()), int(center.y()))
    )
    assert hits == [overlay.id]


def test_endpoint_hit_requires_editable(map_widget):
    overlay = make_overlay(selected=True)
    map_widget.set_transects([overlay])
    map_widget.fit_transects()
    start_px = map_widget._px_of(*overlay.start)
    assert map_widget._endpoint_at(start_px) is None
    map_widget.set_editable(overlay.id)
    hit = map_widget._endpoint_at(start_px)
    assert hit is not None
    assert hit[1] == "start"


def test_render_without_network_is_safe(map_widget):
    map_widget.set_transects([make_overlay(selected=True)])
    map_widget.fit_transects()
    image = map_widget.grab()
    assert not image.isNull()


def test_network_disabled_reads_as_offline(qapp):
    cache = TileCache(OSM_LAYER)
    cache.network_enabled = False
    assert cache.offline is True
    cache.network_enabled = True
    assert cache.offline is False


def test_connectivity_error_flips_offline_but_a_404_does_not(qapp):
    from PySide6.QtNetwork import QNetworkReply

    cache = TileCache(OSM_LAYER)
    flips = []
    cache.offline_changed.connect(flips.append)

    cache._note_reply_error(QNetworkReply.NetworkError.HostNotFoundError)
    assert cache.offline is True
    # A missing tile past the edge of coverage is not a lost connection.
    cache._note_reply_error(QNetworkReply.NetworkError.ContentNotFoundError)
    assert cache.offline is True
    cache._note_reply_error(QNetworkReply.NetworkError.NoError)
    assert cache.offline is False
    assert flips == [True, False]


def test_offline_banner_renders(map_widget):
    # The fixture disables the network, so the map is offline and the banner
    # paints over whatever saved tiles exist rather than a bare grid.
    assert map_widget.is_offline()
    map_widget.set_view(-17.5, 177.1, 12)
    image = map_widget.grab()
    assert not image.isNull()


def wheel(widget, notches, at=None):
    at = at if at is not None else QPointF(200, 150)
    event = QWheelEvent(
        at,
        widget.mapToGlobal(at),
        QPoint(0, 0),
        QPoint(0, int(notches * 120)),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    widget.wheelEvent(event)


def test_one_wheel_notch_zooms_less_than_a_tile_level(map_widget):
    map_widget.set_view(-17.5, 177.1, 12)
    wheel(map_widget, 1)
    assert 12 < map_widget._zoom < 13
    assert map_widget._tile_zoom() == 12
    assert map_widget._tile_px() > 256


def test_three_wheel_notches_cross_one_tile_level(map_widget):
    map_widget.set_view(-17.5, 177.1, 12)
    for _ in range(3):
        wheel(map_widget, 1)
    assert map_widget._tile_zoom() == 13
    assert map_widget._zoom == pytest.approx(13.02)


def test_a_flung_trackpad_cannot_cross_the_whole_range(map_widget):
    map_widget.set_view(-17.5, 177.1, 12)
    wheel(map_widget, 40)
    assert map_widget._zoom <= 12 + 3 * 0.34 + 1e-9


def test_zoom_holds_the_point_under_the_cursor(map_widget):
    map_widget.set_view(-17.5, 177.1, 15)
    anchor = QPointF(310, 90)
    before = map_widget.latlon_at(anchor)
    wheel(map_widget, 1, at=anchor)
    after = map_widget.latlon_at(anchor)
    assert after[0] == pytest.approx(before[0], abs=1e-6)
    assert after[1] == pytest.approx(before[1], abs=1e-6)


def test_zoom_stops_at_the_layer_bounds(map_widget):
    map_widget.set_view(-17.5, 177.1, 19)
    wheel(map_widget, 1)
    assert map_widget._zoom == 19
    map_widget.set_view(-17.5, 177.1, 1)
    wheel(map_widget, -1)
    assert map_widget._zoom == 1


def test_pick_mode_sends_clicks_past_transects(map_widget):
    """While picking a coordinate, a click on a line sets the point rather than
    selecting that transect."""
    overlay = make_overlay()
    map_widget.set_transects([overlay])
    map_widget.fit_transects()
    center = map_widget._px_of(
        (overlay.start[0] + overlay.end[0]) / 2, (overlay.start[1] + overlay.end[1]) / 2
    )
    at = QPoint(int(center.x()), int(center.y()))
    picked, selected = [], []
    map_widget.map_clicked.connect(lambda lat, lon: picked.append((lat, lon)))
    map_widget.transect_clicked.connect(selected.append)

    QTest.mouseClick(map_widget, Qt.MouseButton.LeftButton, pos=at)
    assert selected == [overlay.id]
    assert picked == []

    map_widget.set_pick_mode(True)
    QTest.mouseClick(map_widget, Qt.MouseButton.LeftButton, pos=at)
    assert len(picked) == 1
    assert selected == [overlay.id]
    assert map_widget.cursor().shape() == Qt.CursorShape.CrossCursor


