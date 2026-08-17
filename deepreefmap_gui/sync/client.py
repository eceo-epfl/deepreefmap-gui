"""HTTP client for the metadata registry: enrolment, schema, status, pull and push.

stdlib urllib, as elsewhere in this package. Every call is one attempt: sync is
driven by the operator or by a timer, so a retry here would only stack requests.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from deepreefmap_gui.sync.connect_code import ConnectCode

logger = logging.getLogger(__name__)

# The contract this client speaks. Bump only with the row shapes it sends.
CONTRACT_VERSION = 1

DEFAULT_TIMEOUT = 15.0
# Enrolment mints a token behind argon2, so it is slower than a data call.
ENROL_TIMEOUT = 30.0
PULL_LIMIT = 1000
MAX_PULL_LIMIT = 5000

Sections = Mapping[str, Sequence[Mapping[str, Any]]]


class SyncError(RuntimeError):
    """Any failed exchange with the registry."""


class ServerUnreachableError(SyncError):
    """No answer at all: offline, wrong address, or DNS failure."""


class DeviceRevokedError(SyncError):
    """The server rejected this device's token."""


class EnrolmentRejectedError(SyncError):
    """The connect code was refused: unknown, expired, or already spent."""


class AccessDeniedError(SyncError):
    """Authenticated, but not allowed to do this."""


class ConflictError(SyncError):
    """A pushed row references a parent the server does not have, or collides on a name."""


class RejectedError(SyncError):
    """The server refused the document as malformed."""


class ContractMismatchError(SyncError):
    """The server speaks a different contract version than this app."""


class ServerFaultError(SyncError):
    """The server failed on its own side."""


@dataclass(frozen=True)
class Enrolment:
    """What a spent connect code returns. The token is shown once and then stored."""

    device_id: str
    token: str = field(repr=False)
    # Who onboarded this installation. Audit only: the token's rights are the
    # server's fixed device capability set, not theirs.
    enrolled_by: str = ""


class SyncClient:
    """One registry, one credential.

    `base_url` is whatever the connect code carried, with or without its `/api`
    suffix, since an operator copying an address from a browser gets either.
    """

    def __init__(self, base_url: str, token: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def enrol(
        self,
        code: ConnectCode,
        device_name: str,
        platform: str | None = None,
        gui_version: str | None = None,
    ) -> Enrolment:
        """Trade a connect code for this device's long-lived token."""
        body = {
            "code": code.secret,
            "device_name": device_name,
            "platform": platform,
            "gui_version": gui_version,
        }
        payload = self._request("POST", "/enrol", body=body, authorise=False, timeout=ENROL_TIMEOUT)
        return Enrolment(
            device_id=str(payload.get("device_id", "")),
            token=str(payload.get("token", "")),
            enrolled_by=str(payload.get("enrolled_by") or ""),
        )

    def schema(self) -> dict[str, Any]:
        """The replicated table set, contract-checked."""
        return self._request("GET", "/sync/schema")

    def status(self) -> dict[str, Any]:
        """Server cursor and live row counts, for a status line."""
        return self._request("GET", "/sync/status")

    def pull(self, since: int | None = None, limit: int = PULL_LIMIT) -> dict[str, Any]:
        """One page of changed rows. Keep calling while `has_more` is true."""
        params: dict[str, str] = {"limit": str(min(max(limit, 1), MAX_PULL_LIMIT))}
        if since is not None:
            params["since"] = str(since)
        return self._request("GET", f"/sync/pull?{urllib.parse.urlencode(params)}")

    def push(self, sections: Sections) -> dict[str, Any]:
        """Apply a closed document. `contract_version` is stamped here, not by callers."""
        document = {"contract_version": CONTRACT_VERSION, "sections": sections}
        return self._request("POST", "/sync/push", body=document)

    def _url(self, path: str) -> str:
        prefix = "" if self.base_url.endswith("/api") else "/api"
        return f"{self.base_url}{prefix}{path}"

    def _request(
        self,
        method: str,
        path: str,
        body: Any | None = None,
        authorise: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        url = self._url(path)
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if authorise:
            if not self._token:
                raise DeviceRevokedError("This installation is not connected to a registry yet.")
            headers["Authorization"] = f"Bearer {self._token}"
        # S310: the URL comes from the pasted connect code, already checked to be
        # http or https by connect_code.decode_connect_code.
        request = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=timeout or self._timeout) as response:  # noqa: S310
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc, authorise) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            unreachable = f"Cannot reach {self.base_url}: {exc}. Check the network and try again."
            raise ServerUnreachableError(unreachable) from exc
        except json.JSONDecodeError as exc:
            raise ServerFaultError(f"{url} did not answer with JSON.") from exc
        if not isinstance(payload, dict):
            raise ServerFaultError(f"{url} answered with {type(payload).__name__}, not an object.")
        _verify_contract(payload)
        logger.info("%s %s ok", method, path.split("?", maxsplit=1)[0])
        return payload

    def _http_error(self, exc: urllib.error.HTTPError, authorise: bool) -> SyncError:
        detail = _error_detail(exc)
        status = exc.code
        if status == 401 and not authorise:
            return EnrolmentRejectedError(
                f"The connect code was not accepted: {detail}. Ask whoever runs the registry for a fresh code."
            )
        if status == 401:
            return DeviceRevokedError(
                f"This device's access has been revoked: {detail}. Connect it again with a new "
                "connect code from the registry's web interface."
            )
        if status == 403:
            return AccessDeniedError(f"The registry refused this request: {detail}")
        if status == 409:
            return ConflictError(f"The registry rejected the document: {detail}")
        if status == 400:
            return RejectedError(f"The registry rejected the document: {detail}")
        if status >= 500:
            return ServerFaultError(f"The registry failed on its own side ({status}): {detail}")
        return SyncError(f"Unexpected response {status} from the registry: {detail}")


def enrol(
    code: ConnectCode,
    device_name: str,
    platform: str | None = None,
    gui_version: str | None = None,
) -> Enrolment:
    """Enrol against the address inside the code, so no address is configured here."""
    return SyncClient(code.base_url).enrol(code, device_name, platform=platform, gui_version=gui_version)


def _verify_contract(payload: Mapping[str, Any]) -> None:
    version = payload.get("contract_version")
    if version is None or version == CONTRACT_VERSION:
        return
    raise ContractMismatchError(
        f"This app speaks metadata contract {CONTRACT_VERSION} and the registry speaks {version}. "
        "Update whichever is older before syncing."
    )


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """The server's `error` string, or its status line where there is no JSON body."""
    try:
        document = json.load(exc)
    except Exception:
        return exc.reason if isinstance(exc.reason, str) else str(exc.code)
    if isinstance(document, dict) and isinstance(document.get("error"), str):
        return document["error"]
    return str(exc.code)
