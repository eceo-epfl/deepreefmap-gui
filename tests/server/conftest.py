from __future__ import annotations

import base64
import json

import pytest

from deepreefmap_gui.survey.store import SurveyStore
from deepreefmap_gui.sync.connect_code import CODE_PREFIX

SERVER_URL = "https://reef.example.org"
SECRET = "ab" * 32


@pytest.fixture(autouse=True)
def _isolate_credentials(tmp_path, monkeypatch):
    """Keep the device file and the token file out of the real user data dir."""
    monkeypatch.setenv("DEEPREEFMAP_SYNC_DEVICE", str(tmp_path / "sync_device.json"))
    monkeypatch.setenv("DEEPREEFMAP_SYNC_TOKEN", str(tmp_path / "sync_token.json"))
    monkeypatch.setattr("deepreefmap_gui.sync.credentials._keyring", lambda: None)


@pytest.fixture
def connect_code():
    def build(url: str = SERVER_URL, secret: str = SECRET) -> str:
        payload = json.dumps({"url": url, "code": secret})
        encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        return CODE_PREFIX + encoded

    return build


@pytest.fixture
def store(tmp_path) -> SurveyStore:
    return SurveyStore(tmp_path / "survey.db")
