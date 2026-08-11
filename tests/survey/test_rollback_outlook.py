"""What the app tells someone before it installs an older version over this one.

Scenario: the Updates view offers a rollback. Whether the older build will still
open the survey in the output folder is knowable beforehand -- the target is
older than the build asking, so its format range is already recorded here.

Expected behaviour: the warning names the outcome. It used to promise that the
backup it had just taken was "one version X can restore" without checking, which
was false whenever the survey had moved past what X reads.
"""

from __future__ import annotations

from _factories import write_legacy_database

from deepreefmap_gui.survey import backup as bk
from deepreefmap_gui.survey.rollback import RollbackEffect, rollback_outlook
from deepreefmap_gui.survey.schema_history import (
    current_schema,
    newest_release_reading,
    reads_up_to,
    released_schema,
)
from deepreefmap_gui.survey.store import (
    SurveyStore,
    latest_schema_version,
    oldest_supported_version,
)


def _survey(tmp_path):
    path = tmp_path / "survey.db"
    SurveyStore(path).close()
    return path


def test_a_target_that_reads_the_survey_says_so_plainly(tmp_path):
    """No scare text where there is nothing to be scared of."""
    path = _survey(tmp_path)
    # A release reading everything this build writes: itself.
    outlook = rollback_outlook(path, "0.2.0")
    assert outlook.effect is RollbackEffect.CANNOT_OPEN  # v9 survey, 0.2.0 reads v3

    old = tmp_path / "old.db"
    write_legacy_database(old, 3)
    outlook = rollback_outlook(old, "0.2.0")
    assert outlook.effect is RollbackEffect.OPENS
    assert not outlook.loses_work
    assert "reads this survey's format (v3)" in outlook.summary()


def test_an_unreadable_survey_with_a_backup_old_enough_offers_it(tmp_path):
    path = _survey(tmp_path)
    bk.write_backup(path, 3)

    outlook = rollback_outlook(path, "0.2.0")

    assert outlook.effect is RollbackEffect.RESTORES_BACKUP
    assert outlook.loses_work
    assert outlook.restorable.version == 3
    summary = outlook.summary()
    assert "survey.db.v3.bak" in summary
    assert "Work done since then is not in that copy" in summary


def test_a_backup_the_target_also_refuses_is_not_offered(tmp_path):
    """The bug in miniature: a copy in the format the target cannot read is no
    route back, and offering it sends the user round the same failure again."""
    path = _survey(tmp_path)
    bk.write_backup(path, latest_schema_version())

    outlook = rollback_outlook(path, "0.2.0")

    assert outlook.effect is RollbackEffect.CANNOT_OPEN
    assert outlook.restorable is None


def test_an_unreadable_survey_with_no_backup_says_what_will_happen(tmp_path):
    path = _survey(tmp_path)

    summary = rollback_outlook(path, "0.2.0").summary()

    assert "reads survey formats up to v3" in summary
    assert f"this survey is v{latest_schema_version()}" in summary
    assert "rebuild from your run folders" in summary
    assert "upgrading again opens the survey as it is now" in summary


def test_an_unrecorded_target_is_reported_as_unverified_not_as_safe(tmp_path):
    """A version missing from the table costs a warning, never a silent break."""
    path = _survey(tmp_path)

    outlook = rollback_outlook(path, "0.0.9")

    assert outlook.effect is RollbackEffect.UNVERIFIED
    assert "not recorded in this build" in outlook.summary()


def test_no_survey_means_nothing_to_warn_about(tmp_path):
    outlook = rollback_outlook(tmp_path / "survey.db", "0.2.0")

    assert outlook.effect is RollbackEffect.NO_SURVEY
    assert not outlook.loses_work


def test_a_survey_too_damaged_to_read_is_not_reported_as_a_loss(tmp_path):
    """Rolling back cannot make it worse, so it is not what the dialog is for."""
    path = tmp_path / "survey.db"
    path.write_bytes(b"not a database at all")

    assert rollback_outlook(path, "0.2.0").effect is RollbackEffect.NO_SURVEY


def test_the_version_leading_tag_is_accepted(tmp_path):
    assert reads_up_to("v0.2.0") == reads_up_to("0.2.0") == 3


def test_the_table_covers_every_released_version():
    """A release absent from the table cannot be reasoned about, so adding the
    line belongs in the commit that cuts the tag."""
    assert reads_up_to("0.1.0") == 1
    assert reads_up_to("0.2.0") == 3
    assert reads_up_to("9.9.9") is None


def test_the_current_build_reports_its_own_range():
    current = current_schema()
    assert current.reads_from == oldest_supported_version()
    assert current.reads_up_to == latest_schema_version()


def test_the_newest_release_reading_a_format_is_the_one_to_open_it_with():
    assert newest_release_reading(1).version == "0.2.0"
    assert newest_release_reading(3).version == "0.2.0"
    assert newest_release_reading(latest_schema_version()) is None


def test_released_entries_do_not_claim_a_range_they_never_had():
    """Guards the table against being widened by hand to silence a warning."""
    for version, expected_top in (("0.1.0", 1), ("0.2.0", 3)):
        entry = released_schema(version)
        assert entry.reads_from == 0
        assert entry.reads_up_to == expected_top
        assert entry.reads(expected_top)
        assert not entry.reads(expected_top + 1)
