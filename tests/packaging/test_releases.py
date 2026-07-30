"""GitHub release fetching, version filtering, and PyApp binary detection.

Covers deepreefmap_gui/packaging/releases.py: mock-env shortcuts, the GitHub
releases JSON parse (against a local HTTP server), upgrade/rollback selection,
API-URL overrides, and pyapp_binary_path().

Every network path is served by a local HTTPServer. Nothing here reaches the
real api.github.com: a live call returns None on any failure, so it would pass
offline for the wrong reason.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


@pytest.mark.parametrize(
    "releases, current, expected",
    [
        ([{"tag_name": "v1.0.0"}, {"tag_name": "v0.9.0"}], "1.0.1", []),
        ([{"tag_name": "v1.0.0"}, {"tag_name": "v0.9.0"}], "1.0.0", []),
        (
            [{"tag_name": "v1.5.0"}, {"tag_name": "v2.0.0"}, {"tag_name": "v1.0.0"}],
            "1.0.0",
            ["v2.0.0", "v1.5.0"],
        ),
    ],
)
def test_newer_releases_orders_and_filters(releases, current, expected) -> None:
    from deepreefmap_gui.packaging.releases import newer_releases

    newer = newer_releases(releases, current)
    assert [r["tag_name"] for r in newer] == expected


def test_newer_releases_unparseable_current_falls_back_to_inequality() -> None:
    from deepreefmap_gui.packaging.releases import newer_releases

    releases = [{"tag_name": "v1.0.0"}, {"tag_name": "v1.0.1"}]
    newer = newer_releases(releases, "dev")
    assert {r["tag_name"] for r in newer} == {"v1.0.0", "v1.0.1"}


@pytest.mark.parametrize(
    "include_older, expected",
    [
        (False, ["2.0.0"]),  # upgrades only
        (True, ["2.0.0", "1.0.0"]),  # all but current, newest first → rollback offered
    ],
)
def test_selectable_releases(include_older, expected) -> None:
    from deepreefmap_gui.packaging.releases import selectable_releases

    releases = [
        {"tag_name": "v1.0.0"},
        {"tag_name": "v2.0.0"},
        {"tag_name": "v1.5.0"},  # current
    ]
    got = selectable_releases(releases, "1.5.0", include_older)
    assert [r["tag_name"].lstrip("v") for r in got] == expected


def test_gh_api_url_override(monkeypatch) -> None:
    from deepreefmap_gui.packaging.releases import gh_releases_url

    monkeypatch.setenv("DEEPREEFMAP_GH_API_URL", "http://127.0.0.1:9999/releases")
    monkeypatch.setenv("DEEPREEFMAP_GH_REPO", "owner/repo")  # override wins
    assert gh_releases_url() == "http://127.0.0.1:9999/releases"

    monkeypatch.delenv("DEEPREEFMAP_GH_API_URL")
    assert gh_releases_url() == "https://api.github.com/repos/owner/repo/releases"


def test_fetch_releases_mock_synthesises_assets(monkeypatch) -> None:
    from deepreefmap_gui.packaging.releases import fetch_releases

    monkeypatch.setenv("DEEPREEFMAP_MOCK_VERSIONS", "2.0.0,1.0.1")
    releases = fetch_releases()
    assert releases is not None
    assert [r["tag_name"] for r in releases] == ["v2.0.0", "v1.0.1"]
    names = {a["name"] for a in releases[0]["assets"]}
    assert "deepreefmap-gui-linux-x64" in names
    assert "deepreefmap-gui-windows-x64.exe" in names


def _fetch_releases_via_local_server(releases):
    """Serve one fake GitHub /releases response and run _fetch_releases against it."""
    from deepreefmap_gui.packaging.releases import fetch_releases

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(releases).encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.handle_request, daemon=True).start()

    import deepreefmap_gui.packaging.releases as mod
    orig = mod.gh_releases_url
    mod.gh_releases_url = lambda: f"http://127.0.0.1:{port}/releases"
    try:
        return fetch_releases(timeout=5.0)
    finally:
        mod.gh_releases_url = orig
        server.server_close()


def test_fetch_releases_keeps_assets_from_github_response(monkeypatch) -> None:
    from deepreefmap_gui.packaging import binary_swap

    monkeypatch.delenv("DEEPREEFMAP_MOCK_VERSIONS", raising=False)
    monkeypatch.setattr(binary_swap, "resolve_asset_name", lambda platform=None: "deepreefmap-gui-linux-x64")
    releases = [
        {
            "tag_name": "v2.0.0",
            "draft": False,
            "assets": [
                {
                    "name": "deepreefmap-gui-linux-x64",
                    "browser_download_url": "https://example.invalid/v2.0.0/deepreefmap-gui-linux-x64",
                },
            ],
        },
        {"tag_name": "v1.0.0", "draft": True, "assets": []},
    ]
    result = _fetch_releases_via_local_server(releases)
    assert result is not None and len(result) == 1
    assert result[0]["tag_name"] == "v2.0.0"
    assert result[0]["assets"][0]["browser_download_url"].endswith("/deepreefmap-gui-linux-x64")


def test_fetch_releases_drops_releases_without_platform_binary(monkeypatch) -> None:
    # The published v1.0.0 pre-dates binary distribution (no assets); it must
    # never be offered, nor a release carrying only another platform's binary.
    from deepreefmap_gui.packaging import binary_swap

    monkeypatch.delenv("DEEPREEFMAP_MOCK_VERSIONS", raising=False)
    monkeypatch.setattr(binary_swap, "resolve_asset_name", lambda platform=None: "deepreefmap-gui-linux-x64")
    releases = [
        {
            "tag_name": "v2.0.0",
            "draft": False,
            "assets": [
                {
                    "name": "deepreefmap-gui-linux-x64-2.0.0",
                    "browser_download_url": "https://example.invalid/v2.0.0/deepreefmap-gui-linux-x64-2.0.0",
                },
            ],
        },
        {
            "tag_name": "v1.5.0",
            "draft": False,
            "assets": [
                {
                    "name": "deepreefmap-gui-windows-x64-1.5.0.exe",
                    "browser_download_url": "https://example.invalid/v1.5.0/deepreefmap-gui-windows-x64-1.5.0.exe",
                },
            ],
        },
        {"tag_name": "v1.0.0", "draft": False, "assets": []},
    ]
    result = _fetch_releases_via_local_server(releases)
    assert result is not None
    assert [r["tag_name"] for r in result] == ["v2.0.0"]


def test_fetch_releases_all_filtered_returns_empty_not_none(monkeypatch) -> None:
    # Empty list means "reached GitHub, nothing installable" and renders as
    # "No releases found."; None is reserved for fetch failures.
    from deepreefmap_gui.packaging import binary_swap

    monkeypatch.delenv("DEEPREEFMAP_MOCK_VERSIONS", raising=False)
    monkeypatch.setattr(binary_swap, "resolve_asset_name", lambda platform=None: "deepreefmap-gui-linux-x64")
    result = _fetch_releases_via_local_server(
        [{"tag_name": "v1.0.0", "draft": False, "assets": []}]
    )
    assert result == []


def test_pyapp_mock_path(monkeypatch) -> None:
    from deepreefmap_gui.packaging.releases import pyapp_binary_path

    monkeypatch.setenv("DEEPREEFMAP_MOCK_PYAPP", "1")
    monkeypatch.delenv("PYAPP", raising=False)
    assert pyapp_binary_path() == "/tmp/mock-pyapp"


def test_pyapp_no_env(monkeypatch) -> None:
    from deepreefmap_gui.packaging.releases import pyapp_binary_path

    monkeypatch.delenv("DEEPREEFMAP_MOCK_PYAPP", raising=False)
    monkeypatch.delenv("PYAPP", raising=False)
    assert pyapp_binary_path() is None
