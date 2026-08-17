"""The registry client against a loopback server.

Nothing here reaches a network: every response is served by a local HTTPServer, and
the address under test is 127.0.0.1. A test that asserted a failure would otherwise
pass offline for the wrong reason.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from deepreefmap_gui.sync import client as client_mod
from deepreefmap_gui.sync.client import (
    CONTRACT_VERSION,
    AccessDeniedError,
    ConflictError,
    ContractMismatchError,
    DeviceRevokedError,
    EnrolmentRejectedError,
    RejectedError,
    ServerFaultError,
    ServerUnreachableError,
    SyncClient,
    enrol,
)
from deepreefmap_gui.sync.connect_code import decode_connect_code

TOKEN = "drmd_" + "0" * 16 + "_" + "1" * 64


class FakeRegistry:
    """Programmed responses per path, plus the requests that arrived."""

    def __init__(self) -> None:
        self.base_url = ""
        self.responses: dict[str, tuple[int, dict]] = {}
        self.requests: list[tuple[str, str, dict | None, dict]] = []

    def reply(self, path: str, status: int, body: dict) -> None:
        self.responses[path] = (status, body)


@pytest.fixture
def registry():
    fake = FakeRegistry()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self, body):
            path = self.path.split("?")[0]
            fake.requests.append((self.command, self.path, body, dict(self.headers)))
            status, payload = fake.responses.get(path, (404, {"error": f"no route {path}"}))
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self._serve(None)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self._serve(json.loads(self.rfile.read(length) or b"null"))

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fake.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def make_client(registry, token=TOKEN, suffix=""):
    return SyncClient(registry.base_url + suffix, token=token, timeout=5.0)


def test_enrol_spends_the_code_and_returns_a_token(registry, make_code, code_secret) -> None:
    registry.reply("/api/enrol", 200, {"device_id": "d-1", "token": TOKEN, "enrolled_by": "sub-1"})
    code = decode_connect_code(make_code(registry.base_url))

    enrolment = make_client(registry, token=None).enrol(code, "Field laptop 2", platform="linux", gui_version="0.3.3")

    assert (enrolment.device_id, enrolment.token, enrolment.enrolled_by) == ("d-1", TOKEN, "sub-1")
    method, path, body, headers = registry.requests[-1]
    assert (method, path) == ("POST", "/api/enrol")
    assert body == {
        "code": code_secret,
        "device_name": "Field laptop 2",
        "platform": "linux",
        "gui_version": "0.3.3",
    }
    assert "Authorization" not in headers


def test_module_enrol_uses_the_address_inside_the_code(registry, make_code) -> None:
    """Expected behaviour: the app is never configured with a server address."""
    registry.reply("/api/enrol", 200, {"device_id": "d-1", "token": TOKEN, "enrolled_by": "sub-1"})

    enrolment = enrol(decode_connect_code(make_code(registry.base_url)), "Laptop")

    assert enrolment.token == TOKEN


def test_schema_status_pull_and_push_carry_the_bearer_token(registry) -> None:
    registry.reply("/api/sync/schema", 200, {"contract_version": CONTRACT_VERSION, "tables": []})
    registry.reply("/api/sync/status", 200, {"contract_version": CONTRACT_VERSION, "cursor": 12, "counts": {}})
    registry.reply(
        "/api/sync/pull",
        200,
        {"contract_version": CONTRACT_VERSION, "cursor": 40, "has_more": False, "sections": {"sites": []}},
    )
    registry.reply(
        "/api/sync/push",
        200,
        {"cursor": 41, "sections": {"sites": {"received": 1, "applied": 1, "skipped": []}}},
    )
    client = make_client(registry)

    assert client.schema()["tables"] == []
    assert client.status()["cursor"] == 12
    assert client.pull(since=10, limit=25)["cursor"] == 40
    assert client.push({"sites": [{"id": "s-1"}]})["cursor"] == 41

    assert all(headers["Authorization"] == f"Bearer {TOKEN}" for *_, headers in registry.requests)
    pull_path = next(path for method, path, _, _ in registry.requests if path.startswith("/api/sync/pull"))
    assert "since=10" in pull_path and "limit=25" in pull_path
    push_body = registry.requests[-1][2]
    assert push_body == {"contract_version": CONTRACT_VERSION, "sections": {"sites": [{"id": "s-1"}]}}


def test_pull_limit_is_clamped_to_the_server_maximum(registry) -> None:
    registry.reply("/api/sync/pull", 200, {"contract_version": CONTRACT_VERSION, "cursor": 1, "sections": {}})

    make_client(registry).pull(limit=client_mod.MAX_PULL_LIMIT * 2)

    assert f"limit={client_mod.MAX_PULL_LIMIT}" in registry.requests[-1][1]


def test_base_url_already_ending_in_api_is_not_doubled(registry) -> None:
    registry.reply("/api/sync/status", 200, {"contract_version": CONTRACT_VERSION, "cursor": 0, "counts": {}})

    make_client(registry, suffix="/api").status()

    assert registry.requests[-1][1] == "/api/sync/status"


@pytest.mark.parametrize(
    "status, expected, message",
    [
        (401, DeviceRevokedError, "revoked"),
        (403, AccessDeniedError, "refused"),
        (409, ConflictError, "rejected"),
        (400, RejectedError, "rejected"),
        (500, ServerFaultError, "own side"),
        (503, ServerFaultError, "own side"),
    ],
)
def test_http_status_maps_to_its_exception(registry, status, expected, message) -> None:
    registry.reply("/api/sync/status", status, {"error": "server said so"})

    with pytest.raises(expected, match=message) as raised:
        make_client(registry).status()
    assert "server said so" in str(raised.value)


def test_revoked_device_message_says_what_to_do(registry) -> None:
    registry.reply("/api/sync/status", 401, {"error": "Device revoked"})

    with pytest.raises(DeviceRevokedError, match="new connect code"):
        make_client(registry).status()


def test_rejected_connect_code_is_not_reported_as_a_revoked_device(registry, make_code) -> None:
    registry.reply("/api/enrol", 401, {"error": "Invalid or expired connect code"})

    with pytest.raises(EnrolmentRejectedError, match="fresh code"):
        make_client(registry, token=None).enrol(decode_connect_code(make_code(registry.base_url)), "Laptop")


def test_contract_mismatch_names_both_versions(registry) -> None:
    registry.reply("/api/sync/status", 200, {"contract_version": CONTRACT_VERSION + 1, "cursor": 0, "counts": {}})

    with pytest.raises(ContractMismatchError) as raised:
        make_client(registry).status()
    assert str(CONTRACT_VERSION) in str(raised.value)
    assert str(CONTRACT_VERSION + 1) in str(raised.value)


def test_offline_server_raises_unreachable() -> None:
    # Port 1 on loopback: nothing listens, so this is a connection refusal, not a lookup.
    with pytest.raises(ServerUnreachableError, match="Cannot reach"):
        SyncClient("http://127.0.0.1:1", token=TOKEN, timeout=2.0).status()


def test_missing_token_is_reported_before_any_request(registry) -> None:
    with pytest.raises(DeviceRevokedError, match="not connected"):
        make_client(registry, token=None).status()
    assert registry.requests == []


def test_token_never_appears_in_logs(registry, caplog) -> None:
    registry.reply("/api/sync/status", 200, {"contract_version": CONTRACT_VERSION, "cursor": 0, "counts": {}})

    with caplog.at_level(logging.DEBUG):
        make_client(registry).status()

    assert TOKEN not in caplog.text
