"""Snapshots of a survey database, kept so a rolled-back update can be undone.

A migration is one-way: it rewrites the database into a shape older builds
refuse to open. Taking a copy first means a rollback restores exactly what the
older build last wrote, rather than recovering what can be pieced back together
from run manifests.

Backups are named for the schema version they hold, so the build that needs one
knows which to look for: ``survey.db.v3.bak`` is a database an app that reads up
to v3 can open.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_SUFFIX = ".bak"
_PATTERN = re.compile(r"\.v(\d+)\.bak$")


@dataclass(frozen=True)
class SurveyBackup:
    path: Path
    version: int
    taken_at: datetime
    size_bytes: int


def backup_path(db_path: Path, version: int) -> Path:
    return db_path.with_name(f"{db_path.name}.v{version}{_SUFFIX}")


def write_backup(db_path: Path, version: int) -> Path | None:
    """Copy the database as it stands; return where, or None if it could not be taken.

    Uses sqlite's own backup API rather than copying the file. A WAL database's
    committed data lives partly in its ``-wal`` sidecar, so a plain file copy can
    be a torn snapshot missing the most recent writes.

    Never raises: a backup that cannot be taken must not stop the app from
    opening the database it was about to protect.
    """
    if not db_path.exists():
        return None
    target = backup_path(db_path, version)
    try:
        source = sqlite3.connect(db_path)
        try:
            # A partly-written .bak is worse than none, so build it beside the
            # target and move it into place only once sqlite says it is complete.
            staging = target.with_name(target.name + ".partial")
            staging.unlink(missing_ok=True)
            dest = sqlite3.connect(staging)
            try:
                source.backup(dest)
            finally:
                dest.close()
            staging.replace(target)
        finally:
            source.close()
    except (sqlite3.Error, OSError):
        logger.warning("Could not back up %s before migrating", db_path, exc_info=True)
        return None
    logger.info("Backed up %s as %s", db_path, target.name)
    return target


def find_backup(db_path: Path, version: int) -> SurveyBackup | None:
    """The backup holding the given schema version, if one was ever taken."""
    for backup in list_backups(db_path):
        if backup.version == version:
            return backup
    return None


def best_backup(db_path: Path, max_version: int, min_version: int = 0) -> SurveyBackup | None:
    """The newest backup inside a range of schema versions, or None.

    Both ends matter. A build reads a *range* of formats, not everything below
    the newest it knows, so a caller asking "which backup can I open?" that only
    capped the top would be offered a copy in the very format it had just
    refused -- and restoring it would put the app straight back where it started.
    """
    candidates = [b for b in list_backups(db_path) if min_version <= b.version <= max_version]
    return max(candidates, key=lambda b: b.version) if candidates else None


def list_backups(db_path: Path) -> list[SurveyBackup]:
    """Every backup beside this database, newest schema version first."""
    found: list[SurveyBackup] = []
    try:
        siblings = list(db_path.parent.glob(f"{db_path.name}.v*{_SUFFIX}"))
    except OSError:
        return []
    for path in siblings:
        match = _PATTERN.search(path.name)
        if match is None:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        found.append(SurveyBackup(
            path=path,
            version=int(match.group(1)),
            taken_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            size_bytes=stat.st_size,
        ))
    return sorted(found, key=lambda b: b.version, reverse=True)


def set_aside(db_path: Path, version: int | None) -> Path:
    """Move a database out of the way under a name that says why. Never deletes.

    The displaced file keeps its schema version in its name so a later upgrade
    can find it again -- the work done on the newer version is in there, and the
    user is told where.
    """
    label = f"schema-v{version}" if version is not None else "unreadable"
    target = db_path.with_name(f"{db_path.name}.{label}")
    if target.exists():
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
        target = db_path.with_name(f"{db_path.name}.{label}.{stamp}")
    db_path.replace(target)
    # WAL sidecars belong to the database that was moved; leaving them behind
    # would let sqlite graft them onto whatever takes its place.
    for sidecar in ("-wal", "-shm"):
        companion = db_path.with_name(db_path.name + sidecar)
        if companion.exists():
            companion.replace(target.with_name(target.name + sidecar))
    logger.info("Set aside %s as %s", db_path, target.name)
    return target


def restore(backup: SurveyBackup, db_path: Path, displaced_version: int | None = None) -> None:
    """Put a backup back in place, setting aside whatever is there now.

    ``displaced_version`` names the schema of the database being moved out of the
    way, so the file left behind says which build's work it holds.
    """
    if db_path.exists():
        set_aside(db_path, displaced_version)
    source = sqlite3.connect(backup.path)
    try:
        dest = sqlite3.connect(db_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    logger.info("Restored %s from %s", db_path, backup.path.name)
