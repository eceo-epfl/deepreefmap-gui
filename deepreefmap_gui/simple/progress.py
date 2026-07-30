"""What each simple-mode step has and still needs.

Pure and Qt-free on purpose: the step badge and the Process button read their
verdict from the same function, so the header can never claim a step is fine
while the button that acts on it is disabled.
"""

from __future__ import annotations

from dataclasses import dataclass

# Badge vocabulary. Position is not a state here — the checked pill in the
# header already says which step you are on, so the badge is free to carry
# meaning only.
TODO = "todo"  # nothing done yet, and nothing wrong
OK = "ok"  # the step's precondition is satisfied
ATTENTION = "attention"  # something went wrong, but you can still move
BLOCKED = "blocked"  # the step's action is disabled until you act

SECTION_STATES = (TODO, OK, ATTENTION, BLOCKED)

# Where a blocker is fixed. A reason that names a destination is only useful if
# the user can get there, so the verdict carries the destination rather than
# spelling out directions the page cannot follow for them.
FIX_HERE = ""  # fixed on the page that shows it, so no destination
FIX_SETUP = "setup"  # this machine is not ready, and setup holds the actions
FIX_SETTINGS = "settings"  # the run settings are at fault, and the dialog holds them

FIX_DESTINATIONS = (FIX_HERE, FIX_SETUP, FIX_SETTINGS)


@dataclass(frozen=True)
class SectionState:
    """A step's verdict: how it paints, what it counts, why, and where to go."""

    state: str
    count: str
    reason: str = ""
    fix: str = FIX_HERE

    def __post_init__(self) -> None:
        if self.state not in SECTION_STATES:
            raise ValueError(f"Unknown section state: {self.state!r}")
        if self.fix not in FIX_DESTINATIONS:
            raise ValueError(f"Unknown fix destination: {self.fix!r}")


def _plural(count: int, singular: str, plural: str = "") -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _passes(count: int) -> str:
    return _plural(count, "pass", "passes")


def plan_state(transect_count: int, has_draft: bool) -> SectionState:
    """Plan is satisfied by one saved transect.

    A draft is a transect the user started typing and never completed, which is
    worth flagging because nothing else in the UI will mention it again.
    """
    if has_draft:
        return SectionState(
            ATTENTION,
            _plural(transect_count, "transect"),
            "A transect is half-entered: it needs a name and both endpoints to save.",
        )
    if transect_count == 0:
        return SectionState(TODO, "none yet", "Add a transect, or import a CSV or GPX file.")
    return SectionState(OK, _plural(transect_count, "transect"))


def browse_state(run_count: int, unfiled: int) -> SectionState:
    """The archive is never done and never upcoming, so it only ever counts.

    Runs that belong to no transect are the one thing worth chasing: they are
    invisible to per-transect comparison until they are filed.
    """
    if run_count == 0:
        return SectionState(TODO, "nothing yet", "Processed runs collect here.")
    counts = _plural(run_count, "run")
    if unfiled:
        return SectionState(
            ATTENTION,
            f"{counts} · {unfiled} unfiled",
            f"{_plural(unfiled, 'run')} belong to no transect. "
            "Assign them to compare passes of the same transect.",
        )
    return SectionState(OK, counts)


def run_gate(
    *,
    pass_count: int,
    unassigned: int,
    remaining: int,
    failed: int,
    has_preset: bool,
    missing_models: list[str],
    gpu_only_mapper: str = "",
) -> SectionState:
    """Run's verdict, and by construction the Process button's.

    Order matters: the blockers come first and in the order the user can act on
    them, since only the first one is shown. The graphics card outranks the
    models because changing the processing method changes which models a pass
    needs, so a download chased first can turn out to have been the wrong one.
    """
    if pass_count == 0:
        return SectionState(TODO, "no videos yet", "Add the videos you want processed.")

    counts = _passes(pass_count)
    if unassigned:
        return SectionState(
            BLOCKED,
            f"{counts} · {unassigned} to assign",
            f"{_passes(unassigned)} still need a transect before this batch can run.",
        )
    if not has_preset:
        return SectionState(
            BLOCKED,
            counts,
            "The run settings could not be loaded, so this batch cannot run.",
            fix=FIX_SETTINGS,
        )
    if gpu_only_mapper:
        return SectionState(
            BLOCKED,
            counts,
            f"The {gpu_only_mapper} processing method requires a graphics card, "
            "and none was detected.",
            fix=FIX_SETUP,
        )
    if missing_models:
        return SectionState(
            BLOCKED,
            counts,
            f"{_plural(len(missing_models), 'required model')} not installed "
            f"({', '.join(missing_models)}).",
            fix=FIX_SETUP,
        )
    if failed:
        return SectionState(
            ATTENTION,
            f"{counts} · {failed} failed",
            f"{_passes(failed)} failed. The log holds the error; the batch can be run again.",
        )
    if remaining:
        return SectionState(OK, f"{counts} · {remaining} to process")
    return SectionState(OK, f"{counts} · all processed")
