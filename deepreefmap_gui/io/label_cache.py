"""Readers for a run directory's per-frame label caches.

The library writes labels as `.npy` (int32); runs produced under the branch-era
PNG label cache store uint8 `.png` instead. Both formats stay readable here so
existing field run directories keep loading after the library sheds its own
dual-suffix readers.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

_LABELS_SUFFIXES = (".png", ".npy")


def resolve_labels_path(labels_dir: Path, stem: str) -> Path | None:
    """Locate a frame's label cache, preferring PNG over `.npy`."""
    for suffix in _LABELS_SUFFIXES:
        path = labels_dir / f"{stem}{suffix}"
        if path.exists():
            return path
    return None


def read_labels_file(path: Path) -> np.ndarray | None:
    """Read one label map as uint8. Returns None when unreadable."""
    if path.suffix == ".npy":
        labels = np.load(path)
    else:
        labels = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if labels is None:
        return None
    return _as_uint8_labels(labels, path)


def _as_uint8_labels(labels: np.ndarray, path: Path) -> np.ndarray:
    """Narrow a label map to the uint8 cv2.resize expects.

    `.npy` caches widen the segmenters' uint8 output to int32, which cv2.resize
    rejects as CV_32S for anything but INTER_NEAREST.
    """
    if labels.dtype == np.uint8:
        return labels
    if labels.size:
        lo, hi = int(labels.min()), int(labels.max())
        if lo < 0 or hi > 255:
            raise ValueError(f"Label cache {path} holds class ids outside 0-255 ({lo}..{hi})")
    return labels.astype(np.uint8)
