"""Token storage on both backends, the file mode guard, and the device identity."""

from __future__ import annotations

import json
import logging
import os
import stat

import pytest

from deepreefmap_gui.sync import credentials

TOKEN = "drmd_" + "0" * 16 + "_" + "1" * 64
URL = "https://reef.example.org"


def test_keyring_path_holds_the_token(fake_keyring) -> None:
    assert credentials.credential_backend() == credentials.BACKEND_KEYRING
    assert credentials.save(URL, TOKEN) == credentials.BACKEND_KEYRING

    assert fake_keyring.store[(credentials.KEYRING_SERVICE, credentials.KEYRING_USERNAME)] == TOKEN
    assert not credentials.token_path().exists()
    stored = credentials.load()
    assert stored is not None
    assert (stored.base_url, stored.token) == (URL, TOKEN)


def test_file_fallback_writes_0600() -> None:
    assert credentials.credential_backend() == credentials.BACKEND_FILE
    assert credentials.save(URL, TOKEN) == credentials.BACKEND_FILE

    path = credentials.token_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["token"] == TOKEN
    stored = credentials.load()
    assert stored is not None and stored.token == TOKEN


def test_file_fallback_refuses_a_readable_token_file() -> None:
    credentials.save(URL, TOKEN)
    os.chmod(credentials.token_path(), 0o644)
    with pytest.raises(credentials.CredentialsError, match="readable by other users"):
        credentials.load()


def test_keyring_failure_falls_back_to_the_file(fake_keyring, monkeypatch) -> None:
    """Expected behaviour: a secret service that errors mid-call must not block enrolment."""

    def refuse(*args, **kwargs):
        raise RuntimeError("no session bus")

    monkeypatch.setattr(fake_keyring, "set_password", refuse)
    assert credentials.save(URL, TOKEN) == credentials.BACKEND_FILE
    stored = credentials.load()
    assert stored is not None and stored.token == TOKEN


def test_load_is_none_before_enrolment() -> None:
    assert credentials.load() is None
    assert credentials.connected() is False


def test_device_id_is_minted_once_and_persisted() -> None:
    first = credentials.device_id()
    assert first == credentials.device_id()
    assert json.loads(credentials.device_path().read_text())["device_id"] == first


def test_device_id_survives_forget(fake_keyring) -> None:
    """Forgetting disconnects; it does not make this laptop a different device."""
    credentials.save(URL, TOKEN)
    identity = credentials.device_id()

    credentials.forget()

    assert credentials.load() is None
    assert credentials.device_id() == identity
    assert not credentials.token_path().exists()
    assert fake_keyring.store == {}


def test_token_stays_out_of_repr_and_logs(caplog) -> None:
    credentials.save(URL, TOKEN)
    stored = credentials.load()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("test").info("loaded %r", stored)
    assert TOKEN not in repr(stored)
    assert TOKEN not in caplog.text
