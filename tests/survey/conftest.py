from __future__ import annotations

import pytest

from deepreefmap_gui.survey.store import SurveyStore


@pytest.fixture(autouse=True)
def _isolate_survey_preset(tmp_path, monkeypatch):
    """Keep preset reads off the developer's real data dir.

    load_survey_preset falls back to platformdirs.user_data_dir when the env
    override is unset, so an unguarded test asserts against whatever the app last
    saved on this machine -- and fails outright if that file has a stale key.
    Pointing at a path that does not exist forces the bundled branch.
    """
    monkeypatch.delenv("DEEPREEFMAP_SURVEY_PRESET", raising=False)
    monkeypatch.setattr(
        "deepreefmap_gui.survey.preset.survey_preset_path",
        lambda: tmp_path / "no-user-preset" / "survey_preset.yaml",
    )


@pytest.fixture
def store(tmp_path) -> SurveyStore:
    return SurveyStore(tmp_path / "survey.db")
