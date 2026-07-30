"""Portable model packs for offline field deployment.

Export copies the HuggingFace cache entries (and LoGeR materialise targets) for a
selection of models into a pack folder; import copies them into the cache on an
offline machine so the models load without a network round-trip.

A pack is one folder per repo, in HuggingFace cache shape, holding real files rather
than the cache's relative symlinks:

    <dest>/DeepReefMap-model-pack/
        manifest.json                       # models -> repos, digests, sizes
        models--Junyi42--LoGeR/
            repo.json                       # repo_id, commit, per-file digests
            refs/main
            snapshots/<commit>/latest.pt
        models--facebook--dinov3-vits16-pretrain-lvd1689m/
            ...

Real files rather than symlinks because a FAT32/exFAT stick and Windows cannot carry
the cache's links, and a folder per repo because a repo is the unit anyone wants to
handle: copy one model's folder to a colleague's laptop, or import a subset from a
pack holding the lot. Each folder carries its own repo.json, so a folder that arrives
on its own is still importable and still verifiable.

Import reverses the flattening: where the pack records which blob a snapshot entry
came from, the file lands in blobs/ with the snapshot entry symlinked to it, exactly
as a downloaded cache looks. Where symlinks are unavailable (Windows/exFAT) the blob
is copied into the snapshot instead -- the same fallback ``manager._materialise_files``
uses. ``manager._verify_repo`` accepts either form, so ``is_model_cached`` round-trips.

Packs written before this layout (schema 1, a single ``models.tar``) still import; see
the legacy section at the end of this module.

Cache-path resolution goes exclusively through ``manager``'s call-time accessors so the
import-time ``_HF_CACHE_ROOT`` snapshot (which tests monkeypatch) stays authoritative.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tarfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from deepreefmap_gui.models import manager

if TYPE_CHECKING:
    from deepreefmap_gui.models.manager import ModelInfo

logger = logging.getLogger(__name__)

#: Progress sink taking phase, label, current bytes and total bytes. Phase is one of
#: export, verify or import; label names the model in flight (empty when generic).
ProgressCallback = Callable[[str, str, int, int], None]

PACK_DIR_NAME = "DeepReefMap-model-pack"
MANIFEST_NAME = "manifest.json"
REPO_MANIFEST_NAME = "repo.json"
SCHEMA_VERSION = 2

# Schema 1 packs: one tar, every cache member under this prefix.
TAR_NAME = "models.tar"
CACHE_PREFIX = "cache"

_CHUNK = 1 << 20
# Headroom over the pack's declared size so an import can't fill the disk to zero
# (which would leave a later run unable to write outputs). Mirrors the spirit of
# manager._MIN_FREE_BYTES without demanding its full 10 GB for a small pack.
_IMPORT_MARGIN_BYTES = 512 * 1024**2
# Slack over the pack's own size when exporting. A destination stick needs no room
# for anything else, only enough not to trip on filesystem overhead.
_EXPORT_MARGIN_BYTES = 64 * 1024**2
# A repo folder appears under its final name only once it is written and verified.
_PARTIAL_SUFFIX = ".partial"


class PackError(RuntimeError):
    """Base error for model-pack export/import."""


class PackCancelled(PackError):
    """The caller asked to stop, from its progress callback."""


class PackChecksumError(PackError):
    """An imported repo's recomputed content hash did not match the manifest."""


class PackSecurityError(PackError):
    """A pack member resolved outside the destination cache (path traversal)."""


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


@dataclass
class PackModel:
    """One model offered by a pack, for the import selection UI."""

    name: str
    kind: str
    gated: bool
    approx_size_mb: int | None
    size_bytes: int
    repo_dirs: list[str] = field(default_factory=list)
    available: bool = True


def _app_version() -> str:
    try:
        import importlib.metadata

        return importlib.metadata.version("deepreefmap-gui")
    except Exception:
        return "unknown"


def repo_dir_name(repo_id: str) -> str:
    return "models--" + repo_id.replace("/", "--")


def _short_repo(repo_id: str) -> str:
    return repo_id.rsplit("/", 1)[-1]


def _repo_labels(models: list[ModelInfo]) -> dict[str, str]:
    """A display name for each repo, for progress status. A repo shared by several
    models (a DINOv3 backbone) takes the first model's name; the rest of the map lets
    the progress line say which model it is copying rather than a raw repo id."""
    out: dict[str, str] = {}
    for m in models:
        for repo_id in m.hf_repos:
            out.setdefault(repo_id, m.name)
    return out


def _file_digest(path: Path, on_bytes: Callable[[int], None] | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb", buffering=_CHUNK) as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
            if on_bytes is not None:
                on_bytes(len(chunk))
    return h.hexdigest()


def _fold_repo_digest(entries: Iterable[tuple[str, str]]) -> str:
    """Fold (snapshot relpath, file digest) pairs into one repo digest.

    Per-file digests are what makes export single-pass: each file is hashed while it
    is copied into the pack, and this fold needs no file bytes of its own.
    """
    h = hashlib.sha256()
    for rel, digest in sorted(entries):
        h.update(rel.encode())
        h.update(b"\0")
        h.update(digest.encode())
    return h.hexdigest()


def _snapshot_files(snap: Path) -> list[tuple[str, Path]]:
    """(relpath, path) for every file in a snapshot, in relpath order."""
    return sorted(
        ((p.relative_to(snap).as_posix(), p) for p in snap.rglob("*") if not p.is_dir()),
        key=lambda pair: pair[0],
    )


def _blob_name(entry: Path) -> str | None:
    """The blobs/ filename a snapshot entry links to, or None if it is a real file."""
    if not entry.is_symlink():
        return None
    target = Path(os.readlink(entry))
    return target.name if target.parent.name == "blobs" else None


def repo_content_digest(repo_id: str) -> str:
    """Content digest of a repo's cached snapshot, independent of symlink-vs-copy."""
    snap = manager.snapshot_dir(repo_id)
    if snap is None:
        return ""
    return _fold_repo_digest(
        (rel, _file_digest(path.resolve())) for rel, path in _snapshot_files(snap)
    )


def enumerate_export_repos(models: list[ModelInfo]) -> dict[str, RepoExport]:
    """Repos to export across the selected models, deduped by repo_id.

    Deduplication is what collapses a DINOv3 backbone shared by several DPT heads to a
    single entry. Repos not present in the cache are skipped (the caller guards that
    selected models are fully cached first).

    Sizes come from stat alone: content digests are filled in by export_model_pack as
    the bytes stream into the pack, so nothing here reads a file. A pre-pass that
    hashed every repo up front was a silent multi-minute stall before the bar moved.
    """
    out: dict[str, RepoExport] = {}
    for info in models:
        for repo_id in info.hf_repos:
            if repo_id in out:
                continue
            repo_dir = manager.hf_cache_dir(repo_id)
            snap = manager.snapshot_dir(repo_id)
            if not repo_dir.is_dir() or snap is None:
                continue
            size = sum(p.resolve().stat().st_size for _rel, p in _snapshot_files(snap))
            out[repo_id] = RepoExport(
                repo_id=repo_id,
                repo_dir=repo_dir,
                commit=manager.repo_commit(repo_id),
                content_sha256="",
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


# --- export -----------------------------------------------------------------


def _copy_hashing(src: Path, dst: Path, on_bytes: Callable[[int], None]) -> tuple[str, int]:
    """Copy one file, hashing and counting the bytes on their way through."""
    h = hashlib.sha256()
    size = 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb", buffering=_CHUNK) as fin, open(dst, "wb", buffering=_CHUNK) as fout:
        for chunk in iter(lambda: fin.read(_CHUNK), b""):
            fout.write(chunk)
            h.update(chunk)
            size += len(chunk)
            on_bytes(len(chunk))
    return h.hexdigest(), size


def _dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _check_export_space(pack_dir: Path, repo_exports: dict[str, RepoExport]) -> None:
    """Refuse an export the destination cannot hold, before a byte is written.

    A repo already sitting in the target pack is about to be replaced, so its bytes
    count as available: re-exporting to the same stick must not be refused for want
    of room the old copy is holding.
    """
    needed = sum(r.size_bytes for r in repo_exports.values())
    for rexp in repo_exports.values():
        existing = pack_dir / repo_dir_name(rexp.repo_id)
        if existing.is_dir():
            needed -= _dir_size(existing)
    free = shutil.disk_usage(pack_dir).free
    if free < needed + _EXPORT_MARGIN_BYTES:
        raise manager.InsufficientDiskSpace(
            f"Only {free / 1024**3:.1f} GB free on {pack_dir}; "
            f"this pack needs about {(needed + _EXPORT_MARGIN_BYTES) / 1024**3:.1f} GB."
        )


def _write_repo_folder(
    rexp: RepoExport, pack_dir: Path, on_bytes: Callable[[int], None]
) -> dict:
    """Copy one repo's snapshot into the pack as real files, hashing as it goes.

    Returns the repo.json payload. The folder is built under a .partial name and
    renamed only after the read-back check passes, so a folder sitting in a pack
    under its real name is always a complete, verified repo.
    """
    snap = manager.snapshot_dir(rexp.repo_id)
    if snap is None or rexp.commit is None:
        raise PackError(f"{rexp.repo_id} has no cached snapshot to export.")

    dirname = repo_dir_name(rexp.repo_id)
    staging = pack_dir / (dirname + _PARTIAL_SUFFIX)
    shutil.rmtree(staging, ignore_errors=True)
    snap_root = staging / "snapshots" / rexp.commit

    files: list[dict] = []
    for rel, source in _snapshot_files(snap):
        blob = _blob_name(source)
        digest, size = _copy_hashing(source.resolve(), snap_root / rel, on_bytes)
        # A weight file's cache blob is named by its own sha256 (HuggingFace content
        # addressing), which is the digest we just computed. A mismatch means the
        # local cache has rotted, so refuse to seal that corruption into the pack.
        if blob is not None and len(blob) == 64 and digest != blob:
            raise PackChecksumError(
                f"{rexp.repo_id}: {rel} does not match HuggingFace's recorded hash. "
                "The local model cache is corrupt; re-download the model before "
                "exporting."
            )
        files.append({"path": rel, "size": size, "sha256": digest, "blob": blob})

    payload = {
        "schema_version": SCHEMA_VERSION,
        "repo_id": rexp.repo_id,
        "commit": rexp.commit,
        "size_bytes": sum(f["size"] for f in files),
        "sha256": _fold_repo_digest((f["path"], f["sha256"]) for f in files),
        "files": files,
    }
    refs = staging / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / "main").write_text(rexp.commit)
    (staging / REPO_MANIFEST_NAME).write_text(json.dumps(payload, indent=2))
    return payload


def _verify_repo_folder(
    pack_dir: Path, payload: dict, on_bytes: Callable[[int], None]
) -> None:
    """Read a freshly written repo folder back and check it against its own repo.json,
    so a bad write (failing stick, full or size-limited filesystem) is caught here
    rather than in the field."""
    dirname = repo_dir_name(payload["repo_id"])
    staging = pack_dir / (dirname + _PARTIAL_SUFFIX)
    snap_root = staging / "snapshots" / payload["commit"]
    entries = [
        (f["path"], _file_digest(snap_root / f["path"], on_bytes))
        for f in payload["files"]
    ]
    if _fold_repo_digest(entries) != payload["sha256"]:
        raise PackChecksumError(
            f"Write verification failed for {payload['repo_id']}: what landed on "
            "the destination does not match the source cache. The drive may be "
            "failing or unable to hold a file this large."
        )
    final = pack_dir / dirname
    shutil.rmtree(final, ignore_errors=True)
    staging.rename(final)


def _reuse_existing_repo(
    pack_dir: Path, rexp: RepoExport, on_bytes: Callable[[int], None]
) -> dict | None:
    """Reuse a repo already on the destination instead of copying it again.

    A cache snapshot is immutable per commit, so a destination folder whose repo.json
    records the same commit as the source holds the same content. The copy on the
    drive is still read back and folded to confirm it is intact (a stick can rot),
    but the source is never re-read and nothing is rewritten. Returns the repo.json
    payload to reuse, or None to fall through to a normal write.
    """
    folder = pack_dir / repo_dir_name(rexp.repo_id)
    manifest = folder / REPO_MANIFEST_NAME
    if not manifest.is_file():
        return None
    try:
        payload = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("commit") != rexp.commit or not payload.get("sha256"):
        return None
    snap_root = folder / "snapshots" / str(payload["commit"])
    try:
        entries = [
            (f["path"], _file_digest(snap_root / f["path"], on_bytes))
            for f in payload.get("files", [])
        ]
    except OSError:
        return None  # a missing or unreadable file: rewrite the repo
    if _fold_repo_digest(entries) != payload["sha256"]:
        return None
    return payload


def _resolve_export_dir(dest: Path) -> Path:
    """Where the pack folder actually lands under the folder the user chose.

    The chosen folder is used directly when it is empty or already holds a pack, so
    the folder the user picked is the pack. A folder that holds unrelated files gets
    a named subfolder instead, so an export never scatters repo folders in among a
    user's existing files.
    """
    if dest.exists() and not is_model_pack(dest) and any(dest.iterdir()):
        return dest / PACK_DIR_NAME
    return dest


def _remove_pack_artifacts(pack_dir: Path) -> None:
    """Delete only what an export writes: the manifest and every repo (or partial)
    folder. Anything else the user keeps in the folder is left untouched."""
    (pack_dir / MANIFEST_NAME).unlink(missing_ok=True)
    for child in pack_dir.glob("models--*"):
        shutil.rmtree(child, ignore_errors=True)


def export_model_pack(
    models: list[ModelInfo],
    dest_dir: str | Path,
    progress_cb: ProgressCallback | None = None,
) -> Path:
    """Write a model pack for the given (already-cached) models into dest_dir.

    Two passes over the bytes, not three: each file is hashed while it is copied, and
    only the read-back check touches the data again. Repos are written one at a time
    and each is verified and renamed into place before the next starts, so an
    interrupted export leaves the repos it finished usable.
    """
    not_cached = [m.name for m in models if not manager.is_model_cached(m)]
    if not_cached:
        raise PackError(
            "These models are not fully downloaded and cannot be exported: "
            + ", ".join(not_cached)
        )

    repo_exports = enumerate_export_repos(models)
    if not repo_exports:
        raise PackError("Nothing to export: none of the selected models are cached.")

    pack_dir = _resolve_export_dir(Path(dest_dir))
    pack_dir_existed = pack_dir.exists()
    pack_dir.mkdir(parents=True, exist_ok=True)
    _check_export_space(pack_dir, repo_exports)

    total = sum(r.size_bytes for r in repo_exports.values()) or 1
    labels = _repo_labels(models)
    written = 0
    verified = 0
    label = ""

    def on_written(n: int) -> None:
        nonlocal written
        written += n
        if progress_cb is not None:
            progress_cb("export", label, min(written, total), total)

    def on_verified(n: int) -> None:
        nonlocal verified
        verified += n
        if progress_cb is not None:
            progress_cb("verify", label, min(verified, total), total)

    # A stale manifest must never sit next to repos it doesn't describe.
    sidecar = pack_dir / MANIFEST_NAME
    sidecar.unlink(missing_ok=True)
    try:
        for rexp in repo_exports.values():
            label = labels.get(rexp.repo_id, _short_repo(rexp.repo_id))
            reused = _reuse_existing_repo(pack_dir, rexp, on_verified)
            if reused is not None:
                # Already on the drive and intact: keep it, and advance the write
                # pass so the bar still fills. Nothing was copied.
                on_written(int(reused["size_bytes"]))
                rexp.content_sha256 = reused["sha256"]
                rexp.size_bytes = reused["size_bytes"]
                logger.info("Reusing %s: already exported and intact", rexp.repo_id)
                continue
            payload = _write_repo_folder(rexp, pack_dir, on_written)
            _verify_repo_folder(pack_dir, payload, on_verified)
            rexp.content_sha256 = payload["sha256"]
            rexp.size_bytes = payload["size_bytes"]
    except PackCancelled:
        # The user asked to stop, so leave nothing half-written on their drive. When
        # the pack landed directly in a folder they chose, clear only the pack's own
        # files and keep the folder; a folder this export created is removed whole.
        if pack_dir_existed:
            _remove_pack_artifacts(pack_dir)
        else:
            shutil.rmtree(pack_dir, ignore_errors=True)
        raise
    except BaseException:
        # An unexpected failure keeps the repos already verified (each is usable on
        # its own) and only drops the folder that was mid-write.
        for rexp in repo_exports.values():
            staging = pack_dir / (repo_dir_name(rexp.repo_id) + _PARTIAL_SUFFIX)
            shutil.rmtree(staging, ignore_errors=True)
        if not pack_dir_existed:
            try:
                pack_dir.rmdir()  # only when nothing was ever completed
            except OSError:
                pass
        raise

    manifest = build_pack_manifest(models, repo_exports)
    sidecar.write_text(json.dumps(manifest, indent=2))
    if progress_cb is not None:
        progress_cb("verify", "", total, total)
    logger.info("Exported %d repo(s) to %s", len(repo_exports), pack_dir)
    return pack_dir


# --- reading a pack ---------------------------------------------------------


def read_pack_manifest(pack_dir: str | Path) -> dict:
    """The pack-level manifest. Rebuilt from the repo.json files if it is missing,
    so a pack assembled by copying single repo folders together still reads."""
    pack_dir = Path(pack_dir)
    sidecar = pack_dir / MANIFEST_NAME
    if sidecar.exists():
        return json.loads(sidecar.read_text())
    if (pack_dir / TAR_NAME).exists():
        return _read_legacy_tar_manifest(pack_dir)

    repos = read_repo_manifests(pack_dir)
    if not repos:
        raise PackError(f"{pack_dir} has no {MANIFEST_NAME} and no repo folders")
    entries = [
        {
            "repo_id": p["repo_id"],
            "commit": p["commit"],
            "size_bytes": p["size_bytes"],
            "sha256": p["sha256"],
        }
        for p in repos.values()
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "app_version": "unknown",
        "total_size_bytes": sum(e["size_bytes"] for e in entries),
        "models": [],
        "repos": entries,
    }


def read_repo_manifests(pack_dir: str | Path) -> dict[str, dict]:
    """repo.json payloads in a pack, keyed by folder name."""
    pack_dir = Path(pack_dir)
    out: dict[str, dict] = {}
    for child in sorted(pack_dir.glob("models--*")):
        manifest = child / REPO_MANIFEST_NAME
        if not child.is_dir() or not manifest.is_file():
            continue
        try:
            payload = json.loads(manifest.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise PackError(f"{manifest} is unreadable: {exc}") from exc
        if repo_dir_name(str(payload.get("repo_id", ""))) != child.name:
            raise PackSecurityError(
                f"{manifest} claims repo {payload.get('repo_id')!r}, "
                f"which does not match its folder {child.name}."
            )
        out[child.name] = payload
    return out


def is_model_pack(pack_dir: str | Path) -> bool:
    """True if pack_dir looks like a model pack: a repo folder, a manifest, or the
    single tar of a schema-1 pack."""
    pack_dir = Path(pack_dir)
    if (pack_dir / TAR_NAME).exists() or (pack_dir / MANIFEST_NAME).exists():
        return True
    return any(
        (child / REPO_MANIFEST_NAME).is_file() for child in pack_dir.glob("models--*")
    )


def pack_schema_version(pack_dir: str | Path) -> int:
    pack_dir = Path(pack_dir)
    if (pack_dir / MANIFEST_NAME).exists() or read_repo_manifests(pack_dir):
        return _schema_version(read_pack_manifest(pack_dir))
    return 1


def list_pack_models(pack_dir: str | Path) -> list[PackModel]:
    """Models a pack offers, for the import selection UI.

    ``available`` is False when a model's repos are not all present, which is what a
    pack assembled by copying single folders looks like when a shared backbone was
    left behind.
    """
    pack_dir = Path(pack_dir)
    manifest = read_pack_manifest(pack_dir)
    present = set(read_repo_manifests(pack_dir))
    legacy = _schema_version(manifest) < 2
    out: list[PackModel] = []
    for m in manifest.get("models", []):
        dirs = [repo_dir_name(r["repo_id"]) for r in m.get("repos", [])]
        out.append(
            PackModel(
                name=m["name"],
                kind=m.get("kind", ""),
                gated=bool(m.get("gated")),
                approx_size_mb=m.get("approx_size_mb"),
                size_bytes=sum(r.get("size_bytes", 0) for r in m.get("repos", [])),
                repo_dirs=dirs,
                available=legacy or all(d in present for d in dirs),
            )
        )
    return out


def _schema_version(manifest: dict) -> int:
    try:
        return int(manifest.get("schema_version", 1))
    except (TypeError, ValueError):
        return 1


# --- import -----------------------------------------------------------------


def _ensure_within(root: Path, target: Path) -> None:
    root_resolved = root.resolve()
    try:
        target.resolve().relative_to(root_resolved)
    except ValueError as exc:
        raise PackSecurityError(
            f"Refusing to extract outside the cache: {target}"
        ) from exc


def _safe_component(name: str, what: str) -> str:
    """A single path component: no separators, no traversal, no absolute path."""
    if not name or name in (".", "..") or os.sep in name or "/" in name or "\\" in name:
        raise PackSecurityError(f"Refusing a {what} named {name!r}.")
    return name


def _import_repo_folder(
    repo_dir: Path, payload: dict, dest_root: Path, on_bytes: Callable[[int], None]
) -> None:
    """Copy one pack repo folder into the cache, restoring the blobs/snapshots shape.

    The pack is untrusted input (it arrives on a colleague's stick), so every path it
    names is checked to land inside the cache, and a file the pack stores as a symlink
    is refused rather than followed off the drive.
    """
    repo_id = str(payload["repo_id"])
    commit = _safe_component(str(payload["commit"]), "commit")
    dest = dest_root / repo_dir_name(repo_id)
    _ensure_within(dest_root, dest)
    snap_src = repo_dir / "snapshots" / commit
    snap_dest = dest / "snapshots" / commit
    blobs_dest = dest / "blobs"

    for entry in payload.get("files", []):
        rel = str(entry["path"])
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise PackSecurityError(f"Refusing a pack file path {rel!r}.")
        source = snap_src / rel
        _ensure_within(repo_dir, source)
        if source.is_symlink():
            raise PackSecurityError(
                f"{source} is a symlink; a pack stores real files only."
            )
        if not source.is_file():
            raise PackError(f"{repo_id} is incomplete in this pack: {rel} is missing.")

        target = snap_dest / rel
        _ensure_within(dest_root, target)
        blob = entry.get("blob")
        if blob:
            blob_path = blobs_dest / _safe_component(str(blob), "blob")
            _ensure_within(dest_root, blob_path)
            _copy_hashing(source, blob_path, on_bytes)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                target.unlink()
            try:
                os.symlink(os.path.relpath(blob_path, target.parent), target)
            except (OSError, NotImplementedError):
                shutil.copy2(blob_path, target)
        else:
            _copy_hashing(source, target, on_bytes)

    refs = dest / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / "main").write_text(commit)


def import_model_pack(
    pack_dir: str | Path,
    progress_cb: ProgressCallback | None = None,
    model_names: Iterable[str] | None = None,
) -> ImportResult:
    """Copy a pack into the HF cache, verify checksums, and rebuild LoGeR materialise
    targets. Returns which models became usable.

    ``model_names`` limits the import to those models and the repos they need; None
    takes the lot. A schema-1 pack is a single archive and always imports whole.
    """
    pack_dir = Path(pack_dir)
    if not is_model_pack(pack_dir):
        raise FileNotFoundError(f"{pack_dir} is not a DeepReefMap model pack")

    manifest = read_pack_manifest(pack_dir)
    legacy = _schema_version(manifest) < 2
    repos = {} if legacy else read_repo_manifests(pack_dir)

    wanted: set[str] | None = None if model_names is None else set(model_names)
    if wanted is not None and not legacy:
        keep: set[str] = set()
        for m in list_pack_models(pack_dir):
            if m.name in wanted:
                keep.update(m.repo_dirs)
        repos = {name: p for name, p in repos.items() if name in keep}
        if not repos:
            raise PackError("None of the selected models are in this pack.")

    total = (
        int(manifest.get("total_size_bytes", 0))
        if legacy
        else sum(int(p.get("size_bytes", 0)) for p in repos.values())
    )

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
    if wanted is not None:
        manifest_names = [n for n in manifest_names if n in wanted]
    before = {
        n
        for n in manifest_names
        if n in catalogue and manager.is_model_cached(catalogue[n])
    }

    if legacy:
        _extract_legacy_pack(pack_dir / TAR_NAME, dest_root, total, progress_cb)
        _verify_imported_repos(manifest.get("repos", []), legacy=True)
    else:
        copied = 0
        span = total or 1
        label = ""
        labels = {
            r["repo_id"]: m["name"]
            for m in manifest.get("models", [])
            for r in m.get("repos", [])
        }

        def on_bytes(n: int) -> None:
            nonlocal copied
            copied += n
            if progress_cb is not None:
                progress_cb("import", label, min(copied, span), span)

        _cross_check_manifests(manifest, repos)
        did_import: list[dict] = []
        for name, payload in repos.items():
            label = labels.get(payload["repo_id"], _short_repo(payload["repo_id"]))
            # Scan what is already cached: a repo whose local content already folds
            # to the pack's digest is byte-identical, so copying it again is pure
            # waste. This is what makes re-importing the same pack near-instant.
            if repo_content_digest(payload["repo_id"]) == payload["sha256"]:
                on_bytes(int(payload.get("size_bytes", 0)))
                logger.info("Skipping %s: already present and identical", payload["repo_id"])
                continue
            dest = dest_root / name
            # Only a repo this import created is safe to remove on the way out. One
            # that was already cached keeps whatever it had: blobs are named by
            # content, so a half-finished pass over it overwrites nothing.
            fresh = not dest.exists()
            try:
                _import_repo_folder(pack_dir / name, payload, dest_root, on_bytes)
            except BaseException:
                if fresh:
                    shutil.rmtree(dest, ignore_errors=True)
                raise
            did_import.append(payload)
        # Only the repos actually copied need a read-back; the skipped ones were
        # just hashed to decide the skip and already match.
        _verify_imported_repos(
            [{"repo_id": p["repo_id"], "sha256": p["sha256"]} for p in did_import],
            legacy=False,
        )
        if progress_cb is not None:
            progress_cb("import", "", span, span)

    imported: list[str] = []
    already_present: list[str] = []
    gated: list[str] = []
    for m in manifest.get("models", []):
        name = m["name"]
        if wanted is not None and name not in wanted:
            continue
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


def _cross_check_manifests(manifest: dict, repos: dict[str, dict]) -> None:
    """The pack manifest and each repo.json must tell the same story.

    repo.json is what import verifies against, being the record that travels with a
    folder copied out on its own. A manifest that disagrees means one of the two was
    edited or the folders were mixed between packs, so neither can be trusted.
    """
    for entry in manifest.get("repos", []):
        payload = repos.get(repo_dir_name(str(entry.get("repo_id", ""))))
        if payload is None or not entry.get("sha256"):
            continue
        if payload.get("sha256") != entry["sha256"]:
            raise PackChecksumError(
                f"{MANIFEST_NAME} and {REPO_MANIFEST_NAME} disagree about "
                f"{entry['repo_id']}; this pack has been altered."
            )


def _verify_imported_repos(entries: Iterable[dict], *, legacy: bool) -> None:
    """Recompute each imported repo's digest from the cache and compare."""
    for r in entries:
        expected = r.get("sha256")
        if not expected:
            continue
        repo_id = r["repo_id"]
        actual = (
            _repo_content_sha256_v1(repo_id) if legacy else repo_content_digest(repo_id)
        )
        if actual != expected:
            raise PackChecksumError(
                f"Checksum mismatch for {repo_id} after import "
                "(the pack may be corrupt or truncated)."
            )


# --- schema-1 packs (single models.tar), import only ------------------------


def _read_legacy_tar_manifest(pack_dir: Path) -> dict:
    with tarfile.open(pack_dir / TAR_NAME, "r") as tar:
        member = tar.extractfile(MANIFEST_NAME)
        if member is None:
            raise PackError(f"{pack_dir} has no {MANIFEST_NAME}")
        return json.loads(member.read().decode())


def _repo_content_sha256_v1(repo_id: str) -> str:
    """Schema-1 content hash: one sha256 over path + resolved bytes, in relpath
    order. Superseded by repo_content_digest, which folds per-file digests instead
    and so can be computed while the bytes are already moving."""
    snap = manager.snapshot_dir(repo_id)
    if snap is None:
        return ""
    h = hashlib.sha256()
    for rel, p in _snapshot_files(snap):
        h.update(rel.encode())
        with open(p.resolve(), "rb") as f:
            for chunk in iter(lambda: f.read(_CHUNK), b""):
                h.update(chunk)
    return h.hexdigest()


def _pack_relpath(member_name: str) -> Path | None:
    prefix = CACHE_PREFIX + "/"
    if not member_name.startswith(prefix):
        return None
    rel = member_name[len(prefix):]
    return Path(rel) if rel else None


def _extract_legacy_pack(
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
                # Only directories, regular files and (in pass 2) symlinks are
                # ever created. Device nodes, fifos and hard links fall through
                # untouched; the cache contains none, and a pack that carries
                # one fails the checksum rather than materialising it.
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(m)
                if src is None:
                    continue
                with src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, _CHUNK)
                written += m.size
                if progress_cb is not None:
                    progress_cb("import", "", min(written, total), total)
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
            # Where the link points is checked as well as where it sits. A pack
            # can name a member innocuously and still aim its link anywhere on
            # the importing machine, and the digest walk opens whatever the
            # snapshot resolves to. An absolute linkname discards target.parent
            # here exactly as the OS would resolve it.
            blob = (target.parent / m.linkname).resolve()
            _ensure_within(dest_root, blob)
            try:
                os.symlink(m.linkname, target)
            except (OSError, NotImplementedError):
                shutil.copy2(blob, target)
    if progress_cb is not None:
        progress_cb("import", "", total, total)
