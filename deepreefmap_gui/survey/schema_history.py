"""Which survey database format each released version of the app can open.

The updater offers rollbacks, and a rollback is the one moment where the app can
know something the older build cannot tell it: whether that build will still be
able to open the survey sitting in the output folder. Answering needs a map from
app version to database format, which is what this is.

The table is read **downward only**, which is why it can be a constant compiled
into the binary rather than something fetched:

* A rollback target is older than the build asking, so it is already here. No
  network, no release metadata, nothing read from the default branch.
* An upgrade target is newer and unknown, and does not need to be known: the
  newer build migrates the database forward itself and stamps a backup at the
  version it found, which is what makes the trip back possible.

**Add one entry per release, in the commit that cuts the tag.** A version absent
from the table reads as "could not be verified" rather than as safe, so a
forgotten entry costs a warning, never a silently broken rollback.

Qt-free, no network, no filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass

from deepreefmap_gui.survey.store import latest_schema_version, oldest_supported_version


@dataclass(frozen=True)
class ReleaseSchema:
    """The range of database formats one released version can open.

    ``reads_from`` is the oldest it can carry forward, ``reads_up_to`` the
    newest it understands. A database outside the range is refused by that
    build.
    """

    version: str
    reads_from: int
    reads_up_to: int

    def reads(self, schema: int) -> bool:
        return self.reads_from <= schema <= self.reads_up_to


# Released versions only, oldest first. Both entries carried the migration list
# whose length was the version, so each reads everything from 0 up to its own
# count of steps: 0.1.0 shipped one migration, 0.2.0 three.
_RELEASED: tuple[ReleaseSchema, ...] = (
    ReleaseSchema("0.1.0", 0, 1),
    ReleaseSchema("0.2.0", 0, 3),
)


def _normalised(version: str) -> str:
    return version.strip().lstrip("vV")


def current_schema() -> ReleaseSchema:
    """What the build running now reads, which no table can be stale about."""
    return ReleaseSchema("current", oldest_supported_version(), latest_schema_version())


def released_schema(version: str) -> ReleaseSchema | None:
    """What one released version reads, or None if it is not recorded here.

    None is not "any": callers must treat an unrecorded version as one whose
    compatibility could not be established, never as one that is fine.
    """
    wanted = _normalised(version)
    for entry in _RELEASED:
        if entry.version == wanted:
            return entry
    return None


def reads_up_to(version: str) -> int | None:
    """The newest database format that release can open, or None if unrecorded."""
    entry = released_schema(version)
    return entry.reads_up_to if entry is not None else None


def newest_release_reading(schema: int) -> ReleaseSchema | None:
    """The newest released version that can open a database at this format.

    Used to tell someone holding a database this build has dropped support for
    which version to open it with once. That build migrates it to its own
    ``reads_up_to``, which is how far the trip gets them.
    """
    for entry in reversed(_RELEASED):
        if entry.reads(schema):
            return entry
    return None
