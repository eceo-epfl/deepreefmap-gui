"""The canvas overlay is the only home for the 3D display controls.

Scenario: the controls used to exist twice, once in a sidebar group box that was
built and never shown and once on the overlay. Snap and Camera backoff only had
the sidebar copy, so they were unreachable.

Expected behaviour: every control is on the overlay, once, and the values the
viewer is driven with come from there.
"""

from __future__ import annotations

import pytest

_OVERLAY_CONTROLS = (
    "_ov_pt_slider",
    "_ov_conf_slider",
    "_ov_sem_btn",
    "_ov_acc_btn",
    "_ov_play_btn",
    "_ov_fps_spin",
    "_ov_frustum_btn",
    "_ov_follow_btn",
    "_ov_snap_btn",
    "_ov_backoff_slider",
)


@pytest.fixture
def viewing_window(window):
    """A window with the overlay controls revealed and wired."""
    window._show_viewer_controls()
    return window


def test_every_display_control_is_on_the_overlay(viewing_window):
    overlay = viewing_window._pick_mode_overlay
    for name in _OVERLAY_CONTROLS:
        control = getattr(viewing_window, name)
        assert overlay.isAncestorOf(control), f"{name} is not on the overlay"


def test_the_rescued_controls_are_reachable(viewing_window):
    """Snap and the backoff had no overlay copy, so they could not be used."""
    window = viewing_window
    assert not window._ov_snap_btn.isHidden()
    assert not window._ov_backoff_slider.isHidden()
    assert window._ov_snap_btn.text() == "Snap"


def test_snap_uses_the_overlay_backoff(viewing_window, monkeypatch):
    window = viewing_window
    calls = []
    monkeypatch.setattr(
        window._viewer,
        "view_from_frame_pose",
        lambda t, backoff_m: calls.append((t, backoff_m)),
    )
    window._ov_backoff_slider.setValue(12)
    window._ov_snap_btn.click()
    assert calls == [(0, 1.2)]


def test_following_the_camera_snaps_on_toggle(viewing_window, monkeypatch):
    window = viewing_window
    calls = []
    monkeypatch.setattr(
        window._viewer, "view_from_frame_pose", lambda t, backoff_m: calls.append(t)
    )
    window._ov_follow_btn.setChecked(True)
    assert calls == [0]
    # Turning it off is not a reason to move the camera.
    window._ov_follow_btn.setChecked(False)
    assert calls == [0]


def test_playback_speed_comes_from_the_overlay(viewing_window):
    window = viewing_window
    window._ov_fps_spin.setValue(10)
    window._ov_play_btn.setChecked(True)
    assert window._playback_timer.isActive()
    assert window._playback_timer.interval() == 100
    window._ov_fps_spin.setValue(20)
    assert window._playback_timer.interval() == 50
    window._ov_play_btn.setChecked(False)
    assert not window._playback_timer.isActive()


def test_geometry_only_runs_hide_the_semantic_controls(viewing_window):
    window = viewing_window
    window._set_semantic_only_controls_visible(False)
    assert window._ov_sem_btn.isHidden()
    assert window._ov_acc_btn.isHidden()
    assert window._ov_conf_container.isHidden()
    window._set_semantic_only_controls_visible(True)
    assert not window._ov_sem_btn.isHidden()


def test_clearing_the_run_takes_the_controls_with_it(viewing_window):
    window = viewing_window
    assert not window._overlay_controls_container.isHidden()
    window._on_new_reconstruction()
    assert window._overlay_controls_container.isHidden()
    # Pick and Reset steer the camera over a live preview too, so they stay.
    assert not window._pick_mode_button.isHidden()
    assert not window._reset_view_button.isHidden()


def test_collapsing_folds_the_display_controls_but_not_the_camera_tools(viewing_window):
    """The overlay sits on the cloud, so it has to be able to get out of the way.

    Pick and Reset are the exception: they steer the camera the overlay is
    covering, and they are one row.
    """
    window = viewing_window
    assert not window._overlay_controls_container.isHidden()

    window._toggle_overlay_controls_collapsed()

    assert window._overlay_controls_container.isHidden()
    assert not window._ov_pt_slider.isVisibleTo(window._pick_mode_overlay)
    assert window._overlay_hint_row.isHidden()
    assert not window._pick_mode_button.isHidden()
    assert not window._reset_view_button.isHidden()

    window._toggle_overlay_controls_collapsed()
    assert not window._overlay_controls_container.isHidden()


def test_the_collapse_state_survives_a_restart(viewing_window):
    from PySide6.QtCore import QSettings

    window = viewing_window
    settings = QSettings("ECEO", "deepreefmap")
    try:
        was_collapsed = window._overlay_controls_collapsed
        window._toggle_overlay_controls_collapsed()
        assert settings.value("viewer_controls_collapsed", type=bool) is not was_collapsed
    finally:
        settings.remove("viewer_controls_collapsed")


def test_a_run_loading_into_a_collapsed_overlay_leaves_it_collapsed(viewing_window):
    """Scenario: the controls are folded away and a second run loads.

    Expected behaviour: the fold is the user's, so revealing the controls for a
    new run does not undo it.
    """
    window = viewing_window
    window._toggle_overlay_controls_collapsed()

    window._show_viewer_controls()

    assert window._overlay_controls_container.isHidden()
    window._toggle_overlay_controls_collapsed()


def test_no_second_copy_of_any_control(window):
    """The dead sidebar group box held the only Snap and Camera backoff."""
    for name in (
        "_viewer_controls_group",
        "_semantic_check",
        "_accumulate_check",
        "_point_size_spin",
        "_confidence_slider",
        "_play_check",
        "_play_fps_spin",
        "_follow_camera_check",
        "_view_from_camera_btn",
        "_camera_backoff_spin",
    ):
        assert not hasattr(window, name), f"{name} is a second copy of an overlay control"
