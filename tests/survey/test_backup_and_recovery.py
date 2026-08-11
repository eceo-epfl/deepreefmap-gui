"""Backups taken before a migration, and the routes back when one is needed.

Scenario throughout: an update was installed, migrated the survey, and was then
rolled back -- leaving a database the older build refuses.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

import pytest
from _factories import write_v0_2_0_database

from deepreefmap_gui.survey import backup as bk
from deepreefmap_gui.survey.health import SurveyDbState, inspect_survey_db
from deepreefmap_gui.survey.recovery import (
    RecoveryKind,
    apply_recovery,
    count_rebuild,
    rebuild_losses,
    recovery_options,
)
from deepreefmap_gui.survey.store import (
    SurveyStore,
    latest_schema_version,
    oldest_supported_version,
)

# v0.2.0 is the only build whose database this one carries forward, so it is the
# only state a backup can be taken from.
V0_2_0_VERSION = 3


def _insert_transect_directly(path, name: str) -> None:
    """Write a row the way the older build would have, before any migration."""
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO transect (id, name, description, start_lat, start_lon, end_lat, "
        "end_lon, length_m, depth_m, created_at, updated_at) "
        "VALUES (?, ?, '', 1.0, 2.0, 1.1, 2.1, 50.0, 5.0, ?, ?)",
        (str(uuid.uuid4()), name, "2026-08-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()


def _stamp_ahead(path, ahead: int = 1) -> None:
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {latest_schema_version() + ahead}")
    conn.commit()
    conn.close()


def _write_run(out_root, name="run_001", transect_name="T1"):
    """A run folder with the manifest block rebuild_from_scan reads."""
    run_dir = out_root / name
    run_dir.mkdir()
    (run_dir / "run_manifest.json").write_text(json.dumps({
        "run_timestamp": "2026-08-01T10:00:00Z",
        "survey": {
            "run_id": str(uuid.uuid4()),
            "batch_id": None,
            "pass": {
                "id": str(uuid.uuid4()),
                "direction": "forward",
                "begin_s": 0.0,
                "end_s": 60.0,
            },
            "transect": {
                "id": str(uuid.uuid4()),
                "name": transect_name,
                "start_lat": 1.0,
                "start_lon": 2.0,
                "end_lat": 1.1,
                "end_lon": 2.1,
                "length_m": 50.0,
                "depth_m": 5.0,
            },
        },
        "videos": [{
            "id": str(uuid.uuid4()),
            "file_name": f"{name}.mp4",
            "path": str(out_root / f"{name}.mp4"),
        }],
    }))
    return run_dir


# --- Backups ---


def test_migrating_leaves_the_previous_shape_behind(tmp_path):
    path = tmp_path / "survey.db"
    write_v0_2_0_database(path)
    SurveyStore(path).close()

    backups = bk.list_backups(path)
    assert [b.version for b in backups] == [V0_2_0_VERSION]
    assert inspect_survey_db(backups[0].path).db_version == V0_2_0_VERSION
    assert inspect_survey_db(path).db_version == latest_schema_version()


def test_creating_a_database_takes_no_backup(tmp_path):
    """There is no previous build's work in a database that did not exist."""
    path = tmp_path / "survey.db"
    SurveyStore(path).close()
    assert bk.list_backups(path) == []


def test_opening_an_up_to_date_database_takes_no_backup(tmp_path):
    path = tmp_path / "survey.db"
    SurveyStore(path).close()
    SurveyStore(path).close()
    assert bk.list_backups(path) == []


def test_a_backup_that_cannot_be_written_does_not_block_opening(tmp_path, monkeypatch):
    """Expected behaviour: protecting the database is never a reason to refuse
    to open it."""
    path = tmp_path / "survey.db"
    write_v0_2_0_database(path)
    monkeypatch.setattr(bk.sqlite3, "connect", _raising_connect(bk.sqlite3.connect, path))

    store = SurveyStore(path)
    store.close()
    assert inspect_survey_db(path).db_version == latest_schema_version()


def _raising_connect(real, guarded):
    def connect(target, *args, **kwargs):
        if str(target).endswith(".partial"):
            raise sqlite3.OperationalError("unable to open database file")
        return real(target, *args, **kwargs)

    return connect


def test_best_backup_ignores_ones_this_build_cannot_read(tmp_path):
    path = tmp_path / "survey.db"
    SurveyStore(path).close()
    bk.write_backup(path, 2)
    bk.write_backup(path, 99)

    assert bk.best_backup(path, latest_schema_version()).version == 2
    assert bk.best_backup(path, 1) is None


def test_best_backup_honours_the_bottom_of_the_range_as_well(tmp_path):
    """A build reads a range of formats, not everything below the newest it
    knows, so a caller asking what it can open has to say both ends."""
    path = tmp_path / "survey.db"
    SurveyStore(path).close()
    bk.write_backup(path, 2)
    bk.write_backup(path, 4)

    assert bk.best_backup(path, latest_schema_version()).version == 4
    assert bk.best_backup(path, latest_schema_version(), min_version=3).version == 4
    assert bk.best_backup(path, 3, min_version=3) is None
    assert bk.best_backup(path, 2, min_version=3) is None


def test_setting_aside_moves_the_wal_sidecars_with_it(tmp_path):
    """A -wal left behind would be grafted onto whatever takes the database's
    place."""
    path = tmp_path / "survey.db"
    SurveyStore(path).close()
    (tmp_path / "survey.db-wal").write_bytes(b"")

    moved = bk.set_aside(path, 7)
    assert moved.name == "survey.db.schema-v7"
    assert not path.exists()
    assert (tmp_path / "survey.db.schema-v7-wal").exists()
    assert not (tmp_path / "survey.db-wal").exists()


def test_setting_aside_twice_keeps_both(tmp_path):
    """Nothing is ever deleted, so a second rollback cannot overwrite the first
    one's data."""
    path = tmp_path / "survey.db"
    for _ in range(2):
        SurveyStore(path).close()
        bk.set_aside(path, 7)
    kept = sorted(p.name for p in tmp_path.glob("survey.db.schema-v7*") if p.suffix != "-wal")
    assert len(kept) >= 2


# --- Recovery ---


def test_the_rebuild_count_is_what_the_rebuild_produces(tmp_path):
    """Expected behaviour: the number offered in the dialog is measured, not
    estimated, so the user chooses against a fact."""
    out_root = tmp_path
    _write_run(out_root, "run_001", "T1")
    _write_run(out_root, "run_002", "T2")

    counted = count_rebuild(out_root)
    assert (counted.runs, counted.transects, counted.passes) == (2, 2, 2)

    store = SurveyStore(out_root / "survey.db")
    report = store.rebuild_from_scan(out_root)
    store.close()
    assert (report.runs, report.transects, report.passes) == (2, 2, 2)


def test_unreadable_run_folders_are_counted_as_skipped(tmp_path):
    _write_run(tmp_path, "run_001")
    broken = tmp_path / "run_bad"
    broken.mkdir()
    (broken / "run_manifest.json").write_text("{ not json")

    counted = count_rebuild(tmp_path)
    assert counted.runs == 1
    assert counted.skipped == 1
    assert "could not be read" in counted.summary()


def test_restoring_a_backup_is_offered_first_and_recommended(tmp_path):
    path = tmp_path / "survey.db"
    write_v0_2_0_database(path)
    SurveyStore(path).close()
    _stamp_ahead(path)

    options = recovery_options(inspect_survey_db(path), tmp_path)
    assert options[0].kind is RecoveryKind.RESTORE_BACKUP
    assert options[0].recommended
    assert f"v{V0_2_0_VERSION}" in options[0].detail


def test_rebuilding_is_recommended_when_there_is_no_backup(tmp_path):
    path = tmp_path / "survey.db"
    SurveyStore(path).close()
    _write_run(tmp_path)
    _stamp_ahead(path)

    options = recovery_options(inspect_survey_db(path), tmp_path)
    assert options[0].kind is RecoveryKind.REBUILD
    assert options[0].recommended


def test_choosing_a_folder_is_always_available(tmp_path):
    path = tmp_path / "survey.db"
    SurveyStore(path).close()
    _stamp_ahead(path)
    kinds = [o.kind for o in recovery_options(inspect_survey_db(path), tmp_path)]
    assert kinds[-1] is RecoveryKind.CHOOSE_FOLDER


def test_starting_fresh_is_always_available(tmp_path):
    """A survey that cannot be recovered is not a reason to leave someone with a
    window that will not open one at all."""
    path = tmp_path / "survey.db"
    SurveyStore(path).close()
    _stamp_ahead(path)

    options = recovery_options(inspect_survey_db(path), tmp_path)
    fresh = next(o for o in options if o.kind is RecoveryKind.START_FRESH)
    assert options.index(fresh) == len(options) - 2, "offered above the way out of the folder"
    assert "survey.db.schema-v" in fresh.detail, "the name it is kept under is the point"
    assert not fresh.recommended


def test_starting_fresh_keeps_the_old_database_under_a_new_name(tmp_path):
    path = tmp_path / "survey.db"
    write_v0_2_0_database(path)
    _insert_transect_directly(path, "Reef edge")
    SurveyStore(path).close()
    _stamp_ahead(path)

    health = inspect_survey_db(path)
    option = next(
        o for o in recovery_options(health, tmp_path) if o.kind is RecoveryKind.START_FRESH
    )
    message = apply_recovery(option, health, tmp_path)

    displaced = tmp_path / f"survey.db.schema-v{health.db_version}"
    assert displaced.exists()
    assert displaced.name in message
    store = SurveyStore(path)
    try:
        assert store.list_transects() == [], "a new database, not the old one reopened"
    finally:
        store.close()
    # The row is still in the displaced file, which is what "nothing is deleted"
    # has to mean for it to be worth saying.
    conn = sqlite3.connect(displaced)
    assert conn.execute("SELECT name FROM transect").fetchall() == [("Reef edge",)]
    conn.close()


def test_a_backup_this_build_also_refuses_is_not_offered_as_a_route_back(tmp_path):
    """The closed loop: a database refused for being too old was answered with a
    copy of itself, and restoring it landed straight back on the same refusal."""
    path = tmp_path / "survey.db"
    SurveyStore(path).close()
    bk.write_backup(path, oldest_supported_version() - 1)
    _stamp_ahead(path)

    kinds = [o.kind for o in recovery_options(inspect_survey_db(path), tmp_path)]
    assert RecoveryKind.RESTORE_BACKUP not in kinds


def test_restoring_brings_the_rows_back_and_keeps_the_newer_database(tmp_path):
    """Scenario: an older build recorded a transect, an update migrated the
    database, and the update was then rolled back.

    Expected behaviour: the backup holds the database as the older build left
    it, so restoring is exact rather than reconstructed.
    """
    path = tmp_path / "survey.db"
    write_v0_2_0_database(path)
    _insert_transect_directly(path, "Reef edge")
    # Migrating is what takes the backup, so it captures the row above.
    SurveyStore(path).close()
    _stamp_ahead(path)

    health = inspect_survey_db(path)
    option = next(
        o for o in recovery_options(health, tmp_path) if o.kind is RecoveryKind.RESTORE_BACKUP
    )
    message = apply_recovery(option, health, tmp_path)

    store = SurveyStore(path)
    assert [t.name for t in store.list_transects()] == ["Reef edge"]
    store.close()
    assert (tmp_path / f"survey.db.schema-v{health.db_version}").exists()
    assert "schema-v" in message


def test_rebuilding_sets_the_newer_database_aside_rather_than_deleting_it(tmp_path):
    path = tmp_path / "survey.db"
    SurveyStore(path).close()
    _write_run(tmp_path, "run_001", "Reef edge")
    _stamp_ahead(path, 2)

    health = inspect_survey_db(path)
    option = next(o for o in recovery_options(health, tmp_path) if o.kind is RecoveryKind.REBUILD)
    apply_recovery(option, health, tmp_path)

    displaced = tmp_path / f"survey.db.schema-v{latest_schema_version() + 2}"
    assert displaced.exists()
    store = SurveyStore(path)
    assert [t.name for t in store.list_transects()] == ["Reef edge"]
    store.close()


def test_the_rebuilds_limits_are_stated_concretely(tmp_path):
    """Expected behaviour: each line names something specific that will not come
    back, because "some data may be lost" is not something a choice can be made
    against."""
    losses = rebuild_losses()
    assert len(losses) >= 4
    joined = " ".join(losses).lower()
    for subject in ("never processed", "cancelled", "notes", "coordinates"):
        assert subject in joined


def test_a_recovered_database_opens_normally(tmp_path):
    path = tmp_path / "survey.db"
    SurveyStore(path).close()
    _write_run(tmp_path)
    _stamp_ahead(path)

    health = inspect_survey_db(path)
    option = next(o for o in recovery_options(health, tmp_path) if o.kind is RecoveryKind.REBUILD)
    apply_recovery(option, health, tmp_path)

    assert inspect_survey_db(path).state is SurveyDbState.OK


def test_applying_the_folder_choice_here_is_a_mistake(tmp_path):
    """It is the window's job -- it opens a picker. Failing loudly beats quietly
    doing nothing to the database."""
    path = tmp_path / "survey.db"
    SurveyStore(path).close()
    _stamp_ahead(path)
    health = inspect_survey_db(path)
    option = next(
        o for o in recovery_options(health, tmp_path) if o.kind is RecoveryKind.CHOOSE_FOLDER
    )
    with pytest.raises(ValueError):
        apply_recovery(option, health, tmp_path)
