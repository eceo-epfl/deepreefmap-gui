"""The engine against a registry that never leaves the process."""

from __future__ import annotations

import json
import uuid

import pytest
from _factories import make_batch, make_transect, make_video, seed_pass, seed_survey_run
from deepreefmap.config.classes import load_classes

from deepreefmap_gui.notify.center import NotificationCenter
from deepreefmap_gui.survey.models import RunRecord, Site
from deepreefmap_gui.survey.models.notification import SURVEY, WARNING
from deepreefmap_gui.survey.store import SYNC_SECTIONS
from deepreefmap_gui.sync import wire
from deepreefmap_gui.sync.client import ConflictError, ServerUnreachableError
from deepreefmap_gui.sync.engine import (
    CONFLICT_DISCARDED,
    CONFLICT_OVERWRITTEN,
    CURSOR_KEY,
    PASS_WITHOUT_VIDEOS,
    PULL_LIMIT,
    WATERMARK_PREFIX,
    SyncEngine,
)

# Stamps far enough either side of any clock this runs on that last-write-wins
# reads them the same way on any day.
EARLIER = "2000-01-01T00:00:00+00:00"
LATER = "2099-01-01T00:00:00Z"


class FakeRegistry:
    """Answers like the registry and remembers everything it was told.

    ``skipped`` names rows it claims to hold a newer copy of, per section.
    ``unaccounted`` names a section it answers about without saying what became of
    the rows, which is the shape of a half-answer the engine must not trust.
    """

    def __init__(self, pages=(), fail=None, skipped=None, unaccounted=(), cursor=4830):
        self._pages = list(pages)
        self._fail = fail
        self._skipped = skipped or {}
        self._unaccounted = set(unaccounted)
        self._cursor = cursor
        self.pulls: list[int | None] = []
        self.pushes: list[dict] = []
        self.calls: list[str] = []

    def pull(self, since=None, limit=PULL_LIMIT):
        self.calls.append("pull")
        self.pulls.append(since)
        if not self._pages:
            return page(since, {})
        answer = self._pages.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def push(self, sections):
        self.calls.append("push")
        self.pushes.append(dict(sections))
        if self._fail is not None:
            raise self._fail
        return {
            "cursor": self._cursor,
            "sections": {name: self._outcome(name, rows) for name, rows in sections.items()},
        }

    def _outcome(self, name, rows):
        if name in self._unaccounted:
            return {"received": len(rows), "applied": 0, "skipped": []}
        skipped = [str(row_id) for row_id in self._skipped.get(name, ())]
        return {
            "received": len(rows),
            "applied": len(rows) - len(skipped),
            "skipped": skipped,
        }


def page(cursor, sections, has_more=False):
    return {"contract_version": 1, "cursor": cursor, "has_more": has_more, "sections": sections}


def sync_fields(**overrides):
    return {
        "created_at": EARLIER,
        "updated_at": LATER,
        "deleted_at": None,
        "created_by": "auth0|abc",
        "device_id": None,
        "server_seq": 4102,
        **overrides,
    }


def site_row(site_id, name, **overrides):
    return {"id": str(site_id), "name": name, "description": "", **sync_fields(**overrides)}


def transect_row(transect_id, name="T1", **overrides):
    return {
        "id": str(transect_id),
        "site_id": None,
        "name": name,
        "description": "",
        "start_lat": -17.5,
        "start_lon": 177.1,
        "end_lat": -17.5005,
        "end_lon": 177.1005,
        "length_m": 50.0,
        **sync_fields(**overrides),
    }


def video_row(video_id, file_name="GX010001.MP4", **overrides):
    return {
        "id": str(video_id),
        "hash": None,
        "file_name": file_name,
        "gravity": "unknown",
        "gps": "unknown",
        **sync_fields(**overrides),
    }


def pass_row(pass_id, transect_id=None, **overrides):
    return {
        "id": str(pass_id),
        "transect_id": None if transect_id is None else str(transect_id),
        "campaign_id": None,
        "begin_s": 0.0,
        "end_s": 60.0,
        "direction": "forward",
        "upside_down": False,
        "label": "",
        "notes": "",
        "quality": None,
        **sync_fields(**overrides),
    }


def chapter_row(pass_id, video_id, ordinal, **overrides):
    return {
        "id": str(wire.pass_video_id(pass_id, video_id)),
        "pass_id": str(pass_id),
        "video_id": str(video_id),
        "ordinal": ordinal,
        **sync_fields(**overrides),
    }


def run_row(run_id, pass_id, run_dir_name="t1__p01", **overrides):
    return {
        "id": str(run_id),
        "pass_id": str(pass_id),
        "run_dir_name": run_dir_name,
        "status": "succeeded",
        "started_at": None,
        "finished_at": None,
        "error": "",
        **sync_fields(**overrides),
    }


# --- Push ---


def test_a_push_carries_the_whole_dependency_closure(store, tmp_path):
    """The registry refuses a child whose parent it has never seen, so the document
    is a closed set in foreign-key order."""
    site = Site(name="Reef")
    store.add_site(site)
    transect, video, pass_ = seed_pass(store, transect=make_transect(site_id=site.id))
    chapter = store.upsert_video(make_video("cd" * 16, file_name="GX020001.MP4", path="/data/b.MP4"))
    pass_.extra_video_ids = [chapter.id]
    store.update_pass(pass_)
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01")
    store.add_run(run)
    registry = FakeRegistry()

    report = SyncEngine(store, registry, out_root=tmp_path).push()

    sent = registry.pushes[0]
    assert list(sent) == ["sites", "transects", "videos", "passes", "pass_videos", "runs"]
    assert [row["id"] for row in sent["sites"]] == [str(site.id)]
    assert [row["id"] for row in sent["transects"]] == [str(transect.id)]
    assert {row["id"] for row in sent["videos"]} == {str(video.id), str(chapter.id)}
    assert [(row["video_id"], row["ordinal"]) for row in sent["pass_videos"]] == [
        (str(video.id), 0), (str(chapter.id), 1),
    ]
    assert report.applied == report.sent


def test_a_push_records_a_watermark_per_section(store, tmp_path):
    _transect, _video, pass_ = seed_pass(store)
    engine = SyncEngine(store, FakeRegistry(), out_root=tmp_path)

    report = engine.push()

    assert engine.watermark("passes") == store.get_pass(pass_.id).updated_at
    assert report.watermarks["passes"] == engine.watermark("passes")
    assert engine.watermark("runs") is None


def test_a_section_whose_rows_all_predate_its_watermark_sends_nothing(store, tmp_path):
    seed_pass(store)
    for section in SYNC_SECTIONS:
        store.set_sync_state(f"{WATERMARK_PREFIX}{section}", LATER)
    registry = FakeRegistry()

    report = SyncEngine(store, registry, out_root=tmp_path).push()

    assert registry.pushes == []
    assert report.sections == {}


def test_a_refused_push_leaves_every_watermark_alone(store, tmp_path):
    seed_pass(store)
    engine = SyncEngine(store, FakeRegistry(fail=ConflictError("unknown parent")), out_root=tmp_path)

    with pytest.raises(ConflictError):
        engine.push()

    assert [engine.watermark(section) for section in SYNC_SECTIONS] == [None] * len(SYNC_SECTIONS)


def test_a_half_answered_section_holds_its_watermark(store, tmp_path):
    seed_pass(store)
    engine = SyncEngine(store, FakeRegistry(unaccounted=("passes",)), out_root=tmp_path)

    report = engine.push()

    assert engine.watermark("passes") is None
    assert "passes" not in report.watermarks
    assert engine.watermark("videos") is not None


def test_a_row_the_registry_already_held_newer_is_reported_not_raised(store, tmp_path):
    transect, _video, _pass = seed_pass(store)
    notifications = NotificationCenter()
    registry = FakeRegistry(skipped={"transects": [transect.id]})
    engine = SyncEngine(store, registry, out_root=tmp_path, notifications=notifications)

    report = engine.push()

    assert report.skipped == [transect.id]
    note = notifications.active()[0]
    assert (note.fingerprint, note.severity, note.scope) == (CONFLICT_DISCARDED, WARNING, SURVEY)
    # Accounted for, so the section moves on: the pull that follows brings the
    # registry's copy down, and re-offering this row forever would say nothing new.
    assert engine.watermark("transects") is not None


def test_a_skip_that_was_not_a_local_edit_is_not_a_conflict(store, tmp_path):
    """Scenario: a survey already in step, pushed a second time.

    Expected behaviour: the ancestors dragged in by the closure and the derived
    chapter rows come back skipped, and none of that is reported. Warning about a
    survey where nothing is wrong would train the reader to ignore the warning.
    """
    site = Site(name="Reef")
    store.add_site(site)
    transect, video, pass_ = seed_pass(store, transect=make_transect(site_id=site.id))
    notifications = NotificationCenter()
    every = {"sites": [site.id], "transects": [transect.id], "videos": [video.id],
             "passes": [pass_.id], "pass_videos": [wire.pass_video_id(pass_.id, video.id)]}
    SyncEngine(store, FakeRegistry(), out_root=tmp_path, notifications=notifications).push()
    registry = FakeRegistry(skipped=every)

    report = SyncEngine(store, registry, out_root=tmp_path, notifications=notifications).push()

    assert registry.pushes[0].keys() == every.keys(), "the whole closure went again"
    assert report.applied == 0, "and the registry skipped all of it"
    assert report.skipped == []
    assert notifications.active() == []


def test_the_push_cursor_is_reported_and_never_adopted(store, tmp_path):
    """It is the registry's position after our own write, so pulling from it would
    skip every row another device wrote before it."""
    seed_pass(store)
    engine = SyncEngine(store, FakeRegistry(cursor=9999), out_root=tmp_path)

    assert engine.push().cursor == 9999
    assert engine.cursor() is None


def test_a_local_delete_travels_as_a_row(store, tmp_path):
    _transect, _video, pass_ = seed_pass(store)
    store.delete_pass(pass_.id)
    registry = FakeRegistry()

    SyncEngine(store, registry, out_root=tmp_path).push()

    sent = registry.pushes[0]
    assert [row["deleted_at"] for row in sent["passes"]] == [
        wire.to_wire_time(store.changed_since("passes")[0].deleted_at)
    ]
    assert sent["pass_videos"][0]["deleted_at"] is not None


def test_a_run_pushes_the_provenance_and_the_cover_from_its_directory(store, tmp_path):
    out_root = tmp_path / "out"
    classes_config = load_classes()
    _transect, _pass, run = seed_survey_run(
        store,
        out_root,
        "t1__p01",
        batch=make_batch(store),
        deepreefmap_version="1.2.3",
        segmentation_model="segformer-b2",
        mapping_backend="loger_star",
        enable_tsdf=False,
    )
    first = load_classes().classes[0]
    (out_root / "t1__p01" / "benthic_cover.json").write_text(json.dumps({
        "classes": {str(first.id): {"name": first.name, "count": 30.0, "fraction": 0.3}},
        "denominator": 100.0,
    }))
    registry = FakeRegistry()

    SyncEngine(
        store, registry, out_root=out_root, classes_config=classes_config
    ).push()

    sent = registry.pushes[0]
    assert list(sent) == [name for name in wire.WIRE_SECTIONS if name in sent]
    assert sent["runs"][0]["library_version"] == "1.2.3"
    assert sent["runs"][0]["segmentation_model"] == "segformer-b2"
    assert sent["runs"][0]["taxonomy_hash"]
    cover = sent["cover_rows"]
    assert {row["estimator"] for row in cover} == {wire.PER_PASS}
    assert {row["run_id"] for row in cover} == {str(run.id)}
    assert {row["metric_source"] for row in cover} == {"unprojected"}
    assert all(row["denominator"] == 100.0 for row in cover)


def test_cover_stays_behind_when_no_taxonomy_was_supplied(store, tmp_path):
    """Group names are taxonomy data, so a run cannot claim any without one."""
    out_root = tmp_path / "out"
    seed_survey_run(store, out_root, "t1__p01")
    registry = FakeRegistry()

    SyncEngine(store, registry, out_root=out_root).push()

    assert wire.COVER_ROWS not in registry.pushes[0]


# --- Pull ---


def test_a_pull_lands_rows_and_records_where_it_got_to(store, tmp_path):
    site_id = uuid.uuid4()
    registry = FakeRegistry(pages=[page(4821, {"sites": [site_row(site_id, "Japanese Garden")]})])
    engine = SyncEngine(store, registry, out_root=tmp_path)

    report = engine.pull()

    assert store.get_site(site_id).name == "Japanese Garden"
    assert (report.pages, report.cursor) == (1, 4821)
    assert store.sync_state(CURSOR_KEY) == "4821"
    assert engine.cursor() == 4821
    assert registry.pulls == [None]


def test_an_interrupted_pull_resumes_from_the_page_it_failed_on(store, tmp_path):
    first, second = uuid.uuid4(), uuid.uuid4()
    registry = FakeRegistry(pages=[
        page(100, {"sites": [site_row(first, "Reef")]}, has_more=True),
        ServerUnreachableError("offline"),
    ])
    engine = SyncEngine(store, registry, out_root=tmp_path)

    with pytest.raises(ServerUnreachableError):
        engine.pull()

    assert engine.cursor() == 100
    assert store.get_site(first) is not None

    resumed = FakeRegistry(pages=[page(200, {"sites": [site_row(second, "Wall")]})])
    SyncEngine(store, resumed, out_root=tmp_path).pull()

    assert resumed.pulls == [100]
    assert engine.cursor() == 200
    assert {s.name for s in store.list_sites()} == {"Reef", "Wall"}


def test_a_multi_page_pull_keeps_asking_while_there_is_more(store, tmp_path):
    ids = [uuid.uuid4() for _ in range(3)]
    registry = FakeRegistry(pages=[
        page(10, {"sites": [site_row(ids[0], "One")]}, has_more=True),
        page(20, {"sites": [site_row(ids[1], "Two")]}, has_more=True),
        page(30, {"sites": [site_row(ids[2], "Three")]}),
    ])
    engine = SyncEngine(store, registry, out_root=tmp_path)

    report = engine.pull()

    assert registry.pulls == [None, 10, 20]
    assert (report.pages, report.cursor) == (3, 30)
    assert report.sections["sites"].inserted == 3


def test_a_pull_stops_when_the_cursor_stops_moving(store, tmp_path):
    """A registry claiming more rows at a cursor it will not advance would loop
    forever."""
    registry = FakeRegistry(pages=[page(10, {}, has_more=True), page(10, {}, has_more=True)])

    report = SyncEngine(store, registry, out_root=tmp_path).pull()

    assert (report.pages, report.cursor) == (2, 10)


def test_a_tombstone_from_the_registry_removes_the_row_here(store, tmp_path):
    site = Site(name="Reef", updated_at=EARLIER)
    store.add_site(site)
    registry = FakeRegistry(pages=[page(10, {
        "sites": [site_row(site.id, "Reef", deleted_at=LATER)],
    })])

    SyncEngine(store, registry, out_root=tmp_path).pull()

    assert store.get_site(site.id) is None
    assert store.changed_since("sites")[0].deleted_at is not None


def test_last_write_wins_in_both_directions(store, tmp_path):
    site = Site(name="Reef", updated_at="2026-08-10T00:00:00+00:00")
    store.add_site(site)
    stale = FakeRegistry(pages=[page(10, {
        "sites": [site_row(site.id, "Stale", updated_at="2026-08-01T00:00:00Z")],
    })])

    kept = SyncEngine(store, stale, out_root=tmp_path).pull()

    assert store.get_site(site.id).name == "Reef"
    assert kept.kept == (site.id,)

    fresh = FakeRegistry(pages=[page(11, {"sites": [site_row(site.id, "Fresher")]})])
    landed = SyncEngine(store, fresh, out_root=tmp_path).pull()

    assert store.get_site(site.id).name == "Fresher"
    assert landed.kept == ()


def test_an_overwritten_local_edit_raises_a_notification(store, tmp_path):
    """Scenario: a row edited here since the last push is edited again elsewhere.

    Expected behaviour: the registry's copy wins on the stamp, and the operator is
    told rather than left to notice their typing has gone.
    """
    site = Site(name="Reef")
    store.add_site(site)
    notifications = NotificationCenter()
    store.set_sync_state(f"{WATERMARK_PREFIX}sites", EARLIER)
    site.name = "Reef Wall"
    store.update_site(site)
    registry = FakeRegistry(pages=[page(10, {"sites": [site_row(site.id, "Somebody else's name")]})])

    report = SyncEngine(
        store, registry, out_root=tmp_path, notifications=notifications
    ).pull()

    assert report.overwritten == (site.id,)
    assert store.get_site(site.id).name == "Somebody else's name"
    assert CONFLICT_OVERWRITTEN in {note.fingerprint for note in notifications.active()}


def test_an_untouched_row_the_registry_updates_is_not_a_conflict(store, tmp_path):
    site = Site(name="Reef")
    store.add_site(site)
    engine = SyncEngine(store, FakeRegistry(), out_root=tmp_path)
    engine.push()
    registry = FakeRegistry(pages=[page(10, {"sites": [site_row(site.id, "Renamed")]})])

    report = SyncEngine(store, registry, out_root=tmp_path).pull()

    assert report.overwritten == ()
    assert store.get_site(site.id).name == "Renamed"


# --- Pull: a pass and its chapters ---


def test_a_pass_arrives_with_its_chapters_and_lands_whole(store, tmp_path):
    transect_id, pass_id = uuid.uuid4(), uuid.uuid4()
    videos = [uuid.uuid4(), uuid.uuid4()]
    registry = FakeRegistry(pages=[page(10, {
        "transects": [transect_row(transect_id)],
        "videos": [video_row(videos[0]), video_row(videos[1], "GX020001.MP4")],
        "passes": [pass_row(pass_id, transect_id)],
        "pass_videos": [chapter_row(pass_id, videos[1], 1), chapter_row(pass_id, videos[0], 0)],
    })])

    report = SyncEngine(store, registry, out_root=tmp_path).pull()

    landed = store.get_pass(pass_id)
    assert landed.video_ids() == videos
    assert report.passes_without_videos == ()


def test_chapters_arriving_a_page_late_still_complete_the_pass(store, tmp_path):
    """Chapters sort after their pass, so the two split across a page boundary."""
    pass_id, video_id = uuid.uuid4(), uuid.uuid4()
    registry = FakeRegistry(pages=[
        page(10, {"videos": [video_row(video_id)], "passes": [pass_row(pass_id)]}, has_more=True),
        page(20, {"pass_videos": [chapter_row(pass_id, video_id, 0)]}),
    ])

    report = SyncEngine(store, registry, out_root=tmp_path).pull()

    assert store.get_pass(pass_id).video_ids() == [video_id]
    assert report.passes_without_videos == ()
    assert report.cursor == 20


def test_a_chapter_list_for_a_pass_already_here_is_adopted(store, tmp_path):
    _transect, video, pass_ = seed_pass(store)
    chapter = store.upsert_video(make_video("cd" * 16, file_name="GX020001.MP4", path="/data/b.MP4"))
    registry = FakeRegistry(pages=[page(10, {
        "pass_videos": [
            chapter_row(pass_.id, chapter.id, 0),
            chapter_row(pass_.id, video.id, 1),
        ],
    })])

    SyncEngine(store, registry, out_root=tmp_path).pull()

    assert store.get_pass(pass_.id).video_ids() == [chapter.id, video.id]


def test_a_pass_the_registry_holds_with_no_footage_is_reported(store, tmp_path):
    """A pass has to name a video here, so one with no chapters cannot be recorded.
    It is left out and said out loud, rather than stalling every later page behind
    it."""
    pass_id = uuid.uuid4()
    notifications = NotificationCenter()
    registry = FakeRegistry(pages=[page(10, {"passes": [pass_row(pass_id)]})])
    engine = SyncEngine(store, registry, out_root=tmp_path, notifications=notifications)

    report = engine.pull()

    assert report.passes_without_videos == (pass_id,)
    assert store.get_pass(pass_id) is None
    assert engine.cursor() == 10
    assert PASS_WITHOUT_VIDEOS in {note.fingerprint for note in notifications.active()}


def test_a_run_waits_for_the_pass_it_belongs_to(store, tmp_path):
    """Scenario: a tombstoned pass, whose chapters went to a tombstone with it.

    Expected behaviour: the pass cannot be built, so its runs wait rather than
    breaking the foreign key and taking the whole pull down.
    """
    pass_id, run_id = uuid.uuid4(), uuid.uuid4()
    notifications = NotificationCenter()
    registry = FakeRegistry(pages=[page(10, {
        "passes": [pass_row(pass_id, deleted_at=LATER)],
        "runs": [run_row(run_id, pass_id)],
    })])
    engine = SyncEngine(store, registry, out_root=tmp_path, notifications=notifications)

    report = engine.pull()

    assert report.runs_without_passes == (run_id,)
    assert store.get_run(run_id) is None
    assert engine.cursor() == 10
    assert PASS_WITHOUT_VIDEOS in {note.fingerprint for note in notifications.active()}


def test_a_held_run_lands_once_its_pass_does(store, tmp_path):
    transect, video, pass_ = seed_pass(store)
    later_pass, run_id = uuid.uuid4(), uuid.uuid4()
    registry = FakeRegistry(pages=[
        page(10, {"runs": [run_row(run_id, later_pass)]}, has_more=True),
        page(20, {
            "passes": [pass_row(later_pass, transect.id)],
            "pass_videos": [chapter_row(later_pass, video.id, 0)],
        }),
    ])

    report = SyncEngine(store, registry, out_root=tmp_path).pull()

    assert report.runs_without_passes == ()
    assert store.get_run(run_id).pass_id == later_pass


# --- Both halves ---


def test_sync_pulls_before_it_pushes(store, tmp_path):
    """A conflict is then discovered before the local edit is offered, and the copy
    being pushed is as fresh as it can be."""
    seed_pass(store)
    registry = FakeRegistry()

    report = SyncEngine(store, registry, out_root=tmp_path).sync()

    assert registry.calls == ["pull", "push"]
    assert report.push.sent > 0
    assert report.pull.pages == 1


def test_a_round_trip_leaves_both_sides_holding_the_same_survey(store, tmp_path):
    """Scenario: a laptop pulls a site and a transect, files a pass and a run
    against them, and pushes the lot back."""
    site_id, transect_id = uuid.uuid4(), uuid.uuid4()
    registry = FakeRegistry(pages=[page(10, {
        "sites": [site_row(site_id, "Japanese Garden")],
        "transects": [transect_row(transect_id, site_id=str(site_id))],
    })])
    engine = SyncEngine(store, registry, out_root=tmp_path)
    engine.pull()

    video = store.upsert_video(make_video())
    _transect, _video, pass_ = seed_pass(store, transect=store.get_transect(transect_id), video=video)
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01", status="succeeded")
    store.add_run(run)

    report = engine.push()

    sent = registry.pushes[0]
    assert [row["id"] for row in sent["sites"]] == [str(site_id)]
    assert [row["id"] for row in sent["transects"]] == [str(transect_id)]
    assert [row["id"] for row in sent["runs"]] == [str(run.id)]
    assert report.skipped == []
    assert engine.cursor() == 10
