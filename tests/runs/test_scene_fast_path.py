"""Loading a run through its scene file, against a real run directory.

The scene file used to be self-sufficient. It now carries only the cloud index,
the class table and the frame metadata, and pairs with the artifacts the pipeline
already wrote: frames from the PNG caches, mapping from mapping_outputs.npz. That
makes the run directory a hard dependency of the fast path rather than a
duplicate, so these drive the real thing on disk rather than a stub.

The run directory is read-only input. `test_the_load_writes_nothing_but_the_scene_file`
is the one that pins that, and it is the reason this change cannot corrupt a run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from deepreefmap.config.classes import ClassConfig, SemanticClass
from deepreefmap.pipeline.artifacts import FrameBatch, PreparedFrame
from deepreefmap.pointcloud.final_cloud_index import FinalCloudIndex

from deepreefmap_gui.io.scene_file import (
    SCENE_FILE_SUFFIX,
    SCHEMA_VERSION,
    find_scene_file,
    save_scene_file,
)
from deepreefmap_gui.runs.loaded_run import load_run

H, W = 4, 6
FRAME_INDICES = (0, 3, 9)  # not 0..n-1: frame_index is the source video's number
CLASS_ID = 1
MANIFEST = {"name": "reef north", "mode": "semantic", "mapping_backend": "loger_star"}


@pytest.fixture
def classes_config() -> ClassConfig:
    return ClassConfig(
        classes=[SemanticClass(id=CLASS_ID, name="reef", color=(10, 20, 30), roles=frozenset())],
        path=None,
    )


@pytest.fixture
def run_dir(tmp_path) -> Path:
    """A run directory as the pipeline leaves it: PNG caches plus the npz."""
    rng = np.random.default_rng(0)
    d = tmp_path / "20260101-000000"
    for sub in ("frames", "labels", "masks"):
        (d / sub).mkdir(parents=True)

    for frame_index in FRAME_INDICES:
        stem = f"{frame_index:08d}.png"
        rgb = rng.integers(0, 255, (H, W, 3), dtype=np.uint8)
        cv2.imwrite(str(d / "frames" / stem), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        cv2.imwrite(str(d / "labels" / stem), np.full((H, W), CLASS_ID, dtype=np.uint8))
        cv2.imwrite(str(d / "masks" / stem), np.full((H, W), 255, dtype=np.uint8))

    n = len(FRAME_INDICES)
    intrinsics = np.array([[100.0, 0, W / 2], [0, 100.0, H / 2], [0, 0, 1]])
    np.savez(
        d / "mapping_outputs.npz",
        frame_indices=np.array(FRAME_INDICES, dtype=np.int32),
        depth=rng.random((n, H, W)).astype(np.float32),
        poses_w_c=np.stack([np.eye(4) for _ in range(n)]).astype(np.float64),
        intrinsics=intrinsics,
        confidence=rng.random((n, H, W)).astype(np.float32),
        gravity_vectors=np.asarray([]),
        world_points=rng.random((n, H, W, 3)).astype(np.float32),
        scale_type=np.asarray("metric"),
    )
    (d / "run_manifest.json").write_text(json.dumps(MANIFEST))
    return d


def _write_scene(run_dir: Path, classes_config: ClassConfig, *, schema: int | None = None) -> Path:
    """A scene file over the fixture run, optionally stamped as an older schema."""
    n_points = 12
    rng = np.random.default_rng(1)
    fci = FinalCloudIndex(
        frame_order=FRAME_INDICES,
        class_ids=(CLASS_ID,),
        xyz_by_class={CLASS_ID: rng.random((n_points, 3)).astype(np.float32)},
        rgb_by_class={CLASS_ID: rng.integers(0, 255, (n_points, 3), dtype=np.uint8)},
        semrgb_by_class={CLASS_ID: rng.integers(0, 255, (n_points, 3), dtype=np.uint8)},
        conf_by_class={CLASS_ID: rng.random(n_points).astype(np.float32)},
        prefix_end_by_class={CLASS_ID: np.array([4, 8, 12], dtype=np.int64)},
    )
    frames = tuple(
        PreparedFrame(
            frame_index=i,
            image_rgb=np.zeros((H, W, 3), np.uint8),
            labels=np.zeros((H, W), np.uint8),
            keep_mask=np.zeros((H, W), np.uint8),
            image_path=None,
            labels_path=None,
            mask_path=None,
        )
        for i in FRAME_INDICES
    )
    fb = FrameBatch(
        frames=frames,
        intrinsics=np.eye(3),
        image_size=(W, H),
        clip_counts=(len(FRAME_INDICES),),
    )

    class _Mapping:
        scale_type = "metric"

    out = run_dir / ("scene" + SCENE_FILE_SUFFIX)
    save_scene_file(
        out,
        manifest=MANIFEST,
        classes_config=classes_config,
        mapping_result=_Mapping(),
        frame_batch=fb,
        final_cloud_index=fci,
        run_dir=run_dir,
    )
    if schema is not None:
        import zarr

        with zarr.ZipStore(str(out), mode="a") as store:
            zarr.open_group(store=store, mode="a").attrs["schema_version"] = schema
    return out


# --- the fast path ------------------------------------------------------


def test_the_fast_path_pairs_the_index_with_the_run_directory(run_dir, classes_config):
    """Index from the scene, frames from the PNGs, mapping from the npz."""
    _write_scene(run_dir, classes_config)

    loaded = load_run(run_dir, regenerate_scene_file=False)

    assert loaded.from_scene_file
    assert loaded.final_cloud_index is not None
    # Mapping came from the npz, which the scene file no longer duplicates.
    assert loaded.mapping_result.world_points is not None
    np.testing.assert_array_equal(loaded.mapping_result.frame_indices, FRAME_INDICES)
    # Frames read lazily off the PNG caches, keyed by source frame number.
    assert loaded.frame_batch.frame_indices == list(FRAME_INDICES)
    assert loaded.frame_batch.frames[0].image_rgb.shape == (H, W, 3)
    assert loaded.frame_batch.frames[1].labels.max() == CLASS_ID


def test_a_run_missing_its_mapping_npz_falls_back(run_dir, classes_config):
    """The scene alone can no longer stand in for the run directory, so a run
    stripped of its npz must take the slow path rather than half-load."""
    _write_scene(run_dir, classes_config)
    (run_dir / "mapping_outputs.npz").unlink()

    # The library loader's own error for a run missing its mapping artifact.
    with pytest.raises(RuntimeError, match="mapping_outputs.npz"):
        load_run(run_dir, regenerate_scene_file=False)


def test_the_load_writes_nothing_but_the_scene_file(run_dir, classes_config):
    """The pipeline's outputs are read-only input to the GUI.

    Checksums every artifact the pipeline wrote, so a load that mutated one --
    or a prune whose glob was too broad -- fails here rather than in the field.
    Uses a schema-1 scene deliberately: that is the load that rewrites and
    prunes, so it is the only one with the opportunity to damage a run.
    """
    _write_scene(run_dir, classes_config, schema=1)
    before = {
        p: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(run_dir.rglob("*"))
        if p.is_file() and not p.name.endswith(SCENE_FILE_SUFFIX)
    }
    assert before, "fixture wrote no pipeline artifacts"

    load_run(run_dir, regenerate_scene_file=False)

    after = {
        p: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(run_dir.rglob("*"))
        if p.is_file() and not p.name.endswith(SCENE_FILE_SUFFIX)
    }
    assert after == before


# --- upgrading an older scene ------------------------------------------


def test_an_older_scene_is_rewritten_in_place(run_dir, classes_config):
    """Schema 1 carried the frame pixels and the mapping arrays, 99% of its size.

    Everything the new file needs was just read out of the old one, so the
    upgrade is a rewrite rather than a rebuild, and it reclaims the rest the
    first time the run is opened.
    """
    _write_scene(run_dir, classes_config, schema=1)

    loaded = load_run(run_dir, regenerate_scene_file=False)

    assert loaded.from_scene_file, "the old file must still load, not be discarded"
    scene_path = find_scene_file(run_dir)
    assert scene_path is not None
    import zarr

    with zarr.ZipStore(str(scene_path), mode="r") as store:
        assert zarr.open_group(store=store, mode="r").attrs["schema_version"] == SCHEMA_VERSION


def test_the_upgrade_leaves_exactly_one_scene_file(run_dir, classes_config):
    """The name is derived from the manifest's run name, so a rewrite can land
    under a different one; without the prune both would sit there."""
    _write_scene(run_dir, classes_config, schema=1)
    stale = run_dir / ("an-older-name" + SCENE_FILE_SUFFIX)
    stale.write_bytes((run_dir / ("scene" + SCENE_FILE_SUFFIX)).read_bytes())

    load_run(run_dir, regenerate_scene_file=False)

    assert sorted(p.name for p in run_dir.glob("*" + SCENE_FILE_SUFFIX)) == [
        "reef_north" + SCENE_FILE_SUFFIX
    ]


def test_a_current_scene_is_not_rewritten(run_dir, classes_config):
    """The upgrade must not fire on every load; it would rewrite the file each
    time a run is opened."""
    path = _write_scene(run_dir, classes_config)
    before = path.stat().st_mtime_ns

    load_run(run_dir, regenerate_scene_file=False)

    assert path.exists(), "a current scene must keep its name"
    assert path.stat().st_mtime_ns == before
