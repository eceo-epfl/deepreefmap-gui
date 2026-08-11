"""Whether a run fits in this machine, and if not, what to change.

Peak memory is modelled as a fixed cost plus a per-frame cost, separately for
RAM and VRAM, from the tables in model_costs.py. Both limits are inverted to a
frame ceiling, so the answer to "it does not fit" is a length the machine can
actually process rather than a number the user has to interpret.

Swap is deliberately excluded from the budget. The peak here is a torch.cat over
tensors that are all live and all touched; spilling that to disk thrashes rather
than degrades, and the kernel kills on memory pressure well before the combined
pool is exhausted.

A Linux RAM exhaustion is an uncatchable OOM kill, so this advises before a long
run rather than crash into it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from deepreefmap_gui.profiling.model_costs import (
    FRAME_BYTES_PER_PIXEL,
    INTERPRETER_BASELINE_BYTES,
    mapping_cost,
    segmentation_cost,
)
from deepreefmap_gui.profiling.system_probe import GPU_CUDA, GPU_MPS, SystemProfile, format_bytes

# The OS, desktop and background apps hold RAM the run can never claim. Grading
# against full physical RAM assumes a bare machine.
_OS_RESERVE_FRACTION = 0.12
_OS_RESERVE_MIN = 3 * 1024**3
_OS_RESERVE_MAX = 8 * 1024**3

# Share of the budget above which a run is close enough to warn about.
_WARN_FRACTION = 0.85

# Allocator slack and per-block transients.
_OVERHEAD = 1.15

# The desktop compositor and allocator fragmentation on the card, which a run
# never gets back.
_GPU_RESERVE_FRACTION = 0.08
_GPU_RESERVE_MIN = 512 * 1024**2
_GPU_RESERVE_MAX = int(1.5 * 1024**3)


def _os_reserve(total_ram_bytes: int) -> int:
    return int(min(_OS_RESERVE_MAX, max(_OS_RESERVE_MIN, total_ram_bytes * _OS_RESERVE_FRACTION)))


def _gpu_reserve(total_vram_bytes: int) -> int:
    return int(
        min(_GPU_RESERVE_MAX, max(_GPU_RESERVE_MIN, total_vram_bytes * _GPU_RESERVE_FRACTION))
    )


@dataclass(frozen=True)
class RunShape:
    """The settings that decide what a run costs."""

    frames: int
    width: int
    height: int
    mapping_backend: str
    seg_model: str
    batch_size: int = 4


@dataclass(frozen=True)
class Stage:
    """One stage's RAM as a fixed cost plus a per-frame cost.

    Stages do not coexist, so a run's peak is the worst of them and its frame
    ceiling is the tightest of theirs.
    """

    name: str
    fixed_bytes: int
    bytes_per_frame: int

    def bytes_at(self, frames: int) -> int:
        return self.fixed_bytes + self.bytes_per_frame * frames

    def frame_ceiling(self, budget_bytes: int) -> int:
        if self.bytes_per_frame <= 0:
            return _UNLIMITED if budget_bytes >= self.fixed_bytes else 0
        return max(0, (budget_bytes - self.fixed_bytes) // self.bytes_per_frame)


@dataclass(frozen=True)
class Cost:
    """Peak memory for a run, per stage, so a frame ceiling can be derived."""

    stages: tuple[Stage, ...]
    fixed_vram_bytes: int
    vram_bytes_per_frame: int
    frames: int
    source: str  # "measured" | "estimated" -- of the RAM figures
    vram_source: str = "estimated"  # "measured" once a peak from this card lands
    # Which of the two terms set the fixed VRAM figure: they do not add (the
    # segmenter is released before mapping loads), so one of them is the whole
    # number. Blaming the wrong one sends the user to a control that cannot help.
    vram_fixed_from: str = "mapping"  # "mapping" | "segmentation"

    @property
    def ram_bytes(self) -> int:
        return max(stage.bytes_at(self.frames) for stage in self.stages)

    @property
    def peak_stage(self) -> Stage:
        return max(self.stages, key=lambda stage: stage.bytes_at(self.frames))

    @property
    def vram_bytes(self) -> int:
        return self.fixed_vram_bytes + self.vram_bytes_per_frame * self.frames


@dataclass(frozen=True)
class Budget:
    """What the machine can actually give a run."""

    ram_bytes: int
    vram_bytes: int | None
    unified: bool  # GPU draws from system RAM, so one pool serves both


@dataclass(frozen=True)
class Verdict:
    level: str  # "ok" | "warn" | "block"
    cost: Cost
    budget: Budget
    # "ram" | "vram" | "ram_fixed" | "vram_fixed" | "". The _fixed pair are a
    # different fact from the other two: they are decided before the first frame,
    # so nothing the user can set moves them.
    limit: str
    max_frames: int
    percent: float
    headline: str
    detail: str
    shape: RunShape | None = None

    @property
    def fits(self) -> bool:
        return self.level == "ok"

    @property
    def limit_is_fixed(self) -> bool:
        return self.limit in ("ram_fixed", "vram_fixed")

    @property
    def need_bytes(self) -> int:
        """Demand on whichever resource decided the verdict."""
        return self.cost.vram_bytes if self.limit.startswith("vram") else self.cost.ram_bytes

    @property
    def budget_bytes(self) -> int:
        if self.limit.startswith("vram"):
            return self.budget.vram_bytes or 0
        return self.budget.ram_bytes


def estimate_cost(shape: RunShape, *, recorded: dict | None = None) -> Cost:
    """What this run is expected to peak at.

    ``recorded`` is a measured peak from a comparable run. It replaces the fixed
    term only: the per-frame slope stays analytic, so a peak recorded at one
    length does not scale its own constant baseline when applied to another.
    """
    mapping = mapping_cost(shape.mapping_backend)
    segmentation = segmentation_cost(shape.seg_model)

    # Prepared frames are built before mapping loads and stay resident to the
    # end, so every stage carries them.
    prepared = int(max(1, shape.width * shape.height) * FRAME_BYTES_PER_PIXEL)
    resident = INTERPRETER_BASELINE_BYTES + mapping.weights_ram_bytes

    def slack(value: float) -> int:
        return int(value * _OVERHEAD)

    stages = [
        # Preprocessing: frames accumulate under the segmentation model.
        Stage("preprocess", slack(INTERPRETER_BASELINE_BYTES + segmentation.load_ram_bytes), slack(prepared)),
        # Loading the mapping checkpoint, on top of every prepared frame.
        Stage("load", slack(INTERPRETER_BASELINE_BYTES + mapping.load_ram_bytes), slack(prepared)),
        Stage("mapping", slack(resident), slack(prepared + mapping.merge_bytes_per_frame())),
        Stage("cloud", slack(resident), slack(prepared + mapping.cloud_bytes_per_frame())),
    ]

    source = "estimated"
    if recorded and recorded.get("frames") and recorded.get("ram_bytes"):
        # A recorded peak fixes the level; the analytic slope still shapes it,
        # so a peak measured at one length does not rescale its own baseline.
        recorded_frames = int(recorded["frames"])
        predicted = max(stage.bytes_at(recorded_frames) for stage in stages)
        shortfall = int(recorded["ram_bytes"]) - predicted
        if shortfall > 0:
            stages = [
                Stage(stage.name, stage.fixed_bytes + shortfall, stage.bytes_per_frame)
                for stage in stages
            ]
        source = "measured"

    seg_vram = segmentation.vram_bytes(shape.batch_size)
    fixed_vram = max(seg_vram, mapping.vram_fixed_bytes)
    vram_source = "estimated"
    if recorded and recorded.get("vram_bytes") and recorded.get("vram_frames") is not None:
        # VRAM is a single line, a fixed term plus a slope, so one recorded
        # point identifies the intercept in either direction. The caller offers
        # a peak from this card at this batch size only, and the figure is
        # device-wide use, so correcting to it errs high.
        implied = int(recorded["vram_bytes"]) - mapping.vram_bytes_per_frame * int(
            recorded["vram_frames"]
        )
        if implied > 0:
            # Segmentation runs before mapping loads and is modelled separately,
            # so it floors the fixed term whatever mapping turned out to cost.
            fixed_vram = max(seg_vram, implied)
            vram_source = "measured"

    return Cost(
        stages=tuple(stages),
        fixed_vram_bytes=fixed_vram,
        vram_bytes_per_frame=mapping.vram_bytes_per_frame,
        frames=max(0, shape.frames),
        source=source,
        vram_source=vram_source,
        vram_fixed_from="segmentation" if fixed_vram == seg_vram else "mapping",
    )


def machine_budget(profile: SystemProfile) -> Budget:
    """RAM and VRAM a run may claim.

    Graded against installed RAM less an OS reserve rather than against whatever
    is momentarily free, so the same settings do not change verdict because a
    browser is open.

    VRAM is graded the same way and for the same reason. What the driver reports
    free moves under the user, on Windows even while nothing is running.
    """
    ram = max(0, profile.total_ram_bytes - _os_reserve(profile.total_ram_bytes))
    gpu = profile.gpu
    if gpu.kind == GPU_MPS:
        return Budget(ram_bytes=ram, vram_bytes=None, unified=True)
    vram = None
    if gpu.kind == GPU_CUDA:
        # Installed less a reserve, for the reason above. A card that reports
        # only what is free still gets graded against that -- a noisy budget
        # beats no budget at all, because a VRAM budget of None means every run
        # grades "ok" on the graphics card however large it is.
        total = gpu.total_vram_bytes or gpu.free_vram_bytes
        if total:
            vram = max(0, total - _gpu_reserve(total))
    return Budget(ram_bytes=ram, vram_bytes=vram, unified=False)


def _max_frames_for(cost: Cost, budget: Budget) -> int:
    """Frames the tightest stage allows."""
    stages = cost.stages
    if budget.unified:
        # One pool: the GPU's demand comes out of the same RAM.
        stages = tuple(
            Stage(
                stage.name,
                stage.fixed_bytes + cost.fixed_vram_bytes,
                stage.bytes_per_frame + cost.vram_bytes_per_frame,
            )
            for stage in stages
        )
    ceiling = min(stage.frame_ceiling(budget.ram_bytes) for stage in stages)
    if budget.vram_bytes is not None and not budget.unified:
        vram_stage = Stage("vram", cost.fixed_vram_bytes, cost.vram_bytes_per_frame)
        ceiling = min(ceiling, vram_stage.frame_ceiling(budget.vram_bytes))
    return min(ceiling, _UNLIMITED)


_UNLIMITED = 1 << 40


def max_frames(profile: SystemProfile, shape: RunShape, *, recorded: dict | None = None) -> int:
    """Frames this machine can process in one pass at these settings."""
    return _max_frames_for(estimate_cost(shape, recorded=recorded), machine_budget(profile))


def grade(
    profile: SystemProfile, shape: RunShape, *, recorded: dict | None = None
) -> Verdict:
    """Whether the run fits, what limits it, and how much it would have to shrink."""
    cost = estimate_cost(shape, recorded=recorded)
    budget = machine_budget(profile)

    ram_need = cost.ram_bytes
    if budget.unified:
        ram_need += cost.vram_bytes
    percent = 100.0 * ram_need / budget.ram_bytes if budget.ram_bytes else 0.0
    ceiling = _max_frames_for(cost, budget)

    over_ram = ram_need > budget.ram_bytes
    over_vram = (
        budget.vram_bytes is not None
        and not budget.unified
        and cost.vram_bytes > budget.vram_bytes
    )

    fixed_ram = max(stage.fixed_bytes for stage in cost.stages)
    if over_ram or over_vram:
        level = "block"
        # Which resource decided keeps its old precedence; what is new is
        # telling a fixed cost apart from a length. A run whose fixed term alone
        # is over budget does not fit at one frame, so advice about the frame
        # rate, the trim or the resolution is advice that cannot be taken.
        if over_ram:
            limit = "ram_fixed" if fixed_ram > budget.ram_bytes else "ram"
        else:
            limit = (
                "vram_fixed"
                if cost.fixed_vram_bytes > (budget.vram_bytes or 0)
                else "vram"
            )
    elif percent >= 100.0 * _WARN_FRACTION:
        level, limit = "warn", "ram"
    else:
        level, limit = "ok", ""

    headline, detail = _wording(level, limit, cost, budget, ram_need, shape, fixed_ram)
    return Verdict(
        level=level,
        cost=cost,
        budget=budget,
        limit=limit,
        max_frames=ceiling,
        percent=percent,
        headline=headline,
        detail=detail,
        shape=shape,
    )


@dataclass(frozen=True)
class Fit:
    """A verdict in the units the run is configured in: a length and a frame rate.

    The single entry point for the rest of the app. Cheap enough to call on any
    settings change; nothing here touches the disk or the GPU beyond one probe.
    """

    verdict: Verdict
    seconds: float
    fps: int
    max_seconds: float
    suggested_fps: int | None
    suggested_seconds: float | None
    # A mapping backend that was re-graded and came back fitting, for the case
    # where the run does not fit at any length. None when none of them do.
    suggested_backend: str | None = None
    suggested_backend_vram: int | None = None
    # The largest preprocessing batch that grades non-block, when the batch size
    # is what pushes the device over. Replaces a second, separately calibrated
    # warning that used to contradict this one.
    suggested_batch_size: int | None = None

    @property
    def level(self) -> str:
        return self.verdict.level

    @property
    def fits(self) -> bool:
        return self.verdict.level == "ok"

    @property
    def headline(self) -> str:
        return self.verdict.headline

    @property
    def detail(self) -> str:
        return self.verdict.detail

    @property
    def advice(self) -> str:
        """What to change, in the controls the user has in front of them."""
        if self.fits:
            return ""
        if self.verdict.limit_is_fixed:
            # The batch size leads whenever it is what overflowed: it is a
            # control on this form, and it fixes the run outright. Recommending
            # a different backend for a segmentation overflow would change the
            # method for a reason that had nothing to do with it.
            if self.suggested_batch_size is not None and (
                self.verdict.cost.vram_fixed_from == "segmentation"
            ):
                return (
                    f"Set the preprocessing batch size to {self.suggested_batch_size} "
                    "in advanced settings."
                )
            if self.suggested_backend is None:
                return "No mapping method fits this machine at these settings."
            size = (
                f" It needs about {format_bytes(self.suggested_backend_vram)} "
                "of graphics memory."
                if self.suggested_backend_vram is not None
                else ""
            )
            return f"Choose {self.suggested_backend} under Mapping.{size}"
        parts = []
        if self.suggested_fps is not None:
            parts.append(f"set FPS to {self.suggested_fps}")
        if self.suggested_seconds is not None:
            parts.append(f"trim the pass to about {format_duration(self.suggested_seconds)}")
        if not parts:
            return "Lower the resolution, or split the pass into shorter sections."
        return f"{parts[0][0].upper()}{parts[0][1:]}" + (f", or {parts[1]}." if len(parts) > 1 else ".")


def format_duration(seconds: float) -> str:
    """A run length as `9 min` or `1 h 20 min`."""
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{max(1, minutes)} min"
    hours, rem = divmod(minutes, 60)
    return f"{hours} h {rem} min" if rem else f"{hours} h"


def fit_for_pass(
    profile: SystemProfile,
    *,
    seconds: float,
    fps: int,
    width: int,
    height: int,
    mapping_backend: str,
    seg_model: str,
    batch_size: int = 4,
    recorded: dict | None = None,
) -> Fit:
    """Grade one pass and say what would make it fit."""
    fps = max(1, fps)
    shape = RunShape(
        frames=int(seconds * fps),
        width=width,
        height=height,
        mapping_backend=mapping_backend,
        seg_model=seg_model,
        batch_size=batch_size,
    )
    verdict = grade(profile, shape, recorded=recorded)
    ceiling = verdict.max_frames
    max_seconds = ceiling / fps

    suggested_fps: int | None = None
    suggested_seconds: float | None = None
    suggested_backend: str | None = None
    suggested_backend_vram: int | None = None
    suggested_batch_size: int | None = None
    if verdict.limit_is_fixed:
        # Offered only if it actually fits: re-graded rather than assumed, so the
        # app never sends somebody to a control that will not help either. The
        # recorded peak is dropped for the re-grade -- a peak measured under one
        # backend says nothing about another.
        for backend in _lighter_backends(mapping_backend):
            lighter = grade(profile, replace(shape, mapping_backend=backend))
            if lighter.level != "block":
                suggested_backend = backend
                suggested_backend_vram = lighter.cost.fixed_vram_bytes
                break
    elif verdict.level != "ok":
        # The largest frame rate that still fits the pass as trimmed.
        for rate in range(fps - 1, 0, -1):
            if seconds * rate <= ceiling:
                suggested_fps = rate
                break
        # Only worth saying when it is meaningfully shorter than the pass
        # already is, and long enough to still be a usable transect.
        if max_seconds >= 60 and max_seconds <= 0.85 * seconds:
            suggested_seconds = max_seconds
    if verdict.limit.startswith("vram") and batch_size > 1:
        # The batch size only reaches the device through the segmentation term,
        # so this is worth saying only when shrinking it actually changes the
        # verdict. Same model, same units as everything else on this readout.
        for size in range(batch_size - 1, 0, -1):
            if grade(profile, replace(shape, batch_size=size), recorded=recorded).level != "block":
                suggested_batch_size = size
                break
    return Fit(
        verdict=verdict,
        seconds=seconds,
        fps=fps,
        max_seconds=max_seconds,
        suggested_fps=suggested_fps,
        suggested_seconds=suggested_seconds,
        suggested_backend=suggested_backend,
        suggested_backend_vram=suggested_backend_vram,
        suggested_batch_size=suggested_batch_size,
    )


def _lighter_backends(current: str) -> list[str]:
    """Modelled backends that hold less on the device than `current`, cheapest first."""
    from deepreefmap_gui.profiling.model_costs import modelled_mapping_backends

    costs = modelled_mapping_backends()
    here = costs.get(current)
    ceiling = here.vram_fixed_bytes if here is not None else None
    lighter = [
        (cost.vram_fixed_bytes, key)
        for key, cost in costs.items()
        if key != current and (ceiling is None or cost.vram_fixed_bytes < ceiling)
    ]
    return [key for _, key in sorted(lighter)]


def _wording(
    level: str,
    limit: str,
    cost: Cost,
    budget: Budget,
    ram_need: int,
    shape: RunShape,
    fixed_ram: int,
) -> tuple[str, str]:
    """Plain statements of what was measured against what."""
    if level == "ok":
        return "", (
            f"Needs about {format_bytes(ram_need)} of the "
            f"{format_bytes(budget.ram_bytes)} this machine can give a run."
        )
    # A fixed cost belongs to the model that was chosen, so the sentence names
    # it: the choice is the only thing that can be changed about it, and a
    # verdict that will not say what caused it cannot be acted on.
    if limit == "vram_fixed":
        measured = (
            " Measured on this card on an earlier run."
            if cost.vram_source == "measured"
            else ""
        )
        # Whichever term set the figure is the one named. Segmentation's is the
        # batch size times a per-frame cost, so it is answerable by a control the
        # user has; the mapping backend's is answerable only by changing backend.
        if cost.vram_fixed_from == "segmentation":
            return f"{shape.seg_model} needs more graphics memory than this card has", (
                f"Reading {shape.batch_size} frames at a time through "
                f"{shape.seg_model} takes about {format_bytes(cost.fixed_vram_bytes)} "
                f"on the graphics card; this card can give a run "
                f"{format_bytes(budget.vram_bytes or 0)}. Frame rate, length and "
                f"resolution do not change that -- the batch size does.{measured}"
            )
        return f"{shape.mapping_backend} needs more graphics memory than this card has", (
            f"{shape.mapping_backend} holds about {format_bytes(cost.fixed_vram_bytes)} "
            f"on the graphics card before the first frame is read; this card can "
            f"give a run {format_bytes(budget.vram_bytes or 0)}. Frame rate, "
            f"length and resolution do not change that.{measured}"
        )
    if limit == "ram_fixed":
        return f"{shape.mapping_backend} needs more memory than this computer has", (
            f"Loading {shape.mapping_backend} alone takes about "
            f"{format_bytes(fixed_ram)}; this computer can give a run "
            f"{format_bytes(budget.ram_bytes)}. Frame rate, length and "
            f"resolution do not change that."
        )
    if limit == "vram":
        return "Too long for the graphics card", (
            f"Needs about {format_bytes(cost.vram_bytes)} of graphics memory at "
            f"{cost.frames} frames; this card can give a run "
            f"{format_bytes(budget.vram_bytes or 0)}."
        )
    if level == "block":
        return "Too long to process in one pass", (
            f"Needs about {format_bytes(ram_need)} of memory; this machine can "
            f"give a run {format_bytes(budget.ram_bytes)}."
        )
    return "Close to this machine's limit", (
        f"Needs about {format_bytes(ram_need)} of the "
        f"{format_bytes(budget.ram_bytes)} this machine can give a run."
    )
