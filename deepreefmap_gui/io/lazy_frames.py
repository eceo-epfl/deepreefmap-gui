"""Lazy FrameBatch backed by a run directory's PNG caches.

Lets the viewer hold file handles instead of the decoded frame stack, so the
~9 GiB of prepared frames is freed when the run scope exits instead of staying
pinned for the life of the window. Not tied to any archive format.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import cv2
import numpy as np

if TYPE_CHECKING:
    pass


class FrameAccessor(Protocol):
    """Read interface a LazyFrameBatch reads prepared frames through."""

    @property
    def n_frames(self) -> int: ...
    @property
    def frame_indices(self) -> np.ndarray: ...
    @property
    def clip_counts(self) -> tuple[int, ...]: ...
    @property
    def image_size(self) -> tuple[int, int]: ...
    def get_image(self, positional_index: int) -> np.ndarray: ...
    def get_labels(self, positional_index: int) -> np.ndarray: ...
    def get_mask(self, positional_index: int) -> np.ndarray: ...
    def close(self) -> None: ...


class RunDirFrameAccessor:
    """Read prepared frames on demand from a run directory's PNG caches.

    The PNG caches are lossless, so a reloaded array equals the one preprocessing
    held in RAM. This is the read path behind a scene file: the scene stores the
    cloud index and the frame metadata, the pixels stay here.

    The directory is treated as read-only; nothing on this path writes to it.
    """

    def __init__(
        self,
        run_dir: Path,
        frame_indices: Sequence[int],
        clip_counts: Sequence[int],
        image_size: tuple[int, int],
    ) -> None:
        self._frames_dir = run_dir / "frames"
        self._labels_dir = run_dir / "labels"
        self._masks_dir = run_dir / "masks"
        self._frame_indices = np.asarray(frame_indices, dtype=np.int64)
        self._clip_counts = tuple(int(c) for c in clip_counts)
        self._image_size = (int(image_size[0]), int(image_size[1]))

    @property
    def n_frames(self) -> int:
        return int(self._frame_indices.shape[0])

    @property
    def frame_indices(self) -> np.ndarray:
        return self._frame_indices

    @property
    def clip_counts(self) -> tuple[int, ...]:
        return self._clip_counts

    @property
    def image_size(self) -> tuple[int, int]:
        return self._image_size

    def _stem(self, positional_index: int) -> str:
        return f"{int(self._frame_indices[positional_index]):08d}"

    def _read(self, path: Path, flags: int) -> np.ndarray:
        array = cv2.imread(str(path), flags)
        if array is None:
            raise FileNotFoundError(f"Prepared frame artifact is missing or unreadable: {path}")
        return array

    def get_image(self, positional_index: int) -> np.ndarray:
        # Preprocess wrote RGB through cvtColor(RGB2BGR), so invert on read.
        bgr = self._read(self._frames_dir / f"{self._stem(positional_index)}.png", cv2.IMREAD_COLOR)
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    def get_labels(self, positional_index: int) -> np.ndarray:
        # Not a plain .png read: runs predating the PNG label cache store .npy,
        # widened to int32. The library owns both the suffix search and the
        # narrowing back to uint8, so call it rather than restate it here.
        from deepreefmap.pipeline.resume import read_labels_file, resolve_labels_path

        stem = self._stem(positional_index)
        path = resolve_labels_path(self._labels_dir, stem)
        labels = None if path is None else read_labels_file(path)
        if labels is None:
            raise FileNotFoundError(
                f"Prepared frame artifact is missing or unreadable: {self._labels_dir}/{stem}"
            )
        return labels

    def get_mask(self, positional_index: int) -> np.ndarray:
        return self._read(self._masks_dir / f"{self._stem(positional_index)}.png", cv2.IMREAD_GRAYSCALE)

    def close(self) -> None:
        # Nothing to release: every read opens and closes its own file. Present
        # because FrameAccessor declares it, so callers can close whatever they
        # hold without knowing which implementation it is.
        pass


class LazyPreparedFrame:
    """Duck-typed PreparedFrame that reads from a FrameAccessor on access."""

    __slots__ = ("_accessor", "_pos", "frame_index", "image_path", "labels_path", "mask_path")

    def __init__(self, accessor: FrameAccessor, positional_index: int, frame_index: int) -> None:
        self._accessor = accessor
        self._pos = positional_index
        self.frame_index = frame_index
        self.image_path = None
        self.labels_path = None
        self.mask_path = None

    @property
    def image_rgb(self) -> np.ndarray:
        return self._accessor.get_image(self._pos)

    @property
    def labels(self) -> np.ndarray:
        return self._accessor.get_labels(self._pos)

    @property
    def keep_mask(self) -> np.ndarray:
        return self._accessor.get_mask(self._pos)


class LazyFrameBatch:
    """Duck-typed FrameBatch backed by a FrameAccessor."""

    def __init__(self, accessor: FrameAccessor, intrinsics: np.ndarray) -> None:
        self._accessor = accessor
        self.intrinsics = intrinsics
        self.image_size = accessor.image_size
        self.clip_counts = accessor.clip_counts
        self.gravity_vectors: np.ndarray | None = None
        self.frames = tuple(
            LazyPreparedFrame(accessor, i, int(accessor.frame_indices[i]))
            for i in range(accessor.n_frames)
        )

    @property
    def frame_indices(self) -> list[int]:
        return [f.frame_index for f in self.frames]

    @property
    def images(self) -> list[np.ndarray]:
        return [f.image_rgb for f in self.frames]

    @property
    def labels(self) -> list[np.ndarray]:
        return [f.labels for f in self.frames]

    @property
    def masks(self) -> list[np.ndarray]:
        return [f.keep_mask for f in self.frames]
