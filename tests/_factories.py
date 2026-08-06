"""Builders for the fixtures more than one test directory needs.

Not a conftest: these are plain functions, imported wherever they are needed so
the same transect coordinates, the same HuggingFace cache layout and the same
scene shape describe the same thing everywhere. Anything used by a single file
stays in that file.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

import numpy as np
from deepreefmap.config.classes import ClassConfig, SemanticClass
from deepreefmap.pipeline.artifacts import FrameBatch, MappingSequenceResult, PreparedFrame
from deepreefmap.pointcloud.final_cloud_index import FinalCloudIndex

from deepreefmap_gui.survey.models import (
    BatchItem,
    RunRecord,
    SurveyBatch,
    Transect,
    TransectPass,
    VideoAsset,
)
from deepreefmap_gui.survey.models.convert import survey_manifest_block
from deepreefmap_gui.survey.store import SurveyStore

# --- survey ------------------------------------------------------------------

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


def seed_pass(
    store: SurveyStore,
    direction: str = "forward",
    transect: Transect | None = None,
    batch: SurveyBatch | None = None,
):
    """transect -> video -> pass, the chain every survey row hangs off."""
    transect = transect or make_transect()
    if store.get_transect(transect.id) is None:
        store.add_transect(transect)
    video = store.upsert_video(make_video())
    pass_ = TransectPass(
        transect_id=transect.id,
        video_id=video.id,
        begin_s=0.0,
        end_s=60.0,
        direction=direction,
        batch_id=batch.id if batch is not None else None,
    )
    store.add_pass(pass_)
    if batch is not None:
        store.add_batch_item(BatchItem(batch_id=batch.id, pass_id=pass_.id))
    return transect, video, pass_


def make_batch(store: SurveyStore, name: str = "2026-07-01") -> SurveyBatch:
    batch = SurveyBatch(name=name)
    store.add_batch(batch)
    return batch


def seed_survey_run(
    store: SurveyStore,
    root: Path,
    dir_name: str,
    transect: Transect | None = None,
    batch: SurveyBatch | None = None,
    **manifest_overrides,
):
    """A succeeded run, in the database and on disk with a matching manifest."""
    transect, _video, pass_ = seed_pass(store, transect=transect, batch=batch)
    run = RunRecord(
        pass_id=pass_.id,
        run_dir_name=dir_name,
        status="succeeded",
        batch_id=batch.id if batch is not None else None,
    )
    store.add_run(run)
    write_run(
        root,
        dir_name,
        survey=survey_manifest_block(run, pass_, transect, batch),
        **manifest_overrides,
    )
    return transect, pass_, run


# --- HuggingFace cache -------------------------------------------------------

CACHE_COMMIT = "0" * 40


def make_profile(
    *,
    gpu_name: str | None = "GPU",
    free: int = 50 * 1024**3,
    total_ram: int = 16 * 1024**3,
    vram: int | None = 8 * 1024**3,
):
    """A stand-in for probe_system's result, exposing what the setup rows read."""
    from deepreefmap_gui.profiling.system_probe import GPU_CUDA, GPU_NONE, GpuInfo

    gpu = (
        GpuInfo(GPU_CUDA, gpu_name, vram, vram)
        if gpu_name
        else GpuInfo(GPU_NONE, "CPU only", None, None)
    )
    return SimpleNamespace(gpu=gpu, disk_free_bytes=free, total_ram_bytes=total_ram)


def repo_commit(repo_id: str) -> str:
    """A commit hash unique to one repo.

    delete_revisions resolves a hash across the whole cache, so two repos sharing
    one are deleted together and which of them survives depends on scan order.
    """
    return hashlib.sha1(repo_id.encode()).hexdigest()


def write_cache_repo(cache_root, repo_id, files, *, commit=CACHE_COMMIT, use_symlinks=True):
    """Lay down an HF-cache repo: blobs/ + snapshots/<commit>/ + refs/main.

    Snapshot entries are relative symlinks into ../../blobs/<sha> (the real cache
    layout) unless use_symlinks=False, which writes real files (the Windows
    layout). Pass commit=repo_commit(repo_id) where several repos share a cache
    and must not share a revision.
    """
    repo_dir = cache_root / f"models--{repo_id.replace('/', '--')}"
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


# --- scenes ------------------------------------------------------------------

# The toy class table the scene tests share. Ids are the segmentation model's own,
# so they are not 0..n-1.
SCENE_CLASSES = {1: ("reef", (10, 20, 30)), 5: ("sand", (200, 200, 100))}


def make_classes_config(class_ids=(1,)) -> ClassConfig:
    return ClassConfig(
        classes=[
            SemanticClass(
                id=c, name=SCENE_CLASSES[c][0], color=SCENE_CLASSES[c][1], roles=frozenset()
            )
            for c in class_ids
        ],
        path=None,
    )


class Scene(NamedTuple):
    frame_batch: FrameBatch
    mapping: MappingSequenceResult
    cloud_index: FinalCloudIndex | None


def make_scene(
    *,
    frame_indices=(0, 1, 2),
    size=(6, 4),
    class_ids=(1,),
    clip_counts=None,
    points_by_class=None,
    seed=0,
) -> Scene:
    """The three objects a scene file is written from, at whatever shape a test needs.

    frame_indices are the source video's frame numbers, so they need not be
    contiguous. size is (width, height). points_by_class maps a class id to its
    point count and leaves cloud_index None when omitted, for the tests that let
    the writer build the index themselves. No pixels reach a scene file, so the
    images are filler and only the counts, indices and class ids are load-bearing.
    """
    rng = np.random.default_rng(seed)
    width, height = size
    frame_indices = tuple(frame_indices)
    n = len(frame_indices)
    frames = tuple(
        PreparedFrame(
            frame_index=index,
            image_rgb=rng.integers(0, 255, (height, width, 3), dtype=np.uint8),
            labels=rng.choice(class_ids, size=(height, width)).astype(np.uint8),
            keep_mask=(rng.random((height, width)) > 0.5).astype(np.uint8),
        )
        for index in frame_indices
    )
    intrinsics = np.array([[100.0, 0, width / 2], [0, 100.0, height / 2], [0, 0, 1]])
    frame_batch = FrameBatch(
        frames=frames,
        intrinsics=intrinsics,
        image_size=(width, height),
        clip_counts=clip_counts if clip_counts is not None else (n,),
    )
    mapping = MappingSequenceResult(
        frame_indices=np.asarray(frame_indices, dtype=np.int32),
        depth_maps=rng.random((n, height, width)).astype(np.float32),
        poses_w_c=np.repeat(np.eye(4)[None], n, axis=0),
        intrinsics=intrinsics,
        world_points=rng.random((n, height, width, 3)).astype(np.float32),
        confidence=rng.random((n, height, width)).astype(np.float32),
        scale_type="metric",
    )
    cloud_index = None
    if points_by_class is not None:
        counts = dict(points_by_class)
        cloud_index = FinalCloudIndex(
            frame_order=tuple(frame_indices),
            class_ids=tuple(counts),
            xyz_by_class={c: rng.random((k, 3)).astype(np.float32) for c, k in counts.items()},
            rgb_by_class={
                c: rng.integers(0, 255, (k, 3), dtype=np.uint8) for c, k in counts.items()
            },
            semrgb_by_class={
                c: rng.integers(0, 255, (k, 3), dtype=np.uint8) for c, k in counts.items()
            },
            conf_by_class={c: rng.random(k).astype(np.float32) for c, k in counts.items()},
            prefix_end_by_class={
                c: np.linspace(0, k, n, dtype=np.int64) for c, k in counts.items()
            },
        )
    return Scene(frame_batch, mapping, cloud_index)


# --- window stubs ------------------------------------------------------------


class FakeAccessor:
    """A FrameAccessor stand-in that records close()."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True
