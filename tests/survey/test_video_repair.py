"""Scenario: a clip added while its file was unreadable got no hash, and every
later add of the same file wrote another row.

Expected behaviour: the repair hashes what it can reach, folds the duplicates
into one clip, and moves the passes cut from the losers onto the survivor.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from _factories import make_transect, make_video, write_test_mp4

from deepreefmap_gui.survey.models import TransectPass, VideoAsset
from deepreefmap_gui.survey.video_probe import NO, SOURCE_CONTAINER, UNKNOWN, YES
from deepreefmap_gui.survey.video_repair import (
    backfill_hashes,
    backfill_metadata,
    merge_duplicates,
    repair_video_identity,
)

SHOT_AT = datetime(2023, 5, 17, 8, 27, 16, tzinfo=timezone.utc)


def write_clip(tmp_path, name="GX010001.MP4", content=b"footage"):
    path = tmp_path / name
    path.write_bytes(content * 512)
    return path


def force_duplicate(store, path, **overrides) -> VideoAsset:
    """Write a clip row straight in, bypassing the deduplicating upsert.

    Duplicates are what the old upsert produced, and the fixed one will not make
    another, so the state under repair has to be built rather than provoked.
    """
    asset = VideoAsset(file_name=path.name, path=str(path), hash=None, **overrides)
    store._add("video_asset", asset)
    return asset


def add_pass(store, video, transect=None):
    transect = transect or make_transect()
    if store.get_transect(transect.id) is None:
        store.add_transect(transect)
    pass_ = TransectPass(
        transect_id=transect.id, video_id=video.id, begin_s=0.0, end_s=60.0
    )
    store.add_pass(pass_)
    return pass_


def test_upsert_matches_an_unhashed_row_by_path(store, tmp_path):
    """Without this every add of an unhashable file wrote another row."""
    path = write_clip(tmp_path)
    first = store.upsert_video(
        VideoAsset(file_name=path.name, path=str(path), hash=None)
    )
    second = store.upsert_video(
        VideoAsset(file_name=path.name, path=str(path), hash=None, duration_s=60.0)
    )
    assert second.id == first.id
    assert len(store.list_videos()) == 1
    # What the later add knew is kept.
    assert store.get_video(first.id).duration_s == 60.0


def test_upsert_never_adopts_a_row_that_already_has_a_hash(store, tmp_path):
    """One path can hold different files over time, so a hash always wins."""
    path = write_clip(tmp_path)
    identified = store.upsert_video(
        VideoAsset(file_name=path.name, path=str(path), hash="ab" * 16)
    )
    fresh = store.upsert_video(
        VideoAsset(file_name=path.name, path=str(path), hash=None)
    )
    assert fresh.id != identified.id
    assert len(store.list_videos()) == 2


def test_upsert_still_prefers_the_hash_over_the_path(store, tmp_path):
    moved = write_clip(tmp_path, "moved.MP4")
    original = store.upsert_video(make_video(content_hash="cd" * 16))
    same_file_elsewhere = store.upsert_video(
        VideoAsset(file_name=moved.name, path=str(moved), hash="cd" * 16)
    )
    assert same_file_elsewhere.id == original.id
    assert store.get_video(original.id).path == str(moved)


def test_backfill_hashes_only_what_it_can_read(store, tmp_path):
    present = write_clip(tmp_path, "here.MP4")
    store.upsert_video(VideoAsset(file_name="here.MP4", path=str(present), hash=None))
    store.upsert_video(
        VideoAsset(file_name="gone.MP4", path=str(tmp_path / "gone.MP4"), hash=None)
    )

    hashed, unreadable = backfill_hashes(store)
    assert hashed == 1
    assert unreadable == [str(tmp_path / "gone.MP4")]
    by_name = {v.file_name: v for v in store.list_videos()}
    assert by_name["here.MP4"].hash
    # A clip on an unplugged drive stays, unhashed. Deleting it would take its
    # passes with it.
    assert by_name["gone.MP4"].hash is None


def test_merge_repoints_passes_onto_the_survivor(store, tmp_path):
    path = write_clip(tmp_path)
    older = force_duplicate(store, path)
    newer = force_duplicate(store, path, duration_s=90.0, gravity=YES)
    pass_a = add_pass(store, older)
    pass_b = add_pass(store, newer, transect=make_transect("T2"))

    report = repair_video_identity(store)
    assert report.merged == 1
    assert report.passes_moved == 1
    assert len(store.list_videos()) == 1

    survivor = store.list_videos()[0]
    assert survivor.id == older.id
    assert survivor.hash, "the survivor is hashed by the backfill that runs first"
    assert survivor.duration_s == 90.0, "what the later row knew is carried across"
    assert survivor.gravity == YES, "a stale 'unknown' does not outrank a reading"
    assert store.get_pass(pass_a.id).video_id == survivor.id
    assert store.get_pass(pass_b.id).video_id == survivor.id


def test_merge_collapses_a_chaptered_pass_without_repeating_the_survivor(store, tmp_path):
    """A pass names its chapters in order, and refuses to hold one twice.

    Two chapters that turn out to be the same row would otherwise try to sit in
    video_id and extra_video_ids at once.
    """
    path = write_clip(tmp_path)
    first = force_duplicate(store, path)
    second = force_duplicate(store, path)
    transect = make_transect()
    store.add_transect(transect)
    pass_ = TransectPass(
        transect_id=transect.id,
        video_id=first.id,
        extra_video_ids=[second.id],
        begin_s=0.0,
        end_s=60.0,
    )
    store.add_pass(pass_)

    repair_video_identity(store)
    stored = store.get_pass(pass_.id)
    assert stored.video_id == first.id
    assert stored.extra_video_ids == []


def test_repair_is_quiet_when_there_is_nothing_to_fix(store):
    store.upsert_video(make_video())
    report = repair_video_identity(store)
    assert not report.changed
    assert report.summary() == ""


def test_repair_says_what_it_changed(store, tmp_path):
    path = write_clip(tmp_path)
    force_duplicate(store, path)
    force_duplicate(store, path)
    report = repair_video_identity(store)
    assert "merged 1 duplicate clip" in report.summary()


def test_rows_with_neither_hash_nor_path_are_left_alone(store):
    """Nothing can say those are the same clip, so nothing should guess."""
    store._add("video_asset", VideoAsset(file_name="a.MP4", path="", hash=None))
    store._add("video_asset", VideoAsset(file_name="b.MP4", path="", hash=None))
    merged, moved = merge_duplicates(store)
    assert (merged, moved) == (0, 0)
    assert len(store.list_videos()) == 2


def test_a_clip_nobody_has_read_gets_its_container_read(store, tmp_path):
    """Rows predate the container read, and rebuilt rows never had one."""
    clip = write_test_mp4(tmp_path / "GX010040.MP4", created_at=SHOT_AT)
    video = force_duplicate(store, clip)
    assert video.probed_at is None

    assert backfill_metadata(store) == 1

    stored = store.get_video(video.id)
    assert stored.captured_at == SHOT_AT.isoformat()
    assert stored.captured_source == SOURCE_CONTAINER
    assert stored.gravity == YES
    assert stored.width == 1920
    assert stored.probed_at


def test_a_clip_already_read_is_not_read_again(store, tmp_path):
    clip = write_test_mp4(tmp_path / "GX010041.MP4", created_at=SHOT_AT)
    force_duplicate(store, clip)
    backfill_metadata(store)

    assert backfill_metadata(store) == 0


def test_a_clip_on_an_unplugged_drive_stays_unread(store, tmp_path):
    """The answer is still there once the drive is back, so nothing is written off."""
    force_duplicate(store, tmp_path / "gone.MP4")

    assert backfill_metadata(store) == 0
    assert store.list_videos()[0].probed_at is None


def test_a_file_that_is_no_container_is_read_off_once(store, tmp_path):
    """An AVI will not turn into an MP4, so re-reading it every open buys nothing."""
    video = force_duplicate(store, write_clip(tmp_path, "hand.avi"))

    assert backfill_metadata(store) == 1
    stored = store.get_video(video.id)
    assert stored.probed_at
    assert stored.gravity == UNKNOWN


def test_importing_a_clip_reads_its_container(tmp_path):
    clip = write_test_mp4(tmp_path / "GX010042.MP4", created_at=SHOT_AT, gravity=False)

    asset = VideoAsset.from_path(clip)
    assert asset.captured_at == SHOT_AT.isoformat()
    assert asset.gravity == NO
    assert asset.codec == "hvc1"
    assert asset.duration_s == pytest.approx(12.0)
