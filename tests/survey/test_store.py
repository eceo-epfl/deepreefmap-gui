import json
import re
import sqlite3
import threading
import uuid

import pytest
from _factories import (
    make_batch,
    make_transect,
    make_video,
    seed_pass,
    seed_survey_run,
    write_v0_2_0_database,
)

from deepreefmap_gui.survey.models import BatchItem, RunRecord, SurveyBatch, TransectPass
from deepreefmap_gui.survey.models.convert import survey_manifest_block
from deepreefmap_gui.survey.store import SurveyStore
from deepreefmap_gui.survey.video_probe import UNKNOWN, YES


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


def test_upsert_video_without_hash_falls_back_to_the_path(store):
    """Hashing needs the file to be readable, and in the field it often is not,
    so the path is what holds a clip on an unplugged drive to one row."""
    store.upsert_video(make_video(content_hash=None))
    store.upsert_video(make_video(content_hash=None))
    assert len(store.list_videos()) == 1

    store.upsert_video(make_video(content_hash=None, path="/data/elsewhere.MP4"))
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
    with pytest.raises(ValueError, match="cannot be deleted"):
        store.delete_transect(transect.id)
    assert store.get_transect(transect.id) is not None


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
    from deepreefmap_gui.survey.store import latest_schema_version

    path = tmp_path / "survey.db"
    SurveyStore(path).close()
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {latest_schema_version() + 5}")
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
    store.add_batch_item(BatchItem(batch_id=batch.id, pass_id=pass_.id))
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="t1__p01", batch_id=batch.id))
    doc_path = tmp_path / "survey.json"
    store.export_json(doc_path)

    fresh = SurveyStore(tmp_path / "fresh.db")
    fresh.import_json(doc_path)
    assert fresh.list_transects() == store.list_transects()
    assert fresh.list_videos() == store.list_videos()
    assert fresh.list_passes() == store.list_passes()
    assert fresh.list_runs() == store.list_runs()
    assert fresh.list_all_batch_items() == store.list_all_batch_items()

    fresh.import_json(doc_path)
    assert len(fresh.list_passes()) == 1
    assert len(fresh.list_all_batch_items()) == 1


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
    assert restored_run.batch_id == batch.id
    assert [i.pass_id for i in fresh.list_batch_items(batch.id)] == [pass_.id]

    again = fresh.rebuild_from_scan(out_root)
    assert (again.transects, again.runs) == (0, 0)
    assert len(fresh.list_all_batch_items()) == 1


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


def test_deleting_a_pass_takes_its_cart_rows_with_it(store):
    """A cart row is a plan to process the pass, so it goes when the pass does."""
    _transect, _video, pass_ = seed_pass(store)
    first, second = SurveyBatch(name="Day 1"), SurveyBatch(name="Day 2")
    for batch in (first, second):
        store.add_batch(batch)
        store.add_batch_item(BatchItem(batch_id=batch.id, pass_id=pass_.id))

    store.delete_pass(pass_.id)

    assert store.list_all_batch_items() == []
    assert store.passes_in_batch(first.id) == []
    assert store.get_batch(second.id) is not None


def test_deleting_a_pass_with_a_run_says_why_it_cannot(store):
    """A run is history: it holds the pass, and the refusal reads as a sentence."""
    _transect, _video, pass_ = seed_pass(store)
    batch = SurveyBatch(name="Day 1")
    store.add_batch(batch)
    store.add_batch_item(BatchItem(batch_id=batch.id, pass_id=pass_.id))
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="t1__p01"))

    with pytest.raises(ValueError, match="recorded runs"):
        store.delete_pass(pass_.id)

    assert store.get_pass(pass_.id) is not None
    assert [i.pass_id for i in store.list_batch_items(batch.id)] == [pass_.id]


def test_a_carried_forward_survey_reaches_the_cart_cascade(tmp_path):
    """Scenario: a v0.2.0 survey.db, whose cart rows would restrict on pass delete.

    Expected behaviour: it opens at v7 with its cart membership intact, and
    deleting a pass that sits in a cart succeeds instead of failing on the
    foreign key.
    """
    from deepreefmap_gui.survey.store import latest_schema_version

    transect_id, video_id, batch_id, pass_id = (uuid.uuid4() for _ in range(4))
    now = "2026-08-01T00:00:00+00:00"
    db_path = write_v0_2_0_database(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO transect VALUES (?,?,'',?,?,?,?,?,?,?,?)",
            (str(transect_id), "T1", -17.5, 177.1, -17.5005, 177.1005, 50.0, 8.0, now, now),
        )
        conn.execute(
            "INSERT INTO video_asset VALUES (?,?,?,?,?,?,?,?,?)",
            (str(video_id), "GX010001.MP4", "/data/GX010001.MP4", "ab" * 16, 1024, now, 90.0,
             30.0, now),
        )
        conn.execute(
            "INSERT INTO survey_batch VALUES (?,?,?,?)",
            (str(batch_id), "Day 1", "survey_preset", now),
        )
        conn.execute(
            "INSERT INTO transect_pass (id, transect_id, video_id, batch_id, direction,"
            " begin_s, end_s, notes, created_at, extra_video_ids, held)"
            " VALUES (?,?,?,?,'forward',0,90,'',?,'[]',0)",
            (str(pass_id), str(transect_id), str(video_id), str(batch_id), now),
        )
    conn.close()

    store = SurveyStore(db_path)
    assert [i.pass_id for i in store.list_batch_items(batch_id)] == [pass_id]
    version = sqlite3.connect(db_path).execute("PRAGMA user_version").fetchone()[0]
    assert version == latest_schema_version() == 7

    store.delete_pass(pass_id)
    assert store.list_all_batch_items() == []


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


def _normalised_ddl(sql):
    return re.sub(r"\s*([,()])\s*", r"\1", " ".join((sql or "").replace('"', "").split()))


def database_shape(db_path):
    """Every object in a database, in a form two builds can be compared in.

    Identifier quoting is stripped and space around punctuation collapsed: a
    table SQLite rebuilt and renamed carries quotes the same table created
    outright does not, and ADD COLUMN splices its column in before the closing
    paren rather than reflowing the statement.
    """
    conn = sqlite3.connect(db_path)
    try:
        objects = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        shape = {
            "version": conn.execute("PRAGMA user_version").fetchone()[0],
            "objects": [(kind, name, _normalised_ddl(sql)) for kind, name, sql in objects],
        }
        for kind, name, _sql in objects:
            if kind != "table":
                continue
            shape[f"columns:{name}"] = conn.execute(f"PRAGMA table_info({name})").fetchall()
            shape[f"keys:{name}"] = conn.execute(f"PRAGMA foreign_key_list({name})").fetchall()
    finally:
        conn.close()
    return shape


def test_a_database_older_than_v0_2_0_is_refused(tmp_path):
    """Scenario: a survey.db predating the oldest version this build carries forward.

    Expected behaviour: it refuses to open and names the version that can bring
    the database forward, rather than running a script written for a shape it
    does not have.
    """
    db_path = tmp_path / "ancient.db"
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("PRAGMA user_version = 1")
    conn.close()

    with pytest.raises(RuntimeError, match="0.2.0"):
        SurveyStore(db_path)


def test_a_carried_forward_survey_matches_a_fresh_one(tmp_path):
    """Scenario: one machine upgraded from v0.2.0, another was installed today.

    Expected behaviour: the two schemas are identical. A difference between them
    only ever shows up on whichever kind of machine the developer does not have.
    """
    carried = write_v0_2_0_database(tmp_path / "carried.db")
    SurveyStore(carried).close()
    fresh = tmp_path / "fresh.db"
    SurveyStore(fresh).close()

    assert database_shape(carried) == database_shape(fresh)


def test_a_database_written_before_optional_transects_migrates(tmp_path):
    """Scenario: a survey.db from the build where every pass needed a transect.

    Expected behaviour: it opens, its passes keep the transects they had, their
    runs survive the table rebuild, and a new pass may now name none. The rebuild
    is the only way SQLite can relax NOT NULL, so it is worth proving against a
    database written the old way rather than one this build made.
    """
    transect_id, video_id, pass_id, run_id = (uuid.uuid4() for _ in range(4))
    now = "2026-08-01T00:00:00+00:00"
    db_path = write_v0_2_0_database(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO transect VALUES (?,?,'',?,?,?,?,?,?,?,?)",
            (str(transect_id), "T1", -17.5, 177.1, -17.5005, 177.1005, 50.0, 8.0, now, now),
        )
        conn.execute(
            "INSERT INTO video_asset VALUES (?,?,?,?,?,?,?,?,?)",
            (str(video_id), "GX010001.MP4", "/data/GX010001.MP4", "ab" * 16, 1024, now, 90.0,
             30.0, now),
        )
        conn.execute(
            "INSERT INTO transect_pass (id, transect_id, video_id, batch_id, direction,"
            " begin_s, end_s, notes, created_at, extra_video_ids, held)"
            " VALUES (?,?,?,NULL,'forward',0,90,'',?,'[]',0)",
            (str(pass_id), str(transect_id), str(video_id), now),
        )
        conn.execute(
            "INSERT INTO run_record VALUES (?,?,?,?,?,?,'',?)",
            (str(run_id), str(pass_id), "t1__p01", "succeeded", now, now, now),
        )
    conn.close()

    store = SurveyStore(db_path)
    kept = store.get_pass(pass_id)
    assert kept is not None
    assert kept.transect_id == transect_id
    assert kept.extra_video_ids == []
    assert store.get_run(run_id) is not None
    assert store.runs_for_transect(transect_id) != []

    unfiled = TransectPass(transect_id=None, video_id=video_id, begin_s=0.0, end_s=30.0)
    store.add_pass(unfiled)
    assert store.get_pass(unfiled.id).transect_id is None


def test_a_pass_that_names_no_transect_is_counted_against_none(store):
    """Scenario: a clip is cut without naming a transect, which the schema allows.

    Expected behaviour: it is tallied against no transect, and the null group is
    never read as a transect id.
    """
    transect, video, _assigned = seed_pass(store)
    unfiled = TransectPass(transect_id=None, video_id=video.id, begin_s=0.0, end_s=30.0)
    store.add_pass(unfiled)
    store.add_run(RunRecord(pass_id=unfiled.id, run_dir_name="unfiled"))

    counts = store.transect_usage_counts()
    assert set(counts) == {transect.id}
    assert counts[transect.id] == (1, 0)


def test_rebuild_restores_a_pass_that_named_no_transect(store, tmp_path):
    """Scenario: a clip was processed with the transect skipped.

    Expected behaviour: its manifest carries a null transect block, and a rebuild
    restores the pass unassigned rather than skipping the run or inventing a
    transect to hang it on.
    """
    _transect, _video, pass_ = seed_pass(store)
    pass_.transect_id = None
    store.update_pass(pass_)
    run = RunRecord(pass_id=pass_.id, run_dir_name="unfiled")
    store.add_run(run)

    out_root = tmp_path / "out"
    write_manifest(out_root, "unfiled", survey_manifest_block(run, pass_, None, None))

    fresh = SurveyStore(tmp_path / "rebuilt.db")
    report = fresh.rebuild_from_scan(out_root)
    assert report.skipped == []
    assert report.passes == 1
    restored = fresh.get_pass(pass_.id)
    assert restored is not None
    assert restored.transect_id is None


def test_current_cart_is_the_newest_batch_only_while_it_has_run_nothing(store):
    assert store.current_cart() is None

    _, _, pass_ = seed_pass(store)
    first = SurveyBatch(name="Day 1")
    store.add_batch(first)
    assert store.current_cart() == first

    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="t1__p01", batch_id=first.id))
    assert store.batch_run_count(first.id) == 1
    assert store.current_cart() is None

    second = SurveyBatch(name="Day 2")
    store.add_batch(second)
    assert store.current_cart() == second


def test_an_older_empty_batch_behind_a_started_one_is_not_the_cart(store):
    _, _, pass_ = seed_pass(store)
    abandoned = SurveyBatch(name="Day 1")
    store.add_batch(abandoned)
    started = SurveyBatch(name="Day 2")
    store.add_batch(started)
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="t1__p01", batch_id=started.id))
    assert store.current_cart() is None


def test_a_pass_can_be_a_member_of_two_batches_but_of_one_only_once(store):
    _, _, pass_ = seed_pass(store)
    first = SurveyBatch(name="Day 1")
    second = SurveyBatch(name="Day 2")
    store.add_batch(first)
    store.add_batch(second)

    store.add_batch_item(BatchItem(batch_id=first.id, pass_id=pass_.id))
    store.add_batch_item(BatchItem(batch_id=first.id, pass_id=pass_.id))
    store.add_batch_item(BatchItem(batch_id=second.id, pass_id=pass_.id))
    assert len(store.list_batch_items(first.id)) == 1
    assert [p.id for p in store.passes_in_batch(second.id)] == [pass_.id]

    store.remove_batch_item(first.id, pass_.id)
    assert store.list_batch_items(first.id) == []
    assert len(store.list_batch_items(second.id)) == 1


def test_passes_in_batch_keeps_the_order_the_cart_was_filled_in(store):
    transect, video, first = seed_pass(store)
    later = TransectPass(
        transect_id=transect.id, video_id=video.id, begin_s=70.0, end_s=120.0
    )
    earlier = TransectPass(
        transect_id=transect.id, video_id=video.id, begin_s=130.0, end_s=180.0
    )
    store.add_pass(later)
    store.add_pass(earlier)
    batch = SurveyBatch(name="Day 1")
    store.add_batch(batch)
    for pass_ in (earlier, first, later):
        store.add_batch_item(BatchItem(batch_id=batch.id, pass_id=pass_.id))
    assert [p.id for p in store.passes_in_batch(batch.id)] == [
        earlier.id, first.id, later.id
    ]


def test_carrying_a_survey_forward_gives_every_run_its_session(tmp_path):
    """Scenario: a v0.2.0 survey.db, where the session lived only on the pass.

    Expected behaviour: each run's batch_id is backfilled from its pass, and a
    pass that had a session gains a batch_item row whose id from_row can parse
    back.
    """
    from deepreefmap_gui.survey.store import latest_schema_version

    transect_id, video_id, batch_id, pass_id, run_id = (uuid.uuid4() for _ in range(5))
    now = "2026-08-01T00:00:00+00:00"
    db_path = write_v0_2_0_database(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO transect VALUES (?,?,'',?,?,?,?,?,?,?,?)",
            (str(transect_id), "T1", -17.5, 177.1, -17.5005, 177.1005, 50.0, 8.0, now, now),
        )
        conn.execute(
            "INSERT INTO video_asset VALUES (?,?,?,?,?,?,?,?,?)",
            (str(video_id), "GX010001.MP4", "/data/GX010001.MP4", "ab" * 16, 1024, now, 90.0,
             30.0, now),
        )
        conn.execute(
            "INSERT INTO survey_batch VALUES (?,?,?,?)",
            (str(batch_id), "Day 1", "survey_preset", now),
        )
        conn.execute(
            "INSERT INTO transect_pass (id, transect_id, video_id, batch_id, direction,"
            " begin_s, end_s, notes, created_at, extra_video_ids, held)"
            " VALUES (?,?,?,?,'forward',0,90,'',?,'[]',0)",
            (str(pass_id), str(transect_id), str(video_id), str(batch_id), now),
        )
        conn.execute(
            "INSERT INTO run_record VALUES (?,?,?,?,?,?,'',?)",
            (str(run_id), str(pass_id), "t1__p01", "succeeded", now, now, now),
        )
    conn.close()

    store = SurveyStore(db_path)
    assert store.get_run(run_id).batch_id == batch_id
    items = store.list_batch_items(batch_id)
    assert [i.pass_id for i in items] == [pass_id]
    assert isinstance(items[0].id, uuid.UUID)
    version = sqlite3.connect(db_path).execute("PRAGMA user_version").fetchone()[0]
    assert version == latest_schema_version()


def test_carrying_a_survey_forward_leaves_every_clip_unprobed(tmp_path):
    """Scenario: a v0.2.0 survey.db, from before clips carried container metadata.

    Expected behaviour: the clip keeps everything it already knew, and the two
    tri-states read 'unknown' rather than 'no', since nothing has looked at the
    file yet.
    """
    from deepreefmap_gui.survey.store import latest_schema_version

    video_id = uuid.uuid4()
    now = "2026-08-01T00:00:00+00:00"
    db_path = write_v0_2_0_database(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO video_asset VALUES (?,?,?,?,?,?,?,?,?)",
            (str(video_id), "GX010001.MP4", "/data/GX010001.MP4", "ab" * 16, 1024, now, 90.0,
             30.0, now),
        )
    conn.close()

    restored = SurveyStore(db_path).get_video(video_id)
    assert restored is not None
    assert restored.hash == "ab" * 16
    assert restored.duration_s == 90.0
    assert restored.created_at == now
    assert restored.captured_at is None
    assert restored.probed_at is None
    assert (restored.gravity, restored.gps) == (UNKNOWN, UNKNOWN)
    version = sqlite3.connect(db_path).execute("PRAGMA user_version").fetchone()[0]
    assert version == latest_schema_version()


def test_rebuild_repeats_cleanly_and_keeps_what_a_probe_learned(store, tmp_path):
    """Scenario: a manifest records the clip's path, hash and size, nothing else.

    Expected behaviour: rebuilding a second time adds no rows, and a clip that
    has since been probed keeps its capture time and probe stamp rather than
    being flattened back to what the manifest knows.
    """
    out_root = tmp_path / "out"
    seed_survey_run(store, out_root, "t1__p01")

    fresh = SurveyStore(tmp_path / "rebuilt.db")
    first = fresh.rebuild_from_scan(out_root)
    assert (first.videos, first.passes, first.runs, first.skipped) == (1, 1, 1, [])

    probed = fresh.list_videos()[0]
    probed.captured_at = "2026-06-01T09:15:00+00:00"
    probed.probed_at = "2026-08-01T00:00:00+00:00"
    probed.gravity = YES
    fresh.update_video(probed)

    second = fresh.rebuild_from_scan(out_root)
    assert (second.videos, second.passes, second.runs, second.skipped) == (0, 0, 0, [])
    assert len(fresh.list_videos()) == 1
    kept = fresh.get_video(probed.id)
    assert kept.captured_at == "2026-06-01T09:15:00+00:00"
    assert kept.probed_at == "2026-08-01T00:00:00+00:00"
    assert kept.gravity == YES


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


def test_deleting_a_session_takes_its_records_and_leaves_the_shared(store):
    """The session's cart rows and run records go; footage and transects stay."""
    batch = make_batch(store)
    transect, video, pass_ = seed_pass(store, batch=batch)
    store.add_run(
        RunRecord(pass_id=pass_.id, run_dir_name="t1__p01", batch_id=batch.id)
    )
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="t1__p02"))
    assert {r.run_dir_name for r in store.runs_in_batch(batch.id)} == {
        "t1__p01",
        "t1__p02",
    }

    store.delete_batch(batch.id)

    assert store.get_batch(batch.id) is None
    assert store.list_all_batch_items() == []
    assert store.list_runs() == []
    surviving = store.get_pass(pass_.id)
    assert surviving is not None
    assert surviving.batch_id is None
    assert store.get_transect(transect.id) is not None
    assert store.get_video(video.id) is not None


def test_deleting_a_session_leaves_other_sessions_runs(store):
    first, second = make_batch(store, "Day 1"), make_batch(store, "Day 2")
    _t, _v, pass_ = seed_pass(store, batch=first)
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="one", batch_id=first.id))
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="two", batch_id=second.id))

    store.delete_batch(second.id)

    assert [r.run_dir_name for r in store.list_runs()] == ["one"]
    assert store.get_batch(first.id) is not None
