"""Builders for the survey fixtures shared by tests/survey/ and tests/gui/.

Not a conftest: these are plain functions, imported from both directories so the
same transect coordinates, video hash and run manifest describe the same thing
everywhere. Previously each test file carried its own copy, and the two run
manifest writers had drifted into being character-for-character identical.
"""

from __future__ import annotations

import json
from pathlib import Path

from deepreefmap_gui.survey.models import RunRecord, Transect, TransectPass, VideoAsset
from deepreefmap_gui.survey.models.convert import survey_manifest_block
from deepreefmap_gui.survey.store import SurveyStore

# One reef, one clip, reused everywhere. length_m is the tape reading and is
# deliberately independent of the endpoints (they are ~77 m apart), which is what
# the real workflow produces: the tape is measured, the fixes are GPS.
VIDEO_HASH = "ab" * 16
VIDEO_NAME = "GX010001.MP4"
VIDEO_PATH = "/data/GX010001.MP4"


def make_transect(name: str = "T1", **overrides) -> Transect:
    return Transect(**{
        "name": name,
        "start_lat": -17.5,
        "start_lon": 177.1,
        "end_lat": -17.5005,
        "end_lon": 177.1005,
        "length_m": 50.0,
        **overrides,
    })


def make_video(content_hash: str | None = VIDEO_HASH, **overrides) -> VideoAsset:
    return VideoAsset(**{
        "file_name": VIDEO_NAME,
        "path": VIDEO_PATH,
        "hash": content_hash,
        **overrides,
    })


def write_run(root: Path, dir_name: str, **overrides) -> Path:
    """Write a run directory with a manifest, as the pipeline leaves one."""
    manifest = {
        "name": None,
        "mode": "semantic",
        "input_videos": [VIDEO_PATH],
        "video_hashes": [VIDEO_HASH],
        "run_timestamp": "2026-07-01T10:00:00+00:00",
        "begin_s": 0.0,
        "end_s": 60.0,
        "run_duration_s": 120.0,
        "semantic_reference_points": 1_000_000,
    }
    manifest.update(overrides)
    run_dir = root / dir_name
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest))
    return run_dir


def seed_pass(store: SurveyStore, direction: str = "forward", transect: Transect | None = None):
    """transect -> video -> pass, the chain every survey row hangs off."""
    transect = transect or make_transect()
    if store.get_transect(transect.id) is None:
        store.add_transect(transect)
    video = store.upsert_video(make_video())
    pass_ = TransectPass(
        transect_id=transect.id, video_id=video.id, begin_s=0.0, end_s=60.0, direction=direction
    )
    store.add_pass(pass_)
    return transect, video, pass_


def seed_survey_run(store: SurveyStore, root: Path, dir_name: str, transect: Transect | None = None):
    """A succeeded run, in the database and on disk with a matching manifest."""
    transect, _video, pass_ = seed_pass(store, transect=transect)
    run = RunRecord(pass_id=pass_.id, run_dir_name=dir_name, status="succeeded")
    store.add_run(run)
    write_run(root, dir_name, survey=survey_manifest_block(run, pass_, transect, None))
    return transect, pass_, run
