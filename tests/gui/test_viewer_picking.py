"""Point/frustum picking and the screen-space selection reticle.

`viewer/picking.py` decides *which* point a click selected and *where* to draw
the marker for it. Both are numeric and both are wrong-silently: a bad class
lookup labels the point with someone else's name, and a bad anchor draws the
reticle away from the point it is meant to be circling.

These drive a real `QtPointCloudViewer` with a real plotter and real VTK props,
stopping short of `_pick_at`, which reads the OpenGL depth buffer and so needs a
rendered frame. Its numeric core, `_select_pick_pixel`, is covered in
`test_viewer_widget.py`.
"""

from __future__ import annotations

import numpy as np
import pytest
import pyvista as pv
from deepreefmap.pointcloud.final_cloud_index import FinalCloudIndex

CORAL, SAND = 1, 2
COLORS = {CORAL: (255, 0, 0), SAND: (200, 190, 120)}
NAMES = {CORAL: "coral", SAND: "sand"}

# Three coral points, spread so a wrong index is visible in the payload.
CORAL_XYZ = np.array([[0.0, 0.0, 0.0], [1.0, 1.0, 1.0], [2.0, 2.0, 2.0]], dtype=np.float32)
CORAL_CONF = np.array([0.1, 0.5, 0.9], dtype=np.float32)


@pytest.fixture
def viewer(qapp):
    from deepreefmap_gui.viewer.point_cloud import QtPointCloudViewer

    v = QtPointCloudViewer(class_colors=COLORS, class_names=NAMES)
    v._ensure_plotter()
    return v


def _index(*, conf=CORAL_CONF, frame_order=(5, 9), prefix_end=(2, 3)) -> FinalCloudIndex:
    """A one-class index: points 0-1 land on frame 5, point 2 on frame 9."""
    empty_rgb = np.zeros((len(CORAL_XYZ), 3), dtype=np.uint8)
    return FinalCloudIndex(
        frame_order=frame_order,
        class_ids=(CORAL,),
        xyz_by_class={CORAL: CORAL_XYZ},
        rgb_by_class={CORAL: empty_rgb},
        semrgb_by_class={CORAL: empty_rgb},
        conf_by_class={} if conf is None else {CORAL: conf},
        prefix_end_by_class={CORAL: np.asarray(prefix_end, dtype=np.int64)},
    )


def _load_class_actor(viewer, points=CORAL_XYZ, cid=CORAL, visible=True, orig_idx=None):
    """Show `points` as class `cid`, the way the timeline update does.

    The actor is created once and re-pointed at each new mesh with
    `SetInputData` (widget.py::_apply_class_clouds), which is what makes the
    mapper's input the identical object `_process_pick` compares against.
    """
    mesh = pv.PolyData(np.asarray(points, dtype=np.float32))
    if orig_idx is not None:
        mesh.point_data["orig_idx"] = np.asarray(orig_idx, dtype=np.int32)
    actor = viewer._plotter.add_mesh(
        pv.PolyData(np.zeros((1, 3), dtype=np.float32)), style="points", name=f"class_{cid}"
    )
    actor.GetMapper().SetInputData(mesh)
    actor.SetVisibility(visible)
    viewer._class_actors[cid] = actor
    return mesh


def _picks(viewer) -> list:
    picked: list = []
    viewer.point_picked.connect(picked.append)
    return picked


def _clears(viewer) -> list:
    cleared: list = []
    viewer.point_picked_clear.connect(lambda: cleared.append(True))
    return cleared


# --- resolving a click to a point ---------------------------------------


def test_a_pick_reports_the_class_and_position_of_the_point_under_it(viewer) -> None:
    viewer._final_index = _index()
    mesh = _load_class_actor(viewer)
    picked = _picks(viewer)

    viewer._process_pick(mesh, 1)

    (payload,) = picked
    assert payload["class_id"] == CORAL
    assert payload["class_name"] == "coral"
    assert payload["color"] == COLORS[CORAL]
    assert payload["xyz"] == pytest.approx((1.0, 1.0, 1.0))
    assert payload["confidence"] == pytest.approx(0.5)


@pytest.mark.parametrize("point_id, expected_frame", [(0, 5), (1, 5), (2, 9)])
def test_a_point_is_attributed_to_the_frame_that_contributed_it(
    viewer, point_id, expected_frame
) -> None:
    """`prefix_end` is cumulative, so the bucket boundary is the interesting case.

    Point 1 is the last of frame 5's two points and point 2 the first of frame
    9's: an off-by-one in the searchsorted side would move both.
    """
    viewer._final_index = _index()
    mesh = _load_class_actor(viewer)
    picked = _picks(viewer)

    viewer._process_pick(mesh, point_id)

    assert picked[0]["frame_index"] == expected_frame


def test_a_filtered_display_mesh_is_remapped_back_to_the_full_cloud(viewer) -> None:
    """Confidence and class filters draw a subset carrying `orig_idx`.

    The click arrives with an index into that subset. Without the remap the
    payload would describe whichever full-cloud point happens to share the
    subset's numbering.
    """
    viewer._final_index = _index()
    mesh = _load_class_actor(viewer, points=CORAL_XYZ[[2]], orig_idx=[2])
    picked = _picks(viewer)

    viewer._process_pick(mesh, 0)

    assert picked[0]["xyz"] == pytest.approx((2.0, 2.0, 2.0))
    assert picked[0]["confidence"] == pytest.approx(0.9)


def test_a_class_with_no_confidence_channel_reports_nan_not_zero(viewer) -> None:
    """Zero would read as "the model was certain this is nothing"."""
    viewer._final_index = _index(conf=None)
    mesh = _load_class_actor(viewer)
    picked = _picks(viewer)

    viewer._process_pick(mesh, 0)

    assert np.isnan(picked[0]["confidence"])


def test_an_unnamed_class_still_produces_a_usable_payload(viewer) -> None:
    viewer._final_index = _index()
    mesh = _load_class_actor(viewer, cid=99)
    viewer._final_index.xyz_by_class[99] = CORAL_XYZ
    viewer._final_index.conf_by_class[99] = CORAL_CONF
    viewer._final_index.prefix_end_by_class[99] = np.array([2, 3], dtype=np.int64)
    picked = _picks(viewer)

    viewer._process_pick(mesh, 0)

    assert picked[0]["class_name"] == "class 99"
    assert picked[0]["color"] == (180, 180, 180)


# --- clicks that must not resolve to a point ----------------------------


def test_a_click_on_a_hidden_class_is_a_miss(viewer) -> None:
    """The actor still owns the mesh while hidden, so identity alone is not enough."""
    viewer._final_index = _index()
    mesh = _load_class_actor(viewer, visible=False)
    picked, cleared = _picks(viewer), _clears(viewer)

    viewer._process_pick(mesh, 1)

    assert not picked
    assert cleared


def test_a_click_on_an_unregistered_mesh_is_a_miss(viewer) -> None:
    viewer._final_index = _index()
    _load_class_actor(viewer)
    picked, cleared = _picks(viewer), _clears(viewer)

    viewer._process_pick(pv.PolyData(CORAL_XYZ), 1)

    assert not picked
    assert cleared


def test_a_miss_tells_the_user_rather_than_failing_silently(viewer) -> None:
    said: list[str] = []
    viewer.set_status_callback(said.append)
    viewer._final_index = _index()
    _load_class_actor(viewer)

    viewer._process_pick(pv.PolyData(CORAL_XYZ), 0)

    assert said == ["No point under cursor"]


def test_picking_before_a_scene_is_loaded_clears_the_selection(viewer) -> None:
    viewer._final_index = None
    picked, cleared = _picks(viewer), _clears(viewer)

    viewer._process_pick(pv.PolyData(CORAL_XYZ), 0)

    assert not picked
    assert cleared


def test_an_index_past_the_end_of_the_class_clears_instead_of_reporting(viewer) -> None:
    """A stale mesh outliving its index would otherwise index out of bounds."""
    viewer._final_index = _index()
    mesh = _load_class_actor(viewer, points=np.zeros((10, 3), dtype=np.float32))
    picked, cleared = _picks(viewer), _clears(viewer)

    viewer._process_pick(mesh, 7)

    assert not picked
    assert cleared


# --- frustum picks ------------------------------------------------------


def test_a_frustum_pick_reports_the_frame_the_camera_belongs_to(viewer) -> None:
    """Frustums share one mesh; the frame is recovered by integer division.

    Every frustum contributes `_frustum_pts_per` points, so point 33 of a
    16-points-per-frustum batch is frustum 2.
    """
    viewer._final_index = _index()
    mesh = pv.PolyData(np.zeros((48, 3), dtype=np.float32))
    viewer._frustum_batch_actor = viewer._plotter.add_mesh(mesh, style="points")
    viewer._frustum_frame_ids = [4, 5, 6]
    viewer._frustum_pts_per = 16
    frames: list[int] = []
    viewer.frustum_picked.connect(frames.append)

    viewer._process_pick(mesh, 33)

    assert frames == [6]


def test_a_frustum_point_id_past_the_last_camera_does_not_report_a_frame(viewer) -> None:
    viewer._final_index = _index()
    mesh = pv.PolyData(np.zeros((48, 3), dtype=np.float32))
    viewer._frustum_batch_actor = viewer._plotter.add_mesh(mesh, style="points")
    viewer._frustum_frame_ids = [4, 5, 6]
    viewer._frustum_pts_per = 16
    frames: list[int] = []
    viewer.frustum_picked.connect(frames.append)
    cleared = _clears(viewer)

    viewer._process_pick(mesh, 200)

    assert not frames
    assert cleared


def test_only_final_cloud_actors_are_offered_to_the_picker(viewer) -> None:
    """The live and simple actors are absent from _process_pick's lookup tables,
    so offering them would produce picks that silently resolve to nothing."""
    # show_point_cloud clears scene data, so the final-cloud actors go on after it.
    viewer.show_point_cloud(
        np.random.default_rng(0).random((5, 3)).astype(np.float32),
        np.zeros((5, 3), dtype=np.uint8),
    )
    viewer._final_index = _index()
    _load_class_actor(viewer)
    frustum = viewer._plotter.add_mesh(pv.PolyData(np.zeros((16, 3), np.float32)), style="points")
    viewer._frustum_batch_actor = frustum

    pickable = list(viewer._iter_pickable_actors())

    assert any(a is frustum for a in pickable)
    assert any(a is viewer._class_actors[CORAL] for a in pickable)
    assert viewer._simple_actor is not None
    assert not any(a is viewer._simple_actor for a in pickable)


# --- pick mode ----------------------------------------------------------


def test_pick_mode_announces_each_change_once(viewer) -> None:
    states: list[bool] = []
    viewer.pick_mode_changed.connect(states.append)

    viewer.set_pick_mode(True)
    viewer.set_pick_mode(True)
    viewer.set_pick_mode(False)

    assert states == [True, False]


def test_leaving_pick_mode_forgets_a_press_that_was_never_released(viewer) -> None:
    """Otherwise the stale press pairs with the next release and fires a pick."""
    viewer.set_pick_mode(True)
    viewer._pick_press_pos = (10, 20)

    viewer.set_pick_mode(False)

    assert viewer._pick_press_pos is None


# --- the selection reticle ----------------------------------------------


def _ring_sources(viewer):
    return viewer._pick_ring_sources


def test_the_reticle_is_a_ring_and_four_ticks_drawn_twice_over(viewer) -> None:
    """Each element is stroked black first, then in the class colour, so the
    marker stays visible against a cloud of its own colour."""
    viewer.set_picked_marker((1.0, 2.0, 3.0), (10, 200, 30), anchor_display=(100.0, 50.0))

    assert len(viewer._pick_ring_sources) == 2
    assert len(viewer._pick_tick_sources) == 8
    assert not viewer._pick_line_sources  # no leader target given

    halo, colored = viewer._pick_2d_actors[0], viewer._pick_2d_actors[5]
    assert halo.GetProperty().GetColor() == (0.0, 0.0, 0.0)
    assert colored.GetProperty().GetColor() == pytest.approx((10 / 255, 200 / 255, 30 / 255))
    assert halo.GetProperty().GetLineWidth() > colored.GetProperty().GetLineWidth()


def test_the_ticks_straddle_the_anchor_leaving_its_centre_open(viewer) -> None:
    """A filled blob would hide the point the user just selected."""
    from deepreefmap_gui.viewer.picking import ViewerPickingMixin as M

    viewer.set_picked_marker((1.0, 2.0, 3.0), (10, 200, 30), anchor_display=(100.0, 50.0))

    offsets = {(ox1, oy1, ox2, oy2) for _src, ox1, oy1, ox2, oy2 in viewer._pick_tick_sources}
    ri, ro = M._PICK_TICK_INNER, M._PICK_TICK_OUTER
    assert offsets == {
        (0.0, ri, 0.0, ro),
        (0.0, -ri, 0.0, -ro),
        (ri, 0.0, ro, 0.0),
        (-ri, 0.0, -ro, 0.0),
    }
    assert all(src.GetRadius() == M._PICK_RING_RADIUS for src in viewer._pick_ring_sources)


def test_the_leader_line_runs_from_the_anchor_to_the_callout(viewer) -> None:
    viewer.set_picked_marker(
        (1.0, 2.0, 3.0), (10, 200, 30),
        anchor_display=(100.0, 50.0), leader_target_display=(140.0, 90.0),
    )

    assert len(viewer._pick_line_sources) == 2
    for src in viewer._pick_line_sources:
        assert src.GetPoint1() == pytest.approx((100.0, 50.0, 0.0))
        assert src.GetPoint2() == pytest.approx((140.0, 90.0, 0.0))


def test_repicking_the_same_point_slides_the_marker_instead_of_rebuilding_it(viewer) -> None:
    """Rebuilding on every camera frame is what update_pick_anchor exists to avoid."""
    viewer.set_picked_marker((1.0, 2.0, 3.0), (10, 200, 30), anchor_display=(100.0, 50.0))
    before = list(viewer._pick_ring_sources)

    viewer.set_picked_marker((1.0, 2.0, 3.0), (10, 200, 30), anchor_display=(160.0, 70.0))

    assert viewer._pick_ring_sources == before
    assert before[0].GetCenter() == pytest.approx((160.0, 70.0, 0.0))


def test_picking_a_different_point_rebuilds_the_marker(viewer) -> None:
    viewer.set_picked_marker((1.0, 2.0, 3.0), (10, 200, 30), anchor_display=(100.0, 50.0))
    before = list(viewer._pick_ring_sources)

    viewer.set_picked_marker((9.0, 9.0, 9.0), (10, 200, 30), anchor_display=(100.0, 50.0))

    assert viewer._pick_ring_sources != before
    assert viewer._picked_xyz == pytest.approx((9.0, 9.0, 9.0))


def test_moving_the_anchor_carries_the_ring_ticks_and_leader_with_it(viewer) -> None:
    """All three are positioned from the same anchor; one left behind reads as a
    reticle that has come apart."""
    viewer.set_picked_marker(
        (1.0, 2.0, 3.0), (10, 200, 30),
        anchor_display=(100.0, 50.0), leader_target_display=(140.0, 90.0),
    )

    viewer.update_pick_anchor((200.0, 80.0), (260.0, 120.0))

    assert all(s.GetCenter() == pytest.approx((200.0, 80.0, 0.0)) for s in viewer._pick_ring_sources)
    for src in viewer._pick_line_sources:
        assert src.GetPoint1() == pytest.approx((200.0, 80.0, 0.0))
        assert src.GetPoint2() == pytest.approx((260.0, 120.0, 0.0))
    for src, ox1, oy1, ox2, oy2 in viewer._pick_tick_sources:
        assert src.GetPoint1() == pytest.approx((200.0 + ox1, 80.0 + oy1, 0.0))
        assert src.GetPoint2() == pytest.approx((200.0 + ox2, 80.0 + oy2, 0.0))


def test_dropping_the_callout_collapses_the_leader_onto_the_anchor(viewer) -> None:
    viewer.set_picked_marker(
        (1.0, 2.0, 3.0), (10, 200, 30),
        anchor_display=(100.0, 50.0), leader_target_display=(140.0, 90.0),
    )

    viewer.update_pick_anchor((200.0, 80.0), None)

    assert viewer._picked_leader_target is None
    for src in viewer._pick_line_sources:
        assert src.GetPoint1() == pytest.approx(src.GetPoint2())


def test_moving_an_anchor_with_no_marker_is_a_noop(viewer) -> None:
    viewer.update_pick_anchor((10.0, 10.0), None)

    assert viewer._picked_leader_target is None
    assert not viewer._pick_2d_actors


def test_clearing_removes_every_overlay_prop_it_added(viewer) -> None:
    """Props left behind accumulate one reticle per pick for the life of the run."""
    baseline = viewer._plotter.renderer.GetViewProps().GetNumberOfItems()
    viewer.set_picked_marker(
        (1.0, 2.0, 3.0), (10, 200, 30),
        anchor_display=(100.0, 50.0), leader_target_display=(140.0, 90.0),
    )
    assert viewer._plotter.renderer.GetViewProps().GetNumberOfItems() > baseline

    viewer.clear_picked_marker()

    assert viewer._plotter.renderer.GetViewProps().GetNumberOfItems() == baseline
    assert viewer._pick_2d_actors == []
    assert viewer._pick_tick_sources == []
    assert viewer._picked_xyz is None


def test_a_camera_move_asks_for_the_anchor_to_be_recomputed(viewer) -> None:
    """The anchor is a projection of a world point, so it is stale the moment
    the camera moves."""
    moved: list[bool] = []
    viewer.canvas_resized.connect(lambda: moved.append(True))
    viewer.set_picked_marker((1.0, 2.0, 3.0), (10, 200, 30), anchor_display=(100.0, 50.0))

    viewer._plotter.renderer.GetActiveCamera().Azimuth(10.0)
    assert moved

    viewer.clear_picked_marker()
    moved.clear()
    viewer._plotter.renderer.GetActiveCamera().Azimuth(10.0)

    assert not moved


def test_the_camera_observer_is_released_rather_than_accumulated(viewer) -> None:
    """One observer is installed per marker and every pick builds a new marker,
    so a missing detach leaves one more live observer per click for the session."""
    camera = viewer._plotter.renderer.GetActiveCamera()
    assert camera.HasObserver("ModifiedEvent") == 0

    for i in range(5):
        viewer.set_picked_marker((float(i), 0.0, 0.0), (10, 200, 30), anchor_display=(10.0, 10.0))
    # HasObserver returns the tag of the most recent match; ids are handed out
    # in sequence, so anything above 1 means earlier ones were never removed.
    assert camera.HasObserver("ModifiedEvent") == 1

    viewer.clear_picked_marker()

    assert camera.HasObserver("ModifiedEvent") == 0
    assert viewer._pick_camera_obs_id is None


# --- projection ---------------------------------------------------------


def test_the_focal_point_projects_to_the_centre_of_the_viewport(viewer) -> None:
    """The one world point whose display position is known independently."""
    camera = viewer._plotter.renderer.GetActiveCamera()
    camera.SetFocalPoint(1.0, 2.0, 3.0)
    camera.SetPosition(1.0, 2.0, 13.0)
    camera.SetViewUp(0.0, 1.0, 0.0)
    width, height = viewer._plotter.renderer.GetSize()

    display = viewer.world_to_display((1.0, 2.0, 3.0))

    assert display == pytest.approx((width / 2.0, height / 2.0), abs=1.0)


def test_display_coordinates_are_bottom_origin(viewer) -> None:
    """VTK counts up from the bottom edge, Qt down from the top; controls.py
    flips one into the other, and both must agree on which is which."""
    camera = viewer._plotter.renderer.GetActiveCamera()
    camera.SetFocalPoint(0.0, 0.0, 0.0)
    camera.SetPosition(0.0, 0.0, 10.0)
    camera.SetViewUp(0.0, 1.0, 0.0)

    centre = viewer.world_to_display((0.0, 0.0, 0.0))
    above = viewer.world_to_display((0.0, 1.0, 0.0))
    right = viewer.world_to_display((1.0, 0.0, 0.0))

    assert above[1] > centre[1]
    assert right[0] > centre[0]


def test_projection_without_a_plotter_reports_no_position(viewer) -> None:
    viewer._plotter = None

    assert viewer.world_to_display((1.0, 2.0, 3.0)) is None
