from datetime import datetime, timezone

import pytest
from _factories import write_test_mp4

from deepreefmap_gui.survey.video_probe import (
    NO,
    SOURCE_CONTAINER,
    SOURCE_MTIME,
    UNKNOWN,
    YES,
    VideoMeta,
    capture_datetime,
    probe_metadata,
)

SHOT_AT = datetime(2023, 5, 17, 8, 27, 16, tzinfo=timezone.utc)


def test_a_gopro_clip_reports_its_own_facts(tmp_path):
    meta = probe_metadata(write_test_mp4(tmp_path / "GX010040.MP4", created_at=SHOT_AT))

    assert meta.readable
    assert meta.captured_at == SHOT_AT.isoformat()
    assert meta.duration_s == pytest.approx(12.0)
    assert meta.resolution == "1920x1080"
    assert meta.codec == "hvc1"
    assert meta.gravity == YES
    assert meta.gps == YES


def test_moov_after_mdat_is_still_read(tmp_path):
    """Some GoPro firmware writes the index last, which is the case that matters.

    Expected behaviour: a walk that gave up early would blank the capture date
    for exactly the cameras this feature is for.
    """
    clip = write_test_mp4(tmp_path / "last.MP4", created_at=SHOT_AT, moov_last=True)

    meta = probe_metadata(clip)
    assert meta.captured_at == SHOT_AT.isoformat()
    assert meta.gravity == YES


def test_a_clip_with_no_telemetry_track_says_no_gravity(tmp_path):
    meta = probe_metadata(write_test_mp4(tmp_path / "phone.mp4", telemetry=False))

    assert meta.readable
    assert meta.gravity == NO
    assert meta.gps == NO


def test_telemetry_without_a_gravity_stream_says_no(tmp_path):
    """Older cameras write GPS but no gravity vector, and that is not "unknown"."""
    meta = probe_metadata(write_test_mp4(tmp_path / "hero5.MP4", gravity=False))

    assert meta.gravity == NO
    assert meta.gps == YES


@pytest.mark.parametrize("codec", [b"avc1", b"hvc1"])
def test_the_video_codec_is_read_from_the_sample_entry(tmp_path, codec):
    meta = probe_metadata(write_test_mp4(tmp_path / "clip.mp4", codec=codec))
    assert meta.codec == codec.decode()


def test_sample_sizes_may_be_tabulated_rather_than_uniform(tmp_path):
    clip = write_test_mp4(tmp_path / "table.MP4", uniform_sizes=False)
    assert probe_metadata(clip).gravity == YES


def test_a_clip_with_no_creation_time_reports_none(tmp_path):
    """Re-encoding drops it, so a trimmed clip has nothing to offer here."""
    meta = probe_metadata(write_test_mp4(tmp_path / "trimmed.mp4", created_at=None))

    assert meta.readable
    assert meta.captured_at is None
    assert meta.gravity == YES


def test_a_creation_time_outside_living_memory_is_not_a_date(tmp_path):
    ancient = datetime(1905, 6, 1, tzinfo=timezone.utc)
    meta = probe_metadata(write_test_mp4(tmp_path / "odd.mp4", created_at=ancient))
    assert meta.captured_at is None


def test_a_truncated_file_reports_nothing_rather_than_guessing(tmp_path):
    clip = write_test_mp4(tmp_path / "cut.MP4", created_at=SHOT_AT, truncate_to=200)

    meta = probe_metadata(clip)
    assert not meta.readable
    assert meta.gravity == UNKNOWN
    assert meta.captured_at is None


def test_something_that_is_not_a_video_is_unreadable(tmp_path):
    junk = tmp_path / "notes.txt"
    junk.write_bytes(b"this is not a container" * 40)

    meta = probe_metadata(junk)
    assert not meta.readable
    assert meta.gravity == UNKNOWN


def test_a_missing_file_never_raises(tmp_path):
    assert probe_metadata(tmp_path / "gone.MP4") == VideoMeta()


def test_the_file_timestamp_stands_in_when_the_container_has_no_date():
    embedded = capture_datetime(VideoMeta(captured_at="2023-05-17T08:27:16+00:00"), "2024-01-01")
    assert embedded == ("2023-05-17T08:27:16+00:00", SOURCE_CONTAINER)

    estimated = capture_datetime(VideoMeta(), "2024-01-01")
    assert estimated == ("2024-01-01", SOURCE_MTIME)

    assert capture_datetime(VideoMeta(), None) == (None, None)
