"""Removing things, and refusing to.

Every path this module can reach has to be proved before it is touched, because
the whole point of the page above it is that somebody is deleting in bulk while
looking at a total rather than at a filename. Run data goes through
``catalogue.require_run_dir``, which admits direct children of the output root
and nothing else. Footage lives outside that root by definition, so it carries
three proofs of its own: the database still names that exact path, the caller
repeats the name the row was showing, and the thing on disk is a regular file.

Nothing here consults the database except to check a clip is still the clip.
A run keeps its record and a clip keeps its row: what is being reclaimed is
bytes, never history.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from deepreefmap_gui.io.scene_file import tmp_write_in_progress
from deepreefmap_gui.storage.inventory import (
    KIND_ABORTED_RUN,
    KIND_DB_PARTIAL,
    KIND_DB_SET_ASIDE,
    KIND_ORPHAN_DIR,
    KIND_SCENE_TMP,
    KIND_UNKNOWN_FILE,
    MountClip,
    MountItem,
)
from deepreefmap_gui.storage.tiers import DELETABLE_TIERS, RunBreakdown, tree_bytes
from deepreefmap_gui.survey import catalogue
from deepreefmap_gui.survey.store import SurveyStore, resolved_path

logger = logging.getLogger(__name__)


class ReclaimError(Exception):
    """A delete that could not be proved safe, so it did not happen."""


@dataclass(frozen=True, slots=True)
class Reclaimed:
    """What actually went, which is not always what was offered."""

    freed_bytes: int = 0
    items: int = 0
    failures: tuple[tuple[str, str], ...] = ()

    def __add__(self, other: Reclaimed) -> Reclaimed:
        return Reclaimed(
            freed_bytes=self.freed_bytes + other.freed_bytes,
            items=self.items + other.items,
            failures=self.failures + other.failures,
        )


def _weigh(path: Path) -> int:
    """What removing this would free, measured now rather than when it was listed."""
    try:
        size = os.lstat(path).st_size
    except OSError:
        return 0
    if path.is_dir() and not path.is_symlink():
        size += tree_bytes(path)[0]
    return size


def _remove(path: Path) -> Reclaimed:
    """Unlink a file or a link, or remove a tree. A path already gone is not a failure."""
    freed = _weigh(path)
    try:
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path)
    except FileNotFoundError:
        return Reclaimed()
    except OSError as exc:
        logger.warning("Could not remove %s", path, exc_info=True)
        return Reclaimed(failures=((path.name, str(exc)),))
    return Reclaimed(freed_bytes=freed, items=1)


def delete_tier(
    out_root: Path, run_dir: Path, tier: str, breakdown: RunBreakdown
) -> Reclaimed:
    """Remove one tier of one run, leaving the record and the other tiers alone.

    Exactly the entries the breakdown listed, never a fresh glob: a file written
    between the scan and the click was not part of what anybody agreed to.
    """
    if tier not in DELETABLE_TIERS:
        raise ReclaimError(f"{tier!r} is not a tier that can be deleted")
    resolved = catalogue.require_run_dir(out_root, run_dir)
    total = Reclaimed()
    for name in breakdown.tier_entries(tier):
        target = resolved / name
        if target.parent != resolved:
            raise ReclaimError(f"{name!r} is not directly inside {resolved}")
        total += _remove(target)
    return total


def delete_run_folder(out_root: Path, run_dir: Path) -> Reclaimed:
    """Remove a whole run directory. The database row stays, so Browse keeps the run."""
    resolved = catalogue.require_run_dir(out_root, run_dir)
    freed = _weigh(resolved)
    catalogue.delete_run_data(out_root, resolved)
    return Reclaimed(freed_bytes=freed, items=1)


def delete_other(out_root: Path, item: MountItem, store: SurveyStore | None = None) -> Reclaimed:
    """Remove one piece of residue, once its kind has been proved again."""
    if item.kind in (KIND_ABORTED_RUN, KIND_ORPHAN_DIR):
        resolved = catalogue.require_run_dir(out_root, item.path)
        freed = _weigh(resolved)
        # The record goes with it: an aborted run's row describes a folder that
        # was never openable and will not be resumed.
        catalogue.delete_run_dir(out_root, resolved, store)
        return Reclaimed(freed_bytes=freed, items=1)

    if item.path.parent.resolve() != out_root.resolve():
        raise ReclaimError(f"{item.path} is not directly under {out_root}")
    if item.kind == KIND_SCENE_TMP:
        # Re-checked here rather than trusted from the scan: a scene write runs
        # on a daemon thread while its run stays open, so one can start between
        # the page being drawn and the button being pressed.
        if tmp_write_in_progress(item.path):
            raise ReclaimError(f"{item.label} is being written right now")
        return _remove(item.path)
    if item.kind in (KIND_DB_PARTIAL, KIND_DB_SET_ASIDE, KIND_UNKNOWN_FILE):
        return _remove(item.path)
    raise ReclaimError(f"{item.kind!r} is not something this can remove")


def delete_input_clip(
    clip: MountClip, store: SurveyStore, *, confirmed_name: str
) -> Reclaimed:
    """Delete one original recording, and nothing in the database.

    The row survives with its hash, its length, its capture time and every
    section cut from it, so the clip stays in the library and simply reads as
    missing footage from here on.
    """
    if confirmed_name != Path(clip.path).name:
        raise ReclaimError("That is not the file the row was showing")
    record = store.get_video(clip.video_id)
    if record is None:
        raise ReclaimError("The library no longer holds that clip")
    if resolved_path(record.path) != resolved_path(clip.path):
        raise ReclaimError("That clip has moved since the page was drawn")

    path = Path(clip.path)
    try:
        stat = os.lstat(path)
    except OSError as exc:
        raise ReclaimError(f"Could not read {path.name}") from exc
    if not os.path.isfile(path) or os.path.islink(path):
        raise ReclaimError(f"{path.name} is not a plain file")

    freed = stat.st_size
    try:
        path.unlink()
    except OSError as exc:
        raise ReclaimError(f"Could not delete {path.name}") from exc
    logger.info("Deleted original footage %s (%d bytes)", path, freed)
    return Reclaimed(freed_bytes=freed, items=1)
