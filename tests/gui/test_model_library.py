"""Model-pack export/import round-trips (deepreefmap_gui/models/library.py).

Pure-logic tests: no Qt, no torch. They build fake HF-cache repos with real relative
symlinks (the layout export must round-trip), monkeypatch manager._HF_CACHE_ROOT the
same way test_model_manager.py does, and assert is_model_cached() as the canonical
"the model loads again" check after import.
"""

from __future__ import annotations

import hashlib
import json
import os
import tarfile

import pytest

from deepreefmap_gui.models import library
from deepreefmap_gui.models import manager
from deepreefmap_gui.models.manager import ModelInfo, is_model_cached


def _write_cache_repo(cache_root, repo_id, files, *, use_symlinks=True):
    """Lay down a HF-cache repo: blobs/ + snapshots/<commit>/ + refs/main.

    Snapshot entries are relative symlinks into ../../blobs/<sha> (the real cache
    layout) unless use_symlinks=False, which writes real files (the Windows layout).
    """
    repo_dir = cache_root / f"models--{repo_id.replace('/', '--')}"
    commit = "0" * 40
    (repo_dir / "refs").mkdir(parents=True, exist_ok=True)
    (repo_dir / "refs" / "main").write_text(commit)
    blobs = repo_dir / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    snap = repo_dir / "snapshots" / commit
    snap.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        data = content if isinstance(content, bytes) else content.encode()
        blob = blobs / hashlib.sha256(data).hexdigest()
        blob.write_bytes(data)
        dest = snap / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if use_symlinks:
            os.symlink(os.path.relpath(blob, dest.parent), dest)
        else:
            dest.write_bytes(data)
    return repo_dir


def _seg_info(name="seg-fake", repo="fake/seg", **kw):
    return ModelInfo(
        name=name,
        kind="segmentation",
        hf_repos=[repo],
        gated=False,
        description="test",
        **kw,
    )


def test_export_import_round_trip(tmp_path, monkeypatch):
    src_cache = tmp_path / "src_hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", src_cache)
    _write_cache_repo(
        src_cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"weights"}
    )
    materialised = tmp_path / "ckpts" / "weight.pt"
    info = _seg_info(materialise_to={"model.safetensors": materialised})

    # A missing materialise target reads as not-cached; rebuilding it flips to cached.
    assert is_model_cached(info) is False
    manager.materialise_model(info)
    assert is_model_cached(info) is True

    pack = library.export_model_pack([info], tmp_path / "usb")
    assert library.is_model_pack(pack)

    # Import into a pristine cache with the source gone.
    dst_cache = tmp_path / "dst_hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", dst_cache)
    materialised.unlink()
    monkeypatch.setattr(manager, "all_known_models", lambda: [info])
    assert is_model_cached(info) is False

    result = library.import_model_pack(pack)
    assert is_model_cached(info) is True
    assert result.imported == ["seg-fake"]
    assert materialised.exists()


def test_export_dedups_shared_backbone(tmp_path, monkeypatch):
    cache = tmp_path / "hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", cache)
    _write_cache_repo(cache, "org/dpt-head", {"config.json": "{}", "model.safetensors": b"head"})
    _write_cache_repo(cache, "facebook/backbone", {"config.json": "{}", "model.safetensors": b"bbone"})
    head = ModelInfo(
        name="dpt", kind="segmentation",
        hf_repos=["org/dpt-head", "facebook/backbone"],
        gated=True, description="test",
    )
    backbone = ModelInfo(
        name="backbone", kind="backbone", hf_repos=["facebook/backbone"],
        gated=True, description="test",
    )
    assert is_model_cached(head) and is_model_cached(backbone)

    repos = library.enumerate_export_repos([head, backbone])
    assert set(repos) == {"org/dpt-head", "facebook/backbone"}

    manifest = library.build_pack_manifest([head, backbone], repos)
    backbone_entries = [r for r in manifest["repos"] if r["repo_id"] == "facebook/backbone"]
    assert len(backbone_entries) == 1

    pack = library.export_model_pack([head, backbone], tmp_path / "usb")
    with tarfile.open(pack / library.TAR_NAME) as tar:
        names = tar.getnames()
    assert names.count("cache/models--facebook--backbone") == 1


def test_manifest_schema_and_checksums(tmp_path, monkeypatch):
    cache = tmp_path / "hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", cache)
    _write_cache_repo(cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"weights"})
    info = _seg_info()

    pack = library.export_model_pack([info], tmp_path / "usb")
    manifest = library.read_pack_manifest(pack)

    assert manifest["schema_version"] == library.SCHEMA_VERSION
    assert manifest["total_size_bytes"] > 0
    repo = manifest["repos"][0]
    assert repo["repo_id"] == "fake/seg"
    assert repo["commit"] == "0" * 40
    assert repo["size_bytes"] > 0
    assert len(repo["sha256"]) == 64
    # The stored hash is reproducible from the on-disk snapshot.
    assert library._repo_content_sha256("fake/seg") == repo["sha256"]


def test_import_symlink_fallback_copies(tmp_path, monkeypatch):
    src_cache = tmp_path / "src_hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", src_cache)
    _write_cache_repo(src_cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"weights"})
    info = _seg_info()
    pack = library.export_model_pack([info], tmp_path / "usb")

    dst_cache = tmp_path / "dst_hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", dst_cache)
    monkeypatch.setattr(manager, "all_known_models", lambda: [info])

    def _no_symlink(*_a, **_k):
        raise OSError("symlinks unavailable (simulating Windows/exFAT)")

    monkeypatch.setattr(os, "symlink", _no_symlink)

    result = library.import_model_pack(pack)
    assert is_model_cached(info) is True
    assert result.imported == ["seg-fake"]

    snap = manager.snapshot_dir("fake/seg")
    weight = snap / "model.safetensors"
    assert weight.is_file() and not weight.is_symlink()


def test_import_checksum_mismatch_raises(tmp_path, monkeypatch):
    src_cache = tmp_path / "src_hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", src_cache)
    _write_cache_repo(src_cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"weights"})
    info = _seg_info()
    pack = library.export_model_pack([info], tmp_path / "usb")

    manifest = json.loads((pack / library.MANIFEST_NAME).read_text())
    manifest["repos"][0]["sha256"] = "de" * 32
    (pack / library.MANIFEST_NAME).write_text(json.dumps(manifest))

    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", tmp_path / "dst_hf")
    monkeypatch.setattr(manager, "all_known_models", lambda: [info])
    with pytest.raises(library.PackChecksumError):
        library.import_model_pack(pack)


def test_import_refuses_when_disk_is_low(tmp_path, monkeypatch):
    src_cache = tmp_path / "src_hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", src_cache)
    _write_cache_repo(src_cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"weights"})
    pack = library.export_model_pack([_seg_info()], tmp_path / "usb")

    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", tmp_path / "dst_hf")
    monkeypatch.setattr(
        library.shutil,
        "disk_usage",
        lambda _p: type("U", (), {"total": 1, "used": 1, "free": 1})(),
    )
    with pytest.raises(manager.InsufficientDiskSpace):
        library.import_model_pack(pack)


def test_export_rejects_partial_model(tmp_path, monkeypatch):
    cache = tmp_path / "hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", cache)
    # config.json naming a loader that never landed -> PARTIAL, never packable.
    _write_cache_repo(
        cache, "fake/dpt", {"config.json": json.dumps({"hub_inference_module": "loader.py"})}
    )
    info = ModelInfo(
        name="dpt-partial", kind="segmentation", hf_repos=["fake/dpt"],
        gated=False, description="test",
    )
    assert is_model_cached(info) is False
    with pytest.raises(library.PackError):
        library.export_model_pack([info], tmp_path / "usb")


def test_models_tab_has_library_buttons(window):
    assert window._export_models_btn is not None
    assert window._import_pack_btn is not None


def test_open_model_library_reveals_cache(window, tmp_path, monkeypatch):
    from PySide6.QtGui import QDesktopServices

    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", tmp_path / "hf")
    opened: dict = {}
    monkeypatch.setattr(
        QDesktopServices, "openUrl", lambda url: opened.setdefault("path", url.toLocalFile())
    )
    window._open_model_library()
    assert opened["path"].endswith("hf")
    assert (tmp_path / "hf").is_dir()


def test_export_with_no_cached_models_sets_status(window):
    window._downloading = set()
    window._last_model_states = []
    window._on_export_models()
    assert "No downloaded models" in window._status_label.text()
