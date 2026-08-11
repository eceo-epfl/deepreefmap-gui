"""Per-version environment discovery, real sizing, and deletion.

Each app version keeps its own PyApp environment under
``<data_local>/pyapp/deepreefmap-gui/<dist_id>/<version>/``. They are never pruned
automatically; this module backs the System-tab manager where the user sees each
one's real footprint and removes the ones they no longer want.

"Real footprint" is the space deleting an environment would actually free, not the
apparent 8 GB. uv hardlinks (or copy-on-write clones) most package files out of
its cache, so the bulk of an env shares physical bytes with the cache and other
versions. Deleting the env frees only the files unique to it (its ``.pyc`` and
metadata). See ``env_disk_usage``.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Environment:
    version: str  # the version dir name, e.g. "1.2.0" or "1.1.0+g81e7b5f"
    path: Path  # the version dir (parent of its python/ tree)
    current: bool  # True for the environment this process is running from


def _current_env_dir() -> Path | None:
    """The running version's env dir, or None outside an installed PyApp tree.

    ``sys.prefix`` is ``.../pyapp/deepreefmap-gui/<dist_id>/<version>/python``; the
    version dir is its parent. A dev checkout has no ``pyapp`` component.
    """
    env_dir = Path(sys.prefix).parent
    if "pyapp" not in env_dir.parts:
        return None
    return env_dir


def list_environments() -> list[Environment]:
    """Every installed version's environment, the running one first.

    Empty outside an installed binary (a dev checkout), so the Updates-view
    section hides just like the update controls do.
    """
    current = _current_env_dir()
    if current is None or not current.is_dir():
        return []
    dist_dir = current.parent
    environments: list[Environment] = []
    for child in dist_dir.iterdir():
        if not child.is_dir():
            continue
        environments.append(
            Environment(version=child.name, path=child, current=child == current)
        )
    # Version dirs carry a build suffix (1.1.0+g81e7b5f), so this is a name
    # order rather than a version one: it only has to be stable, and the running
    # env is the one entry whose position says anything.
    environments.sort(key=lambda e: e.version, reverse=True)
    environments.sort(key=lambda e: not e.current)
    return environments


def env_disk_usage(env_dir: str | os.PathLike[str]) -> tuple[int, int]:
    """``(reclaimable, apparent)`` bytes for an environment.

    - ``reclaimable``: what deleting it frees, i.e. bytes in files with a single
      hardlink (``st_nlink == 1``). Files shared with the uv cache have
      ``st_nlink >= 2`` and are not freed, so they are excluded. This is the
      honest "real" size. (Approximation: a file hardlinked twice *within* one env
      is excluded too, which is rare.)
    - ``apparent``: each inode counted once, i.e. what ``du`` reports for the
      folder, shown as context for how much is shared with the cache.

    On Windows ``os.lstat`` reports ``st_nlink == 1`` for everything, so the two
    figures collapse and the caller's "shared with the cache" line reads 0. That
    understates what is shared, never what deleting frees, so the number the user
    acts on stays honest.
    """
    reclaimable = 0
    apparent = 0
    seen: set[tuple[int, int]] = set()
    for root, _dirs, names in os.walk(env_dir, onerror=lambda _e: None):
        for name in names:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            key = (st.st_dev, st.st_ino)
            if key not in seen:
                seen.add(key)
                apparent += st.st_size
            if st.st_nlink == 1:
                reclaimable += st.st_size
    return reclaimable, apparent


def delete_environment(env_dir: str | os.PathLike[str]) -> None:
    """Remove an installed version's environment.

    Refuses the running environment and any path outside an installed PyApp tree,
    so a stray call can't wipe the live env or something unrelated.
    """
    target = Path(env_dir)
    current = _current_env_dir()
    if current is not None and target.resolve() == current.resolve():
        raise ValueError("refusing to delete the running environment")
    if "pyapp" not in target.parts:
        raise ValueError(f"refusing to delete a non-PyApp path: {target}")
    shutil.rmtree(target, ignore_errors=True)
    logger.info("Deleted environment %s", target)
