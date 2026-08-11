"""Find the footage in whatever was dropped on the app.

A dive's clips arrive as a card, a folder of dated folders, or a handful of
files, so what a drop means is "import the videos in here" rather than "import
this file". Walking it is the app's job, not the user's.

Qt-free: this is paths and suffixes, so it is tested without a window.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".avi", ".mkv"})

RUN_MANIFEST_NAME = "run_manifest.json"

# Enough for any card or season of footage. A drop of a home directory would
# otherwise walk it all on the thread painting the window.
_ENTRY_BUDGET = 50_000


def is_video(path: Path) -> bool:
    """A video file, and not one of the sidecars that only look like one.

    macOS writes a ``._NAME.MP4`` AppleDouble beside every file it copies off a
    card. They carry the same suffix and a few KB of resource fork, so taking
    them at their name imports a library of clips that cannot be opened.
    """
    return (
        path.suffix.lower() in VIDEO_SUFFIXES
        and not path.name.startswith("._")
        and not path.name.startswith(".")
    )


def is_run_dir(path: Path) -> bool:
    """A finished run's directory, which is dropped to open rather than import."""
    return (path / RUN_MANIFEST_NAME).is_file()


def find_videos(paths: Iterable[Path]) -> tuple[list[Path], bool]:
    """Every clip in the dropped files and folders. Returns (clips, truncated).

    Folders are walked to the bottom: footage comes off a card as dated folders
    of dated folders, and asking someone to drop each leaf separately is asking
    them to do the walk by hand. Run directories are skipped, since a run holds
    no footage and a survey's output root sits beside its videos often enough to
    be dropped by accident.
    """
    found: list[Path] = []
    seen: set[Path] = set()
    budget = _ENTRY_BUDGET
    truncated = False

    def keep(path: Path) -> None:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            found.append(path)

    for path in paths:
        try:
            if path.is_file():
                if is_video(path):
                    keep(path)
                continue
            if not path.is_dir():
                continue
            for child in sorted(path.rglob("*")):
                budget -= 1
                if budget <= 0:
                    truncated = True
                    break
                if child.is_file() and is_video(child) and not is_run_dir(child.parent):
                    keep(child)
        except OSError as exc:
            logger.warning("Could not read %s while looking for footage: %s", path, exc)
    return found, truncated
