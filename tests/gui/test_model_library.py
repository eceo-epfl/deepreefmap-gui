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
import shutil
import tarfile

import pytest

from deepreefmap_gui.models import library, manager
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
    assert sorted(p.name for p in pack.glob("models--*")) == [
        "models--facebook--backbone",
        "models--org--dpt-head",
    ]


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
    # The stored digest is reproducible from the on-disk snapshot.
    assert library.repo_content_digest("fake/seg") == repo["sha256"]

    # Each folder repeats its own record, so a folder copied out on its own still
    # describes and verifies itself.
    payload = json.loads((pack / "models--fake--seg" / library.REPO_MANIFEST_NAME).read_text())
    assert payload["sha256"] == repo["sha256"]
    assert {f["path"] for f in payload["files"]} == {"config.json", "model.safetensors"}
    assert (pack / "models--fake--seg" / "refs" / "main").read_text() == "0" * 40


def test_pack_files_are_real_files_a_stick_can_hold(tmp_path, monkeypatch):
    """No symlinks anywhere in a pack: FAT32/exFAT and Windows cannot carry them,
    and the snapshot layout is what makes the folder a drop-in cache entry."""
    cache = tmp_path / "hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", cache)
    _write_cache_repo(cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"weights"})

    pack = library.export_model_pack([_seg_info()], tmp_path / "usb")

    assert not [p for p in pack.rglob("*") if p.is_symlink()]
    snap = pack / "models--fake--seg" / "snapshots" / ("0" * 40)
    assert (snap / "model.safetensors").read_bytes() == b"weights"


def test_import_takes_only_the_models_asked_for(tmp_path, monkeypatch):
    cache = tmp_path / "hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", cache)
    _write_cache_repo(cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"w1"})
    _write_cache_repo(cache, "fake/other", {"config.json": "{}", "model.safetensors": b"w2"})
    seg = _seg_info()
    other = _seg_info(name="other-fake", repo="fake/other")
    pack = library.export_model_pack([seg, other], tmp_path / "usb")

    offered = {m.name: m for m in library.list_pack_models(pack)}
    assert set(offered) == {"seg-fake", "other-fake"}
    assert all(m.available for m in offered.values())

    dst = tmp_path / "dst_hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", dst)
    monkeypatch.setattr(manager, "all_known_models", lambda: [seg, other])

    result = library.import_model_pack(pack, model_names=["seg-fake"])
    assert result.imported == ["seg-fake"]
    assert is_model_cached(seg) is True
    assert is_model_cached(other) is False
    assert not (dst / "models--fake--other").exists()


def test_reimport_skips_a_repo_already_present_and_identical(tmp_path, monkeypatch):
    src_cache = tmp_path / "src_hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", src_cache)
    _write_cache_repo(src_cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"weights"})
    info = _seg_info()
    pack = library.export_model_pack([info], tmp_path / "usb")

    dst = tmp_path / "dst_hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", dst)
    monkeypatch.setattr(manager, "all_known_models", lambda: [info])
    library.import_model_pack(pack)
    assert is_model_cached(info) is True

    # A second import must not copy a single byte: the local content already folds
    # to the pack's digest.
    def _fail(*_a, **_k):
        raise AssertionError("copied a repo that was already present and identical")

    monkeypatch.setattr(library, "_import_repo_folder", _fail)
    result = library.import_model_pack(pack)
    assert result.already_present == ["seg-fake"]
    assert result.imported == []


def test_reimport_copies_when_local_content_differs(tmp_path, monkeypatch):
    """A same-named repo with different bytes is not skipped."""
    src_cache = tmp_path / "src_hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", src_cache)
    _write_cache_repo(src_cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"new"})
    info = _seg_info()
    pack = library.export_model_pack([info], tmp_path / "usb")

    dst = tmp_path / "dst_hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", dst)
    _write_cache_repo(dst, "fake/seg", {"config.json": "{}", "model.safetensors": b"old"})
    monkeypatch.setattr(manager, "all_known_models", lambda: [info])

    library.import_model_pack(pack)
    weight = manager.snapshot_dir("fake/seg") / "model.safetensors"
    assert weight.read_bytes() == b"new"


def test_a_repo_folder_on_its_own_is_still_a_pack(tmp_path, monkeypatch):
    """The folder someone copies off a stick by itself, with no manifest.json."""
    cache = tmp_path / "hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", cache)
    _write_cache_repo(cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"weights"})
    info = _seg_info()
    pack = library.export_model_pack([info], tmp_path / "usb")

    loose = tmp_path / "handoff"
    loose.mkdir()
    shutil.copytree(pack / "models--fake--seg", loose / "models--fake--seg", symlinks=True)
    assert library.is_model_pack(loose)

    dst = tmp_path / "dst_hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", dst)
    monkeypatch.setattr(manager, "all_known_models", lambda: [info])

    library.import_model_pack(loose)
    assert is_model_cached(info) is True


def test_a_pack_missing_a_shared_backbone_offers_the_model_as_unavailable(
    tmp_path, monkeypatch
):
    cache = tmp_path / "hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", cache)
    _write_cache_repo(cache, "org/dpt-head", {"config.json": "{}", "model.safetensors": b"head"})
    _write_cache_repo(cache, "facebook/backbone", {"config.json": "{}", "model.safetensors": b"bb"})
    head = ModelInfo(
        name="dpt", kind="segmentation",
        hf_repos=["org/dpt-head", "facebook/backbone"],
        gated=False, description="test",
    )
    pack = library.export_model_pack([head], tmp_path / "usb")
    shutil.rmtree(pack / "models--facebook--backbone")

    offered = library.list_pack_models(pack)
    assert [(m.name, m.available) for m in offered] == [("dpt", False)]


def test_export_verifies_what_landed_on_the_destination(tmp_path, monkeypatch):
    """A failing stick takes the bytes and stores something else. The digest was
    computed from the source as it streamed, so only reading the copy back finds it."""
    cache = tmp_path / "hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", cache)
    _write_cache_repo(cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"weights"})

    real_copy = library._copy_hashing

    def _bad_write(src, dst, on_bytes):
        digest, size = real_copy(src, dst, on_bytes)
        if dst.name == "model.safetensors":
            dst.write_bytes(b"corrupt")
        return digest, size

    monkeypatch.setattr(library, "_copy_hashing", _bad_write)
    with pytest.raises(library.PackChecksumError):
        library.export_model_pack([_seg_info()], tmp_path / "usb")
    assert not (tmp_path / "usb").exists()  # the folder we created is removed whole


def test_export_reports_both_phases(tmp_path, monkeypatch):
    cache = tmp_path / "hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", cache)
    _write_cache_repo(cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"weights"})

    phases: list[str] = []
    library.export_model_pack(
        [_seg_info()], tmp_path / "usb", progress_cb=lambda ph, _l, _c, _t: phases.append(ph)
    )
    assert "export" in phases and "verify" in phases


def test_export_failure_leaves_no_partial_pack(tmp_path, monkeypatch):
    cache = tmp_path / "hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", cache)
    _write_cache_repo(cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"weights"})

    def _fail_mid_write(_phase, _label, _cur, _tot):
        raise OSError(27, "File too large")

    with pytest.raises(OSError):
        library.export_model_pack([_seg_info()], tmp_path / "usb", progress_cb=_fail_mid_write)
    assert not (tmp_path / "usb").exists()


def test_export_refuses_a_rotted_local_cache(tmp_path, monkeypatch):
    """A weight's cache blob is named by its own sha256. If the bytes no longer match
    the name, the local cache has rotted and the corruption must not enter a pack."""
    cache = tmp_path / "hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", cache)
    _write_cache_repo(cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"weights"})

    # Overwrite the blob's bytes while keeping its (now-wrong) sha256 filename.
    snap = manager.snapshot_dir("fake/seg")
    blob = (snap / "model.safetensors").resolve()
    blob.write_bytes(b"rotted")

    with pytest.raises(library.PackChecksumError, match="local model cache is corrupt"):
        library.export_model_pack([_seg_info()], tmp_path / "usb")
    assert not (tmp_path / "usb").exists()


def test_export_writes_directly_into_the_chosen_folder(tmp_path, monkeypatch):
    """The folder the user picks is the pack: no DeepReefMap-model-pack nesting."""
    cache = tmp_path / "hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", cache)
    _write_cache_repo(cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"weights"})

    dest = tmp_path / "chosen"
    pack = library.export_model_pack([_seg_info()], dest)
    assert pack == dest
    assert (dest / "models--fake--seg").is_dir()
    assert not (dest / library.PACK_DIR_NAME).exists()


def test_export_nests_when_the_folder_holds_other_files(tmp_path, monkeypatch):
    """A folder already in use gets a named subfolder, so nothing is scattered."""
    cache = tmp_path / "hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", cache)
    _write_cache_repo(cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"weights"})

    dest = tmp_path / "documents"
    dest.mkdir()
    (dest / "holiday.jpg").write_bytes(b"unrelated")

    pack = library.export_model_pack([_seg_info()], dest)
    assert pack == dest / library.PACK_DIR_NAME
    assert (pack / "models--fake--seg").is_dir()
    assert (dest / "holiday.jpg").exists()


def test_reexport_reuses_a_repo_already_on_the_destination(tmp_path, monkeypatch):
    cache = tmp_path / "hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", cache)
    _write_cache_repo(cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"weights"})

    dest = tmp_path / "usb"
    library.export_model_pack([_seg_info()], dest)

    # A second export to the same folder must not rewrite an intact repo.
    def _fail(*_a, **_k):
        raise AssertionError("rewrote a repo already present and intact")

    monkeypatch.setattr(library, "_write_repo_folder", _fail)
    pack = library.export_model_pack([_seg_info()], dest)
    assert library.is_model_pack(pack)
    assert json.loads((pack / library.MANIFEST_NAME).read_text())["repos"][0]["repo_id"] == "fake/seg"


def test_reexport_rewrites_a_repo_whose_destination_copy_is_damaged(tmp_path, monkeypatch):
    cache = tmp_path / "hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", cache)
    _write_cache_repo(cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"weights"})

    dest = tmp_path / "usb"
    library.export_model_pack([_seg_info()], dest)
    # Corrupt the copy on the drive; a re-export must not reuse it.
    weight = dest / "models--fake--seg" / "snapshots" / ("0" * 40) / "model.safetensors"
    weight.write_bytes(b"rotted!")

    library.export_model_pack([_seg_info()], dest)
    assert weight.read_bytes() == b"weights"


def test_export_refuses_when_the_destination_is_too_small(tmp_path, monkeypatch):
    cache = tmp_path / "hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", cache)
    _write_cache_repo(cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"weights"})
    monkeypatch.setattr(
        library.shutil,
        "disk_usage",
        lambda _p: type("U", (), {"total": 1, "used": 1, "free": 1})(),
    )
    with pytest.raises(manager.InsufficientDiskSpace):
        library.export_model_pack([_seg_info()], tmp_path / "usb")


def test_export_counts_a_repo_it_is_replacing_as_free(tmp_path, monkeypatch):
    """Re-exporting to the same stick must not be refused for room the old copy holds."""
    cache = tmp_path / "hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", cache)
    _write_cache_repo(cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"w" * 4096})
    monkeypatch.setattr(library, "_EXPORT_MARGIN_BYTES", 0)
    library.export_model_pack([_seg_info()], tmp_path / "usb")

    repos = library.enumerate_export_repos([_seg_info()])
    needed = sum(r.size_bytes for r in repos.values())
    # Half a copy of room: too little for a fresh dest, plenty once the existing
    # copy in this pack is credited back, so re-export goes through.
    free = needed // 2
    monkeypatch.setattr(
        library.shutil,
        "disk_usage",
        lambda _p: type("U", (), {"total": 0, "used": 0, "free": free})(),
    )
    with pytest.raises(manager.InsufficientDiskSpace):
        library._check_export_space(tmp_path / "empty", repos)
    library.export_model_pack([_seg_info()], tmp_path / "usb")  # replaces in place


def test_cancel_stops_export_and_removes_the_partial_pack(tmp_path, monkeypatch):
    cache = tmp_path / "hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", cache)
    _write_cache_repo(cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"weights"})

    def _cancel(_phase, _label, _cur, _tot):
        raise library.PackCancelled("stop")

    with pytest.raises(library.PackCancelled):
        library.export_model_pack([_seg_info()], tmp_path / "usb", progress_cb=_cancel)
    assert not (tmp_path / "usb").exists()


def test_cancel_stops_import_and_removes_a_fresh_repo(tmp_path, monkeypatch):
    src_cache = tmp_path / "src_hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", src_cache)
    _write_cache_repo(src_cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"weights"})
    info = _seg_info()
    pack = library.export_model_pack([info], tmp_path / "usb")

    dst = tmp_path / "dst_hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", dst)
    monkeypatch.setattr(manager, "all_known_models", lambda: [info])

    def _cancel(phase, _label, _cur, _tot):
        if phase == "import":
            raise library.PackCancelled("stop")

    with pytest.raises(library.PackCancelled):
        library.import_model_pack(pack, progress_cb=_cancel)
    assert not (dst / "models--fake--seg").exists()


def test_import_restores_the_blob_and_symlink_layout(tmp_path, monkeypatch):
    """The pack flattens the cache to real files; import puts the shape back, so an
    imported cache is indistinguishable from a downloaded one."""
    src_cache = tmp_path / "src_hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", src_cache)
    _write_cache_repo(src_cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"weights"})
    blob_name = os.path.basename(
        os.readlink(manager.snapshot_dir("fake/seg") / "model.safetensors")
    )
    info = _seg_info()
    pack = library.export_model_pack([info], tmp_path / "usb")

    dst = tmp_path / "dst_hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", dst)
    monkeypatch.setattr(manager, "all_known_models", lambda: [info])
    library.import_model_pack(pack)

    weight = manager.snapshot_dir("fake/seg") / "model.safetensors"
    assert weight.is_symlink()
    assert weight.resolve() == (dst / "models--fake--seg" / "blobs" / blob_name).resolve()
    assert weight.read_bytes() == b"weights"


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

    repo_manifest = pack / "models--fake--seg" / library.REPO_MANIFEST_NAME
    payload = json.loads(repo_manifest.read_text())
    payload["sha256"] = "de" * 32
    repo_manifest.write_text(json.dumps(payload))

    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", tmp_path / "dst_hf")
    monkeypatch.setattr(manager, "all_known_models", lambda: [info])
    with pytest.raises(library.PackChecksumError):
        library.import_model_pack(pack)


def test_import_refuses_a_pack_whose_manifests_disagree(tmp_path, monkeypatch):
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


def test_pack_progress_survives_a_multi_gigabyte_pack(window):
    """Byte counts past 2^31 must reach the bar. As plain ints they overflowed Qt's
    32-bit signal argument, which printed OverflowError per emitted megabyte."""
    fifteen_gb = 15 * 1024**3
    window._start_pack_progress("Importing models", passes=1)
    window._sig_pack_progress.emit("import", "segformer-b2", fifteen_gb // 2, fifteen_gb)
    dlg = window._pack_progress_dialog
    assert dlg._bar.value() == 50
    assert dlg._detail.text() == "7.5 GB of 15.0 GB"
    assert dlg._status.text() == "Installing segformer-b2"
    dlg.close()
    window._pack_progress_dialog = None


def test_progress_status_names_the_current_model_without_reshaping(window):
    """A long model name is elided, not wrapped, so the dialog height never changes."""
    window._start_pack_progress("Exporting models", passes=2)
    dlg = window._pack_progress_dialog
    dlg.report("export", "segformer-b2", 1, 100)
    assert dlg._status.text() == "Copying segformer-b2"
    before = dlg.sizeHint().height()

    dlg.report("verify", "dinov3-" + "x" * 200, 2, 100)
    assert dlg._status.text().startswith("Verifying dinov3")
    assert dlg._status.text().endswith("…")  # elided, not wrapped
    assert dlg.sizeHint().height() == before
    dlg.close()
    window._pack_progress_dialog = None


def test_export_bar_stays_monotonic_across_write_and_verify(window):
    """Write and verify passes interleave per repo. The bar must not jump back when
    the phase flips: it tracks bytes done over both passes, so it only ever rises."""
    window._start_pack_progress("Exporting models", passes=2)
    total = 10 * 1024**3
    values = []
    # Repo-by-repo: write half, verify that half, write the rest, verify the rest.
    for phase, cur in [
        ("export", total // 2),
        ("verify", total // 2),
        ("export", total),
        ("verify", total),
    ]:
        window._sig_pack_progress.emit(phase, "m", cur, total)
        values.append(window._pack_progress_dialog._bar.value())
    assert values == sorted(values)  # never decreases
    assert values[0] == 25 and values[-1] == 100
    window._pack_progress_dialog.close()
    window._pack_progress_dialog = None


def test_pack_progress_is_throttled_to_whole_percent(window):
    from deepreefmap_gui.models.library_ui import _throttled

    seen: list[tuple[str, int]] = []
    forward = _throttled(lambda ph, _l, cur, _tot: seen.append((ph, cur)))
    total = 15 * 1024**3
    for done in range(0, total + 1, 1 << 20):
        forward("verify", "seg", done, total)

    assert len(seen) <= 102
    assert seen[-1] == ("verify", total)


def test_cancelling_the_dialog_stops_the_worker_callback(window):
    """The progress dialog's Cancel wires to a threading.Event the throttle checks;
    the next chunk of work raises PackCancelled instead of running to the end."""
    from deepreefmap_gui.models.library import PackCancelled
    from deepreefmap_gui.models.library_ui import _throttled

    cancel = window._start_pack_progress("Exporting models", passes=2)
    forward = _throttled(window._sig_pack_progress.emit, cancel)

    forward("export", "seg", 1, 100)  # fine before cancel
    window._pack_progress_dialog._cancel_btn.click()

    assert cancel.is_set()
    assert window._pack_progress_dialog._status.text() == "Cancelling…"
    with pytest.raises(PackCancelled):
        forward("export", "seg", 2, 100)

    window._pack_progress_dialog.close()
    window._pack_progress_dialog = None


def test_import_dialog_offers_the_packs_models(window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from deepreefmap_gui.models.library_ui import ModelSelectDialog

    cache = tmp_path / "hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", cache)
    _write_cache_repo(cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"w1"})
    _write_cache_repo(cache, "fake/other", {"config.json": "{}", "model.safetensors": b"w2"})
    seg, other = _seg_info(), _seg_info(name="other-fake", repo="fake/other")
    pack = library.export_model_pack([seg, other], tmp_path / "usb")

    shown: dict = {}

    def _capture(self):
        shown["names"] = sorted(self._checks)
        shown["summary"] = self._total_label.text()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(ModelSelectDialog, "exec", _capture)
    assert window._choose_models_to_import(pack) is None
    assert shown["names"] == ["other-fake", "seg-fake"]
    assert "2 of 2 models selected" in shown["summary"]


def test_export_with_no_cached_models_sets_status(window):
    window._downloading = set()
    window._last_model_states = []
    window._on_export_models()
    assert "No downloaded models" in window._status_label.text()


# --- hostile packs ------------------------------------------------------
#
# A pack arrives from a colleague's USB stick, so everything it names is untrusted:
# the paths in repo.json, the blob names, and any link the folder happens to hold.


def _folder_pack(tmp_path, monkeypatch, mutate):
    """A real folder pack whose repo.json has been rewritten by `mutate`."""
    cache = tmp_path / "src_hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", cache)
    _write_cache_repo(cache, "fake/seg", {"config.json": "{}", "model.safetensors": b"w"})
    pack = library.export_model_pack([_seg_info()], tmp_path / "usb")
    repo_manifest = pack / "models--fake--seg" / library.REPO_MANIFEST_NAME
    payload = json.loads(repo_manifest.read_text())
    mutate(payload, pack / "models--fake--seg")
    repo_manifest.write_text(json.dumps(payload))
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", tmp_path / "dst_hf")
    monkeypatch.setattr(manager, "all_known_models", list)
    return pack


def test_import_refuses_a_traversing_file_path(tmp_path, monkeypatch):
    def _escape(payload, _repo_dir):
        payload["files"][0]["path"] = "../../../../evil.txt"

    pack = _folder_pack(tmp_path, monkeypatch, _escape)
    with pytest.raises(library.PackSecurityError):
        library.import_model_pack(pack)
    assert not (tmp_path / "evil.txt").exists()


def test_import_refuses_a_blob_name_that_is_a_path(tmp_path, monkeypatch):
    def _escape(payload, _repo_dir):
        payload["files"][0]["blob"] = "../../../../evil.bin"

    pack = _folder_pack(tmp_path, monkeypatch, _escape)
    with pytest.raises(library.PackSecurityError):
        library.import_model_pack(pack)
    assert not (tmp_path / "evil.bin").exists()


def test_import_refuses_a_symlink_planted_in_the_pack(tmp_path, monkeypatch):
    """A pack stores real files. A link where a file belongs aims the copy at
    whatever it points to on the importing machine."""
    secret = tmp_path / "secret.txt"
    secret.write_text("private")

    def _plant(payload, repo_dir):
        target = repo_dir / "snapshots" / ("0" * 40) / payload["files"][0]["path"]
        target.unlink()
        os.symlink(secret, target)

    pack = _folder_pack(tmp_path, monkeypatch, _plant)
    with pytest.raises(library.PackSecurityError):
        library.import_model_pack(pack)


def test_import_refuses_a_repo_json_that_disowns_its_folder(tmp_path, monkeypatch):
    def _rename(payload, _repo_dir):
        payload["repo_id"] = "fake/other"

    pack = _folder_pack(tmp_path, monkeypatch, _rename)
    with pytest.raises(library.PackSecurityError):
        library.import_model_pack(pack)


# --- schema-1 packs (a single models.tar) still import -------------------
#
# _ensure_within validates where each member is placed; these cover where a
# member's link *points*, which the placement check cannot see.


def _pack_with_member(tmp_path, member):
    """A pack directory whose tar carries one crafted member alongside a real repo."""
    src_cache = tmp_path / "src_hf"
    _write_cache_repo(src_cache, "fake/seg", {"config.json": "{}"})
    pack_dir = tmp_path / "usb" / library.PACK_DIR_NAME
    pack_dir.mkdir(parents=True)
    manifest = {"schema_version": 1, "total_size_bytes": 0, "models": [], "repos": []}
    with tarfile.open(pack_dir / library.TAR_NAME, "w") as tar:
        tar.add(
            src_cache / "models--fake--seg",
            arcname=f"{library.CACHE_PREFIX}/models--fake--seg",
        )
        tar.addfile(member)
    (pack_dir / library.MANIFEST_NAME).write_text(json.dumps(manifest))
    return pack_dir


def _symlink_member(name, linkname):
    member = tarfile.TarInfo(name)
    member.type = tarfile.SYMTYPE
    member.linkname = linkname
    return member


@pytest.mark.parametrize(
    ("linkname", "what"),
    [
        ("../../../../../../etc/passwd", "a relative escape"),
        ("/etc/passwd", "an absolute path"),
        # Four levels up from snapshots/<commit>/ is the first that clears the
        # cache root; three would land on a sibling repo, which is in-domain.
        ("../../../../secret.txt", "the shallowest escape that clears the root"),
    ],
    ids=["relative", "absolute", "shallow"],
)
def test_import_refuses_a_symlink_pointing_outside_the_cache(
    tmp_path, monkeypatch, linkname, what
):
    """Placing the link inside the cache is legal; aiming it out of the cache is
    not. Verification opens whatever a snapshot entry resolves to."""
    assert what
    pack = _pack_with_member(
        tmp_path,
        _symlink_member(
            f"{library.CACHE_PREFIX}/models--fake--seg/snapshots/{'0' * 40}/escape", linkname
        ),
    )
    dst_cache = tmp_path / "dst_hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", dst_cache)
    monkeypatch.setattr(manager, "all_known_models", list)

    with pytest.raises(library.PackSecurityError):
        library.import_model_pack(pack)

    assert not (dst_cache / "models--fake--seg" / "snapshots" / ("0" * 40) / "escape").is_symlink()


def test_import_refuses_a_member_placed_outside_the_cache(tmp_path, monkeypatch):
    member = tarfile.TarInfo(f"{library.CACHE_PREFIX}/../../evil.txt")
    member.size = 0
    pack = _pack_with_member(tmp_path, member)
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", tmp_path / "dst_hf")
    monkeypatch.setattr(manager, "all_known_models", list)

    with pytest.raises(library.PackSecurityError):
        library.import_model_pack(pack)

    assert not (tmp_path / "evil.txt").exists()


def test_import_accepts_the_relative_symlinks_a_real_cache_uses(tmp_path, monkeypatch):
    """The guard must not reject the ../../blobs/<sha> links every snapshot is
    made of, including those in a snapshot subdirectory."""
    src_cache = tmp_path / "src_hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", src_cache)
    _write_cache_repo(src_cache, "fake/seg", {"config.json": "{}", "nested/deep/weights.bin": b"w"})
    info = _seg_info()
    pack = library.export_model_pack([info], tmp_path / "usb")

    dst_cache = tmp_path / "dst_hf"
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", dst_cache)
    monkeypatch.setattr(manager, "all_known_models", lambda: [info])

    library.import_model_pack(pack)

    assert is_model_cached(info) is True
    assert (manager.snapshot_dir("fake/seg") / "nested" / "deep" / "weights.bin").read_bytes() == b"w"


def test_the_symlink_fallback_also_refuses_to_copy_from_outside(tmp_path, monkeypatch):
    """Where symlinks are unavailable the escape becomes a copy of the target
    into the cache, which is the same disclosure by another route."""
    pack = _pack_with_member(
        tmp_path,
        _symlink_member(
            f"{library.CACHE_PREFIX}/models--fake--seg/snapshots/{'0' * 40}/escape",
            "/etc/passwd",
        ),
    )
    monkeypatch.setattr(manager, "_HF_CACHE_ROOT", tmp_path / "dst_hf")
    monkeypatch.setattr(manager, "all_known_models", list)
    monkeypatch.setattr(os, "symlink", lambda *_a, **_k: (_ for _ in ()).throw(OSError()))

    with pytest.raises(library.PackSecurityError):
        library.import_model_pack(pack)
