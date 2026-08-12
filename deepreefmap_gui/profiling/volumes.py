"""Per-drive storage accounting for the survey: clips in, outputs out, what is left.

Only the volumes the survey actually names are reported. Enumerating the
machine's disks would put a user's unrelated drives on screen and would stat
mounts the app has no business touching.

Byte counts come from the caller's records, not from the filesystem: this
module never stats a file. Callers pass only clips whose file is present, so a
row for a clip on an unplugged drive contributes nothing to that drive.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from deepreefmap_gui.survey.store import resolved_path


class DiskUsage(Protocol):
    """The part of `shutil.disk_usage`'s result this module reads."""

    @property
    def total(self) -> int: ...

    @property
    def free(self) -> int: ...


UsageFn = Callable[[str], DiskUsage]
IsMountFn = Callable[[str], bool]


@dataclass(frozen=True, slots=True)
class VolumeUsage:
    """One drive, split into the segments a storage bar draws."""

    root: str
    label: str
    total_bytes: int
    free_bytes: int
    video_bytes: int
    output_bytes: int
    unmeasured_items: int = 0

    @property
    def other_used_bytes(self) -> int:
        """Space in use by everything that is not ours, never below zero.

        Recorded sizes drift past what the volume reports: a row survives the
        file it describes being deleted. A negative segment would paint
        backwards, so the drift is absorbed here instead.
        """
        return max(0, self.total_bytes - self.free_bytes - self.video_bytes - self.output_bytes)


# How full is too full. A drive is judged on whichever of the two readings is
# worse, because neither alone describes a field laptop: 90% of a 4 TB external
# still holds a hundred passes, and 60% of a 128 GB system disk holds two.
FULLNESS_OK, FULLNESS_TIGHT, FULLNESS_FULL = "ok", "tight", "full"

TIGHT_PERCENT, FULL_PERCENT = 85.0, 95.0

# The floor a model download already refuses to start under
# (models/cache.py::_MIN_FREE_BYTES), restated rather than imported: that module
# pulls huggingface_hub, and this one is the app's import-light accounting.
MIN_FREE_BYTES = 10 * 1024**3

# Four passes at simple/setup.py::ROUGH_PASS_BYTES, which is about a session.
SESSION_FREE_BYTES = 12 * 1024**3


def used_percent(volume: VolumeUsage) -> float:
    """How much of the drive is in use, 0 when it reports no size at all."""
    if volume.total_bytes <= 0:
        return 0.0
    return 100.0 * (volume.total_bytes - volume.free_bytes) / volume.total_bytes


def fullness(volume: VolumeUsage) -> str:
    """Whether this drive is fine, getting tight, or about to stop a run."""
    used = used_percent(volume)
    if used >= FULL_PERCENT or volume.free_bytes < MIN_FREE_BYTES:
        return FULLNESS_FULL
    if used >= TIGHT_PERCENT or volume.free_bytes < SESSION_FREE_BYTES:
        return FULLNESS_TIGHT
    return FULLNESS_OK


def _drive_root(path: str) -> str | None:
    """The `C:\\`-style root of `path`, or None where the path names no drive.

    Windows volume identity comes from the drive, as `st_dev` does not identify
    one there. `splitdrive` finds nothing in a POSIX path, which leaves those to
    the mount walk and keeps this branch testable on either platform.
    """
    drive = os.path.splitdrive(path)[0]
    if not drive:
        return None
    return drive + os.sep


def volume_root(path: str, *, ismount: IsMountFn = os.path.ismount) -> str | None:
    """The volume `path` sits on, or None when it cannot be determined.

    `ismount` is injected so tests describe a mount table rather than the host's.
    """
    resolved = resolved_path(path)
    if resolved is None:
        return None
    drive = _drive_root(resolved)
    if drive is not None:
        return drive
    current = resolved
    while True:
        if ismount(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def volume_for_path(
    path: str,
    *,
    usage: UsageFn = shutil.disk_usage,
    ismount: IsMountFn = os.path.ismount,
) -> VolumeUsage | None:
    """The drive `path` sits on, with nothing of the survey's counted onto it.

    For a drive that matters to the app without the survey having put anything
    there yet -- the output root on a laptop set up this morning. The segments
    are zero, which is the truth: the space in use is somebody else's.
    """
    root = volume_root(path, ismount=ismount)
    if root is None:
        return None
    try:
        du = usage(root)
        total, free = int(du.total), int(du.free)
    except OSError:
        return None
    return VolumeUsage(
        root=root,
        label=_label_for(root),
        total_bytes=total,
        free_bytes=free,
        video_bytes=0,
        output_bytes=0,
    )


@dataclass
class _Tally:
    video_bytes: int = 0
    output_bytes: int = 0
    unmeasured_items: int = 0


def _label_for(root: str) -> str:
    """A caption short enough to sit under a thin bar."""
    drive = os.path.splitdrive(root)[0]
    if drive:
        return drive
    return os.path.basename(root.rstrip("/\\")) or root


def group_by_volume(
    videos: Iterable[tuple[str, int | None]],
    outputs: Iterable[tuple[str, int | None]],
    *,
    usage: UsageFn = shutil.disk_usage,
    ismount: IsMountFn = os.path.ismount,
) -> list[VolumeUsage]:
    """Sum recorded clip and output sizes onto the volumes that hold them.

    `videos` are (clip path, size) and `outputs` are (run directory, size). A
    path is counted once however many times it is listed, so a chaptered GoPro
    recording named per chapter, or a mount reached by a symlink, does not
    double up. A size of None counts as zero and raises `unmeasured_items`, so
    a caption can say the total is partial rather than claim an empty drive.

    Volumes are ordered by root, so a bar keeps its place on screen as the
    survey grows. A path whose volume cannot be determined is dropped, as is a
    volume whose `usage` call raises OSError: a dead network mount must not
    take the window down with it.
    """
    tallies: dict[str, _Tally] = {}
    seen: set[str] = set()

    for paths, is_video in ((videos, True), (outputs, False)):
        for path, size in paths:
            resolved = resolved_path(path)
            if resolved is None or resolved in seen:
                continue
            root = volume_root(resolved, ismount=ismount)
            if root is None:
                continue
            seen.add(resolved)
            tally = tallies.setdefault(root, _Tally())
            if size is None:
                tally.unmeasured_items += 1
            elif is_video:
                tally.video_bytes += size
            else:
                tally.output_bytes += size

    volumes = []
    for root in sorted(tallies):
        tally = tallies[root]
        try:
            du = usage(root)
            total, free = int(du.total), int(du.free)
        except OSError:
            continue
        volumes.append(
            VolumeUsage(
                root=root,
                label=_label_for(root),
                total_bytes=total,
                free_bytes=free,
                video_bytes=tally.video_bytes,
                output_bytes=tally.output_bytes,
                unmeasured_items=tally.unmeasured_items,
            )
        )
    return volumes
