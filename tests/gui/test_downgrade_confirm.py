"""The warning shown before an older version is installed over this one.

Scenario: the Updates view has "Show older versions" ticked and a rollback is
picked. Two things have to happen before the binary is swapped -- a backup that
makes the trip reversible, and a warning that says what the older build will
actually find.

Expected behaviour: the warning names the outcome, and declining does nothing.
The earlier text promised the backup was "one version X can restore" without
checking, which was false whenever the survey had moved past what X reads.
"""

from __future__ import annotations

import deepreefmap_gui.update.version as version_mod
from deepreefmap_gui.survey import backup as bk
from deepreefmap_gui.survey.store import SURVEY_DB_NAME, SurveyStore, latest_schema_version


def _capture_confirm(monkeypatch, answer=True):
    """Stand in for the modal and keep the body it was asked to show."""
    shown = []

    def fake_confirm(_parent, _title, body):
        shown.append(body)
        return answer

    monkeypatch.setattr(version_mod, "confirm", fake_confirm)
    return shown


def _window_at(make_window, out_root, monkeypatch, current="2.0.0"):
    monkeypatch.setenv("DEEPREEFMAP_OUT_ROOT", str(out_root))
    window = make_window()
    window._out_root_input.setText(str(out_root))
    window._current_version_str = current
    return window


def test_a_survey_the_target_cannot_open_is_reported_as_such(
    make_window, out_root, monkeypatch
):
    out_root.mkdir(parents=True, exist_ok=True)
    SurveyStore(out_root / SURVEY_DB_NAME).close()
    window = _window_at(make_window, out_root, monkeypatch)
    shown = _capture_confirm(monkeypatch)

    assert window._confirm_downgrade("0.2.0") is True

    body = shown[0]
    assert "reads survey formats up to v3" in body
    assert f"this survey is v{latest_schema_version()}" in body
    assert "rebuild from your run folders" in body


def test_a_restorable_backup_is_named_with_its_date(make_window, out_root, monkeypatch):
    out_root.mkdir(parents=True, exist_ok=True)
    path = out_root / SURVEY_DB_NAME
    SurveyStore(path).close()
    bk.write_backup(path, 3)
    window = _window_at(make_window, out_root, monkeypatch)
    shown = _capture_confirm(monkeypatch)

    window._confirm_downgrade("0.2.0")

    assert "survey.db.v3.bak" in shown[0]
    assert "Work done since then is not in that copy" in shown[0]


def test_the_backup_is_taken_before_the_swap_not_after(make_window, out_root, monkeypatch):
    """A warning that only warns leaves the user to find out afterwards."""
    out_root.mkdir(parents=True, exist_ok=True)
    path = out_root / SURVEY_DB_NAME
    SurveyStore(path).close()
    window = _window_at(make_window, out_root, monkeypatch)
    _capture_confirm(monkeypatch)
    assert bk.list_backups(path) == []

    window._confirm_downgrade("0.2.0")

    assert [b.version for b in bk.list_backups(path)] == [latest_schema_version()]


def test_the_backup_just_taken_is_not_offered_as_the_way_back(
    make_window, out_root, monkeypatch
):
    """It is stamped with the format the target cannot read, so it is for the
    upgrade back, not for the older build."""
    out_root.mkdir(parents=True, exist_ok=True)
    path = out_root / SURVEY_DB_NAME
    SurveyStore(path).close()
    window = _window_at(make_window, out_root, monkeypatch)
    shown = _capture_confirm(monkeypatch)

    window._confirm_downgrade("0.2.0")

    assert f"survey.db.v{latest_schema_version()}.bak" not in shown[0]


def test_declining_reports_no(make_window, out_root, monkeypatch):
    out_root.mkdir(parents=True, exist_ok=True)
    SurveyStore(out_root / SURVEY_DB_NAME).close()
    window = _window_at(make_window, out_root, monkeypatch)
    _capture_confirm(monkeypatch, answer=False)

    assert window._confirm_downgrade("0.2.0") is False


def test_an_upgrade_is_not_a_downgrade_and_asks_nothing(make_window, out_root, monkeypatch):
    window = _window_at(make_window, out_root, monkeypatch, current="0.2.0")
    shown = _capture_confirm(monkeypatch)

    assert window._confirm_downgrade("2.0.0") is True
    assert shown == []
