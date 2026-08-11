"""What each destination has and still needs.

Pure and Qt-free: the notification centre and the Start processing button read
their verdict from the same function, so the bell cannot call a destination fine
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

# Why a verdict says what it says, in a form code can match on. It becomes the
# fingerprint of the notification the verdict raises, so a reworded sentence
# does not read as a different problem and does not void somebody's decision to
# never hear about this one again. Treat these strings as a public interface.
CAUSE_NONE = ""
CAUSE_DRAFT_TRANSECT = "transects.draft"
CAUSE_MISSING_CLIPS = "videos.missing_clips"
CAUSE_UNFILED_RUNS = "browse.unfiled_runs"
CAUSE_MISSING_FOOTAGE = "process.missing_footage"
CAUSE_NO_PRESET = "process.no_preset"
CAUSE_NO_GPU = "process.no_gpu"
CAUSE_MISSING_MODELS = "process.missing_models"
CAUSE_FAILED_PASSES = "process.failed_passes"
CAUSE_UNASSIGNED_PASSES = "process.unassigned_passes"
CAUSE_UNSCALED_PASSES = "process.unscaled_passes"
CAUSE_UNMET_REQUIREMENTS = "machine.unmet_requirements"
CAUSE_MACHINE_ADVISORY = "machine.advisory"

CAUSES = (
    CAUSE_NONE,
    CAUSE_DRAFT_TRANSECT,
    CAUSE_MISSING_CLIPS,
    CAUSE_UNFILED_RUNS,
    CAUSE_MISSING_FOOTAGE,
    CAUSE_NO_PRESET,
    CAUSE_NO_GPU,
    CAUSE_MISSING_MODELS,
    CAUSE_FAILED_PASSES,
    CAUSE_UNASSIGNED_PASSES,
    CAUSE_UNSCALED_PASSES,
    CAUSE_UNMET_REQUIREMENTS,
    CAUSE_MACHINE_ADVISORY,
)


@dataclass(frozen=True)
class SectionState:
    """A destination's verdict: how it paints, what it counts, why, where to go."""

    state: str
    count: str
    reason: str = ""
    fix: str = FIX_HERE
    cause: str = CAUSE_NONE
    # The number behind ``count``, so a log can say the clips went from ten to
    # nine without parsing the sentence that said so.
    n: int = 0

    def __post_init__(self) -> None:
        if self.state not in SECTION_STATES:
            raise ValueError(f"Unknown section state: {self.state!r}")
        if self.fix not in FIX_DESTINATIONS:
            raise ValueError(f"Unknown fix destination: {self.fix!r}")
        if self.cause not in CAUSES:
            raise ValueError(f"Unknown cause: {self.cause!r}")


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


def advice(reason: str) -> str:
    """What is left after the headline: what to do about it, or nothing.

    The pair is exhaustive, so a surface showing both shows the reason once.
    Showing ``reason`` under ``headline`` repeats the first sentence.
    """
    _, _, tail = reason.strip().partition(". ")
    return tail.strip()


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
            cause=CAUSE_DRAFT_TRANSECT,
            n=1,
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
            cause=CAUSE_UNFILED_RUNS,
            n=unfiled,
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
            cause=CAUSE_MISSING_CLIPS,
            n=missing,
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
            cause=CAUSE_MISSING_FOOTAGE,
            n=missing_files,
        )
    if not has_preset:
        return SectionState(
            BLOCKED,
            counts,
            "The run settings could not be loaded, so nothing here can be processed.",
            fix=FIX_SETTINGS,
            cause=CAUSE_NO_PRESET,
        )
    if gpu_only_mapper:
        return SectionState(
            BLOCKED,
            counts,
            f"The {gpu_only_mapper} processing method requires a graphics card, "
            "and none was detected.",
            fix=FIX_MACHINE,
            cause=CAUSE_NO_GPU,
        )
    if missing_models:
        return SectionState(
            BLOCKED,
            counts,
            f"{_plural(len(missing_models), 'required model')} not installed "
            f"({', '.join(missing_models)}).",
            fix=FIX_MACHINE,
            cause=CAUSE_MISSING_MODELS,
            n=len(missing_models),
        )
    if failed:
        return SectionState(
            ATTENTION,
            f"{counts} · {failed} failed",
            f"{passes_phrase(failed)} failed. The log holds the error; processing can be started again.",
            cause=CAUSE_FAILED_PASSES,
            n=failed,
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
            cause=CAUSE_UNASSIGNED_PASSES,
            n=unassigned,
        )
    # Below unassigned: a missing transect swallows a missing tape length.
    if unscaled:
        return SectionState(
            OK,
            f"{counts} · {unscaled} unscaled",
            f"{passes_phrase(unscaled)} are on a transect with no tape length, so they "
            "will run unscaled. Set the length under Transects.",
            cause=CAUSE_UNSCALED_PASSES,
            n=unscaled,
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
            cause=CAUSE_UNMET_REQUIREMENTS,
            n=unmet,
        )
    if advisory:
        # Not a bare "Ready": the button paints a warning glyph in this state,
        # and a name that says the machine is fine beside an icon that says it
        # is not leaves a screen-reader user with the half that is wrong.
        return SectionState(
            ATTENTION,
            "Ready, with a warning",
            " ".join(filter(None, [advisory, version_note])),
            cause=CAUSE_MACHINE_ADVISORY,
        )
    if update_version:
        return SectionState(OK, "Ready", version_note)
    return SectionState(OK, "Ready", "This computer is ready to process.")
