"""Small frames pulled out of a video file, for hover previews.

Written to disk rather than kept as pixmaps because the one caller is a Qt rich
text tooltip, and Qt's rich text can only reach an image through a URL. The
files go in a temporary directory that goes with the process, so nothing is left
behind and nothing is carried between sessions.

Decoding runs on one worker thread reading a single pending slot rather than a
queue: dragging the cursor down a list of sections should cost one decode, not
thirty. A small LRU keeps a second hover over the same section free.
"""

from __future__ import annotations

import hashlib
import tempfile
import threading
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

import cv2
from PySide6.QtCore import QObject, Signal

# Wide enough to tell coral from sand, narrow enough that three of them side by
# side still make a tooltip rather than a window. Qt lays a rich tooltip out at
# no more than two thirds of the screen's width and clips the rest, so three of
# these plus their spacing has to stay inside that: ~630px, which needs a screen
# wider than 950.
THUMB_WIDTH = 200
_JPEG_QUALITY = 82

# Three frames a section, and a handful of sections hovered in a row.
_CACHE_ENTRIES = 64


def _frame_key(path: str, t_s: float) -> str:
    """A file name for one frame of one clip. A digest for naming, not for trust."""
    raw = f"{path}|{t_s:.2f}|{THUMB_WIDTH}".encode()
    return hashlib.sha1(raw, usedforsecurity=False).hexdigest()


class FrameGrabber(QObject):
    """Decodes frames off the GUI thread and hands back paths to JPEGs."""

    # The request key it was asked for, and one path per requested point, in
    # order, with None where a frame could not be read.
    frames_ready = Signal(str, list)

    def __init__(self) -> None:
        super().__init__()
        self._dir = tempfile.TemporaryDirectory(prefix="deepreefmap-frames-")
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, str] = OrderedDict()
        # A file that will not decode is not worth asking about on every hover.
        self._failed: set[str] = set()
        self._pending: tuple[str, list[tuple[str, float]]] | None = None
        self._wake = threading.Event()
        self._worker: threading.Thread | None = None

    def cached(self, points: list[tuple[str, float]]) -> list[str] | None:
        """Every frame of a request, if they are all already on disk."""
        with self._lock:
            found = []
            for path, t_s in points:
                hit = self._cache.get(_frame_key(path, t_s))
                if hit is None:
                    return None
                self._cache.move_to_end(_frame_key(path, t_s))
                found.append(hit)
            return found

    def request(self, key: str, points: list[tuple[str, float]]) -> list[str] | None:
        """The frames now if they are cached, else None and ``frames_ready`` later.

        Only the newest request survives: an older one still waiting is dropped
        rather than decoded for a row the cursor has already left.
        """
        if not points:
            return None
        hit = self.cached(points)
        if hit is not None:
            return hit
        with self._lock:
            if all(path in self._failed for path, _ in points):
                return None
            self._pending = (key, list(points))
            self._ensure_worker()
            self._wake.set()
        return None

    def _ensure_worker(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(
                target=self._serve, daemon=True, name="frame-grab"
            )
            self._worker.start()

    def _serve(self) -> None:
        while True:
            self._wake.wait()
            with self._lock:
                self._wake.clear()
                job, self._pending = self._pending, None
            if job is None:
                continue
            key, points = job
            frames = self._decode(points)
            if any(frames):
                self.frames_ready.emit(key, frames)

    def _decode(self, points: list[tuple[str, float]]) -> list[str | None]:
        """One capture per file, seeked to each time asked of it.

        Opening the file is the expensive part, so the three frames of a section
        cut from one clip pay for it once.
        """
        frames: list[str | None] = [None] * len(points)
        by_path: dict[str, list[int]] = {}
        for index, (path, _t_s) in enumerate(points):
            by_path.setdefault(path, []).append(index)
        for path, indices in by_path.items():
            with self._lock:
                if path in self._failed:
                    continue
            cap = cv2.VideoCapture(path)
            if not cap.isOpened():
                with self._lock:
                    self._failed.add(path)
                continue
            try:
                for index in indices:
                    frames[index] = self._grab(cap, path, points[index][1])
            finally:
                cap.release()
        return frames

    def _grab(self, cap: cv2.VideoCapture, path: str, t_s: float) -> str | None:
        key = _frame_key(path, t_s)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
        # cv2's ffmpeg backend seeks to the prior keyframe and decodes forward,
        # so the frame is time-accurate at up to one GOP of decode cost.
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t_s) * 1000.0)
        ok, frame = cap.read()
        if not ok or frame is None:
            return None
        height, width = frame.shape[:2]
        if width > THUMB_WIDTH:
            scaled = round(height * THUMB_WIDTH / width)
            frame = cv2.resize(
                frame, (THUMB_WIDTH, max(1, scaled)), interpolation=cv2.INTER_AREA
            )
        out = Path(self._dir.name) / f"{key}.jpg"
        # BGR straight out of the decoder is what imwrite expects, so the frame
        # never needs converting for the file.
        if not cv2.imwrite(str(out), frame, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY]):
            return None
        self._remember(key, str(out))
        return str(out)

    def _remember(self, key: str, jpeg: str) -> None:
        with self._lock:
            self._cache[key] = jpeg
            self._cache.move_to_end(key)
            while len(self._cache) > _CACHE_ENTRIES:
                _old_key, old = self._cache.popitem(last=False)
                Path(old).unlink(missing_ok=True)


@lru_cache(maxsize=None)
def shared_frame_grabber() -> FrameGrabber:
    """The one grabber, so its cache and its worker are shared by every row."""
    return FrameGrabber()
