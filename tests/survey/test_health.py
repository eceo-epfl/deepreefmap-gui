"""Classifying a survey database without opening it for writing.

Scenario throughout: an app about to build its window needs to know whether the
database will open, before finding out the hard way.
"""

from __future__ import annotations

import sqlite3

import pytest

from deepreefmap_gui.survey.health import SurveyDbState, inspect_survey_db
from deepreefmap_gui.survey.store import _MIGRATIONS, SurveyStore


def test_a_folder_with_no_database_is_not_a_problem(tmp_path):
    health = inspect_survey_db(tmp_path / "survey.db")
    assert health.state is SurveyDbState.MISSING
    assert health.openable


def test_a_database_this_build_wrote_reads_as_ok(tmp_path):
    path = tmp_path / "survey.db"
    SurveyStore(path).close()
    health = inspect_survey_db(path)
    assert health.state is SurveyDbState.OK
    assert health.db_version == len(_MIGRATIONS)
    assert health.openable


@pytest.mark.parametrize("ahead", [1, 5])
def test_a_database_from_a_newer_build_is_refused(tmp_path, ahead):
    """Expected behaviour: the rollback case is named, with both versions in the
    message, and it is not openable."""
    path = tmp_path / "survey.db"
    SurveyStore(path).close()
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {len(_MIGRATIONS) + ahead}")
    conn.commit()
    conn.close()

    health = inspect_survey_db(path)
    assert health.state is SurveyDbState.TOO_NEW
    assert not health.openable
    assert health.db_version == len(_MIGRATIONS) + ahead
    assert str(len(_MIGRATIONS)) in health.detail


def test_a_file_that_is_not_a_database_reads_as_corrupt(tmp_path):
    path = tmp_path / "survey.db"
    path.write_bytes(b"holiday photos, not a database")
    assert inspect_survey_db(path).state is SurveyDbState.CORRUPT


def test_a_read_only_folder_reads_as_unwritable(tmp_path):
    """WAL needs to create sidecars beside the database, so the folder has to be
    writable even when the database itself already exists."""
    root = tmp_path / "readonly"
    root.mkdir()
    path = root / "survey.db"
    SurveyStore(path).close()
    root.chmod(0o500)
    try:
        health = inspect_survey_db(path)
        assert health.state is SurveyDbState.UNWRITABLE
        assert not health.openable
        assert str(root) in health.detail
    finally:
        root.chmod(0o700)


def test_inspecting_never_creates_or_migrates(tmp_path):
    """Expected behaviour: looking is free. A missing database stays missing, so
    merely asking cannot litter an output root with empty survey files."""
    path = tmp_path / "survey.db"
    inspect_survey_db(path)
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []
