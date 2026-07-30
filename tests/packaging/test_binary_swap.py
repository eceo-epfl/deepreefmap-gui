"""In-app update mechanics: asset resolution, download/swap, env provisioning."""

from __future__ import annotations

import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from deepreefmap_gui.packaging import binary_swap


@pytest.mark.parametrize(
    "platform, expected",
    [
        ("linux", "deepreefmap-gui-linux-x64"),
        ("win32", "deepreefmap-gui-windows-x64.exe"),
        ("darwin", "deepreefmap-gui-macos-arm64"),
    ],
)
def test_resolve_asset_name(platform, expected, monkeypatch) -> None:
    from deepreefmap_gui.packaging import binary_swap

    # Pin cu126 so the base name is deterministic regardless of the host's torch wheel.
    monkeypatch.setattr(binary_swap, "_is_rocm_build", lambda: False)
    monkeypatch.setattr(binary_swap, "_cuda_variant_suffix", lambda: "")
    assert binary_swap.resolve_asset_name(platform) == expected


def test_resolve_asset_name_unsupported_raises() -> None:
    from deepreefmap_gui.packaging.binary_swap import BinarySwapError, resolve_asset_name

    with pytest.raises(BinarySwapError):
        resolve_asset_name("freebsd")


def test_resolve_asset_name_rocm_linux(monkeypatch) -> None:
    from deepreefmap_gui.packaging import binary_swap

    monkeypatch.setattr(binary_swap, "_is_rocm_build", lambda: True)
    assert binary_swap.resolve_asset_name("linux") == "deepreefmap-gui-linux-x64-rocm"


@pytest.mark.parametrize(
    "platform, expected",
    [
        ("linux", "deepreefmap-gui-linux-x64-cu130"),
        ("win32", "deepreefmap-gui-windows-x64-cu130.exe"),
    ],
)
def test_resolve_asset_name_cu130(platform, expected, monkeypatch) -> None:
    from deepreefmap_gui.packaging import binary_swap

    # A Blackwell (cu130) install must update to the cu130 asset, not the cu126 default.
    monkeypatch.setattr(binary_swap, "_is_rocm_build", lambda: False)
    monkeypatch.setattr(binary_swap, "_cuda_variant_suffix", lambda: "-cu130")
    assert binary_swap.resolve_asset_name(platform) == expected


@pytest.mark.parametrize("cuda, expected", [("13.0", "-cu130"), ("12.6", ""), (None, "")])
def test_cuda_variant_suffix_from_torch_version(cuda, expected, monkeypatch) -> None:
    import torch

    from deepreefmap_gui.packaging import binary_swap

    monkeypatch.delenv("PYAPP", raising=False)
    monkeypatch.setattr(torch.version, "cuda", cuda, raising=False)
    assert binary_swap._cuda_variant_suffix() == expected


def test_is_rocm_build_from_pyapp_binary_name(monkeypatch) -> None:
    from deepreefmap_gui.packaging import binary_swap

    monkeypatch.setenv("PYAPP", "/opt/pyapp/deepreefmap-gui-linux-x64-rocm")
    assert binary_swap._is_rocm_build() is True


def test_is_rocm_build_reads_torch_hip_version(monkeypatch) -> None:
    import torch

    from deepreefmap_gui.packaging import binary_swap

    monkeypatch.delenv("PYAPP", raising=False)
    monkeypatch.setattr(torch.version, "hip", None, raising=False)
    assert binary_swap._is_rocm_build() is False
    monkeypatch.setattr(torch.version, "hip", "6.3.42", raising=False)
    assert binary_swap._is_rocm_build() is True


def test_find_asset_url_returns_match() -> None:
    from deepreefmap_gui.packaging.binary_swap import BinarySwapError, find_asset_url

    rel = {
        "tag_name": "v1.0.0",
        "assets": [
            {"name": "deepreefmap-gui-linux-x64", "browser_download_url": "https://x/y"},
            {"name": "other", "browser_download_url": "https://nope"},
        ],
    }
    assert find_asset_url(rel, "deepreefmap-gui-linux-x64") == "https://x/y"
    with pytest.raises(BinarySwapError):
        find_asset_url({"tag_name": "v0.5.0", "assets": []}, "deepreefmap-gui-linux-x64")


def test_match_asset_url_returns_none_when_absent() -> None:
    from deepreefmap_gui.packaging.binary_swap import match_asset_url

    assert match_asset_url({"tag_name": "v1.0.0", "assets": []}, "deepreefmap-gui-linux-x64") is None


def test_find_asset_url_matches_version_labelled_assets() -> None:
    # Real releases label assets with the version (release.yml "Label binary");
    # variant suffixes must not cross-match.
    from deepreefmap_gui.packaging.binary_swap import find_asset_url

    rel = {
        "tag_name": "v1.2.0",
        "assets": [
            {"name": "deepreefmap-gui-linux-x64-cu130-1.2.0", "browser_download_url": "https://x/cu130"},
            {"name": "deepreefmap-gui-linux-x64-1.2.0", "browser_download_url": "https://x/base"},
            {"name": "deepreefmap-gui-windows-x64-1.2.0.exe", "browser_download_url": "https://x/win"},
        ],
    }
    assert find_asset_url(rel, "deepreefmap-gui-linux-x64") == "https://x/base"
    assert find_asset_url(rel, "deepreefmap-gui-linux-x64-cu130") == "https://x/cu130"
    assert find_asset_url(rel, "deepreefmap-gui-windows-x64.exe") == "https://x/win"


def test_download_to_streams_chunks_and_reports_progress(tmp_path) -> None:
    from deepreefmap_gui.packaging.binary_swap import download_to

    payload = b"x" * (200 * 1024)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()

    progress: list[tuple[int, int]] = []
    dest = tmp_path / "bin"
    try:
        download_to(
            f"http://127.0.0.1:{port}/bin", dest,
            lambda done, total: progress.append((done, total)),
            chunk_size=32 * 1024,
        )
    finally:
        server.server_close()

    assert dest.read_bytes() == payload
    assert progress and progress[-1] == (len(payload), len(payload))


def test_replace_binary_atomic_rename(tmp_path) -> None:
    from deepreefmap_gui.packaging.binary_swap import replace_binary

    target = tmp_path / "current"
    target.write_bytes(b"old")
    src = tmp_path / "new"
    src.write_bytes(b"new")

    replace_binary(target, src)

    assert target.read_bytes() == b"new"
    assert not src.exists()


def test_env_is_healthy_detects_missing_and_intact(tmp_path) -> None:
    from deepreefmap_gui.packaging.binary_swap import env_is_healthy

    # Missing torch/PySide6 → unhealthy.
    assert env_is_healthy(tmp_path) is False

    purelib = tmp_path / "site-packages"
    (purelib / "torch" / "lib").mkdir(parents=True)
    (purelib / "torch" / "lib" / "libtorch.so").write_bytes(b"\x00")
    (purelib / "PySide6").mkdir()
    assert env_is_healthy(purelib) is True

    # An emptied torch/lib (antivirus quarantine) → unhealthy.
    (purelib / "torch" / "lib" / "libtorch.so").unlink()
    assert env_is_healthy(purelib) is False


def test_prune_stale_envs_keeps_current_and_newest_fallback(tmp_path, monkeypatch) -> None:
    from deepreefmap_gui.packaging import binary_swap

    pyapp_root = tmp_path / "pyapp" / "deepreefmap" / "hash"
    oldest_env = pyapp_root / "1.0.0"
    old_env = pyapp_root / "1.1.0+gaaa"
    newest_stale_env = pyapp_root / "1.1.0+gbbb"
    current_env = pyapp_root / "1.1.0+gccc"
    for age, env in enumerate([newest_stale_env, old_env, oldest_env]):
        (env / "python").mkdir(parents=True)
        stamp = time.time() - age * 1000
        os.utime(env, (stamp, stamp))
    (current_env / "python").mkdir(parents=True)

    marker = tmp_path / "pending_env_prune.json"
    marker.write_text("{}")  # leftover from the retired marker mechanism
    monkeypatch.setattr("deepreefmap_gui.paths.env_prune_marker_path", lambda: marker)

    removed = binary_swap.prune_stale_envs(current_prefix=current_env / "python")

    assert sorted(removed) == sorted([oldest_env, old_env])
    assert not oldest_env.exists()
    assert not old_env.exists()
    assert newest_stale_env.exists()  # offline rollback target
    assert current_env.exists()
    assert not marker.exists()


def test_prune_stale_envs_refuses_paths_outside_pyapp(tmp_path, monkeypatch) -> None:
    from deepreefmap_gui.packaging import binary_swap

    monkeypatch.setattr(
        "deepreefmap_gui.paths.env_prune_marker_path",
        lambda: tmp_path / "absent.json",
    )
    current = tmp_path / "not-pyapp" / "1.1.0"
    victim = tmp_path / "not-pyapp" / "1.0.0"
    (current / "python").mkdir(parents=True)
    (victim / "python").mkdir(parents=True)

    removed = binary_swap.prune_stale_envs(current_prefix=current / "python")

    assert removed == []
    assert victim.exists()  # guard kept us from deleting outside a pyapp dir


def test_perform_update_downloads_and_swaps(tmp_path, monkeypatch) -> None:
    from deepreefmap_gui.packaging import binary_swap

    payload = b"NEW-BINARY-BYTES"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.handle_request, daemon=True).start()

    asset = binary_swap.resolve_asset_name()
    release = {
        "tag_name": "v1.1.0",
        "assets": [{"name": asset, "browser_download_url": f"http://127.0.0.1:{port}/bin"}],
    }
    target = tmp_path / "deepreefmap"
    target.write_bytes(b"OLD-BINARY")

    lines: list[str] = []
    try:
        binary_swap.perform_update(
            release, target, "1.1.0", line_cb=lines.append
        )
    finally:
        server.server_close()

    assert target.read_bytes() == payload
    assert not target.with_name(target.name + ".new").exists()
    assert any("Replacing binary" in line for line in lines)


def test_update_then_prune_end_to_end(tmp_path, monkeypatch) -> None:
    """The container e2e as a fast headless test: real download + swap, then
    the launch-time sweep drops the old env with the shared uv cache intact."""
    from deepreefmap_gui.packaging import binary_swap

    payload = b"NEW-BINARY-BYTES"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.handle_request, daemon=True).start()

    # PyApp layout: .../pyapp/deepreefmap/<hash>/<version>/python
    pyapp_root = tmp_path / "pyapp" / "deepreefmap" / "hash"
    oldest_env = pyapp_root / "1.0.0"
    old_env = pyapp_root / "1.1.0"
    new_env = pyapp_root / "1.2.0"
    for age, env in enumerate([old_env, oldest_env]):
        (env / "python").mkdir(parents=True)
        stamp = time.time() - age * 1000
        os.utime(env, (stamp, stamp))
    (new_env / "python").mkdir(parents=True)
    uv_cache = tmp_path / ".cache" / "uv"  # shared cache must survive the prune
    uv_cache.mkdir(parents=True)

    monkeypatch.setattr(
        "deepreefmap_gui.paths.env_prune_marker_path",
        lambda: tmp_path / "pending_env_prune.json",
    )
    # While the old version runs, sys.prefix points into its env.
    monkeypatch.setattr(binary_swap.sys, "prefix", str(old_env / "python"))

    asset = binary_swap.resolve_asset_name()
    release = {
        "tag_name": "v1.2.0",
        "assets": [{"name": asset, "browser_download_url": f"http://127.0.0.1:{port}/bin"}],
    }
    target = tmp_path / "deepreefmap"
    target.write_bytes(b"OLD-BINARY")

    try:
        binary_swap.perform_update(release, target, "1.2.0")
    finally:
        server.server_close()

    assert target.read_bytes() == payload  # swapped in place

    # The new version's first launch prunes old envs, keeping one fallback.
    removed = binary_swap.prune_stale_envs(current_prefix=str(new_env / "python"))

    assert removed == [oldest_env]
    assert not oldest_env.exists()
    assert old_env.exists()  # offline rollback target
    assert new_env.exists()
    assert uv_cache.exists()


def test_self_restore_invokes_pyapp_self_restore(monkeypatch) -> None:
    from deepreefmap_gui.packaging import binary_swap

    calls = []
    monkeypatch.setattr(
        binary_swap.subprocess, "run", lambda cmd, check: calls.append((cmd, check))
    )
    assert binary_swap.self_restore("/path/bin") is True
    assert calls == [(["/path/bin", "self", "restore"], True)]

    def boom(cmd, check):
        raise OSError("restore failed")

    monkeypatch.setattr(binary_swap.subprocess, "run", boom)
    assert binary_swap.self_restore("/path/bin") is False


def _fake_binary(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "deepreefmap-fake"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return script


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shell fake binary")
def test_provision_env_streams_cleaned_lines(tmp_path) -> None:
    binary = _fake_binary(
        tmp_path,
        r"""
[ "$1" = "self" ] && [ "$2" = "restore" ] || exit 2
printf '==> Installing deepreefmap\n'
printf 'downloading \033[32mtorch\033[0m\n'
printf 'progress 10%%\rprogress 100%%\n'
printf 'stderr line\n' >&2
""",
    )
    lines: list[str] = []
    assert binary_swap.provision_env(binary, line_cb=lines.append) is True
    assert "==> Installing deepreefmap" in lines
    assert "downloading torch" in lines
    assert "progress 100%" in lines
    assert "stderr line" in lines
    assert not any("\x1b" in line or "\r" in line for line in lines)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shell fake binary")
def test_provision_env_failure_returns_false(tmp_path) -> None:
    binary = _fake_binary(tmp_path, "printf 'boom\\n'\nexit 1\n")
    lines: list[str] = []
    assert binary_swap.provision_env(binary, line_cb=lines.append) is False
    assert any("retried on next launch" in line for line in lines)


def test_provision_env_missing_binary_returns_false(tmp_path) -> None:
    lines: list[str] = []
    assert binary_swap.provision_env(tmp_path / "missing", line_cb=lines.append) is False
    assert any("retried on next launch" in line for line in lines)


def test_perform_update_provisions_after_swap(tmp_path, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(binary_swap, "resolve_asset_name", lambda: "asset")
    monkeypatch.setattr(binary_swap, "find_asset_url", lambda release, name: "http://x")

    def fake_download(url, dest, progress_cb=None, **kwargs):
        dest.write_bytes(b"binary")
        calls.append("download")

    monkeypatch.setattr(binary_swap, "download_to", fake_download)
    monkeypatch.setattr(
        binary_swap, "replace_binary", lambda target, src: calls.append("replace")
    )
    monkeypatch.setattr(
        binary_swap,
        "provision_env",
        lambda path, line_cb=None: calls.append("provision") or True,
    )
    binary_swap.perform_update({"tag_name": "v9.9.9"}, tmp_path / "bin", "9.9.9")
    assert calls == ["download", "replace", "provision"]


# --- a download that does not finish ------------------------------------
#
# The updater replaces the program the user is running. "Verifying download"
# used to be a log line with nothing behind it, so a transfer cut short by a
# dropped wifi link was installed as the binary and the app stopped launching.


def _serve_once(handler_cls):
    """Start a one-shot HTTP server and return its port; caller closes it."""
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.handle_request, daemon=True).start()
    return server, server.server_address[1]


def _truncating_handler(payload, declared):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(declared))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    return Handler


def test_a_short_download_is_refused_and_leaves_nothing_behind(tmp_path) -> None:
    server, port = _serve_once(_truncating_handler(b"x" * 500, declared=5000))
    dest = tmp_path / "bin"
    try:
        with pytest.raises(binary_swap.BinarySwapError, match="500 bytes but the server said 5000"):
            binary_swap.download_to(f"http://127.0.0.1:{port}/bin", dest)
    finally:
        server.server_close()

    assert not dest.exists()
    assert list(tmp_path.iterdir()) == [], "a .part file was left for the next run to find"


def test_a_download_contradicting_the_release_metadata_is_refused(tmp_path) -> None:
    """Content-Length and the payload agree, so only the size GitHub recorded
    for the asset -- fetched from a different host -- catches this."""
    payload = b"y" * 800
    server, port = _serve_once(_truncating_handler(payload, declared=len(payload)))
    dest = tmp_path / "bin"
    try:
        with pytest.raises(binary_swap.BinarySwapError, match="release metadata said 4096"):
            binary_swap.download_to(f"http://127.0.0.1:{port}/bin", dest, expected_size=4096)
    finally:
        server.server_close()

    assert not dest.exists()


def test_a_complete_download_lands_at_the_destination(tmp_path) -> None:
    payload = b"z" * 2048
    server, port = _serve_once(_truncating_handler(payload, declared=len(payload)))
    dest = tmp_path / "bin"
    try:
        binary_swap.download_to(
            f"http://127.0.0.1:{port}/bin", dest, expected_size=len(payload)
        )
    finally:
        server.server_close()

    assert dest.read_bytes() == payload
    assert [p.name for p in tmp_path.iterdir()] == ["bin"]


def test_download_bounds_how_long_it_will_wait(monkeypatch, tmp_path) -> None:
    """An unbounded urlopen hangs the update dialog with no way out."""
    seen: dict = {}

    def fake_urlopen(_req, timeout=None):
        seen["timeout"] = timeout
        raise OSError("no route to host")

    monkeypatch.setattr(binary_swap.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(OSError, match="no route"):
        binary_swap.download_to("http://example/bin", tmp_path / "bin")

    assert seen["timeout"] and seen["timeout"] > 0


def test_perform_update_keeps_the_old_binary_when_the_download_is_short(tmp_path) -> None:
    server, port = _serve_once(_truncating_handler(b"PARTIAL", declared=9999))
    asset = binary_swap.resolve_asset_name()
    release = {
        "tag_name": "v1.1.0",
        "assets": [{"name": asset, "browser_download_url": f"http://127.0.0.1:{port}/bin", "size": 9999}],
    }
    target = tmp_path / "deepreefmap"
    target.write_bytes(b"OLD-BINARY")

    try:
        with pytest.raises(binary_swap.BinarySwapError):
            binary_swap.perform_update(release, target, "1.1.0")
    finally:
        server.server_close()

    assert target.read_bytes() == b"OLD-BINARY"
    assert [p.name for p in tmp_path.iterdir()] == ["deepreefmap"]


def test_perform_update_checks_the_size_the_release_records(tmp_path) -> None:
    payload = b"NEW-BINARY-BYTES"
    server, port = _serve_once(_truncating_handler(payload, declared=len(payload)))
    asset = binary_swap.resolve_asset_name()
    release = {
        "tag_name": "v1.1.0",
        "assets": [
            {
                "name": asset,
                "browser_download_url": f"http://127.0.0.1:{port}/bin",
                "size": len(payload),
            }
        ],
    }
    target = tmp_path / "deepreefmap"
    target.write_bytes(b"OLD-BINARY")
    lines: list[str] = []

    try:
        binary_swap.perform_update(release, target, "1.1.0", line_cb=lines.append)
    finally:
        server.server_close()

    assert target.read_bytes() == payload
    assert any("matching the size recorded" in line for line in lines)


def test_the_log_does_not_claim_a_check_the_release_cannot_support(tmp_path) -> None:
    """A release without an asset size still updates, but must not report that
    anything was matched."""
    payload = b"NEW-BINARY-BYTES"
    server, port = _serve_once(_truncating_handler(payload, declared=len(payload)))
    asset = binary_swap.resolve_asset_name()
    release = {
        "tag_name": "v1.1.0",
        "assets": [{"name": asset, "browser_download_url": f"http://127.0.0.1:{port}/bin"}],
    }
    target = tmp_path / "deepreefmap"
    target.write_bytes(b"OLD-BINARY")
    lines: list[str] = []

    try:
        binary_swap.perform_update(release, target, "1.1.0", line_cb=lines.append)
    finally:
        server.server_close()

    assert target.read_bytes() == payload
    assert any("no size to check it against" in line for line in lines)
    assert not any("matching the size recorded" in line for line in lines)


def test_declared_asset_size_reads_the_matched_asset() -> None:
    release = {
        "tag_name": "v1.2.0",
        "assets": [
            {"name": "deepreefmap-gui-linux-x64-1.2.0", "browser_download_url": "https://x/a", "size": 4321},
            {"name": "deepreefmap-gui-windows-x64-1.2.0.exe", "browser_download_url": "https://x/b", "size": 8765},
        ],
    }
    assert binary_swap.declared_asset_size(release, "deepreefmap-gui-linux-x64") == 4321
    assert binary_swap.declared_asset_size(release, "deepreefmap-gui-windows-x64.exe") == 8765
    assert binary_swap.declared_asset_size(release, "deepreefmap-gui-macos-arm64") is None


def test_declared_asset_size_is_none_when_the_release_omits_it() -> None:
    release = {"tag_name": "v1.2.0", "assets": [{"name": "a", "browser_download_url": "https://x/a"}]}
    assert binary_swap.declared_asset_size(release, "a") is None


# --- the Windows swap, which is not atomic ------------------------------
#
# Windows cannot overwrite a running .exe, so the old binary is renamed aside
# before the new one is renamed in. Between those two steps the installation has
# no binary, and the second rename is the one antivirus is most likely to block.


@pytest.fixture
def windows_swap(monkeypatch, tmp_path):
    monkeypatch.setattr(binary_swap.sys, "platform", "win32")
    target = tmp_path / "deepreefmap.exe"
    target.write_bytes(b"OLD-BINARY")
    src = tmp_path / "deepreefmap.exe.new"
    src.write_bytes(b"NEW-BINARY")
    return target, src


def _fail_the_move_into_place(monkeypatch, target, *, and_the_restore=False):
    """Let the move-aside succeed and the move-in fail, as a lock on the new file would."""
    real_rename = os.rename

    def rename(a, b):
        if Path(b) == target and (and_the_restore or not str(a).endswith(".old")):
            raise OSError("Access is denied")
        real_rename(a, b)

    monkeypatch.setattr(os, "rename", rename)


def test_a_blocked_swap_puts_the_old_binary_back(windows_swap, monkeypatch) -> None:
    target, src = windows_swap
    _fail_the_move_into_place(monkeypatch, target)

    with pytest.raises(binary_swap.BinarySwapError, match="Could not move the new binary"):
        binary_swap.replace_binary(target, src)

    assert target.read_bytes() == b"OLD-BINARY", "the installation was left with no binary"
    assert not target.with_suffix(".exe.old").exists()


def test_a_swap_that_cannot_be_undone_says_where_the_old_binary_is(
    windows_swap, monkeypatch
) -> None:
    target, src = windows_swap
    _fail_the_move_into_place(monkeypatch, target, and_the_restore=True)

    with pytest.raises(binary_swap.BinarySwapError, match=r"could not be restored.*\.old"):
        binary_swap.replace_binary(target, src)

    assert target.with_suffix(".exe.old").read_bytes() == b"OLD-BINARY"


def test_the_windows_swap_still_works_when_nothing_blocks_it(windows_swap) -> None:
    target, src = windows_swap

    binary_swap.replace_binary(target, src)

    assert target.read_bytes() == b"NEW-BINARY"
    assert target.with_suffix(".exe.old").read_bytes() == b"OLD-BINARY"
    assert not src.exists()
