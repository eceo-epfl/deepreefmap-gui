"""The archive queue: what is planned, and how one pass over it runs.

Only the part-upload test reaches a socket, and it is a loopback server. The
registry is a fake object standing in for `SyncClient`, so the real plan builder
and the real executor run against real files.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from _factories import seed_pass

from deepreefmap_gui.survey.models import RunRecord, VideoAsset
from deepreefmap_gui.survey.store import SurveyStore
from deepreefmap_gui.sync import archive
from deepreefmap_gui.sync.archive import (
    ArchiveJob,
    ArchiveReport,
    archive_plan,
    run_archive,
    sha256_file,
    upload_part,
)
from deepreefmap_gui.sync.client import ServerUnreachableError, SyncError

CLIP_BYTES = b"reef footage " * 64


@pytest.fixture
def store(tmp_path) -> SurveyStore:
    return SurveyStore(tmp_path / "out" / "survey.db")


@pytest.fixture
def out_root(store) -> Path:
    return store.path.parent


def add_clip(store, tmp_path, name="GX010001.MP4", content=CLIP_BYTES, sha256=None):
    path = tmp_path / name
    path.write_bytes(content)
    asset = VideoAsset(file_name=name, path=str(path), sha256=sha256)
    store.upsert_video(asset)
    return path, asset


def add_succeeded_run(store, out_root, dir_name="run-1", status="succeeded"):
    _, _, pass_ = seed_pass(store)
    run = RunRecord(pass_id=pass_.id, run_dir_name=dir_name, status=status)
    store.add_run(run)
    run_dir = out_root / dir_name
    run_dir.mkdir(parents=True, exist_ok=True)
    return run, run_dir


def refuse_to_hash(monkeypatch, *, unless=()):
    """Fail any hashing the test says must not happen."""
    real = archive.sha256_file

    def hasher(path):
        if path.name in unless:
            return real(path)
        raise AssertionError(f"{path.name} must not be re-hashed")

    monkeypatch.setattr(archive, "sha256_file", hasher)


# --- hashing ------------------------------------------------------------------


def test_sha256_file_streams_to_lowercase_hex(tmp_path) -> None:
    path = tmp_path / "clip.bin"
    path.write_bytes(CLIP_BYTES)

    digest = sha256_file(path)

    assert digest == hashlib.sha256(CLIP_BYTES).hexdigest()
    assert digest == digest.lower() and len(digest) == 64


# --- planning: videos -----------------------------------------------------------


def test_a_video_without_a_digest_gets_one_computed_and_stored(store, out_root, tmp_path):
    _, asset = add_clip(store, tmp_path)

    jobs = archive_plan(store, out_root)

    expected = hashlib.sha256(CLIP_BYTES).hexdigest()
    assert [(job.kind, job.sha256, job.size_bytes) for job in jobs] == [
        ("video", expected, len(CLIP_BYTES))
    ]
    assert store.get_video(asset.id).sha256 == expected


def test_a_video_that_knows_its_digest_is_not_rehashed(store, out_root, tmp_path, monkeypatch):
    recorded = "ab" * 32
    add_clip(store, tmp_path, sha256=recorded)
    refuse_to_hash(monkeypatch)

    jobs = archive_plan(store, out_root)

    assert [job.sha256 for job in jobs] == [recorded]


def test_a_clip_whose_file_is_gone_is_left_out(store, out_root, tmp_path):
    path, _ = add_clip(store, tmp_path)
    path.unlink()

    assert archive_plan(store, out_root) == []


# --- planning: run artefacts ------------------------------------------------------


def test_run_files_are_enumerated_with_their_relpaths(store, out_root):
    run, run_dir = add_succeeded_run(store, out_root)
    (run_dir / "run_manifest.json").write_text("{}")
    (run_dir / "ortho.png").write_bytes(b"png bytes")
    (run_dir / "frames").mkdir()
    (run_dir / "frames" / "0001.png").write_bytes(b"frame bytes")
    (run_dir / ".cache").mkdir()
    (run_dir / ".cache" / "scratch.bin").write_bytes(b"working state")
    (run_dir / ".hidden").write_bytes(b"working state")

    jobs = archive_plan(store, out_root)

    assert {job.relpath for job in jobs} == {
        "run_manifest.json",
        "ortho.png",
        "frames/0001.png",
    }
    assert all(job.kind == "artifact" for job in jobs)
    assert all(job.run_id == str(run.id) for job in jobs)
    assert all(job.label.startswith("run-1/") for job in jobs)


def test_only_succeeded_runs_are_offered(store, out_root):
    _, run_dir = add_succeeded_run(store, out_root, dir_name="run-failed", status="failed")
    (run_dir / "ortho.png").write_bytes(b"png bytes")

    assert archive_plan(store, out_root) == []


def test_manifest_digests_are_reused_without_rehashing(store, out_root, monkeypatch):
    _, run_dir = add_succeeded_run(store, out_root)
    recorded = "cd" * 32
    (run_dir / "ortho.png").write_bytes(b"png bytes")
    (run_dir / "run_manifest.json").write_text(
        json.dumps({"output_file_digests": {"ortho.png": recorded}})
    )
    refuse_to_hash(monkeypatch, unless=("run_manifest.json",))

    jobs = archive_plan(store, out_root)

    assert {job.relpath: job.sha256 for job in jobs}["ortho.png"] == recorded


def test_a_file_rewritten_after_the_manifest_is_rehashed(store, out_root):
    """The manifest is the last thing a run writes, so a newer file has drifted
    from the digest it recorded."""
    _, run_dir = add_succeeded_run(store, out_root)
    manifest = run_dir / "run_manifest.json"
    manifest.write_text(json.dumps({"output_file_digests": {"ortho.png": "cd" * 32}}))
    past = manifest.stat().st_mtime - 100
    os.utime(manifest, (past, past))
    (run_dir / "ortho.png").write_bytes(b"rewritten later")

    jobs = archive_plan(store, out_root)

    digests = {job.relpath: job.sha256 for job in jobs}
    assert digests["ortho.png"] == hashlib.sha256(b"rewritten later").hexdigest()


# --- executing -----------------------------------------------------------------


class FakeArchive:
    """Answers like the registry's archive routes, recording what was asked."""

    def __init__(self, answers):
        self._answers = dict(answers)
        self.initiated: list[dict] = []
        self.completed: list[tuple[str, list[dict]]] = []

    def archive_initiate(self, payload):
        self.initiated.append(dict(payload))
        answer = self._answers[payload["sha256"]]
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    def archive_complete(self, object_id, parts):
        self.completed.append((object_id, [dict(p) for p in parts]))
        return {"object_id": object_id, "status": "uploaded"}


def make_job(tmp_path, content=CLIP_BYTES, name="clip.mp4"):
    path = tmp_path / name
    path.write_bytes(content)
    return ArchiveJob(
        label=name,
        path=path,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        kind="video",
    )


def pending(object_id, part_size, part_numbers, parts_done=()):
    return {
        "object_id": object_id,
        "status": "pending",
        "upload_id": "u-1",
        "part_size_bytes": part_size,
        "parts_done": list(parts_done),
        "part_urls": [{"part_number": n, "url": f"https://blobs/{n}"} for n in part_numbers],
    }


def capture_uploads(monkeypatch):
    sent: list[tuple[str, bytes]] = []

    def fake_upload(url, chunk):
        sent.append((url, chunk))
        return f"etag-{url.rsplit('/', 1)[-1]}"

    monkeypatch.setattr(archive, "upload_part", fake_upload)
    return sent


def no_progress(text, done, total):
    pass


def test_content_the_server_already_holds_uploads_nothing(tmp_path, monkeypatch):
    job = make_job(tmp_path)
    client = FakeArchive({job.sha256: {"object_id": "o-1", "status": "complete"}})
    sent = capture_uploads(monkeypatch)

    report = run_archive(client, [job], no_progress)

    assert (report.archived, report.already, report.failed) == (0, 1, [])
    assert sent == [] and client.completed == []


def test_content_still_being_verified_is_counted_and_not_resent(tmp_path, monkeypatch):
    job = make_job(tmp_path)
    client = FakeArchive({job.sha256: {"object_id": "o-1", "status": "uploaded"}})
    sent = capture_uploads(monkeypatch)

    report = run_archive(client, [job], no_progress)

    assert report.uploading_verification == 1
    assert sent == [] and client.completed == []


def test_missing_parts_are_read_at_their_offsets(tmp_path, monkeypatch):
    """Scenario: a prior pass got part 1 up before the connection dropped.

    Expected behaviour: only the parts the server presigned travel, each read at
    the offset its part number names, not wherever the file handle sat.
    """
    part_size = 16
    content = bytes(range(48))
    job = make_job(tmp_path, content=content)
    client = FakeArchive(
        {job.sha256: pending("o-1", part_size, part_numbers=[2, 3], parts_done=[1])}
    )
    sent = capture_uploads(monkeypatch)

    report = run_archive(client, [job], no_progress)

    assert report.archived == 1
    assert sent == [
        ("https://blobs/2", content[16:32]),
        ("https://blobs/3", content[32:48]),
    ]
    assert client.completed == [
        ("o-1", [{"part_number": 2, "etag": "etag-2"}, {"part_number": 3, "etag": "etag-3"}])
    ]


def test_etags_reach_complete_in_part_order(tmp_path, monkeypatch):
    part_size = 16
    content = bytes(range(48))
    job = make_job(tmp_path, content=content)
    client = FakeArchive({job.sha256: pending("o-1", part_size, part_numbers=[3, 1, 2])})
    capture_uploads(monkeypatch)

    run_archive(client, [job], no_progress)

    _, parts = client.completed[0]
    assert [part["part_number"] for part in parts] == [1, 2, 3]


def test_a_failing_job_does_not_stop_the_rest(tmp_path, monkeypatch):
    """A flaky connection costs a retry, never the whole queue."""
    broken = make_job(tmp_path, name="broken.mp4", content=b"one")
    fine = make_job(tmp_path, name="fine.mp4", content=b"two")
    client = FakeArchive(
        {
            broken.sha256: ServerUnreachableError("Cannot reach the blob store"),
            fine.sha256: {"object_id": "o-2", "status": "complete"},
        }
    )
    capture_uploads(monkeypatch)

    report = run_archive(client, [broken, fine], no_progress)

    assert report.already == 1
    assert [(label, "blob store" in reason) for label, reason in report.failed] == [
        ("broken.mp4", True)
    ]


def test_a_cancelled_queue_stops_between_jobs(tmp_path, monkeypatch):
    job = make_job(tmp_path)
    client = FakeArchive({job.sha256: {"object_id": "o-1", "status": "complete"}})
    capture_uploads(monkeypatch)
    cancelled = threading.Event()
    cancelled.set()

    report = run_archive(client, [job], no_progress, cancel_event=cancelled)

    assert report == ArchiveReport()
    assert client.initiated == []


# --- the raw part upload ----------------------------------------------------------


@pytest.fixture
def blob_store():
    """A loopback stand-in for S3: answers PUTs with an ETag, records the bytes."""
    received: list[tuple[str, bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        etag = '"abc123"'

        def do_PUT(self):
            length = int(self.headers.get("Content-Length", 0))
            received.append((self.path, self.rfile.read(length)))
            self.send_response(200)
            if self.etag:
                self.send_header("ETag", self.etag)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield url, received, Handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_upload_part_sends_the_bytes_and_strips_the_etag_quotes(blob_store) -> None:
    url, received, _ = blob_store

    etag = upload_part(f"{url}/part-1?sig=xyz", b"part bytes")

    assert etag == "abc123"
    assert received == [("/part-1?sig=xyz", b"part bytes")]


def test_upload_part_refuses_an_answer_without_an_etag(blob_store) -> None:
    url, _, handler = blob_store
    handler.etag = ""

    with pytest.raises(SyncError, match="without an ETag"):
        upload_part(f"{url}/part-1", b"part bytes")


def test_upload_part_reports_an_unreachable_blob_store() -> None:
    # Port 1 on loopback: nothing listens, so this is a refusal, not a lookup.
    with pytest.raises(ServerUnreachableError, match="Cannot reach"):
        upload_part("http://127.0.0.1:1/part-1", b"part bytes")


# --- single-item plans ---


def test_a_single_clip_plan_carries_that_clip_and_nothing_else(store, out_root, tmp_path):
    _path, wanted = add_clip(store, tmp_path)
    add_clip(store, tmp_path, name="GX020001.MP4", content=b"other " * 40)
    add_succeeded_run(store, out_root)
    (out_root / "run-1" / "ortho.png").write_bytes(b"png")

    jobs = archive.archive_plan_for_video(store, wanted.id)

    assert [job.label for job in jobs] == ["GX010001.MP4"]
    assert jobs[0].kind == archive.KIND_VIDEO


def test_a_single_run_plan_carries_that_run_and_nothing_else(store, out_root, tmp_path):
    add_clip(store, tmp_path)
    run, run_dir = add_succeeded_run(store, out_root)
    (run_dir / "benthic_cover.json").write_text("{}")
    other = RunRecord(pass_id=run.pass_id, run_dir_name="run-2", status="succeeded")
    store.add_run(other)
    other_dir = out_root / "run-2"
    other_dir.mkdir()
    (other_dir / "ortho.png").write_bytes(b"png")

    jobs = archive.archive_plan_for_run(store, out_root, run.id)

    assert {job.run_id for job in jobs} == {str(run.id)}
    assert [job.relpath for job in jobs] == ["benthic_cover.json"]


def test_a_single_item_plan_for_an_unknown_id_is_empty(store, out_root):
    assert archive.archive_plan_for_video(store, "not-a-real-id") == []
    assert archive.archive_plan_for_run(store, out_root, "not-a-real-id") == []


# --- probing ---


class FakeProbeRegistry:
    def __init__(self, blob_states=None, run_states=None):
        self.blob_states = blob_states or {}
        self.run_states = run_states or {}
        self.asked_hashes: list[list[str]] = []
        self.asked_runs: list[list[str]] = []

    def archive_probe(self, sha256s):
        self.asked_hashes.append(list(sha256s))
        return {"states": self.blob_states}

    def archive_runs_probe(self, run_ids):
        self.asked_runs.append(list(run_ids))
        return {"states": self.run_states}


def test_probe_maps_every_state_a_badge_can_show(store, tmp_path):
    """Scenario: five clips in every server-side state, and four run shapes.

    Expected behaviour: complete reads as archived, failed as failed, anything
    in flight as pending, and a clip the registry has never been offered stays
    unknown, exactly as a clip with no digest does.
    """
    digests = {name: hashlib.sha256(name.encode()).hexdigest() for name in "abcd"}
    clips = [
        add_clip(store, tmp_path, name=f"{name}.MP4", sha256=digest)[1]
        for name, digest in digests.items()
    ]
    unhashed = add_clip(store, tmp_path, name="e.MP4")[1]
    registry = FakeProbeRegistry(
        blob_states={
            digests["a"]: {"status": "complete"},
            digests["b"]: {"status": "failed"},
            digests["c"]: {"status": "pending"},
        },
        run_states={
            "r-archived": {"artifacts": 3, "complete": 3, "failed": 0},
            "r-partial": {"artifacts": 3, "complete": 1, "failed": 0},
            "r-failed": {"artifacts": 3, "complete": 2, "failed": 1},
        },
    )
    runs = [
        RunRecord(pass_id=clips[0].id, run_dir_name=name, status="succeeded")
        for name in ("r1", "r2", "r3", "r4")
    ]
    ids = {run.run_dir_name: str(run.id) for run in runs}
    registry.run_states = {
        ids["r1"]: {"artifacts": 3, "complete": 3, "failed": 0},
        ids["r2"]: {"artifacts": 3, "complete": 1, "failed": 0},
        ids["r3"]: {"artifacts": 3, "complete": 2, "failed": 1},
    }

    states = archive.probe_archive_states(registry, [*clips, unhashed], runs)

    assert states.videos[str(clips[0].id)] == archive.STATE_ARCHIVED
    assert states.videos[str(clips[1].id)] == archive.STATE_FAILED
    assert states.videos[str(clips[2].id)] == archive.STATE_PENDING
    assert states.videos[str(clips[3].id)] == archive.STATE_UNKNOWN
    assert str(unhashed.id) not in states.videos, "no digest, nothing to ask about"
    assert states.runs[ids["r1"]] == archive.STATE_ARCHIVED
    assert states.runs[ids["r2"]] == archive.STATE_PARTIAL
    assert states.runs[ids["r3"]] == archive.STATE_FAILED
    assert states.runs[ids["r4"]] == archive.STATE_UNKNOWN
    assert registry.asked_hashes == [sorted(set(digests.values()))]


def test_probe_makes_no_calls_with_nothing_to_ask(store):
    registry = FakeProbeRegistry()

    states = archive.probe_archive_states(registry, [], [])

    assert (states.videos, states.runs) == ({}, {})
    assert registry.asked_hashes == []
    assert registry.asked_runs == []
