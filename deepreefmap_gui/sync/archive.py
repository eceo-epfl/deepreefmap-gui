"""The archive upload queue: what this laptop offers the registry's blob store.

Two kinds of content travel. Every clip the survey knows about with a readable
file goes up as a `video` blob, and every file inside a succeeded run's
directory goes up as an `artifact` under that run. The store is
content-addressed and the server verifies every claimed hash itself, so
re-running the queue costs one initiate per file already archived and sends
nothing twice.

No Qt here, deliberately: the Server page runs this on a worker thread and
marshals progress back through signals, the same shape as `engine.py`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from deepreefmap_gui.survey.models import RunRecord, VideoAsset
from deepreefmap_gui.survey.store import SurveyStore
from deepreefmap_gui.sync.client import ServerUnreachableError, SyncError

logger = logging.getLogger(__name__)

# Streamed, because a clip can be tens of gigabytes.
HASH_CHUNK_BYTES = 1024 * 1024

# One PUT moves a whole part, 32 MiB at the server's default, and a field
# uplink can be slow enough that the client timeout is what would kill it.
UPLOAD_TIMEOUT = 600.0

KIND_VIDEO = "video"
KIND_ARTIFACT = "artifact"

STATUS_PENDING = "pending"
STATUS_UPLOADED = "uploaded"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"

# What the registry holds of one clip or one run, as the badges read it.
STATE_ARCHIVED = "archived"
STATE_PARTIAL = "partial"
STATE_PENDING = "pending"
STATE_FAILED = "failed"
STATE_UNKNOWN = "unknown"

# The step's text, then jobs done and jobs total.
ProgressFn = Callable[[str, int, int], None]


class ArchiveTransport(Protocol):
    """The two calls one pass over the queue makes on a registry client."""

    def archive_initiate(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def archive_complete(
        self, object_id: str, parts: Sequence[dict[str, Any]]
    ) -> dict[str, Any]: ...


def sha256_file(path: Path) -> str:
    """SHA-256 of the whole file, as the 64 lowercase hex characters the server wants."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def upload_part(url: str, chunk: bytes) -> str:
    """PUT one part's bytes to its presigned URL, returning the ETag it answered.

    The URL is not under the registry's address and carries its own auth in the
    query string, so no bearer token travels here. The ETag comes back quoted,
    and the completion call wants it bare.
    """
    # S310: the URL was presigned by the registry this device is enrolled with.
    request = urllib.request.Request(url, data=chunk, method="PUT")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=UPLOAD_TIMEOUT) as response:  # noqa: S310
            etag = response.headers.get("ETag", "")
    except urllib.error.HTTPError as exc:
        raise SyncError(f"The blob store refused a part ({exc.code}).") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ServerUnreachableError(f"Cannot reach the blob store: {exc}.") from exc
    if not etag:
        raise SyncError("The blob store answered a part without an ETag.")
    return etag.strip('"')


@dataclass(frozen=True)
class ArchiveJob:
    """One file to offer: a clip, or one artefact inside a run directory."""

    label: str
    path: Path
    sha256: str
    size_bytes: int
    kind: str
    run_id: str | None = None
    relpath: str | None = None

    def initiate_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "kind": self.kind,
        }
        if self.kind == KIND_ARTIFACT:
            payload["run_id"] = self.run_id
            payload["relpath"] = self.relpath
        return payload


@dataclass
class ArchiveReport:
    """What one pass over the queue did.

    ``archived`` counts files sent and assembled this pass, which the server is
    now verifying. ``uploading_verification`` counts files somebody sent before
    that are still being verified. Failures carry their reason per file because
    the queue keeps going: re-running resumes server-side, so a flaky connection
    costs a retry rather than the whole batch.
    """

    archived: int = 0
    already: int = 0
    uploading_verification: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)


# --- planning -----------------------------------------------------------------


def archive_plan(store: SurveyStore, out_root: Path) -> list[ArchiveJob]:
    """Everything worth archiving: the clips, then each succeeded run's files."""
    return _video_jobs(store) + _run_jobs(store, out_root)


def archive_plan_for_video(store: SurveyStore, video_id: object) -> list[ArchiveJob]:
    """One clip's job and nothing else. Empty when its file cannot be read."""
    wanted = str(video_id)
    for video in store.list_videos():
        if str(video.id) == wanted:
            return _jobs_for_video(store, video)
    return []


def archive_plan_for_run(store: SurveyStore, out_root: Path, run_id: object) -> list[ArchiveJob]:
    """One run's artefacts and nothing else. Empty unless it succeeded and kept its directory."""
    wanted = str(run_id)
    for run in store.list_runs():
        if str(run.id) == wanted:
            return _jobs_for_run(run, out_root)
    return []


def _video_jobs(store: SurveyStore) -> list[ArchiveJob]:
    jobs: list[ArchiveJob] = []
    for video in store.list_videos():
        jobs.extend(_jobs_for_video(store, video))
    return jobs


def _jobs_for_video(store: SurveyStore, video: VideoAsset) -> list[ArchiveJob]:
    """This clip's job, where its file can still be read.

    The full-file digest is computed here where the row lacks one, and written
    back so the next pass, and the registry's `video_asset.sha256` linkage, get
    it for free. Nothing else on the row is touched.
    """
    path = Path(video.path)
    try:
        size_bytes = path.stat().st_size
    except OSError:
        return []
    if not path.is_file():
        return []
    digest = video.sha256
    if not digest:
        try:
            digest = sha256_file(path)
        except OSError as exc:
            logger.info("Cannot hash %s: %s", path, exc)
            return []
        video.sha256 = digest
        store.update_video(video)
    return [
        ArchiveJob(
            label=video.file_name,
            path=path,
            sha256=digest,
            size_bytes=size_bytes,
            kind=KIND_VIDEO,
        )
    ]


def _run_jobs(store: SurveyStore, out_root: Path) -> list[ArchiveJob]:
    jobs: list[ArchiveJob] = []
    for run in store.list_runs():
        jobs.extend(_jobs_for_run(run, out_root))
    return jobs


def _jobs_for_run(run: RunRecord, out_root: Path) -> list[ArchiveJob]:
    if run.status != "succeeded":
        return []
    run_dir = out_root / run.run_dir_name
    if not run_dir.is_dir():
        return []
    return _run_dir_jobs(run_dir, run.run_dir_name, str(run.id))


def _run_dir_jobs(run_dir: Path, run_dir_name: str, run_id: str) -> list[ArchiveJob]:
    digests, manifest_mtime = _manifest_digests(run_dir)
    jobs: list[ArchiveJob] = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(run_dir)
        # Hidden entries are working state, .cache/ included, never outputs.
        if any(part.startswith(".") for part in rel.parts):
            continue
        relpath = rel.as_posix()
        try:
            stat = path.stat()
            digest = _recorded_digest(digests, manifest_mtime, path, relpath) or sha256_file(
                path
            )
        except OSError as exc:
            logger.info("Cannot read %s: %s", path, exc)
            continue
        jobs.append(
            ArchiveJob(
                label=f"{run_dir_name}/{relpath}",
                path=path,
                sha256=digest,
                size_bytes=stat.st_size,
                kind=KIND_ARTIFACT,
                run_id=run_id,
                relpath=relpath,
            )
        )
    return jobs


def _manifest_digests(run_dir: Path) -> tuple[dict[str, str], float | None]:
    """The manifest's recorded output digests, and when the manifest was written."""
    path = run_dir / "run_manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        written = path.stat().st_mtime
    except (OSError, json.JSONDecodeError):
        return {}, None
    recorded = manifest.get("output_file_digests") if isinstance(manifest, dict) else None
    if not isinstance(recorded, dict):
        return {}, written
    return {
        name: digest
        for name, digest in recorded.items()
        if isinstance(digest, str) and _is_sha256_hex(digest)
    }, written


def _recorded_digest(
    digests: dict[str, str], manifest_mtime: float | None, path: Path, relpath: str
) -> str | None:
    """The manifest's digest for this file, where it still describes it.

    Manifest v5 records digests but no sizes, so freshness is judged by time:
    the manifest is the last thing a run writes, and a file rewritten after it
    has drifted from what was recorded.
    """
    digest = digests.get(relpath)
    if digest is None or manifest_mtime is None:
        return None
    if path.stat().st_mtime > manifest_mtime:
        return None
    return digest


def _is_sha256_hex(text: str) -> bool:
    return len(text) == 64 and all(c in "0123456789abcdef" for c in text)


# --- probing --------------------------------------------------------------------


class ProbeTransport(Protocol):
    """The two bulk lookups one badge refresh makes on a registry client."""

    def archive_probe(self, sha256s: Sequence[str]) -> dict[str, Any]: ...

    def archive_runs_probe(self, run_ids: Sequence[str]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ArchiveStates:
    """What the registry holds right now, keyed by clip id and by run id.

    Built from a live probe and never stored: a badge painted from anything
    older would claim content is safe on a server that may no longer hold it.
    """

    videos: dict[str, str] = field(default_factory=dict)
    runs: dict[str, str] = field(default_factory=dict)


def probe_archive_states(
    client: ProbeTransport,
    videos: Sequence[VideoAsset],
    runs: Sequence[RunRecord],
) -> ArchiveStates:
    """Ask the registry what it holds of these clips and runs, in two calls.

    A clip with no recorded digest cannot be asked about, so it stays unknown
    rather than being hashed here: this runs on every list refresh.
    """
    hashes = {str(video.id): video.sha256 for video in videos if video.sha256}
    blob_states: Mapping[str, Any] = {}
    if hashes:
        answer = client.archive_probe(sorted(set(hashes.values())))
        found = answer.get("states")
        blob_states = found if isinstance(found, Mapping) else {}
    video_states = {
        video_id: _clip_state(blob_states.get(digest))
        for video_id, digest in hashes.items()
    }
    run_ids = [str(run.id) for run in runs]
    run_states: dict[str, str] = {}
    if run_ids:
        answer = client.archive_runs_probe(run_ids)
        found = answer.get("states")
        counts = found if isinstance(found, Mapping) else {}
        run_states = {run_id: _run_state(counts.get(run_id)) for run_id in run_ids}
    return ArchiveStates(videos=video_states, runs=run_states)


def _clip_state(state: object) -> str:
    """One blob's probe entry as a badge state. Absent means never offered."""
    if not isinstance(state, Mapping):
        return STATE_UNKNOWN
    status = state.get("status")
    if status == STATUS_COMPLETE:
        return STATE_ARCHIVED
    if status == STATUS_FAILED:
        return STATE_FAILED
    return STATE_PENDING


def _run_state(counts: object) -> str:
    """One run's artefact tallies as a badge state. Absent means never offered."""
    if not isinstance(counts, Mapping):
        return STATE_UNKNOWN
    artifacts = int(counts.get("artifacts") or 0)
    complete = int(counts.get("complete") or 0)
    failed = int(counts.get("failed") or 0)
    if artifacts > 0 and complete == artifacts:
        return STATE_ARCHIVED
    # Failure first: a run part-archived with one refused blob needs acting on,
    # and "partial" reads as merely unfinished.
    if failed > 0:
        return STATE_FAILED
    if complete > 0:
        return STATE_PARTIAL
    return STATE_PENDING if artifacts > 0 else STATE_UNKNOWN


# --- executing ------------------------------------------------------------------


def run_archive(
    client: ArchiveTransport,
    jobs: Sequence[ArchiveJob],
    progress: ProgressFn,
    cancel_event: Any = None,
) -> ArchiveReport:
    """One pass over the queue. A job that fails is recorded and the rest still run."""
    report = ArchiveReport()
    total = len(jobs)
    for done, job in enumerate(jobs):
        if cancel_event is not None and cancel_event.is_set():
            break
        progress(f"Archiving {job.label}…", done, total)
        try:
            _send_one(client, job, report, progress, done, total)
        except Exception as exc:
            logger.warning("Archive of %s failed: %s", job.label, exc)
            report.failed.append((job.label, str(exc)))
    return report


def _send_one(
    client: ArchiveTransport,
    job: ArchiveJob,
    report: ArchiveReport,
    progress: ProgressFn,
    done: int,
    total: int,
) -> None:
    answer = client.archive_initiate(job.initiate_payload())
    status = answer.get("status")
    if status == STATUS_COMPLETE:
        report.already += 1
        return
    if status == STATUS_UPLOADED:
        # Assembled already, by whoever: the server is re-hashing it now.
        report.uploading_verification += 1
        return
    part_size = int(answer.get("part_size_bytes") or 0)
    part_urls = answer.get("part_urls") or []
    if status != STATUS_PENDING or part_size < 1:
        raise SyncError(f"The registry answered an unusable upload state ({status}).")
    etags: list[tuple[int, str]] = []
    with job.path.open("rb") as handle:
        for sent, part in enumerate(part_urls):
            number = int(part["part_number"])
            progress(
                f"Archiving {job.label} (part {sent + 1} of {len(part_urls)})…",
                done,
                total,
            )
            # Only the missing parts were presigned, so the offset comes from
            # the part number rather than from read position.
            handle.seek((number - 1) * part_size)
            etags.append((number, upload_part(str(part["url"]), handle.read(part_size))))
    parts = [
        {"part_number": number, "etag": etag} for number, etag in sorted(etags)
    ]
    client.archive_complete(str(answer["object_id"]), parts)
    report.archived += 1
