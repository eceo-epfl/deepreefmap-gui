"""What each destination has and still needs.

Pure and Qt-free: the header alert and the Start processing button read their
verdict from the same function, so the header cannot call a destination fine
while the button that acts on it is disabled.
"""

from __future__ import annotations

from dataclasses import dataclass

# Verdict vocabulary. Position is not a state here: the checked pill in the
# header already says where you are, so a verdict carries meaning only.
TODO = "todo"  # nothing done yet, and nothing wrong
OK = "ok"  # there is something here, and nothing wrong with it
ATTENTION = "attention"  # something went wrong, but you can still work
BLOCKED = "blocked"  # the destination's action is disabled until you act

SECTION_STATES = (TODO, OK, ATTENTION, BLOCKED)

# Where a blocker is fixed. A reason that names a destination is only useful if
# the user can get there, so the verdict carries the destination rather than
# spelling out directions the page cannot follow for them.
FIX_HERE = ""  # fixed on the page that shows it, so no destination
FIX_MACHINE = "machine"  # this computer is not ready, and Setup holds the actions
FIX_SETTINGS = "settings"  # the run settings are at fault, and the dialog holds them

FIX_DESTINATIONS = (FIX_HERE, FIX_MACHINE, FIX_SETTINGS)


# How loudly a verdict asks to be heard. Only the two states worth acting on
# rank; the others have nothing to interrupt with.
_URGENCY = {BLOCKED: 2, ATTENTION: 1}


@dataclass(frozen=True)
class SectionState:
    """A destination's verdict: how it paints, what it counts, why, where to go."""

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


def passes_phrase(count: int) -> str:
    """"3 passes". Shared so the gate's reason and the button that obeys it
    count the same thing in the same words."""
    return _plural(count, "pass", "passes")


def headline(reason: str) -> str:
    """A reason's first sentence: what is wrong, without the advice that follows
    it. The full text stays reachable as the tooltip."""
    head, _, _ = reason.strip().partition(". ")
    return head.rstrip(".")


def most_urgent(states: dict[str, SectionState]) -> tuple[str, SectionState] | None:
    """The one destination worth interrupting for, or None if none is.

    Ties go to whichever was given first, which the caller orders as the header
    shows them.
    """
    ranked = [
        (rank, index, name, state)
        for index, (name, state) in enumerate(states.items())
        if (rank := _URGENCY.get(state.state)) is not None
    ]
    if not ranked:
        return None
    _, _, name, state = max(ranked, key=lambda row: (row[0], -row[1]))
    return name, state


def transects_state(transect_count: int, has_draft: bool) -> SectionState:
    """Transects is satisfied by one saved transect.

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


def videos_state(clip_count: int, missing: int) -> SectionState:
    """What the footage itself has to report: how much of it, and what is lost.

    A clip whose file cannot be found is the one thing worth chasing here. It
    still lists, and its runs still read, but nothing more can be cut from it
    until the drive it lives on is back.
    """
    if clip_count == 0:
        return SectionState(TODO, "no footage", "Import the day's clips to start.")
    counts = _plural(clip_count, "clip")
    if missing:
        return SectionState(
            ATTENTION,
            f"{counts} · {missing} missing",
            f"{_plural(missing, 'clip')} cannot be found. "
            "Plug the drive back in, or add them again from where they live now.",
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
    unscaled: int = 0,
    missing_files: int = 0,
) -> SectionState:
    """Process's verdict, and by construction the Start processing button's.

    Order matters: the blockers come first and in the order the user can act on
    them, since only the first one is shown. The graphics card outranks the
    models because changing the processing method changes which models a pass
    needs, so a download chased first can turn out to have been the wrong one.
    """
    if pass_count == 0:
        return SectionState(TODO, "no videos yet", "Add the videos you want processed.")

    counts = passes_phrase(pass_count)
    # First of all the blockers: footage that is not there is the one thing a
    # user can fix in seconds, and the run would fail on it anyway, mid-session
    # and after everything before it had already been processed.
    if missing_files:
        return SectionState(
            BLOCKED,
            f"{counts} · {missing_files} without footage",
            f"{passes_phrase(missing_files)} name a video file that cannot be "
            "found. Plug the drive back in, or add the footage again from where "
            "it lives now.",
        )
    if not has_preset:
        return SectionState(
            BLOCKED,
            counts,
            "The run settings could not be loaded, so nothing here can be processed.",
            fix=FIX_SETTINGS,
        )
    if gpu_only_mapper:
        return SectionState(
            BLOCKED,
            counts,
            f"The {gpu_only_mapper} processing method requires a graphics card, "
            "and none was detected.",
            fix=FIX_MACHINE,
        )
    if missing_models:
        return SectionState(
            BLOCKED,
            counts,
            f"{_plural(len(missing_models), 'required model')} not installed "
            f"({', '.join(missing_models)}).",
            fix=FIX_MACHINE,
        )
    if failed:
        return SectionState(
            ATTENTION,
            f"{counts} · {failed} failed",
            f"{passes_phrase(failed)} failed. The log holds the error; processing can be started again.",
        )
    # Not a blocker, and deliberately below the ones that are. A pass with no
    # transect processes perfectly well; it just cannot be compared against
    # repeat passes of the same place afterwards, which is worth saying once
    # while there is still time to assign it.
    if unassigned:
        return SectionState(
            OK,
            f"{counts} · {unassigned} without a transect",
            f"{passes_phrase(unassigned)} will run without a transect, so they will not be "
            "compared against repeat passes.",
        )
    # Below unassigned: a missing transect swallows a missing tape length.
    if unscaled:
        return SectionState(
            OK,
            f"{counts} · {unscaled} unscaled",
            f"{passes_phrase(unscaled)} are on a transect with no tape length, so they "
            "will run unscaled. Set the length under Transects.",
        )
    if remaining:
        return SectionState(OK, f"{counts} · {remaining} to process")
    return SectionState(OK, f"{counts} · all processed")


def machine_state(
    *,
    unmet: int,
    advisory: str = "",
    update_version: str = "",
) -> SectionState:
    """What the computer itself has to say, for the header button that opens it.

    Deliberately the same vocabulary as the destinations, and computed from the
    same checks the Process gate blocks on, so the header cannot report a healthy
    machine while the Start processing button refuses to run for want of a model.

    Two things are reported, and they are different in kind. An unmet
    requirement stops work and is the state; an available update is a chore that
    can wait, so it is never the state and only ever named in the sentence. The
    button paints it as its own glyph, which is why it is returned separately
    rather than folded into a severity.
    """
    version_note = f"Version {update_version} is available." if update_version else ""
    if unmet:
        return SectionState(
            BLOCKED,
            _plural(unmet, "requirement") + " not met",
            " ".join(
                filter(
                    None,
                    [
                        f"{_plural(unmet, 'requirement')} for processing not met.",
                        version_note,
                    ],
                )
            ),
        )
    if advisory:
        # Not a bare "Ready": the button paints a warning glyph in this state,
        # and a name that says the machine is fine beside an icon that says it
        # is not leaves a screen-reader user with the half that is wrong.
        return SectionState(
            ATTENTION,
            "Ready, with a warning",
            " ".join(filter(None, [advisory, version_note])),
        )
    if update_version:
        return SectionState(OK, "Ready", version_note)
    return SectionState(OK, "Ready", "This computer is ready to process.")
