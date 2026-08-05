"""The bound on the viewer's composed image panels.

Scenario: playback walks a long run frame by frame. Each frame composes an
RGB/segmentation/depth triple at full processing resolution.

Expected behaviour: what is held stays inside a byte budget, and the frames kept
are the ones most recently looked at. Since the scene file stopped carrying
pixels, a miss costs two PNG decodes from the run directory rather than a zarr
chunk read -- the reason this is bounded by bytes and not dropped entirely.
"""

from __future__ import annotations

import numpy as np
import pytest

from deepreefmap_gui.viewer import point_cloud as widget_mod


@pytest.fixture
def viewer(qapp):
    from deepreefmap_gui.viewer.point_cloud import QtPointCloudViewer

    view = QtPointCloudViewer(class_colors={1: (255, 0, 0)}, class_names={1: "coral"})
    yield view
    view.deleteLater()


def _panel(size_px: int = 64):
    """One composed panel: three RGB images of the same shape."""
    return tuple(
        np.zeros((size_px, size_px, 3), dtype=np.uint8) for _ in range(3)
    )


def test_the_cache_stays_inside_its_byte_budget(viewer, monkeypatch):
    """A thousand frames at full resolution is several GB unbounded."""
    monkeypatch.setattr(widget_mod, "_FRAME_PANEL_CACHE_BYTES", 200_000)

    for t in range(200):
        viewer._cache_frame_panel(t, _panel())

    assert viewer._frame_panel_bytes <= 200_000
    assert len(viewer._frame_panel_cache) < 200


def test_the_tracked_total_matches_what_is_held(viewer, monkeypatch):
    """A drifting counter would either evict early or stop evicting at all."""
    monkeypatch.setattr(widget_mod, "_FRAME_PANEL_CACHE_BYTES", 200_000)

    for t in range(50):
        viewer._cache_frame_panel(t, _panel())

    held = sum(
        int(p.nbytes) for parts in viewer._frame_panel_cache.values() for p in parts
    )
    assert viewer._frame_panel_bytes == held


def test_re_caching_a_frame_does_not_double_count(viewer):
    viewer._cache_frame_panel(7, _panel())
    once = viewer._frame_panel_bytes

    viewer._cache_frame_panel(7, _panel())

    assert viewer._frame_panel_bytes == once
    assert len(viewer._frame_panel_cache) == 1


def test_the_least_recently_used_frame_goes_first(viewer, monkeypatch):
    monkeypatch.setattr(widget_mod, "_FRAME_PANEL_CACHE_BYTES", 1)
    monkeypatch.setattr(widget_mod, "_FRAME_PANEL_MIN_ENTRIES", 2)

    viewer._cache_frame_panel(1, _panel())
    viewer._cache_frame_panel(2, _panel())
    viewer._cached_frame_panel(1)
    viewer._cache_frame_panel(3, _panel())

    assert 2 not in viewer._frame_panel_cache, "eviction ignored the recent read of 1"
    assert set(viewer._frame_panel_cache) == {1, 3}


def test_a_floor_of_entries_survives_any_budget(viewer, monkeypatch):
    """A single panel larger than the whole budget must not evict itself."""
    monkeypatch.setattr(widget_mod, "_FRAME_PANEL_CACHE_BYTES", 1)

    for t in range(10):
        viewer._cache_frame_panel(t, _panel())

    assert len(viewer._frame_panel_cache) == widget_mod._FRAME_PANEL_MIN_ENTRIES


def test_the_playback_path_goes_through_the_cap(viewer, monkeypatch):
    """The cap only holds if the insert sites use it. A plain assignment back
    into the dict would pass every test above and still grow without bound."""
    monkeypatch.setattr(widget_mod, "_FRAME_PANEL_CACHE_BYTES", 200_000)
    monkeypatch.setattr(viewer, "_compose_frame_panel", lambda t: _panel())
    monkeypatch.setattr(viewer, "_paint_label", lambda *a: None)
    viewer._frame_batch = object()
    viewer._mapping_result = object()
    viewer._final_index = object()

    for t in range(200):
        viewer._update_image_panel(t)

    assert viewer._frame_panel_bytes <= 200_000
    assert len(viewer._frame_panel_cache) < 200


def test_the_frame_export_path_goes_through_the_cap(viewer, monkeypatch):
    monkeypatch.setattr(widget_mod, "_FRAME_PANEL_CACHE_BYTES", 200_000)
    monkeypatch.setattr(viewer, "_compose_frame_panel", lambda t: _panel())
    viewer._frame_batch = object()
    viewer._mapping_result = object()
    viewer._final_index = object()

    for t in range(200):
        viewer._last_t = t
        viewer.current_frame_stack()

    assert viewer._frame_panel_bytes <= 200_000


def test_clearing_the_scene_resets_the_total(viewer):
    viewer._cache_frame_panel(1, _panel())
    assert viewer._frame_panel_bytes > 0

    viewer._clear_scene_data()

    assert viewer._frame_panel_cache == {}
    assert viewer._frame_panel_bytes == 0
