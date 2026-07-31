import json
import sqlite3
import threading
import uuid

import pytest
from _factories import make_transect, make_video, seed_pass

from deepreefmap_gui.survey.models import RunRecord, SurveyBatch, TransectPass
from deepreefmap_gui.survey.models.convert import survey_manifest_block
from deepreefmap_gui.survey.store import SurveyStore


def test_transect_crud_round_trip(store):
    transect = make_transect()
    store.add_transect(transect)
    assert store.get_transect(transect.id) == transect
    transect.end_lat = -17.501
    before = transect.updated_at
    transect.updated_at = "2000-01-01T00:00:00+00:00"
    store.update_transect(transect)
    stored = store.get_transect(transect.id)
    assert stored.end_lat == -17.501
    assert stored.updated_at >= before
    store.delete_transect(transect.id)
    assert store.get_transect(transect.id) is None


def test_transect_names_are_unique(store):
    store.add_transect(make_transect())
    with pytest.raises(sqlite3.IntegrityError):
        store.add_transect(make_transect())


def test_list_transects_orders_by_name(store):
    store.add_transect(make_transect("B"))
    store.add_transect(make_transect("A"))
    assert [t.name for t in store.list_transects()] == ["A", "B"]


def test_upsert_video_dedups_by_hash(store):
    first = store.upsert_video(make_video())
    again = make_video()
    again.path = "/moved/GX010001.MP4"
    again.duration_s = 61.5
    second = store.upsert_video(again)
    assert second.id == first.id
    assert len(store.list_videos()) == 1
    stored = store.get_video(first.id)
    assert stored.path == "/moved/GX010001.MP4"
    assert stored.duration_s == 61.5


def test_upsert_video_without_hash_always_inserts(store):
    store.upsert_video(make_video(content_hash=None))
    store.upsert_video(make_video(content_hash=None))
    assert len(store.list_videos()) == 2


def test_pass_filters(store):
    transect, video, pass_ = seed_pass(store)
    batch = SurveyBatch(name="Day 1")
    store.add_batch(batch)
    second = TransectPass(
        transect_id=transect.id,
        video_id=video.id,
        batch_id=batch.id,
        begin_s=70.0,
        end_s=120.0,
        direction="reverse",
    )
    store.add_pass(second)
    assert len(store.list_passes()) == 2
    assert store.list_passes(transect_id=transect.id) == [pass_, second]
    assert store.list_passes(batch_id=batch.id) == [second]


def test_delete_transect_with_passes_is_blocked(store):
    transect, _, _ = seed_pass(store)
    with pytest.raises(sqlite3.IntegrityError):
        store.delete_transect(transect.id)


def test_run_status_stamps_lifecycle(store):
    _, _, pass_ = seed_pass(store)
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01")
    store.add_run(run)
    store.set_run_status(run.id, "running")
    started = store.get_run(run.id)
    assert started.status == "running"
    assert started.started_at is not None
    assert started.finished_at is None
    store.set_run_status(run.id, "failed", error="boom")
    finished = store.get_run(run.id)
    assert finished.status == "failed"
    assert finished.error == "boom"
    assert finished.finished_at is not None


def test_reconcile_marks_non_terminal_runs_interrupted(store):
    """Pending and running rows are leftovers once the process is gone; a
    terminal succeeded/failed/cancelled row is a finished decision and stays."""
    _, _, pass_ = seed_pass(store)
    running = RunRecord(pass_id=pass_.id, run_dir_name="running")
    pending = RunRecord(pass_id=pass_.id, run_dir_name="pending")
    done = RunRecord(pass_id=pass_.id, run_dir_name="done")
    for record in (running, pending, done):
        store.add_run(record)
    store.set_run_status(running.id, "running")
    store.set_run_status(done.id, "succeeded")

    assert store.reconcile_interrupted_runs() == 2
    assert store.get_run(running.id).status == "interrupted"
    assert store.get_run(pending.id).status == "interrupted"
    assert store.get_run(done.id).status == "succeeded"
    reconciled = store.get_run(running.id)
    assert reconciled.finished_at is not None
    assert reconciled.error


def test_reopening_a_store_reconciles_stale_runs(tmp_path):
    """Scenario: a crash left a run 'running'.

    Expected behaviour: the next open flips it to interrupted, so it reads as
    work to redo rather than live work that blocks the pass forever.
    """
    path = tmp_path / "survey.db"
    store = SurveyStore(path)
    _, _, pass_ = seed_pass(store)
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01")
    store.add_run(run)
    store.set_run_status(run.id, "running")
    store.close()

    reopened = SurveyStore(path)
    assert reopened.get_run(run.id).status == "interrupted"


def test_a_newer_schema_is_refused_rather_than_opened_blindly(tmp_path):
    """Scenario: an update was rolled back, leaving a newer survey.db behind.

    Expected behaviour: opening it raises a readable error, rather than running
    an empty migration slice and reading columns this build does not understand.
    """
    from deepreefmap_gui.survey.store import _MIGRATIONS

    path = tmp_path / "survey.db"
    SurveyStore(path).close()
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {len(_MIGRATIONS) + 5}")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="schema v"):
        SurveyStore(path)


def test_run_status_rejects_unknown_run_and_status(store):
    _, _, pass_ = seed_pass(store)
    with pytest.raises(KeyError):
        store.set_run_status(uuid.uuid4(), "running")
    with pytest.raises(ValueError):
        run = RunRecord(pass_id=pass_.id, run_dir_name="run")
        store.add_run(run)
        store.set_run_status(run.id, "exploded")


def test_runs_for_transect_joins_passes(store):
    transect, _, pass_ = seed_pass(store)
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01")
    store.add_run(run)
    assert store.runs_for_transect(transect.id) == [store.get_run(run.id)]
    assert store.runs_for_pass(pass_.id) == [store.get_run(run.id)]


def test_run_lookup_by_dir_name_and_delete(store):
    _, _, pass_ = seed_pass(store)
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01")
    store.add_run(run)
    assert store.run_by_dir_name("t1__p01") == store.get_run(run.id)
    assert store.run_by_dir_name("missing") is None
    store.delete_run(run.id)
    assert store.get_run(run.id) is None


def test_json_export_import_round_trip(store, tmp_path):
    _, _, pass_ = seed_pass(store)
    batch = SurveyBatch(name="Day 1")
    store.add_batch(batch)
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="t1__p01"))
    doc_path = tmp_path / "survey.json"
    store.export_json(doc_path)

    fresh = SurveyStore(tmp_path / "fresh.db")
    fresh.import_json(doc_path)
    assert fresh.list_transects() == store.list_transects()
    assert fresh.list_videos() == store.list_videos()
    assert fresh.list_passes() == store.list_passes()
    assert fresh.list_runs() == store.list_runs()

    fresh.import_json(doc_path)
    assert len(fresh.list_passes()) == 1


def write_manifest(out_root, run_dir_name, block, video_path="/data/GX010001.MP4"):
    run_dir = out_root / run_dir_name
    run_dir.mkdir(parents=True)
    manifest = {
        "name": run_dir_name,
        "input_videos": [video_path],
        "video_hashes": ["ab" * 16],
        "video_sizes": [1024],
        "video_mtimes": ["2026-07-20T09:00:00+00:00"],
        "run_timestamp": "2026-07-20T09:05:00+00:00",
        "survey": block,
    }
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest))


def test_rebuild_from_scan_restores_everything(store, tmp_path):
    transect, video, pass_ = seed_pass(store, direction="reverse")
    batch = SurveyBatch(name="Day 1")
    store.add_batch(batch)
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01")
    store.add_run(run)

    out_root = tmp_path / "out"
    write_manifest(out_root, "t1__p01", survey_manifest_block(run, pass_, transect, batch))
    (out_root / "plain_run").mkdir()
    (out_root / "plain_run" / "run_manifest.json").write_text(json.dumps({"name": "plain"}))

    fresh = SurveyStore(tmp_path / "rebuilt.db")
    report = fresh.rebuild_from_scan(out_root)
    assert (report.transects, report.videos, report.batches, report.passes, report.runs) == (
        1, 1, 1, 1, 1,
    )
    assert report.skipped == ["plain_run"]
    restored = fresh.get_transect(transect.id)
    assert restored.name == transect.name
    assert restored.start_lat == transect.start_lat
    restored_pass = fresh.get_pass(pass_.id)
    assert restored_pass.direction == "reverse"
    assert restored_pass.end_s == 60.0
    restored_run = fresh.get_run(run.id)
    assert restored_run.status == "succeeded"
    assert restored_run.run_dir_name == "t1__p01"

    again = fresh.rebuild_from_scan(out_root)
    assert (again.transects, again.runs) == (0, 0)


def test_store_reopens_existing_database(tmp_path):
    path = tmp_path / "survey.db"
    first = SurveyStore(path)
    first.add_transect(make_transect())
    first.close()
    second = SurveyStore(path)
    assert len(second.list_transects()) == 1


def test_worker_thread_writes_are_visible(store):
    _, _, pass_ = seed_pass(store)
    errors = []

    def worker():
        try:
            run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01")
            store.add_run(run)
            store.set_run_status(run.id, "running")
            store.set_run_status(run.id, "succeeded")
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert not errors
    assert [r.status for r in store.runs_for_pass(pass_.id)] == ["succeeded"]


@pytest.mark.parametrize(
    "corruption, why",
    [
        ({"transect": {"id": "not-a-uuid"}}, "unparseable id"),
        ({"transect": None}, "null section"),
        ({"pass": {"direction": "sideways"}}, "invalid enum"),
        ({"pass": {"begin_s": "soon"}}, "wrong type"),
        ({"transect": {"start_lat": 999.0}}, "out-of-range coordinate"),
    ],
)
def test_rebuild_skips_a_corrupt_survey_block_without_losing_the_rest(
    store, tmp_path, corruption, why
):
    """Scenario: one run dir's manifest was hand-edited or half-written.

    Expected behaviour: that run is skipped and the others still restore. The
    module docstring calls the database rebuildable from disk, and the broad
    except in rebuild_from_scan is the whole of that promise.
    """
    transect, video, pass_ = seed_pass(store)
    good = RunRecord(pass_id=pass_.id, run_dir_name="good")
    bad = RunRecord(pass_id=pass_.id, run_dir_name="bad")
    store.add_run(good)
    store.add_run(bad)

    out_root = tmp_path / "out"
    block = survey_manifest_block(good, pass_, transect, None)
    write_manifest(out_root, "good", block)

    broken = json.loads(json.dumps(block))
    for section, patch in corruption.items():
        if patch is None:
            broken[section] = None
        else:
            broken[section].update(patch)
    write_manifest(out_root, "bad", broken)

    fresh = SurveyStore(tmp_path / "rebuilt.db")
    report = fresh.rebuild_from_scan(out_root)

    assert "bad" in report.skipped, f"a manifest with an {why} was accepted"
    assert fresh.run_by_dir_name("good") is not None
    assert fresh.run_by_dir_name("bad") is None


def test_rebuild_skips_an_unreadable_manifest(store, tmp_path):
    out_root = tmp_path / "out"
    (out_root / "truncated").mkdir(parents=True)
    (out_root / "truncated" / "run_manifest.json").write_text('{"survey": {')

    report = SurveyStore(tmp_path / "rebuilt.db").rebuild_from_scan(out_root)
    assert report.skipped == ["truncated"]


def test_delete_pass_removes_it(store):
    _transect, _video, pass_ = seed_pass(store)
    assert len(store.list_passes()) == 1
    store.delete_pass(pass_.id)
    assert store.list_passes() == []
    assert store.get_pass(pass_.id) is None


def test_list_passes_combines_both_filters(store):
    """Filtering by transect and batch at once is an AND, not the last one set."""
    batch = SurveyBatch(name="Day 1")
    store.add_batch(batch)
    transect_a, video, pass_a = seed_pass(store)
    transect_b = make_transect("T2")
    store.add_transect(transect_b)
    pass_b = TransectPass(
        transect_id=transect_b.id, video_id=video.id, begin_s=0.0, end_s=60.0,
        batch_id=batch.id,
    )
    store.add_pass(pass_b)

    assert len(store.list_passes(transect_id=transect_b.id)) == 1
    assert len(store.list_passes(batch_id=batch.id)) == 1
    # T1 has no pass in this batch, so the AND is empty.
    assert store.list_passes(transect_id=transect_a.id, batch_id=batch.id) == []
    assert len(store.list_passes(transect_id=transect_b.id, batch_id=batch.id)) == 1


def test_batches_are_readable_after_being_added(store):
    batch = SurveyBatch(name="Day 1")
    store.add_batch(batch)
    assert store.get_batch(batch.id) == batch
    assert [b.name for b in store.list_batches()] == ["Day 1"]
    assert store.get_batch(uuid.uuid4()) is None


def test_pass_chapters_round_trip(store):
    transect, video, _ = seed_pass(store)
    second = store.upsert_video(
        make_video("cd" * 16, file_name="GX020001.MP4", path="/data/GX020001.MP4")
    )
    chaptered = TransectPass(
        transect_id=transect.id,
        video_id=video.id,
        extra_video_ids=[second.id],
        begin_s=0.0,
        end_s=600.0,
    )
    store.add_pass(chaptered)
    assert store.get_pass(chaptered.id).video_ids() == [video.id, second.id]

    chaptered.extra_video_ids = []
    store.update_pass(chaptered)
    assert store.get_pass(chaptered.id).extra_video_ids == []


def test_a_database_written_before_chapters_migrates(tmp_path):
    """A survey.db from an earlier build opens, and its passes read back."""
    from deepreefmap_gui.survey.store import _MIGRATIONS

    pass_id, transect_id, video_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    with conn:
        conn.executescript(_MIGRATIONS[0])
        conn.execute("PRAGMA user_version = 1")
        # foreign_keys is off on a raw connection, so the pass can stand alone.
        conn.execute(
            "INSERT INTO transect_pass (id, transect_id, video_id, direction, begin_s, end_s,"
            " notes, created_at) VALUES (?, ?, ?, 'forward', 0.0, 60.0, '', ?)",
            (str(pass_id), str(transect_id), str(video_id), "2026-07-01T00:00:00+00:00"),
        )
    conn.close()

    restored = SurveyStore(db_path).get_pass(pass_id)
    assert restored is not None
    assert restored.video_ids() == [video_id]
    version = sqlite3.connect(db_path).execute("PRAGMA user_version").fetchone()[0]
    assert version == len(_MIGRATIONS)


def test_rebuild_restores_every_chapter_of_a_pass(store, tmp_path):
    """A pass that spanned GoPro chapters comes back naming all of them."""
    transect, video, pass_ = seed_pass(store)
    second = store.upsert_video(
        make_video("cd" * 16, file_name="GX020001.MP4", path="/data/GX020001.MP4")
    )
    pass_.extra_video_ids = [second.id]
    store.update_pass(pass_)
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01")
    store.add_run(run)

    out_root = tmp_path / "out"
    run_dir = out_root / "t1__p01"
    run_dir.mkdir(parents=True)
    (run_dir / "run_manifest.json").write_text(json.dumps({
        "input_videos": [video.path, second.path],
        "video_hashes": [video.hash, second.hash],
        "survey": survey_manifest_block(run, pass_, transect, None),
    }))

    fresh = SurveyStore(tmp_path / "rebuilt.db")
    report = fresh.rebuild_from_scan(out_root)
    assert report.videos == 2
    restored = fresh.get_pass(pass_.id)
    assert [fresh.get_video(v).file_name for v in restored.video_ids()] == [
        "GX010001.MP4", "GX020001.MP4"
    ]
