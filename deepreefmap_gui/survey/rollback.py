"""What rolling back to an older version does to the survey in the output folder.

The updater offers older releases, and installing one is the one action whose
consequence for the survey can be worked out beforehand rather than discovered
on next launch. Which of four things happens is decided by three facts: the
format the survey is in, the range of formats the target release reads
(:mod:`deepreefmap_gui.survey.schema_history`), and whether a backup old enough
for it is already on disk.

The outcome is stated rather than hedged. "May not be able to open surveys this
one has saved" is not something a decision can be made against; "0.2.0 reads up
to v3, this survey is v9, and no v3-or-older copy exists beside it" is.

Nothing here writes, migrates or installs. Qt-free, so the wording is testable
without a window.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from deepreefmap_gui.survey.backup import SurveyBackup, best_backup
from deepreefmap_gui.survey.health import inspect_survey_db
from deepreefmap_gui.survey.schema_history import released_schema


class RollbackEffect(str, Enum):
    # No survey database here that could be read, so the rollback cannot change
    # what happens to one. Covers a folder without one and a file too damaged to
    # report its format -- neither is made worse by installing an older version.
    NO_SURVEY = "no_survey"
    # The target reads this survey's format as it stands.
    OPENS = "opens"
    # It does not, but a copy old enough for it is already on disk.
    RESTORES_BACKUP = "restores_backup"
    # It does not, and no copy is old enough.
    CANNOT_OPEN = "cannot_open"
    # The target is not in this build's table, so nothing can be established.
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class RollbackOutlook:
    """What the target version will find, and what it will do about it."""

    target: str
    effect: RollbackEffect
    db_version: int | None = None
    target_reads_up_to: int | None = None
    restorable: SurveyBackup | None = None

    @property
    def loses_work(self) -> bool:
        """Whether taking this route leaves work unreachable to the older build."""
        return self.effect in (RollbackEffect.RESTORES_BACKUP, RollbackEffect.CANNOT_OPEN)

    def summary(self) -> str:
        """One paragraph naming the actual consequence, for the confirm dialog."""
        if self.effect is RollbackEffect.NO_SURVEY:
            return (
                "No survey database could be read in the current output folder, "
                "so nothing here is affected."
            )
        if self.effect is RollbackEffect.OPENS:
            return (
                f"Version {self.target} reads this survey's format (v{self.db_version}), "
                f"so it opens as it is."
            )
        if self.effect is RollbackEffect.RESTORES_BACKUP:
            assert self.restorable is not None
            taken = self.restorable.taken_at.astimezone().strftime("%d %b %Y, %H:%M")
            return (
                f"Version {self.target} reads survey formats up to "
                f"v{self.target_reads_up_to}, and this survey is v{self.db_version}, "
                f"so it will not open it directly. It will offer to restore "
                f"{self.restorable.path.name}, saved {taken}. Work done since then "
                f"is not in that copy, and comes back only by upgrading again."
            )
        if self.effect is RollbackEffect.CANNOT_OPEN:
            return (
                f"Version {self.target} reads survey formats up to "
                f"v{self.target_reads_up_to}, this survey is v{self.db_version}, and "
                f"there is no copy beside it in a format that old. It will not open "
                f"this survey: it will offer to rebuild from your run folders, which "
                f"recovers processed runs only, or to start a new database. Nothing "
                f"is deleted, and upgrading again opens the survey as it is now."
            )
        return (
            f"Which survey formats version {self.target} reads is not recorded in "
            f"this build, so whether it can open this survey (v{self.db_version}) "
            f"cannot be checked here. If it cannot, it will offer to restore a "
            f"backup or rebuild from your run folders."
        )


def rollback_outlook(db_path: Path, target: str) -> RollbackOutlook:
    """Work out what installing ``target`` means for the survey at ``db_path``.

    Read-only: it inspects, it does not back up. The caller writes the
    pre-rollback backup, which is what makes the trip back reversible and is a
    separate concern from what the older build will find.
    """
    health = inspect_survey_db(db_path)
    db_version = health.db_version
    entry = released_schema(target)

    if db_version is None:
        return RollbackOutlook(target, RollbackEffect.NO_SURVEY)
    if entry is None:
        return RollbackOutlook(target, RollbackEffect.UNVERIFIED, db_version=db_version)
    if entry.reads(db_version):
        return RollbackOutlook(
            target,
            RollbackEffect.OPENS,
            db_version=db_version,
            target_reads_up_to=entry.reads_up_to,
        )

    # Bounded by what the target reads at both ends, for the same reason the
    # recovery dialog is: a copy the older build also refuses is no route back.
    backup = best_backup(db_path, entry.reads_up_to, min_version=entry.reads_from)
    effect = (
        RollbackEffect.RESTORES_BACKUP if backup is not None else RollbackEffect.CANNOT_OPEN
    )
    return RollbackOutlook(
        target,
        effect,
        db_version=db_version,
        target_reads_up_to=entry.reads_up_to,
        restorable=backup,
    )
