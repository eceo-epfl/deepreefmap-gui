"""Token storage, the file mode guard, and the device identity."""

from __future__ import annotations

import json
import logging
import os
import stat
import time

import pytest

from deepreefmap_gui.sync import credentials

TOKEN = "drmd_" + "0" * 16 + "_" + "1" * 64
URL = "https://reef.example.org"


def test_the_keyring_holds_the_token_where_one_answers(fake_keyring) -> None:
    assert credentials.save(URL, TOKEN) == credentials.BACKEND_KEYRING

    key = (credentials.KEYRING_SERVICE, credentials.KEYRING_USERNAME)
    assert fake_keyring.store[key] == TOKEN
    assert not credentials.token_path().exists()
    stored = credentials.load()
    assert stored is not None
    assert (stored.base_url, stored.token) == (URL, TOKEN)


def test_a_keyring_that_hangs_falls_back_to_the_file(fake_keyring, monkeypatch) -> None:
    """A locked collection answers with a dialog, not an error, so a sync must not
    wait behind it."""
    monkeypatch.setattr(credentials, "KEYRING_TIMEOUT_S", 0.05)
    monkeypatch.setattr(fake_keyring, "set_password", lambda *a: time.sleep(5))

    assert credentials.save(URL, TOKEN) == credentials.BACKEND_FILE
    stored = credentials.load()
    assert stored is not None and stored.token == TOKEN


def test_a_keyring_that_stops_answering_says_what_to_do(fake_keyring, monkeypatch) -> None:
    """The token is not recoverable from anywhere else, so the page must name the fix
    rather than report this laptop as never connected."""
    credentials.save(URL, TOKEN)
    monkeypatch.setattr(credentials, "_keyring", lambda: None)

    with pytest.raises(credentials.CredentialsError, match="connect the device again"):
        credentials.load()


def test_macos_is_left_alone_while_the_bundle_is_unsigned(monkeypatch) -> None:
    monkeypatch.setattr(credentials.sys, "platform", "darwin")

    assert credentials._keyring() is None


def test_the_token_file_is_written_0600() -> None:
    assert credentials.save(URL, TOKEN) == credentials.BACKEND_FILE

    path = credentials.token_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["token"] == TOKEN
    stored = credentials.load()
    assert stored is not None and stored.token == TOKEN


def test_a_readable_token_file_is_refused() -> None:
    credentials.save(URL, TOKEN)
    os.chmod(credentials.token_path(), 0o644)
    with pytest.raises(credentials.CredentialsError, match="readable by other users"):
        credentials.load()


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
