"""Portable model packs for offline field deployment.

Export copies the HuggingFace cache entries (and LoGeR materialise targets) for a
selection of models into a self-describing pack folder; import unpacks them into the
cache on an offline machine so the models load without a network round-trip.

The pack is one uncompressed ``models.tar`` plus a readable ``manifest.json`` sidecar:

    <dest>/DeepReefMap-model-pack/
        manifest.json     # schema_version, per-model repos/commit/size/sha256, total
        models.tar        # cache/models--<org>--<name>/... verbatim (symlinks preserved)

A single tar file (rather than a raw cache folder) survives copying to a FAT32/exFAT
USB stick and to Windows, where the cache's relative symlinks cannot exist. Import
recreates each symlink and, where the filesystem/OS refuses (Windows/exFAT), copies the
referenced blob into the snapshot instead — the same fallback ``manager._materialise_files``
uses. ``manager._verify_repo`` accepts either form, so ``is_model_cached`` round-trips.

Cache-path resolution goes exclusively through ``manager``'s call-time accessors so the
import-time ``_HF_CACHE_ROOT`` snapshot (which tests monkeypatch) stays authoritative.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import posixpath
import shutil
import tarfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from deepreefmap_gui.models import manager

if TYPE_CHECKING:
    from deepreefmap_gui.models.manager import ModelInfo

logger = logging.getLogger(__name__)

# (phase, current_bytes, total_bytes); phase is "export", "verify" or "import".
ProgressCallback = Callable[[str, int, int], None]

PACK_DIR_NAME = "DeepReefMap-model-pack"
TAR_NAME = "models.tar"
MANIFEST_NAME = "manifest.json"
# Every cache member is stored under this prefix so import knows what to strip and
# non-cache members (the manifest) are never mistaken for repo files.
CACHE_PREFIX = "cache"
SCHEMA_VERSION = 1

_CHUNK = 1 << 20
# Headroom over the pack's declared size so an import can't fill the disk to zero
# (which would leave a later run unable to write outputs). Mirrors the spirit of
# manager._MIN_FREE_BYTES without demanding its full 10 GB for a small pack.
_IMPORT_MARGIN_BYTES = 512 * 1024**2


class PackError(RuntimeError):
    """Base error for model-pack export/import."""


class PackChecksumError(PackError):
    """An imported repo's recomputed content hash did not match the manifest."""


class PackSecurityError(PackError):
    """A tar member resolved outside the destination cache (path traversal)."""


@dataclass
class RepoExport:
    """One HuggingFace repo staged for export."""

    repo_id: str
    repo_dir: Path
    commit: str | None
    content_sha256: str
    size_bytes: int


@dataclass
class ImportResult:
    imported: list[str]
    already_present: list[str]
    gated: list[str]


def _app_version() -> str:
    try:
        import importlib.metadata

        return importlib.metadata.version("deepreefmap-gui")
    except Exception:
        return "unknown"


def _iter_regular_files(root: Path) -> Iterator[Path]:
    """Real (non-symlink) files under root. Skips the snapshot symlinks so blob
    bytes are counted once on POSIX; on Windows the real snapshot copies are counted."""
    for p in root.rglob("*"):
        if p.is_symlink():
            continue
        if p.is_file():
            yield p


def _repo_content_sha256(repo_id: str) -> str:
    """Content hash of a repo's current snapshot, independent of symlink-vs-copy.

    Walks the snapshot in relpath order, folding each file's path and its resolved
    bytes into one sha256. Recomputed identically after import to detect corruption.
    """
    snap = manager.snapshot_dir(repo_id)
    if snap is None:
        return ""
    h = hashlib.sha256()
    files = sorted(
        (p for p in snap.rglob("*") if not p.is_dir()),
        key=lambda p: str(p.relative_to(snap)),
    )
    for p in files:
        h.update(str(p.relative_to(snap)).encode())
        with open(p.resolve(), "rb") as f:
            for chunk in iter(lambda: f.read(_CHUNK), b""):
                h.update(chunk)
    return h.hexdigest()


def enumerate_export_repos(models: list[ModelInfo]) -> dict[str, RepoExport]:
    """Repos to export across the selected models, deduped by repo_id.

    Deduplication is what collapses a DINOv3 backbone shared by several DPT heads to a
    single entry. Repos not present in the cache are skipped (the caller guards that
    selected models are fully cached first).
    """
    out: dict[str, RepoExport] = {}
    for info in models:
        for repo_id in info.hf_repos:
            if repo_id in out:
                continue
            repo_dir = manager.hf_cache_dir(repo_id)
            if not repo_dir.is_dir():
                continue
            size = sum(f.stat().st_size for f in _iter_regular_files(repo_dir))
            out[repo_id] = RepoExport(
                repo_id=repo_id,
                repo_dir=repo_dir,
                commit=manager.repo_commit(repo_id),
                content_sha256=_repo_content_sha256(repo_id),
                size_bytes=size,
            )
    return out


def build_pack_manifest(
    models: list[ModelInfo], repo_exports: dict[str, RepoExport]
) -> dict:
    total = sum(r.size_bytes for r in repo_exports.values())

    def _repo_entry(repo_id: str) -> dict:
        r = repo_exports[repo_id]
        return {
            "repo_id": r.repo_id,
            "commit": r.commit,
            "size_bytes": r.size_bytes,
            "sha256": r.content_sha256,
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "app_version": _app_version(),
        "created": datetime.now(timezone.utc).isoformat(),
        "total_size_bytes": total,
        "models": [
            {
                "name": m.name,
                "kind": m.kind,
                "gated": m.gated,
                "approx_size_mb": m.approx_size_mb,
                "repos": [
                    _repo_entry(rid) for rid in m.hf_repos if rid in repo_exports
                ],
            }
            for m in models
        ],
        "repos": [_repo_entry(rid) for rid in repo_exports],
    }


def _tar_repo_sha256(
    tar: tarfile.TarFile,
    index: dict[str, tarfile.TarInfo],
    repo_dirname: str,
    commit: str,
    on_bytes: Callable[[int], None],
) -> str:
    """Recompute a repo's content hash from tar members, mirroring
    _repo_content_sha256: snapshot files in relpath order, symlinks resolved to
    the blob member they reference."""
    snap_prefix = f"{CACHE_PREFIX}/{repo_dirname}/snapshots/{commit}/"
    entries = sorted(
        (m for m in index.values() if m.name.startswith(snap_prefix) and not m.isdir()),
        key=lambda m: m.name[len(snap_prefix):],
    )
    h = hashlib.sha256()
    for m in entries:
        h.update(m.name[len(snap_prefix):].encode())
        if m.issym():
            blob_name = posixpath.normpath(
                posixpath.join(posixpath.dirname(m.name), m.linkname)
            )
            source = index.get(blob_name)
            if source is None:
                raise PackChecksumError(
                    f"{m.name} points at {blob_name}, which is missing from the pack."
                )
        else:
            source = m
        stream = tar.extractfile(source)
        if stream is None:
            raise PackChecksumError(f"Could not read {source.name} back from the pack.")
        with stream:
            for chunk in iter(lambda: stream.read(_CHUNK), b""):
                h.update(chunk)
                on_bytes(len(chunk))
    return h.hexdigest()


def _verify_written_tar(
    tar_path: Path, manifest: dict, progress_cb: ProgressCallback | None
) -> None:
    """Read models.tar back from the destination and check every repo's content
    against the manifest hashes, so a bad write (failing USB stick, full or
    size-limited filesystem, transfer corruption) is caught at export time
    rather than at import time in the field."""
    total = int(manifest.get("total_size_bytes", 0)) or 1
    done = 0

    def on_bytes(n: int) -> None:
        nonlocal done
        done += n
        if progress_cb is not None:
            progress_cb("verify", min(done, total), total)

    with tarfile.open(tar_path, "r") as tar:
        index = {m.name: m for m in tar.getmembers()}
        for r in manifest.get("repos", []):
            expected = r.get("sha256")
            commit = r.get("commit")
            if not expected or not commit:
                continue
            dirname = "models--" + r["repo_id"].replace("/", "--")
            actual = _tar_repo_sha256(tar, index, dirname, commit, on_bytes)
            if actual != expected:
                raise PackChecksumError(
                    f"Write verification failed for {r['repo_id']}: what landed on "
                    "the destination does not match the source cache. The drive may "
                    "be failing or unable to hold a file this large."
                )


def export_model_pack(
    models: list[ModelInfo],
    dest_dir: str | Path,
    progress_cb: ProgressCallback | None = None,
) -> Path:
    """Write a model pack for the given (already-cached) models into dest_dir.

    The written tar is read back and verified against the manifest hashes before
    the manifest.json sidecar appears, so the sidecar doubles as the "export
    completed and verified" marker. Any failure removes the partial tar."""
    not_cached = [m.name for m in models if not manager.is_model_cached(m)]
    if not_cached:
        raise PackError(
            "These models are not fully downloaded and cannot be exported: "
            + ", ".join(not_cached)
        )

    repo_exports = enumerate_export_repos(models)
    if not repo_exports:
        raise PackError("Nothing to export: none of the selected models are cached.")
    manifest = build_pack_manifest(models, repo_exports)
    manifest_bytes = json.dumps(manifest, indent=2).encode()

    pack_dir = Path(dest_dir) / PACK_DIR_NAME
    pack_dir.mkdir(parents=True, exist_ok=True)
    tar_path = pack_dir / TAR_NAME

    total = manifest["total_size_bytes"] or 1
    written = 0

    def _progress_filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo:
        nonlocal written
        written += tarinfo.size
        if progress_cb is not None:
            progress_cb("export", min(written, total), total)
        return tarinfo

    # A stale sidecar must never sit next to a tar it doesn't describe.
    sidecar = pack_dir / MANIFEST_NAME
    sidecar.unlink(missing_ok=True)
    try:
        with tarfile.open(tar_path, "w") as tar:
            # Manifest as the first member too, so a stray sidecar loss is recoverable.
            info = tarfile.TarInfo(MANIFEST_NAME)
            info.size = len(manifest_bytes)
            tar.addfile(info, io.BytesIO(manifest_bytes))
            for rexp in repo_exports.values():
                tar.add(
                    rexp.repo_dir,
                    arcname=f"{CACHE_PREFIX}/{rexp.repo_dir.name}",
                    filter=_progress_filter,
                )
        _verify_written_tar(tar_path, manifest, progress_cb)
    except BaseException:
        tar_path.unlink(missing_ok=True)
        try:
            pack_dir.rmdir()
        except OSError:
            pass
        raise

    sidecar.write_text(json.dumps(manifest, indent=2))
    if progress_cb is not None:
        progress_cb("export", total, total)
    logger.info("Exported %d repo(s) to %s", len(repo_exports), pack_dir)
    return pack_dir


def read_pack_manifest(pack_dir: str | Path) -> dict:
    pack_dir = Path(pack_dir)
    sidecar = pack_dir / MANIFEST_NAME
    if sidecar.exists():
        return json.loads(sidecar.read_text())
    tar_path = pack_dir / TAR_NAME
    with tarfile.open(tar_path, "r") as tar:
        member = tar.extractfile(MANIFEST_NAME)
        if member is None:
            raise PackError(f"{pack_dir} has no {MANIFEST_NAME}")
        return json.loads(member.read().decode())


def is_model_pack(pack_dir: str | Path) -> bool:
    """True if pack_dir looks like a model pack (has the tar; manifest may be a
    sidecar or recoverable from the tar)."""
    return (Path(pack_dir) / TAR_NAME).exists()


def _pack_relpath(member_name: str) -> Path | None:
    prefix = CACHE_PREFIX + "/"
    if not member_name.startswith(prefix):
        return None
    rel = member_name[len(prefix):]
    return Path(rel) if rel else None


def _ensure_within(root: Path, target: Path) -> None:
    root_resolved = root.resolve()
    try:
        target.resolve().relative_to(root_resolved)
    except ValueError as exc:
        raise PackSecurityError(
            f"Refusing to extract outside the cache: {target}"
        ) from exc


def _extract_pack(
    tar_path: Path,
    dest_root: Path,
    total: int,
    progress_cb: ProgressCallback | None,
) -> None:
    written = 0
    total = total or 1
    with tarfile.open(tar_path, "r") as tar:
        members = tar.getmembers()
        # Pass 1: dirs + regular files. Blobs land before the snapshot symlinks
        # that point at them, so the pass-2 copy fallback can always resolve one.
        for m in members:
            if m.name == MANIFEST_NAME:
                continue
            rel = _pack_relpath(m.name)
            if rel is None:
                continue
            target = dest_root / rel
            _ensure_within(dest_root, target)
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif m.issym():
                continue
            elif m.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(m)
                if src is None:
                    continue
                with src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, _CHUNK)
                written += m.size
                if progress_cb is not None:
                    progress_cb("import", min(written, total), total)
        # Pass 2: symlinks, recreated (or copied where symlinks are unavailable).
        for m in members:
            if not m.issym():
                continue
            rel = _pack_relpath(m.name)
            if rel is None:
                continue
            target = dest_root / rel
            _ensure_within(dest_root, target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                target.unlink()
            try:
                os.symlink(m.linkname, target)
            except (OSError, NotImplementedError):
                blob = (target.parent / m.linkname).resolve()
                _ensure_within(dest_root, blob)
                shutil.copy2(blob, target)
    if progress_cb is not None:
        progress_cb("import", total, total)


def import_model_pack(
    pack_dir: str | Path, progress_cb: ProgressCallback | None = None
) -> ImportResult:
    """Unpack a model pack into the HF cache, verify checksums, and rebuild
    LoGeR materialise targets. Returns which models became usable."""
    pack_dir = Path(pack_dir)
    tar_path = pack_dir / TAR_NAME
    if not tar_path.exists():
        raise FileNotFoundError(f"No {TAR_NAME} found in {pack_dir}")

    manifest = read_pack_manifest(pack_dir)
    total = int(manifest.get("total_size_bytes", 0))

    dest_root = manager.hf_cache_root()
    dest_root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(dest_root).free
    if free < total + _IMPORT_MARGIN_BYTES:
        need_gb = (total + _IMPORT_MARGIN_BYTES) / 1024**3
        raise manager.InsufficientDiskSpace(
            f"Only {free / 1024**3:.1f} GB free under {dest_root}; "
            f"need about {need_gb:.1f} GB to import this pack."
        )

    from deepreefmap_gui.models.manager import all_known_models

    catalogue = {m.name: m for m in all_known_models()}
    manifest_names = [m["name"] for m in manifest.get("models", [])]
    before = {
        n
        for n in manifest_names
        if n in catalogue and manager.is_model_cached(catalogue[n])
    }

    _extract_pack(tar_path, dest_root, total, progress_cb)

    for r in manifest.get("repos", []):
        expected = r.get("sha256")
        if not expected:
            continue
        actual = _repo_content_sha256(r["repo_id"])
        if actual != expected:
            raise PackChecksumError(
                f"Checksum mismatch for {r['repo_id']} after import "
                "(the pack may be corrupt or truncated)."
            )

    imported: list[str] = []
    already_present: list[str] = []
    gated: list[str] = []
    for m in manifest.get("models", []):
        name = m["name"]
        if m.get("gated"):
            gated.append(name)
        info = catalogue.get(name)
        if info is None:
            continue
        manager.materialise_model(info)
        if manager.is_model_cached(info):
            (already_present if name in before else imported).append(name)

    logger.info(
        "Imported pack from %s: %d new, %d already present",
        pack_dir,
        len(imported),
        len(already_present),
    )
    return ImportResult(
        imported=imported, already_present=already_present, gated=gated
    )
