from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pytest


@pytest.fixture(scope="module")
def tiny_video(tmp_path_factory) -> tuple[Path, float]:
    path = tmp_path_factory.mktemp("scrub") / "tiny.mp4"
    frames = np.zeros((20, 48, 64, 3), dtype=np.uint8)
    for i in range(20):
        frames[i, :, :, 0] = i * 12
    iio.imwrite(path, frames, fps=10)
    return path, 2.0


def test_defaults_span_the_full_video(qapp, tiny_video) -> None:
    from deepreefmap_gui.form.video_scrub import VideoScrubDialog

    path, duration = tiny_video
    dialog = VideoScrubDialog(path, duration)
    begin, end = dialog.time_range()
    assert begin == 0.0
    assert end == duration
    dialog.reject()


def test_end_at_slider_max_returns_exact_duration(qapp, tiny_video) -> None:
    # An untrimmed pass has to come back reading as the whole clip, so the max
    # tick must not round away from the probed duration.
    from deepreefmap_gui.form.video_scrub import VideoScrubDialog

    path, _ = tiny_video
    duration = 2.0004999
    dialog = VideoScrubDialog(path, duration)
    dialog._range_slider.setEnd(dialog._range_slider.maximum())
    assert dialog.time_range()[1] == duration
    dialog.reject()


def test_slider_maps_ticks_to_seconds(qapp, tiny_video) -> None:
    from deepreefmap_gui.form.video_scrub import VideoScrubDialog

    path, duration = tiny_video
    dialog = VideoScrubDialog(path, duration, begin_s=0.5, end_s=1.5)
    assert dialog.time_range() == (0.5, 1.5)
    dialog._range_slider.setBegin(80)
    assert dialog.time_range()[0] == 0.8
    dialog.reject()


def test_handles_cannot_cross(qapp, tiny_video) -> None:
    from deepreefmap_gui.form.video_scrub import VideoScrubDialog

    path, duration = tiny_video
    dialog = VideoScrubDialog(path, duration, begin_s=0.5, end_s=1.0)
    dialog._range_slider.setBegin(150)
    assert dialog._range_slider.begin() == 100

    dialog._range_slider.setEnd(30)
    assert dialog._range_slider.end() == 100
    dialog.reject()


def test_collapsed_range_can_reopen(qapp, tiny_video) -> None:
    from deepreefmap_gui.form.video_scrub import VideoScrubDialog

    path, duration = tiny_video
    dialog = VideoScrubDialog(path, duration, begin_s=1.0, end_s=1.0)
    slider = dialog._range_slider
    assert slider.begin() == slider.end() == 100
    slider.setEnd(150)
    slider.setBegin(50)
    assert (slider.begin(), slider.end()) == (50, 150)
    dialog.reject()


def test_preview_paints_a_frame(qapp, tiny_video) -> None:
    from deepreefmap_gui.form.video_scrub import VideoScrubDialog

    path, duration = tiny_video
    dialog = VideoScrubDialog(path, duration)
    dialog._request_preview(1.0)
    dialog._show_pending_frame()
    pixmap = dialog._preview.pixmap()
    assert pixmap is not None and not pixmap.isNull()
    dialog.reject()


def test_capture_released_on_close(qapp, tiny_video) -> None:
    from deepreefmap_gui.form.video_scrub import VideoScrubDialog

    path, duration = tiny_video
    dialog = VideoScrubDialog(path, duration)
    assert dialog._cap.isOpened()
    dialog.reject()
    assert not dialog._cap.isOpened()


def test_format_time_shows_seconds_and_minutes() -> None:
    from deepreefmap_gui.form.video_scrub import _format_time

    assert _format_time(83.45) == "83.45 s (1:23.45)"
    assert _format_time(0.0) == "0.00 s (0:00.00)"


def _click(slider, x: float) -> None:
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    slider.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, 14), Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    ))


def test_playing_walks_the_playhead_down_the_clip(qapp, tiny_video) -> None:
    from deepreefmap_gui.form.video_scrub import VideoScrubDialog

    path, duration = tiny_video
    dialog = VideoScrubDialog(path, duration, fps=10)
    assert not dialog.is_playing()

    dialog._play()
    assert dialog.is_playing()
    dialog._advance_frame()
    first = dialog._range_slider.playhead()
    dialog._advance_frame()

    assert first is not None
    assert dialog._range_slider.playhead() > first
    dialog.reject()


def test_playback_stops_at_the_end_of_the_trim(qapp, tiny_video) -> None:
    """The range is what will be processed, so playing past it previews nothing."""
    from deepreefmap_gui.form.video_scrub import VideoScrubDialog

    path, duration = tiny_video
    dialog = VideoScrubDialog(path, duration, end_s=0.4, fps=10)
    dialog._play()
    for _ in range(40):
        if not dialog.is_playing():
            break
        dialog._advance_frame()

    assert not dialog.is_playing()
    dialog.reject()


def test_scrubbing_takes_the_capture_back_from_playback(qapp, tiny_video) -> None:
    """Both seek the one capture, so a drag mid-play would fight the timer."""
    from deepreefmap_gui.form.video_scrub import VideoScrubDialog

    path, duration = tiny_video
    dialog = VideoScrubDialog(path, duration, fps=10)
    dialog._play()
    dialog._range_slider.setBegin(50)

    assert not dialog.is_playing()
    dialog.reject()


def test_preview_mode_is_a_player_not_a_trimmer(qapp, tiny_video) -> None:
    from deepreefmap_gui.form.video_scrub import VideoScrubDialog

    path, duration = tiny_video
    dialog = VideoScrubDialog(path, duration, trim=False)
    dialog.show()
    moved = []
    dialog._range_slider.playhead_moved.connect(moved.append)

    assert dialog.windowTitle().startswith("Preview")
    assert not dialog._begin_readout.isVisibleTo(dialog)
    _click(dialog._range_slider, dialog._range_slider.width() / 2)

    assert moved and dialog._range_slider.playhead() == moved[0]
    dialog.reject()


def test_closing_mid_play_stops_the_timer(qapp, tiny_video) -> None:
    from deepreefmap_gui.form.video_scrub import VideoScrubDialog

    path, duration = tiny_video
    dialog = VideoScrubDialog(path, duration, fps=10)
    dialog._play()
    dialog.reject()

    assert not dialog.is_playing()
