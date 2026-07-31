"""Lazy frame access over a run directory's PNG caches.

The viewer holds a whole run's frames, so these are read on demand rather than
kept in RAM. Two things have to hold for that swap to be invisible: the PNG
round-trip must be lossless (it is the reason preprocessing writes PNG and not
JPEG), and the RGB/BGR conversion must be inverted on read.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from deepreefmap_gui.io.lazy_frames import (
    LazyFrameBatch,
    LazyPreparedFrame,
    RunDirFrameAccessor,
)

H, W = 5, 7
# Deliberately not 0..n-1: frame_index is the source video's frame number, and
# the accessor has to key files by it rather than by position.
FRAME_INDICES = (0, 7, 21)


@pytest.fixture
def run_dir(tmp_path):
    """A run dir holding the three PNG caches preprocessing writes."""
    rng = np.random.default_rng(0)
    written = {}
    for sub in ("frames", "labels", "masks"):
        (tmp_path / sub).mkdir()

    for pos, frame_index in enumerate(FRAME_INDICES):
        stem = f"{frame_index:08d}.png"
        rgb = rng.integers(0, 255, (H, W, 3), dtype=np.uint8)
        labels = rng.integers(0, 30, (H, W), dtype=np.uint8)
        mask = (rng.random((H, W)) > 0.5).astype(np.uint8) * 255
        # Preprocessing writes RGB through cvtColor(RGB2BGR); mirror that here so
        # the accessor's inverse conversion is actually under test.
        cv2.imwrite(str(tmp_path / "frames" / stem), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        cv2.imwrite(str(tmp_path / "labels" / stem), labels)
        cv2.imwrite(str(tmp_path / "masks" / stem), mask)
        written[pos] = (rgb, labels, mask)
    return tmp_path, written


@pytest.fixture
def accessor(run_dir):
    path, _written = run_dir
    return RunDirFrameAccessor(path, FRAME_INDICES, clip_counts=(2, 1), image_size=(W, H))


def test_accessor_reports_the_batch_shape(accessor):
    assert accessor.n_frames == len(FRAME_INDICES)
    assert accessor.clip_counts == (2, 1)
    assert accessor.image_size == (W, H)
    np.testing.assert_array_equal(accessor.frame_indices, FRAME_INDICES)


def test_reads_are_lossless_and_rgb_ordered(run_dir, accessor):
    _path, written = run_dir
    for pos, (rgb, labels, mask) in written.items():
        np.testing.assert_array_equal(accessor.get_image(pos), rgb)
        np.testing.assert_array_equal(accessor.get_labels(pos), labels)
        np.testing.assert_array_equal(accessor.get_mask(pos), mask)


def test_files_are_keyed_by_frame_index_not_position(run_dir, accessor):
    """Position 1 is frame 7, so it must read 00000007.png."""
    path, written = run_dir
    (path / "frames" / "00000007.png").unlink()
    with pytest.raises(FileNotFoundError, match="00000007"):
        accessor.get_image(1)
    # The neighbours are untouched.
    np.testing.assert_array_equal(accessor.get_image(0), written[0][0])


@pytest.mark.parametrize("missing", ["frames", "labels", "masks"])
def test_a_missing_artifact_names_the_file(run_dir, accessor, missing):
    """cv2.imread returns None rather than raising, so the check is ours to make."""
    path, _written = run_dir
    (path / missing / "00000000.png").unlink()
    getter = {"frames": accessor.get_image, "labels": accessor.get_labels, "masks": accessor.get_mask}
    with pytest.raises(FileNotFoundError, match=missing):
        getter[missing](0)


def test_lazy_frame_defers_reads_until_attribute_access(accessor, run_dir):
    _path, written = run_dir
    frame = LazyPreparedFrame(accessor, 2, FRAME_INDICES[2])

    assert frame.frame_index == 21
    # A lazy frame carries no paths; the eager one does. Downstream code checks
    # these for None, so they must exist rather than raise.
    assert frame.image_path is None and frame.labels_path is None and frame.mask_path is None

    np.testing.assert_array_equal(frame.image_rgb, written[2][0])
    np.testing.assert_array_equal(frame.labels, written[2][1])
    np.testing.assert_array_equal(frame.keep_mask, written[2][2])


def test_lazy_batch_presents_the_eager_frame_batch_interface(accessor, run_dir):
    _path, written = run_dir
    intrinsics = np.eye(3)
    batch = LazyFrameBatch(accessor, intrinsics)

    assert batch.image_size == (W, H)
    assert batch.clip_counts == (2, 1)
    assert batch.gravity_vectors is None
    assert batch.frame_indices == list(FRAME_INDICES)
    assert len(batch.frames) == len(FRAME_INDICES)

    for pos, (rgb, labels, mask) in written.items():
        np.testing.assert_array_equal(batch.images[pos], rgb)
        np.testing.assert_array_equal(batch.labels[pos], labels)
        np.testing.assert_array_equal(batch.masks[pos], mask)


def test_close_is_safe_to_call(accessor):
    """The accessor holds no handles, but the protocol requires the method."""
    accessor.close()
    accessor.close()


# --- pre-PNG label caches ---------------------------------------------------

def test_labels_fall_back_to_the_pre_png_npy_cache(tmp_path):
    """Runs predating the PNG label cache store .npy, widened to int32.

    Reached whenever an older run is opened, because the scene file no longer
    carries its own copy of the labels. A plain `{stem}.png` read returns None
    for these and the frame fails to load at all.
    """
    for sub in ("frames", "labels", "masks"):
        (tmp_path / sub).mkdir()
    labels = np.array([[1, 5], [22, 25]], dtype=np.int32)
    for frame_index in (0,):
        stem = f"{frame_index:08d}"
        cv2.imwrite(str(tmp_path / "frames" / f"{stem}.png"), np.zeros((2, 2, 3), np.uint8))
        cv2.imwrite(str(tmp_path / "masks" / f"{stem}.png"), np.zeros((2, 2), np.uint8))
        np.save(tmp_path / "labels" / f"{stem}.npy", labels)

    accessor = RunDirFrameAccessor(tmp_path, (0,), clip_counts=(1,), image_size=(2, 2))
    got = accessor.get_labels(0)

    np.testing.assert_array_equal(got, labels)
    # Narrowed on the way out: the viewer's colour LUT and cv2.resize both
    # expect the uint8 the PNG cache would have given.
    assert got.dtype == np.uint8


def test_a_png_label_cache_wins_over_a_stale_npy(tmp_path):
    """Both can exist after a re-run; the PNG is the current one."""
    for sub in ("frames", "labels", "masks"):
        (tmp_path / sub).mkdir()
    cv2.imwrite(str(tmp_path / "frames" / "00000000.png"), np.zeros((2, 2, 3), np.uint8))
    cv2.imwrite(str(tmp_path / "masks" / "00000000.png"), np.zeros((2, 2), np.uint8))
    np.save(tmp_path / "labels" / "00000000.npy", np.full((2, 2), 9, dtype=np.int32))
    cv2.imwrite(str(tmp_path / "labels" / "00000000.png"), np.full((2, 2), 4, dtype=np.uint8))

    accessor = RunDirFrameAccessor(tmp_path, (0,), clip_counts=(1,), image_size=(2, 2))

    assert accessor.get_labels(0).tolist() == [[4, 4], [4, 4]]


def test_a_missing_label_cache_names_the_frame_it_could_not_read(tmp_path):
    for sub in ("frames", "labels", "masks"):
        (tmp_path / sub).mkdir()
    cv2.imwrite(str(tmp_path / "frames" / "00000000.png"), np.zeros((2, 2, 3), np.uint8))
    cv2.imwrite(str(tmp_path / "masks" / "00000000.png"), np.zeros((2, 2), np.uint8))

    accessor = RunDirFrameAccessor(tmp_path, (0,), clip_counts=(1,), image_size=(2, 2))

    with pytest.raises(FileNotFoundError, match="00000000"):
        accessor.get_labels(0)
