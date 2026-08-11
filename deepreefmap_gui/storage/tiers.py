"""What a run directory is made of, and what it costs to delete each part.

A run leaves four kinds of thing behind and they are worth wildly different
amounts. The scene file the viewer builds is put back the next time the run is
opened. The ortho and the cloud come back only from a re-run. The frame caches
and the mapping arrays are both the resume cache and the viewer's pixel source,
so losing them ends the run's life as anything but a record. Tiering the
directory is what lets somebody free three gigabytes without discovering
afterwards which of those they chose.

Entries are matched by name at the top level of the run directory and nothing
recurses into the decision: the pipeline writes a flat set of names, and a rule
that reached inside `frames/` would be a rule about a file the pipeline is free
to rename. Anything unrecognised is filed `unknown`, which is measured and shown
and never offered, because a near-miss on a glob is how somebody's own file gets
deleted by a tool that was only ever asked about caches.

Qt-free and stat-only, like profiling/volumes.py, so the whole classification is
testable against a directory tree in a tmp_path.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from deepreefmap_gui.io.scene_file import LEGACY_SCENE_SUFFIXES, SCENE_FILE_SUFFIX

TIER_CACHE, TIER_RESULTS, TIER_WORKING = "cache", "results", "working"
TIER_KEEP, TIER_UNKNOWN = "keep", "unknown"

# In the order somebody should be offered them: cheapest loss first. Ticking one
# ticks everything above it, so the order is the meaning.
DELETABLE_TIERS = (TIER_CACHE, TIER_RESULTS, TIER_WORKING)
ALL_TIERS = (*DELETABLE_TIERS, TIER_KEEP, TIER_UNKNOWN)

# Directories the viewer reads pixels out of and the pipeline resumes from.
WORKING_DIRS = frozenset({"frames", "labels", "masks", ".cache"})
_WORKING_FILES = frozenset({"mapping_outputs.npz", "geometry_cloud.ply"})

# The run's account of itself, and small enough that nobody frees space with it.
_KEEP_FILES = frozenset({"run_manifest.json", "run.log"})

_RESULTS_FILES = frozenset(
    {
        "semantic_reference_cloud.ply",
        "ortho.npz",
        "ortho.png",
        "benthic_cover.json",
        "tsdf_cloud.ply",
        "semantic_tsdf_cloud.ply",
    }
)
_RESULTS_DIRS = frozenset({"videos"})

_SCENE_SUFFIXES = (SCENE_FILE_SUFFIX, *LEGACY_SCENE_SUFFIXES)

# What run_loader.py refuses to open without, and what resume.py keys off.
_OPENABLE_FILES = ("run_manifest.json", "mapping_outputs.npz")
_FRAME_DIRS = ("frames", "labels", "masks")


@dataclass(frozen=True, slots=True)
class TierBreakdown:
    """One tier of one run: what it weighs, and the entries it is made of."""

    tier: str
    size_bytes: int = 0
    entries: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunBreakdown:
    """A run directory, split into the tiers a delete can be offered by."""

    dir_name: str
    total_bytes: int = 0
    tiers: Mapping[str, TierBreakdown] = field(default_factory=dict)
    # Entries stat refused: counted as present, contributing nothing, so a
    # partial total says so rather than claiming an empty folder.
    unmeasured_items: int = 0

    def tier_bytes(self, tier: str) -> int:
        found = self.tiers.get(tier)
        return found.size_bytes if found is not None else 0

    def tier_entries(self, tier: str) -> tuple[str, ...]:
        found = self.tiers.get(tier)
        return found.entries if found is not None else ()

    def _has(self, *names: str) -> bool:
        present = {name for tier in self.tiers.values() for name in tier.entries}
        return all(name in present for name in names)

    @property
    def openable(self) -> bool:
        """Whether the viewer could still load this run."""
        return self._has(*_OPENABLE_FILES, *_FRAME_DIRS)

    @property
    def resumable(self) -> bool:
        """Whether the pipeline could still pick this run up where it stopped."""
        return self._has(".cache", *_FRAME_DIRS)


def tier_for(name: str, *, is_dir: bool) -> str:
    """Which tier a top-level entry of a run directory belongs to."""
    if is_dir:
        if name in WORKING_DIRS:
            return TIER_WORKING
        if name in _RESULTS_DIRS:
            return TIER_RESULTS
        return TIER_UNKNOWN
    if name in _WORKING_FILES:
        return TIER_WORKING
    if name in _KEEP_FILES:
        return TIER_KEEP
    stem = name.removesuffix(".tmp")
    if stem.endswith(_SCENE_SUFFIXES):
        return TIER_CACHE
    if name in _RESULTS_FILES:
        return TIER_RESULTS
    return TIER_UNKNOWN


def tree_bytes(path: Path) -> tuple[int, int]:
    """Everything under a directory, as (bytes, entries that refused to stat).

    ``lstat`` throughout, so a symlink inside a run is measured as the link and
    never as whatever it points at, which could be the whole drive.
    """
    total = 0
    unmeasured = 0
    for parent, dirs, files in os.walk(path, followlinks=False):
        for name in (*dirs, *files):
            try:
                total += os.lstat(os.path.join(parent, name)).st_size
            except OSError:
                unmeasured += 1
    return total, unmeasured


def measure_run(run_dir: Path) -> RunBreakdown:
    """Weigh every top-level entry of a run directory and file it under a tier.

    A symlinked top-level entry is filed `unknown` whatever it is named, so no
    tier can ever delete through one into somewhere else.
    """
    sizes: dict[str, int] = dict.fromkeys(ALL_TIERS, 0)
    entries: dict[str, list[str]] = {tier: [] for tier in ALL_TIERS}
    unmeasured = 0
    total = 0

    try:
        children = list(os.scandir(run_dir))
    except OSError:
        return RunBreakdown(dir_name=run_dir.name, tiers=_frozen(sizes, entries))

    for child in children:
        try:
            is_link = child.is_symlink()
            is_dir = child.is_dir(follow_symlinks=False)
            size = child.stat(follow_symlinks=False).st_size
        except OSError:
            unmeasured += 1
            continue
        if is_dir:
            below, refused = tree_bytes(Path(child.path))
            size += below
            unmeasured += refused
        tier = TIER_UNKNOWN if is_link else tier_for(child.name, is_dir=is_dir)
        sizes[tier] += size
        entries[tier].append(child.name)
        total += size

    return RunBreakdown(
        dir_name=run_dir.name,
        total_bytes=total,
        tiers=_frozen(sizes, entries),
        unmeasured_items=unmeasured,
    )


def _frozen(
    sizes: Mapping[str, int], entries: Mapping[str, list[str]]
) -> dict[str, TierBreakdown]:
    """Every tier present, so a caller never has to guard a lookup."""
    return {
        tier: TierBreakdown(
            tier=tier,
            size_bytes=sizes.get(tier, 0),
            entries=tuple(sorted(entries.get(tier, ()))),
        )
        for tier in ALL_TIERS
    }
