from __future__ import annotations

import base64
import json

import pytest

from deepreefmap_gui.survey.store import SurveyStore
from deepreefmap_gui.sync.connect_code import CODE_PREFIX

DEFAULT_CODE_URL = "https://reef.example.org"


@pytest.fixture
def code_secret() -> str:
    return "ab" * 32


@pytest.fixture
def make_code(code_secret):
    """Build a pasted connect code, as the registry's web interface hands one out."""

    def build(url: str = DEFAULT_CODE_URL, secret: str | None = None) -> str:
        payload = json.dumps({"url": url, "code": code_secret if secret is None else secret})
        encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        return CODE_PREFIX + encoded

    return build


@pytest.fixture(autouse=True)
def _isolate_credentials(tmp_path, monkeypatch):
    """Keep the device file, the token file and the real keyring out of the suite."""
    monkeypatch.setenv("DEEPREEFMAP_SYNC_DEVICE", str(tmp_path / "sync_device.json"))
    monkeypatch.setenv("DEEPREEFMAP_SYNC_TOKEN", str(tmp_path / "sync_token.json"))
    monkeypatch.setattr("deepreefmap_gui.sync.credentials._keyring", lambda: None)


class FakeKeyring:
    """A keyring that answers, for the tests that need the store to exist."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)


@pytest.fixture
def fake_keyring(monkeypatch) -> FakeKeyring:
    answering = FakeKeyring()
    monkeypatch.setattr("deepreefmap_gui.sync.credentials._keyring", lambda: answering)
    return answering


@pytest.fixture
def store(tmp_path) -> SurveyStore:
    return SurveyStore(tmp_path / "survey.db")
