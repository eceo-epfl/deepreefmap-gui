"""The .scene.zarr.zip quick-load format.

This is the cache that lets `view-run` skip the 1-2 minute reference-cloud
rebuild, so a silent format regression costs a minute per load and, worse, a
silently wrong cloud. The tests drive real zarr writes and reads on tmp_path --
no mocking -- and check the two guards that decide whether a cached scene is
trusted at all: the schema range and the source fingerprint.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from deepreefmap.config.classes import ClassConfig, SemanticClass
from deepreefmap.pipeline.artifacts import FrameBatch, MappingSequenceResult, PreparedFrame
from deepreefmap.pointcloud.final_cloud_index import FinalCloudIndex

from deepreefmap_gui.io.scene_file import (
    SCENE_FILE_SUFFIX,
    compute_source_fingerprint,
    find_scene_file,
    fingerprint_matches,
    load_scene_file,
    save_scene_file,
    scene_file_name,
)

H, W = 4, 6
N_FRAMES = 3
CLASS_IDS = (1, 5)


@pytest.fixture
def classes_config() -> ClassConfig:
    return ClassConfig(
        classes=[
            SemanticClass(id=1, name="reef", color=(10, 20, 30), roles=frozenset()),
            SemanticClass(id=5, name="sand", color=(200, 200, 100), roles=frozenset()),
        ],
        path=None,
    )


@pytest.fixture
def scene(classes_config):
    """A small but structurally complete scene: frames, mapping, cloud index."""
    rng = np.random.default_rng(0)

    frames = tuple(
        PreparedFrame(
            frame_index=i * 2,  # non-contiguous on purpose: indices are data, not positions
            image_rgb=rng.integers(0, 255, (H, W, 3), dtype=np.uint8),
            labels=rng.choice(CLASS_IDS, size=(H, W)).astype(np.uint8),
            keep_mask=(rng.random((H, W)) > 0.5).astype(np.uint8),
            image_path=None,
            labels_path=None,
            mask_path=None,
        )
        for i in range(N_FRAMES)
    )
    intrinsics = np.array([[100.0, 0, W / 2], [0, 100.0, H / 2], [0, 0, 1]])
    frame_batch = FrameBatch(
        frames=frames,
        intrinsics=intrinsics,
        image_size=(W, H),
        clip_counts=(2, 1),
        gravity_vectors=None,
    )

    mapping = MappingSequenceResult(
        frame_indices=np.array([f.frame_index for f in frames], dtype=np.int32),
        depth_maps=rng.random((N_FRAMES, H, W)).astype(np.float32),
        poses_w_c=np.stack([np.eye(4) for _ in range(N_FRAMES)]).astype(np.float64),
        intrinsics=intrinsics,
        world_points=rng.random((N_FRAMES, H, W, 3)).astype(np.float32),
        local_points=None,
        confidence=rng.random((N_FRAMES, H, W)).astype(np.float32),
        scale_type="metric",
        gravity_vectors=None,
    )

    counts = {1: 7, 5: 4}
    fci = FinalCloudIndex(
        frame_order=tuple(f.frame_index for f in frames),
        class_ids=CLASS_IDS,
        xyz_by_class={c: rng.random((n, 3)).astype(np.float32) for c, n in counts.items()},
        rgb_by_class={
            c: rng.integers(0, 255, (n, 3), dtype=np.uint8) for c, n in counts.items()
        },
        semrgb_by_class={
            c: rng.integers(0, 255, (n, 3), dtype=np.uint8) for c, n in counts.items()
        },
        conf_by_class={c: rng.random(n).astype(np.float32) for c, n in counts.items()},
        prefix_end_by_class={
            c: np.linspace(0, n, N_FRAMES, dtype=np.int64) for c, n in counts.items()
        },
    )
    return frame_batch, mapping, fci


@pytest.fixture
def run_dir(tmp_path) -> Path:
    """A run directory shaped like one the pipeline leaves behind."""
    d = tmp_path / "20260101-000000"
    (d / "frames").mkdir(parents=True)
    (d / "labels").mkdir()
    (d / "masks").mkdir()
    for i in range(N_FRAMES):
        for sub in ("frames", "labels", "masks"):
            (d / sub / f"{i:06d}.png").write_bytes(b"x" * (10 + i))
    (d / "run_manifest.json").write_text(json.dumps({"name": "reef north", "mode": "semantic"}))
    (d / "mapping_outputs.npz").write_bytes(b"npz" * 100)
    return d


def _save(path, scene, classes_config, manifest=None, run_dir=None, progress_cb=None):
    frame_batch, mapping, fci = scene
    save_scene_file(
        path,
        manifest=manifest if manifest is not None else {"name": "reef north", "mode": "semantic"},
        classes_config=classes_config,
        mapping_result=mapping,
        frame_batch=frame_batch,
        final_cloud_index=fci,
        run_dir=run_dir,
        progress_cb=progress_cb,
    )


# --- naming and discovery ---------------------------------------------------

@pytest.mark.parametrize(
    "manifest, dir_name, expected",
    [
        ({"name": "reef north"}, None, "reef_north"),
        ({"name": "  "}, "20260101-000000", "20260101-000000"),
        ({}, None, "scene"),
        ({"name": "///"}, None, "scene"),          # sanitised to nothing -> fallback
        ({"name": "a/b:c*d"}, None, "a_b_c_d"),    # path separators cannot survive
    ],
)
def test_scene_file_name_sanitises(manifest, dir_name, expected):
    run_dir = Path(dir_name) if dir_name else None
    assert scene_file_name(manifest, run_dir) == expected + SCENE_FILE_SUFFIX


def test_find_scene_file_prefers_current_suffix_over_legacy(tmp_path):
    (tmp_path / "old.drm.zarr.zip").write_bytes(b"")
    assert find_scene_file(tmp_path).name == "old.drm.zarr.zip"

    (tmp_path / ("new" + SCENE_FILE_SUFFIX)).write_bytes(b"")
    assert find_scene_file(tmp_path).name == "new" + SCENE_FILE_SUFFIX


def test_find_scene_file_returns_none_when_absent(tmp_path):
    assert find_scene_file(tmp_path) is None


# --- fingerprint ------------------------------------------------------------

def test_fingerprint_notices_every_kind_of_source_change(run_dir):
    base = compute_source_fingerprint(run_dir)
    assert fingerprint_matches(base, compute_source_fingerprint(run_dir))

    (run_dir / "run_manifest.json").write_text(json.dumps({"name": "renamed"}))
    assert not fingerprint_matches(base, compute_source_fingerprint(run_dir))

    (run_dir / "run_manifest.json").write_text(json.dumps({"name": "reef north", "mode": "semantic"}))
    (run_dir / "frames" / "999999.png").write_bytes(b"extra")
    assert not fingerprint_matches(base, compute_source_fingerprint(run_dir))


def test_fingerprint_ignores_mapping_mtime(run_dir):
    """mtime is recorded but deliberately not compared: a touch is not a change."""
    base = compute_source_fingerprint(run_dir)
    stale = {**base, "mapping_npz_mtime": base["mapping_npz_mtime"] + 1000.0}
    assert fingerprint_matches(stale, base)


def test_fingerprint_of_an_empty_dir_is_stable(tmp_path):
    fp = compute_source_fingerprint(tmp_path)
    assert fp["manifest_sha256"] == ""
    assert fp["frame_count"] == 0


# --- round trip -------------------------------------------------------------

def test_round_trip_preserves_every_section(tmp_path, scene, classes_config):
    frame_batch, mapping, fci = scene
    path = tmp_path / ("s" + SCENE_FILE_SUFFIX)
    _save(path, scene, classes_config)

    loaded = load_scene_file(path)
    assert loaded is not None

    assert loaded.manifest["name"] == "reef north"
    assert loaded.run_mode == "semantic"

    assert [c.id for c in loaded.classes_config.classes] == [1, 5]
    assert [c.name for c in loaded.classes_config.classes] == ["reef", "sand"]
    assert loaded.classes_config.id_to_color[1] == (10, 20, 30)

    mr = loaded.mapping_result
    np.testing.assert_array_equal(mr.frame_indices, mapping.frame_indices)
    np.testing.assert_allclose(mr.depth_maps, mapping.depth_maps)
    np.testing.assert_allclose(mr.poses_w_c, mapping.poses_w_c)
    np.testing.assert_allclose(mr.intrinsics, mapping.intrinsics)
    np.testing.assert_allclose(mr.confidence, mapping.confidence)
    np.testing.assert_allclose(mr.world_points, mapping.world_points)
    assert str(mr.scale_type) == str(mapping.scale_type)

    out = loaded.final_cloud_index
    assert out.frame_order == fci.frame_order
    assert tuple(out.class_ids) == CLASS_IDS
    for cid in CLASS_IDS:
        # The CSR split must hand back exactly the per-class arrays that went in.
        np.testing.assert_allclose(out.xyz_by_class[cid], fci.xyz_by_class[cid])
        np.testing.assert_array_equal(out.rgb_by_class[cid], fci.rgb_by_class[cid])
        np.testing.assert_array_equal(out.semrgb_by_class[cid], fci.semrgb_by_class[cid])
        np.testing.assert_allclose(out.conf_by_class[cid], fci.conf_by_class[cid])
        np.testing.assert_array_equal(out.prefix_end_by_class[cid], fci.prefix_end_by_class[cid])

    loaded.frame_accessor.close()


def test_frames_load_lazily_and_match(tmp_path, scene, classes_config):
    frame_batch, _mapping, _fci = scene
    path = tmp_path / ("s" + SCENE_FILE_SUFFIX)
    _save(path, scene, classes_config)

    acc = load_scene_file(path).frame_accessor
    try:
        assert acc.n_frames == N_FRAMES
        assert acc.image_size == (W, H)
        assert acc.clip_counts == (2, 1)
        np.testing.assert_array_equal(
            acc.frame_indices, [f.frame_index for f in frame_batch.frames]
        )
        for i, frame in enumerate(frame_batch.frames):
            np.testing.assert_array_equal(acc.get_image(i), frame.image_rgb)
            np.testing.assert_array_equal(acc.get_labels(i), frame.labels)
            np.testing.assert_array_equal(acc.get_mask(i), frame.keep_mask)
    finally:
        acc.close()


def test_progress_is_reported_for_each_stage(tmp_path, scene, classes_config):
    seen: list[str] = []
    path = tmp_path / ("s" + SCENE_FILE_SUFFIX)
    _save(path, scene, classes_config, progress_cb=lambda stage, c, t: seen.append(stage))

    assert {"scene_meta", "scene_frames", "scene_fci", "scene_done"} <= set(seen)
    assert seen[-1] == "scene_done"

    loaded_stages: list[str] = []
    load_scene_file(path, progress_cb=lambda s, c, t: loaded_stages.append(s)).frame_accessor.close()
    assert {"scene_open", "scene_classes", "scene_cloud_index", "scene_mapping"} <= set(loaded_stages)


def test_a_failing_progress_callback_does_not_abort_the_save(tmp_path, scene, classes_config):
    """The callback drives a progress bar; a UI error must not lose the file."""
    path = tmp_path / ("s" + SCENE_FILE_SUFFIX)

    def boom(stage, cur, tot):
        raise RuntimeError("the viewer went away")

    _save(path, scene, classes_config, progress_cb=boom)
    assert path.exists()
    load_scene_file(path).frame_accessor.close()


# --- staleness guards -------------------------------------------------------

def test_load_rejects_a_scene_whose_run_changed(tmp_path, scene, classes_config, run_dir):
    path = run_dir / ("s" + SCENE_FILE_SUFFIX)
    _save(path, scene, classes_config, run_dir=run_dir)

    loaded = load_scene_file(path, run_dir=run_dir)
    assert loaded is not None
    loaded.frame_accessor.close()

    # Reprocessing the run at a different fps changes the frame count.
    (run_dir / "frames" / "999999.png").write_bytes(b"a new frame")
    assert load_scene_file(path, run_dir=run_dir) is None


def test_load_without_a_run_dir_skips_the_fingerprint_check(tmp_path, scene, classes_config, run_dir):
    path = run_dir / ("s" + SCENE_FILE_SUFFIX)
    _save(path, scene, classes_config, run_dir=run_dir)
    (run_dir / "frames" / "999999.png").write_bytes(b"a new frame")

    loaded = load_scene_file(path)
    assert loaded is not None
    loaded.frame_accessor.close()


@pytest.mark.parametrize("version", [0, 99])
def test_load_rejects_schemas_outside_the_supported_range(tmp_path, scene, classes_config, version):
    import zarr

    path = tmp_path / ("s" + SCENE_FILE_SUFFIX)
    _save(path, scene, classes_config)

    # Rewrite the version in place, as a future (or ancient) build would have left it.
    store = zarr.ZipStore(str(path), mode="a")
    zarr.open_group(store=store, mode="a").attrs["schema_version"] = version
    store.close()

    assert load_scene_file(path) is None


def test_a_failed_save_leaves_no_partial_file(tmp_path, scene, classes_config):
    """A half-written scene that still loads would be worse than none at all."""
    frame_batch, mapping, _fci = scene
    path = tmp_path / ("s" + SCENE_FILE_SUFFIX)

    class Broken:
        """Passes the early sections, then fails inside _save_fci."""

        frame_order = (0, 2, 4)
        class_ids = (1,)
        xyz_by_class = {1: np.zeros((2, 3), dtype=np.float32)}

        def __getattr__(self, name):
            raise OSError(28, "No space left on device")

    with pytest.raises(OSError):
        save_scene_file(
            path,
            manifest={"name": "x"},
            classes_config=classes_config,
            mapping_result=mapping,
            frame_batch=frame_batch,
            final_cloud_index=Broken(),
        )

    assert not path.exists()
    assert not (path.parent / (path.name + ".tmp")).exists()
