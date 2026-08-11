"""The notification log: one row per episode, and a table that stays bounded."""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from deepreefmap_gui.survey.models.notification import (
    BLOCKER,
    CONDITION,
    EVENT,
    INFO,
    MACHINE,
    SURVEY,
    WARNING,
    Notification,
)
from deepreefmap_gui.survey.store import SurveyStore, latest_schema_version


def note(fingerprint: str = "videos.missing_clips", **overrides) -> Notification:
    kwargs = {
        "fingerprint": fingerprint,
        "kind": CONDITION,
        "severity": WARNING,
        "scope": SURVEY,
        "title": "10 clips cannot be found",
    }
    kwargs.update(overrides)
    return Notification(**kwargs)


@pytest.fixture
def store(tmp_path) -> SurveyStore:
    return SurveyStore(tmp_path / "survey.db")


def test_a_notification_survives_the_round_trip(store):
    written = note(body="Plug the drive back in.", section="videos", subject_count=10)
    store.add_notification(written)

    (read,) = store.list_notifications()
    assert read == written


def test_an_episode_is_updated_in_place(store):
    written = note(subject_count=10)
    store.add_notification(written)

    written.subject_count = 9
    written.title = "9 clips cannot be found"
    store.update_notification(written)

    assert [n.subject_count for n in store.list_notifications()] == [9]


def test_resolving_closes_the_episode(store):
    written = note()
    store.add_notification(written)
    store.resolve_notification(written.id, "2026-08-10T12:00:00+00:00")

    assert store.open_notifications() == []
    assert store.list_notifications()[0].resolved_at == "2026-08-10T12:00:00+00:00"


def test_one_fault_cannot_have_two_open_episodes(store):
    """Expected behaviour: a second window opening the same root must not be
    able to start a second episode for a fault already running."""
    store.add_notification(note())
    with pytest.raises(sqlite3.IntegrityError):
        store.add_notification(note())


def test_a_fault_that_recurs_opens_a_fresh_episode(store):
    first = note()
    store.add_notification(first)
    store.resolve_notification(first.id, "2026-08-10T12:00:00+00:00")
    store.add_notification(note())

    assert len(store.list_notifications()) == 2
    assert len(store.open_notifications()) == 1


def test_two_of_the_same_event_are_two_rows(store):
    """An event is not deduped: two finished sessions happened twice."""
    for _ in range(2):
        store.add_notification(note("batch.finished", kind=EVENT, severity=INFO))

    assert len(store.list_notifications()) == 2


def test_the_log_filters_by_severity_and_scope(store):
    store.add_notification(note("process.no_gpu", severity=BLOCKER, scope=MACHINE))
    store.add_notification(note())

    assert [n.fingerprint for n in store.list_notifications(scope=MACHINE)] == ["process.no_gpu"]
    assert [n.fingerprint for n in store.list_notifications(severity=WARNING)] == [
        "videos.missing_clips"
    ]


def test_pruning_keeps_the_newest_resolved_and_never_an_open_one(store):
    for i in range(5):
        closed = note(f"cleared.{i}", created_at=f"2026-08-0{i + 1}T00:00:00+00:00")
        store.add_notification(closed)
        store.resolve_notification(closed.id, "2026-08-10T00:00:00+00:00")
    store.add_notification(note(created_at="2020-01-01T00:00:00+00:00"))

    assert store.prune_notifications(keep=2) == 3

    kept = {n.fingerprint for n in store.list_notifications()}
    assert kept == {"cleared.4", "cleared.3", "videos.missing_clips"}


def test_an_export_carries_no_notifications(store, tmp_path):
    """The log says what this app told this reader, which is not survey content."""
    store.add_notification(note())
    path = tmp_path / "survey.json"
    store.export_json(path)

    assert "notification" not in path.read_text()


def test_an_older_database_migrates_with_its_rows_intact(tmp_path):
    from _factories import write_v0_2_0_database

    db = tmp_path / "survey.db"
    write_v0_2_0_database(db)
    store = SurveyStore(db)
    store.add_notification(note())

    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_schema_version()
    assert len(store.list_notifications()) == 1


def test_resolving_a_row_that_is_not_there_is_quiet(store):
    """The centre stamps from an in-memory copy, which a pruned log may outlive."""
    store.resolve_notification(uuid.uuid4(), "2026-08-10T12:00:00+00:00")
