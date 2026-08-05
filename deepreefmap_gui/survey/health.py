"""Whether a survey database can be opened, decided without opening it for writing.

:class:`~deepreefmap_gui.survey.store.SurveyStore` raises when it cannot open a
database, and it is constructed while the window is being built. Asking here
first turns that into a verdict the GUI can render, so a field laptop shows a
window and a way out rather than exiting with a traceback nobody sees.

Qt-free and read-only: nothing here migrates, writes, or raises.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class SurveyDbState(str, Enum):
    OK = "ok"
    # No file yet. The ordinary first run in a new output folder.
    MISSING = "missing"
    # Stamped past the last migration this build carries: a rolled-back update.
    TOO_NEW = "too_new"
    CORRUPT = "corrupt"
    # The file may be fine; the location is not. A read-only mount, an unplugged
    # drive, or a network share where WAL cannot be used.
    UNWRITABLE = "unwritable"


# The states where opening a store would fail, so callers do not restate the list.
UNOPENABLE = (SurveyDbState.TOO_NEW, SurveyDbState.CORRUPT, SurveyDbState.UNWRITABLE)


@dataclass(frozen=True)
class SurveyDbHealth:
    """A verdict on one database file, with the numbers behind it."""

    state: SurveyDbState
    path: Path
    known_version: int
    db_version: int | None = None
    detail: str = ""

    @property
    def openable(self) -> bool:
        return self.state not in UNOPENABLE


def _known_version() -> int:
    from deepreefmap_gui.survey.store import _MIGRATIONS

    return len(_MIGRATIONS)


def _writable(path: Path) -> tuple[bool, str]:
    """Whether a database could be created or written at this path.

    WAL needs to create sidecar files beside the database, so the directory has
    to be writable even when the database itself already exists.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"The folder {path.parent} could not be created ({exc.strerror or exc})."
    probe = path.parent / f".{path.name}.probe"
    try:
        probe.touch()
        probe.unlink()
    except OSError as exc:
        return False, f"The folder {path.parent} cannot be written to ({exc.strerror or exc})."
    return True, ""


def inspect_survey_db(path: Path) -> SurveyDbHealth:
    """Classify a survey database without migrating or modifying it."""
    known = _known_version()

    writable, why = _writable(path)
    if not writable:
        return SurveyDbHealth(SurveyDbState.UNWRITABLE, path, known, detail=why)

    if not path.exists():
        return SurveyDbHealth(SurveyDbState.MISSING, path, known)

    try:
        # Read-only, so a database mid-migration in another process is never
        # disturbed and a corrupt file is never rewritten. as_uri percent-encodes
        # the path, which a plain f-string would leave to break on "?" or "#".
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except (sqlite3.Error, OSError, ValueError) as exc:
        return SurveyDbHealth(SurveyDbState.CORRUPT, path, known, detail=str(exc))

    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        # user_version reads 0 on any file sqlite can open, including one that is
        # not a database at all, so confirm the header separately.
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except (sqlite3.Error, TypeError, ValueError) as exc:
        return SurveyDbHealth(SurveyDbState.CORRUPT, path, known, detail=str(exc))
    finally:
        conn.close()

    if version > known:
        return SurveyDbHealth(
            SurveyDbState.TOO_NEW,
            path,
            known,
            db_version=version,
            detail=(
                f"This survey was last opened by a newer version of DeepReefMap "
                f"(database format v{version}; this version reads up to v{known})."
            ),
        )
    return SurveyDbHealth(SurveyDbState.OK, path, known, db_version=version)
