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

from dataclasses import dataclass

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


def _os_reserve(total_ram_bytes: int) -> int:
    return int(min(_OS_RESERVE_MAX, max(_OS_RESERVE_MIN, total_ram_bytes * _OS_RESERVE_FRACTION)))


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
    source: str  # "measured" | "estimated"

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
    limit: str  # "ram" | "vram" | ""
    max_frames: int
    percent: float
    headline: str
    detail: str

    @property
    def fits(self) -> bool:
        return self.level == "ok"


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

    return Cost(
        stages=tuple(stages),
        fixed_vram_bytes=max(segmentation.vram_bytes(shape.batch_size), mapping.vram_fixed_bytes),
        vram_bytes_per_frame=mapping.vram_bytes_per_frame,
        frames=max(0, shape.frames),
        source=source,
    )


def machine_budget(profile: SystemProfile) -> Budget:
    """RAM and VRAM a run may claim.

    Graded against installed RAM less an OS reserve rather than against whatever
    is momentarily free, so the same settings do not change verdict because a
    browser is open.
    """
    ram = max(0, profile.total_ram_bytes - _os_reserve(profile.total_ram_bytes))
    gpu = profile.gpu
    if gpu.kind == GPU_MPS:
        return Budget(ram_bytes=ram, vram_bytes=None, unified=True)
    vram = None
    if gpu.kind == GPU_CUDA:
        vram = gpu.free_vram_bytes if gpu.free_vram_bytes is not None else gpu.total_vram_bytes
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

    if over_ram or over_vram:
        level = "block"
        limit = "ram" if over_ram else "vram"
    elif percent >= 100.0 * _WARN_FRACTION:
        level, limit = "warn", "ram"
    else:
        level, limit = "ok", ""

    headline, detail = _wording(level, limit, cost, budget, ram_need)
    return Verdict(
        level=level,
        cost=cost,
        budget=budget,
        limit=limit,
        max_frames=ceiling,
        percent=percent,
        headline=headline,
        detail=detail,
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

    suggested_fps = None
    suggested_seconds = None
    if verdict.level != "ok":
        # The largest frame rate that still fits the pass as trimmed.
        for candidate in range(fps - 1, 0, -1):
            if seconds * candidate <= ceiling:
                suggested_fps = candidate
                break
        # Only worth saying when it is meaningfully shorter than the pass
        # already is, and long enough to still be a usable transect.
        if max_seconds >= 60 and max_seconds <= 0.85 * seconds:
            suggested_seconds = max_seconds
    return Fit(
        verdict=verdict,
        seconds=seconds,
        fps=fps,
        max_seconds=max_seconds,
        suggested_fps=suggested_fps,
        suggested_seconds=suggested_seconds,
    )


def _wording(level: str, limit: str, cost: Cost, budget: Budget, ram_need: int) -> tuple[str, str]:
    """Plain statements of what was measured against what."""
    if level == "ok":
        return "", (
            f"Needs about {format_bytes(ram_need)} of the "
            f"{format_bytes(budget.ram_bytes)} this machine can give a run."
        )
    if limit == "vram":
        return "Too much for the graphics card", (
            f"Needs about {format_bytes(cost.vram_bytes)} of graphics memory; "
            f"{format_bytes(budget.vram_bytes)} is free."
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
