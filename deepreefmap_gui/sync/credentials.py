"""Where this installation keeps its device token, its server address and its identity.

The token goes to the OS keyring when there is one and to a 0600 file when there is
not, because a field laptop with no secret service daemon still has to sync.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import platformdirs

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "deepreefmap-gui"
KEYRING_USERNAME = "device_token"

# What the UI shows the operator, since where a token lives changes how they revoke it.
BACKEND_KEYRING = "keyring"
BACKEND_FILE = "file"


class CredentialsError(RuntimeError):
    """The stored credentials cannot be read or written safely."""


@dataclass(frozen=True)
class Credentials:
    """This device's registry credentials, as one thing the client can be built from."""

    base_url: str
    token: str = field(repr=False)
    device_id: str = ""


def device_path() -> Path:
    """Device identity and server address, overridable for tests."""
    override = os.environ.get("DEEPREEFMAP_SYNC_DEVICE")
    if override:
        return Path(override)
    return Path(platformdirs.user_data_dir("deepreefmap", appauthor=False)) / "sync_device.json"


def token_path() -> Path:
    """Token file used only when no keyring is available, overridable for tests."""
    override = os.environ.get("DEEPREEFMAP_SYNC_TOKEN")
    if override:
        return Path(override)
    return Path(platformdirs.user_data_dir("deepreefmap", appauthor=False)) / "sync_token.json"


def credential_backend() -> str:
    """Which store the token would be written to now: `keyring` or `file`."""
    return BACKEND_KEYRING if _keyring() is not None else BACKEND_FILE


def device_id() -> str:
    """This installation's device UUID, minted once and then permanent."""
    document = _read_device()
    existing = document.get("device_id")
    if isinstance(existing, str) and existing:
        return existing
    minted = str(uuid.uuid4())
    document["device_id"] = minted
    _write_device(document)
    return minted


def save(base_url: str, token: str) -> str:
    """Persist the credentials from an enrolment, returning the backend used."""
    document = _read_device()
    document["device_id"] = device_id()
    document["base_url"] = base_url
    _write_device(document)
    keyring = _keyring()
    if keyring is not None:
        try:
            keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, token)
            _forget_token_file()
            return BACKEND_KEYRING
        except Exception as exc:
            logger.warning("Keyring refused the device token, falling back to a file: %s", exc)
    _write_token_file(token)
    return BACKEND_FILE


def load() -> Credentials | None:
    """The stored credentials, or None when this installation is not connected."""
    document = _read_device()
    base_url = document.get("base_url")
    if not isinstance(base_url, str) or not base_url:
        return None
    token = _read_token()
    if token is None:
        return None
    identity = document.get("device_id")
    return Credentials(
        base_url=base_url,
        token=token,
        device_id=identity if isinstance(identity, str) else "",
    )


def connected() -> bool:
    return load() is not None


def forget() -> None:
    """Drop the local credentials. Revocation itself happens server-side, in the web interface."""
    keyring = _keyring()
    if keyring is not None:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
        except Exception as exc:
            logger.info("No keyring entry to remove: %s", exc)
    _forget_token_file()
    document = _read_device()
    document.pop("base_url", None)
    _write_device(document)


def _keyring() -> Any:
    """The `keyring` module when a real backend answers, else None.

    Imported lazily and optionally: the base install does not require it, and an
    unavailable secret service must degrade to the file path rather than fail.
    """
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        backend = keyring.get_keyring()
    except Exception as exc:
        logger.info("Keyring unavailable: %s", exc)
        return None
    # keyring.backends.fail is how the library says no service is running.
    if type(backend).__module__.startswith("keyring.backends.fail"):
        return None
    return keyring


def _read_device() -> dict[str, Any]:
    path = device_path()
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialsError(f"Cannot read {path}: {exc}") from exc
    return document if isinstance(document, dict) else {}


def _write_device(document: dict[str, Any]) -> None:
    path = device_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _read_token() -> str | None:
    keyring = _keyring()
    if keyring is not None:
        try:
            token = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
        except Exception as exc:
            logger.warning("Keyring lookup failed, trying the token file: %s", exc)
        else:
            if token:
                return str(token)
    path = token_path()
    if not path.exists():
        return None
    _require_private(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialsError(f"Cannot read {path}: {exc}") from exc
    token = document.get("token") if isinstance(document, dict) else None
    return str(token) if token else None


def _write_token_file(token: str) -> None:
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Created empty at 0600 first, so the token is never written to a readable file.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"token": token}, handle)
    os.chmod(path, 0o600)
    _require_private(path)


def _forget_token_file() -> None:
    token_path().unlink(missing_ok=True)


def _require_private(path: Path) -> None:
    """Refuse a token file anyone else on the machine can read."""
    if os.name != "posix":
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise CredentialsError(
            f"{path} is readable by other users (mode {mode:o}). Run `chmod 600 {path}` or reconnect this device."
        )
