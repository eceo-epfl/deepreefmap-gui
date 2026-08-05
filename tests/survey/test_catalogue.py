import json

import pytest
from _factories import (
    make_batch,
    make_transect,
    make_video,
    seed_pass,
    seed_survey_run,
    write_run,
)

from deepreefmap_gui.survey import catalogue
from deepreefmap_gui.survey.catalogue import UNASSIGNED_TITLE
from deepreefmap_gui.survey.models import RunRecord, TransectPass
from deepreefmap_gui.survey.store import SurveyStore


@pytest.fixture
def out_root(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    return root


def scan(out_root, store=None):
    entries = catalogue.scan_out_root(out_root)
    if store is not None:
        catalogue.reconcile(entries, store)
    return entries


def test_scan_orders_newest_first(out_root):
    write_run(out_root, "older", run_timestamp="2026-07-01T10:00:00+00:00")
    write_run(out_root, "newer", run_timestamp="2026-07-02T10:00:00+00:00")
    assert [e.dir_name for e in catalogue.scan_out_root(out_root)] == ["newer", "older"]


def test_group_key_falls_back_from_pass_to_footage(out_root, store):
    seed_survey_run(store, out_root, "with_pass")
    write_run(out_root, "adhoc_a", begin_s=0.0, end_s=60.0)
    write_run(out_root, "adhoc_b", begin_s=0.0, end_s=60.0)
    write_run(out_root, "adhoc_other_window", begin_s=60.0, end_s=120.0)
    entries = {e.dir_name: e for e in scan(out_root, store)}
    assert catalogue.group_key(entries["with_pass"])[0] == "pass"
    assert catalogue.group_key(entries["adhoc_a"]) == catalogue.group_key(entries["adhoc_b"])
    assert catalogue.group_key(entries["adhoc_a"]) != catalogue.group_key(
        entries["adhoc_other_window"]
    )


def test_manifest_pass_id_groups_without_database(out_root, store):
    seed_survey_run(store, out_root, "original")
    fresh = SurveyStore(out_root / "fresh.db")
    entries = scan(out_root, fresh)
    assert catalogue.group_key(entries[0]) == ("pass", str(entries[0].manifest_pass_id))


def test_transects_facet_puts_unassigned_first(out_root, store):
    seed_survey_run(store, out_root, "assigned")
    write_run(out_root, "loose")
    groups = catalogue.transects_facet(scan(out_root, store), store.list_transects())
    assert groups[0].title == UNASSIGNED_TITLE
    assert [e.dir_name for e in groups[0].entries] == ["loose"]
    assert groups[1].title == "T1"
    assert groups[1].children[0].entries[0].dir_name == "assigned"


def test_transects_facet_lists_transects_without_runs(out_root, store):
    store.add_transect(make_transect("Empty"))
    groups = catalogue.transects_facet(scan(out_root, store), store.list_transects())
    assert [g.title for g in groups] == ["Empty"]
    assert groups[0].all_entries() == []


def test_sessions_facet_gathers_a_day_across_transects(out_root, store):
    """The session is the only container that spans transects, which is the
    whole reason it earns a facet of its own."""
    batch = make_batch(store, "2026-07-01")
    seed_survey_run(store, out_root, "north", transect=make_transect("North"), batch=batch)
    seed_survey_run(store, out_root, "south", transect=make_transect("South"), batch=batch)

    groups = catalogue.sessions_facet(scan(out_root, store), store.list_batches())
    assert [g.title for g in groups] == ["2026-07-01"]
    assert sorted(e.dir_name for e in groups[0].all_entries()) == ["north", "south"]
    assert catalogue.session_summary(groups[0]) == "2 runs · 2 transects"


def test_sessions_facet_orders_newest_first(out_root, store):
    older = make_batch(store, "2026-06-01")
    newer = make_batch(store, "2026-07-01")
    seed_survey_run(
        store, out_root, "old_run", batch=older, run_timestamp="2026-06-01T10:00:00+00:00"
    )
    seed_survey_run(
        store,
        out_root,
        "new_run",
        transect=make_transect("Other"),
        batch=newer,
        run_timestamp="2026-07-01T10:00:00+00:00",
    )
    groups = catalogue.sessions_facet(scan(out_root, store), store.list_batches())
    assert [g.title for g in groups] == ["2026-07-01", "2026-06-01"]


def test_sessions_facet_surfaces_runs_with_no_session_first(out_root, store):
    """Runs from before sessions were recorded still have to be reachable."""
    batch = make_batch(store)
    seed_survey_run(store, out_root, "filed", batch=batch)
    write_run(out_root, "loose")
    groups = catalogue.sessions_facet(scan(out_root, store), store.list_batches())
    assert groups[0].title == catalogue.UNFILED_SESSION_TITLE
    assert [e.dir_name for e in groups[0].entries] == ["loose"]


def test_sessions_facet_lists_a_session_with_no_runs(out_root, store):
    make_batch(store, "Empty day")
    groups = catalogue.sessions_facet(scan(out_root, store), store.list_batches())
    assert [g.title for g in groups] == ["Empty day"]
    assert groups[0].all_entries() == []


def test_session_key_agrees_from_the_manifest_and_the_database(out_root, store):
    """Both sides of the join have to land on one key or a session splits in two.

    A run folder copied off another machine has a manifest and no row here, so
    the manifest fallback is the case that matters.
    """
    batch = make_batch(store)
    seed_survey_run(store, out_root, "run_a", batch=batch)
    fresh = SurveyStore(out_root / "fresh.db")
    entry = scan(out_root, fresh)[0]
    assert entry.db_pass is None
    assert entry.session_id == batch.id
    assert catalogue.session_group_key(entry.session_id) == catalogue.session_group_key(batch.id)


def test_videos_facet_separates_windows_on_shared_video(out_root, store):
    t1, t2 = make_transect("T1"), make_transect("T2")
    seed_survey_run(store, out_root, "first_half", transect=t1)
    store2_pass = TransectPass(
        transect_id=t2.id,
        video_id=store.list_videos()[0].id,
        begin_s=60.0,
        end_s=120.0,
    )
    store.add_transect(t2)
    store.add_pass(store2_pass)
    run = RunRecord(pass_id=store2_pass.id, run_dir_name="second_half", status="succeeded")
    store.add_run(run)
    write_run(out_root, "second_half", begin_s=60.0, end_s=120.0)
    groups = catalogue.videos_facet(scan(out_root, store))
    assert len(groups) == 1
    assert len(groups[0].children) == 2
    titles = {c.title for c in groups[0].children}
    assert any("T1" in t for t in titles) and any("T2" in t for t in titles)


def test_an_unhashed_clip_and_its_runs_are_one_group(out_root, store):
    """Scenario: a clip added while its file was unreadable has no checksum, so
    both sides of the By video join fall back to a name.

    Expected behaviour: one group. Both sides fall back to the file name, so a
    clip and the runs cut from it still meet.
    """
    video = store.upsert_video(
        make_video(content_hash=None, file_name="GX010001.MP4", path="/data/GX010001.MP4")
    )
    transect = make_transect()
    store.add_transect(transect)
    pass_ = TransectPass(
        transect_id=transect.id, video_id=video.id, begin_s=0.0, end_s=60.0
    )
    store.add_pass(pass_)
    run = RunRecord(pass_id=pass_.id, run_dir_name="a_run", status="succeeded")
    store.add_run(run)
    write_run(out_root, "a_run", video_hashes=[], input_videos=["/data/GX010001.MP4"])

    library = catalogue.video_library(
        store.list_videos(), store.list_passes(), store.list_runs()
    )
    groups = catalogue.videos_facet(scan(out_root, store), library)
    assert [g.title for g in groups] == ["GX010001.MP4"]
    assert len(groups[0].all_entries()) == 1


def test_reconcile_database_wins_and_records_move(out_root, store):
    transect, pass_, _run = seed_survey_run(store, out_root, "run1")
    elsewhere = make_transect("Elsewhere")
    store.add_transect(elsewhere)
    pass_.transect_id = elsewhere.id
    store.update_pass(pass_)
    entry = scan(out_root, store)[0]
    assert entry.transect_name == "Elsewhere"
    assert entry.moved_from == "T1"


def test_rename_run_rewrites_manifest_in_place(out_root):
    run_dir = write_run(out_root, "run1")
    manifest = catalogue.rename_run(run_dir, "  reef north  ")
    assert manifest["name"] == "reef north"
    on_disk = json.loads((run_dir / "run_manifest.json").read_text())
    assert on_disk["name"] == "reef north"
    assert not (run_dir / "run_manifest.json.tmp").exists()


def test_delete_run_refuses_outside_root(out_root, tmp_path, store):
    stray = tmp_path / "elsewhere" / "run1"
    stray.mkdir(parents=True)
    (stray / "run_manifest.json").write_text("{}")
    with pytest.raises(ValueError):
        catalogue.delete_run(out_root, stray, store)
    nested = write_run(out_root, "real")
    (nested / "no_manifest").mkdir()
    with pytest.raises(ValueError):
        catalogue.delete_run(out_root, nested / "no_manifest", store)


def test_delete_run_removes_directory_and_row(out_root, store):
    _t, _p, run = seed_survey_run(store, out_root, "doomed")
    catalogue.delete_run(out_root, out_root / "doomed", store)
    assert not (out_root / "doomed").exists()
    assert store.get_run(run.id) is None


def test_assign_moves_pass_with_sibling_reruns(out_root, store):
    transect, pass_, _run = seed_survey_run(store, out_root, "take1")
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="take2", status="succeeded"))
    write_run(out_root, "take2")
    target = make_transect("Target")
    store.add_transect(target)
    entries = scan(out_root, store)
    catalogue.assign_to_transect(store, [e for e in entries if e.dir_name == "take1"], target.id)
    assert all(e.transect_name == "Target" for e in scan(out_root, store))


def test_assign_adopts_adhoc_runs_as_one_pass(out_root, store):
    write_run(out_root, "adhoc_a")
    write_run(out_root, "adhoc_b")
    target = make_transect("Target")
    store.add_transect(target)
    catalogue.assign_to_transect(store, scan(out_root, store), target.id)
    passes = store.list_passes(transect_id=target.id)
    assert len(passes) == 1
    assert {r.run_dir_name for r in store.runs_for_pass(passes[0].id)} == {"adhoc_a", "adhoc_b"}


def test_assign_derives_window_from_frames_when_untrimmed(out_root, store):
    write_run(out_root, "untrimmed", begin_s=None, end_s=None, frames_processed=900, fps=3)
    target = make_transect("Target")
    store.add_transect(target)
    catalogue.assign_to_transect(store, scan(out_root, store), target.id)
    pass_ = store.list_passes(transect_id=target.id)[0]
    assert pass_.begin_s == 0.0
    assert pass_.end_s == pytest.approx(300.0)


def test_assign_refuses_a_run_whose_time_range_cannot_be_recovered(out_root, store):
    """Both the trim and the frame-count fallback are absent, so there is no
    window to give the pass. Adopting it anyway would write begin == end, which
    TransectPass rejects further down with a message naming neither run."""
    write_run(out_root, "no_window", begin_s=None, end_s=None, frames_processed=None, fps=None)
    target = make_transect("Target")
    store.add_transect(target)

    with pytest.raises(ValueError, match="no_window"):
        catalogue.assign_to_transect(store, scan(out_root, store), target.id)

    assert store.list_passes(transect_id=target.id) == []


def test_assign_keeps_manifest_ids_so_rebuild_stays_idempotent(out_root, store):
    seed_survey_run(store, out_root, "copied")
    fresh = SurveyStore(out_root / "fresh.db")
    target = make_transect("Target")
    fresh.add_transect(target)
    entries = scan(out_root, fresh)
    catalogue.assign_to_transect(fresh, entries, target.id)
    report = fresh.rebuild_from_scan(out_root)
    assert report.transects == 1 and report.passes == 0 and report.runs == 0
    entry = scan(out_root, fresh)[0]
    assert entry.transect_name == "Target"
    assert entry.moved_from == "T1"


def test_scan_incomplete_runs_surfaces_recorded_crash(out_root, store):
    _t, _v, pass_ = seed_pass(store)
    run = RunRecord(pass_id=pass_.id, run_dir_name="crashed", status="running")
    store.add_run(run)
    (out_root / "crashed").mkdir()
    incomplete = catalogue.scan_incomplete_runs(out_root, store, set())
    assert [e.dir_name for e in incomplete] == ["crashed"]
    assert incomplete[0].incomplete
    catalogue.reconcile(incomplete, store)
    assert incomplete[0].status_label == "running"


def test_scan_incomplete_runs_detects_run_log_without_record(out_root):
    half = out_root / "halfrun"
    half.mkdir()
    (half / "run.log").write_text("started")
    incomplete = catalogue.scan_incomplete_runs(out_root, None, set())
    assert [e.dir_name for e in incomplete] == ["halfrun"]
    assert incomplete[0].status_label == "incomplete"


def test_scan_incomplete_runs_ignores_complete_known_and_bare_dirs(out_root, store):
    write_run(out_root, "done")
    (out_root / "random").mkdir()
    incomplete = catalogue.scan_incomplete_runs(out_root, store, {"done"})
    assert incomplete == []


def test_delete_run_dir_removes_manifestless_dir_and_row(out_root, store):
    _t, _v, pass_ = seed_pass(store)
    run = RunRecord(pass_id=pass_.id, run_dir_name="crashed", status="failed")
    store.add_run(run)
    (out_root / "crashed").mkdir()
    catalogue.delete_run_dir(out_root, out_root / "crashed", store)
    assert not (out_root / "crashed").exists()
    assert store.get_run(run.id) is None


def test_delete_run_dir_refuses_outside_root(out_root, tmp_path, store):
    stray = tmp_path / "elsewhere" / "run1"
    stray.mkdir(parents=True)
    with pytest.raises(ValueError):
        catalogue.delete_run_dir(out_root, stray, store)


def test_video_library_flags_orphans(out_root, store):
    _t, video, _pass = seed_pass(store)
    orphan = store.upsert_video(
        make_video(content_hash="ff" * 16, file_name="orphan.mp4", path="/data/orphan.mp4")
    )
    by_id = {
        e.video.id: e
        for e in catalogue.video_library(
            store.list_videos(), store.list_passes(), store.list_runs()
        )
    }
    assert by_id[video.id].pass_count == 1
    assert not by_id[video.id].orphan
    assert by_id[orphan.id].orphan


def test_group_stats_ranges(out_root):
    write_run(out_root, "small", run_duration_s=100.0, semantic_reference_points=1_000)
    write_run(out_root, "large", run_duration_s=300.0, semantic_reference_points=9_000)
    entries = catalogue.scan_out_root(out_root)
    entries[0].size_bytes = 5
    stats = catalogue.group_stats(entries)
    assert stats.run_count == 2
    assert stats.total_bytes == 5
    assert stats.duration_range == (100.0, 300.0)
    assert stats.point_range == (1_000, 9_000)


def test_library_counts_a_pass_against_every_chapter(store):
    """The second half of a swim the camera split is not an unused clip."""
    transect, video, pass_ = seed_pass(store)
    second = store.upsert_video(
        make_video("cd" * 16, file_name="GX020001.MP4", path="/data/GX020001.MP4")
    )
    pass_.extra_video_ids = [second.id]
    store.update_pass(pass_)

    entries = {e.video.file_name: e for e in catalogue.video_library(
        store.list_videos(), store.list_passes()
    )}
    assert entries["GX020001.MP4"].pass_count == 1
    assert not entries["GX020001.MP4"].orphan
