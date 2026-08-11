"""Give every clip in a survey one row, and every row its content hash.

A clip's identity is its hash, but hashing needs the file to be readable, and in
the field it often is not: footage lives on a card that gets pulled, or on a
drive that comes back with a different letter. Rows written in that state carried
no hash, and nothing since would go back and add one, so one file could end up as
several clips. Browse then listed it several times and none of the entries knew
about the others' passes.

This is the repair. It is safe to run repeatedly and does nothing when there is
nothing to fix, which is what lets Browse simply call it on open.

Qt-free on purpose: the whole thing is a store and a filesystem, so it is tested
without a window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from deepreefmap_gui.survey.models.video_asset import VideoAsset, container_fields
from deepreefmap_gui.survey.store import SurveyStore, resolved_path
from deepreefmap_gui.survey.video_probe import probe_metadata

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RepairReport:
    """What the pass changed, so it can be reported rather than done silently."""

    hashed: int = 0
    merged: int = 0
    passes_moved: int = 0
    # Reading a container changes nothing the user chose, so it is counted for
    # the tests and never announced.
    probed: int = 0
    unreadable: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.hashed or self.merged)

    def summary(self) -> str:
        """One sentence, or empty when there is nothing worth saying."""
        if not self.changed:
            return ""
        parts = []
        if self.merged:
            parts.append(
                f"merged {self.merged} duplicate clip"
                f"{'' if self.merged == 1 else 's'}"
            )
        if self.hashed:
            parts.append(
                f"identified {self.hashed} clip{'' if self.hashed == 1 else 's'}"
            )
        sentence = "Video library: " + ", ".join(parts) + "."
        if self.passes_moved:
            sentence += f" {self.passes_moved} pass(es) repointed."
        return sentence


def backfill_hashes(store: SurveyStore) -> tuple[int, list[str]]:
    """Hash every clip that has no hash and whose file is still readable.

    imohash samples the file rather than reading it through, so this stays cheap
    on a library of 4 GB clips. A file that is not there is left alone and named
    in the returned list: a clip on an unplugged drive is a normal thing to have,
    not a row to delete.
    """
    hashed = 0
    unreadable: list[str] = []
    for video in store.list_videos():
        if video.hash:
            continue
        path = Path(video.path)
        if not path.is_file():
            unreadable.append(video.path)
            continue
        try:
            described = VideoAsset.from_path(path)
        except OSError as exc:
            logger.warning("Could not hash %s: %s", video.path, exc)
            unreadable.append(video.path)
            continue
        if not described.hash:
            unreadable.append(video.path)
            continue
        video.hash = described.hash
        if video.size_bytes is None:
            video.size_bytes = described.size_bytes
        if video.mtime is None:
            video.mtime = described.mtime
        store.update_video(video)
        hashed += 1
    return hashed, unreadable


def backfill_metadata(store: SurveyStore) -> int:
    """Read the container of every clip nothing has looked at yet.

    Rows written before the app read containers, and rows rebuilt from run
    manifests, carry no capture date and no gravity answer. ``probed_at`` is
    what marks them, so this runs once per clip and then never again.

    A clip whose file is not there keeps ``probed_at`` empty rather than being
    written off: an unplugged drive is a normal thing to have, and the answer is
    still available once it comes back.
    """
    probed = 0
    for video in store.list_videos():
        if video.probed_at:
            continue
        path = Path(video.path)
        if not path.is_file():
            continue
        meta = probe_metadata(path)
        # Through overlay_from rather than field by field, so a file that turns
        # out not to be a container cannot answer "nobody looked" over a reading
        # an earlier row already made of it.
        reading = VideoAsset(
            file_name=video.file_name,
            path=video.path,
            **container_fields(meta, video.mtime),  # type: ignore[arg-type]
        )
        video.overlay_from(reading)
        if video.duration_s is None:
            video.duration_s = meta.duration_s
        store.update_video(video)
        probed += 1
    return probed


def _identity(video: VideoAsset) -> tuple | None:
    """What makes two rows the same clip, or None when nothing can say.

    A hash is identity. Without one the resolved path is the best available
    stand-in, and a row with neither is left alone rather than merged into
    something on a guess.
    """
    if video.hash:
        return ("hash", video.hash)
    resolved = resolved_path(video.path)
    return ("path", resolved) if resolved else None


def merge_duplicates(store: SurveyStore) -> tuple[int, int]:
    """Fold rows that are the same clip into one. Returns (merged, passes moved).

    The oldest row wins, so the id that has been referenced longest is the one
    that survives; the rest are folded into it and their passes repointed.
    """
    groups: dict[tuple, list[VideoAsset]] = {}
    for video in store.list_videos():
        key = _identity(video)
        if key is None:
            continue
        groups.setdefault(key, []).append(video)

    merged = 0
    moved = 0
    for videos in groups.values():
        if len(videos) < 2:
            continue
        # list_videos orders by created_at, so the first is the oldest.
        keeper, *losers = videos
        # Carry across anything the keeper never learned. A duplicate written
        # later often knows more (a probe filled in its duration), and dropping
        # that with the row would lose it for good.
        for loser in losers:
            keeper.fill_from(loser)
        store.update_video(keeper)
        moved += store.merge_videos(keeper.id, [v.id for v in losers])
        merged += len(losers)
    return merged, moved


def repair_video_identity(store: SurveyStore) -> RepairReport:
    """Hash what can be hashed, then merge what turns out to be the same clip.

    Order matters: hashing first is what lets two rows for one file be
    recognised as duplicates at all, since without hashes they are only
    comparable when they also happen to agree on a path. Containers are read
    last, so a row that is about to be folded into another is not read twice.
    """
    hashed, unreadable = backfill_hashes(store)
    merged, moved = merge_duplicates(store)
    probed = backfill_metadata(store)
    return RepairReport(
        hashed=hashed,
        merged=merged,
        passes_moved=moved,
        probed=probed,
        unreadable=unreadable,
    )
