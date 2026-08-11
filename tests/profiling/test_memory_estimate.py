"""Peak-memory model, the frame ceiling it implies, and the advice it gives."""

from __future__ import annotations

import pytest

from deepreefmap_gui.profiling.memory_estimate import (
    RunShape,
    estimate_cost,
    fit_for_pass,
    grade,
    machine_budget,
    max_frames,
)
from deepreefmap_gui.profiling.system_probe import GPU_CUDA, GPU_MPS, GPU_NONE, GpuInfo, SystemProfile

_GB = 1024**3
_SEG = "coralscapes-vit-b-dpt"


@pytest.fixture(autouse=True)
def _tabled_weights(monkeypatch):
    """Grade against the table, not against whatever this machine has installed.

    Model weights are read off the checkpoints when they are present, so without
    this the same assertions would measure different numbers on a developer's
    machine and on a clean one. The calculated path has its own tests, which
    supply a known figure rather than depending on a download.
    """
    monkeypatch.setattr(
        "deepreefmap_gui.profiling.model_weights.weights_bytes", lambda name: None
    )


def _profile(*, total_gb=64, avail_gb=None, gpu=None, swap_gb=0):
    return SystemProfile(
        os_name="Linux", os_release="x", cpu_logical=8, cpu_physical=4,
        total_ram_bytes=int(total_gb * _GB),
        available_ram_bytes=int((avail_gb if avail_gb is not None else total_gb) * _GB),
        total_swap_bytes=int(swap_gb * _GB), free_swap_bytes=int(swap_gb * _GB),
        gpu=gpu or GpuInfo(GPU_NONE, "CPU only", None, None),
        disk_total_bytes=0, disk_free_bytes=0, disk_path="/",
    )


def _shape(frames, backend="loger_star", **kw):
    return RunShape(frames=frames, width=1376, height=768, mapping_backend=backend,
                    seg_model=_SEG, **kw)


def test_loading_the_model_dominates_a_short_run() -> None:
    """Scenario: a 20-frame clip on LoGeR.

    Expected behaviour: the peak is the checkpoint load, not the frames, so the
    estimate stays in the region the isolated model load was measured at rather
    than collapsing towards zero.
    """
    cost = estimate_cost(_shape(20))
    assert cost.peak_stage.name == "load"
    assert 12 * _GB < cost.ram_bytes < 20 * _GB


def test_stages_are_a_maximum_not_a_sum() -> None:
    """The checkpoint copy is freed before the merge, so the two never add."""
    cost = estimate_cost(_shape(2000))
    total = sum(stage.bytes_at(2000) for stage in cost.stages)
    assert cost.ram_bytes < total
    assert cost.ram_bytes == max(stage.bytes_at(2000) for stage in cost.stages)


def test_the_backend_changes_the_estimate() -> None:
    """scsfmlearner returns depth only and holds no sequence of point maps.

    The gap is bounded by the prepared frames, which both backends carry and
    which dominate at a large processing resolution.
    """
    heavy = estimate_cost(_shape(2000, backend="loger_star"))
    light = estimate_cost(_shape(2000, backend="scsfmlearner"))
    assert light.ram_bytes < heavy.ram_bytes
    assert light.peak_stage.bytes_per_frame < heavy.peak_stage.bytes_per_frame
    assert light.vram_bytes < heavy.vram_bytes


def test_loger_star_costs_more_than_loger() -> None:
    """se3 routes through a sim3 merge that builds a second set of point maps."""
    assert estimate_cost(_shape(2000, backend="loger_star")).ram_bytes > estimate_cost(
        _shape(2000, backend="loger")
    ).ram_bytes


def test_an_unknown_backend_is_costed_as_the_most_expensive_one() -> None:
    """Overstating is recoverable; understating is an OOM kill."""
    assert estimate_cost(_shape(2000, backend="whatever")).ram_bytes == estimate_cost(
        _shape(2000, backend="loger_star")
    ).ram_bytes


def test_vram_grows_with_the_clip() -> None:
    """LoGeR uploads the whole sequence before inference."""
    assert estimate_cost(_shape(3000)).vram_bytes > estimate_cost(_shape(300)).vram_bytes


def test_swap_is_not_part_of_the_budget() -> None:
    """A peak that is one torch.cat thrashes rather than degrades in swap."""
    without = machine_budget(_profile(total_gb=32))
    with_swap = machine_budget(_profile(total_gb=32, swap_gb=64))
    assert without.ram_bytes == with_swap.ram_bytes


def test_the_budget_ignores_what_is_momentarily_free() -> None:
    """Otherwise the same settings change verdict when a browser opens."""
    busy = machine_budget(_profile(total_gb=64, avail_gb=8))
    idle = machine_budget(_profile(total_gb=64, avail_gb=60))
    assert busy.ram_bytes == idle.ram_bytes
    assert busy.ram_bytes < 64 * _GB  # an OS reserve is held back


def test_a_comfortable_run_fits() -> None:
    verdict = grade(_profile(total_gb=64), _shape(600))
    assert verdict.level == "ok"
    assert verdict.limit == ""


def test_too_long_a_pass_is_blocked_on_ram() -> None:
    verdict = grade(_profile(total_gb=64), _shape(8000))
    assert verdict.level == "block"
    assert verdict.limit == "ram"
    assert verdict.headline == "Too long to process in one pass"


def _small_card() -> GpuInfo:
    return GpuInfo(GPU_CUDA, "GTX", total_vram_bytes=8 * _GB, free_vram_bytes=8 * _GB)


def test_a_small_card_is_reported_as_the_limit() -> None:
    """RAM is ample but the backend will not fit the GPU.

    LoGeR holds more on the card before the first frame than the card has, so
    this is a fixed cost rather than a length: the pass does not fit at any
    trim, and the verdict says which of the two it is.
    """
    verdict = grade(_profile(total_gb=256, gpu=_small_card()), _shape(2000))
    assert verdict.level == "block"
    assert verdict.limit == "vram_fixed"
    assert "graphics" in verdict.detail


def test_a_fixed_cost_names_the_backend_that_caused_it() -> None:
    """Advice about the frame rate cannot be taken on a cost decided at zero frames."""
    verdict = grade(_profile(total_gb=256, gpu=_small_card()), _shape(2000))

    assert "loger_star" in verdict.headline
    assert "do not change that" in verdict.detail


def test_a_fixed_cost_offers_a_backend_that_actually_fits() -> None:
    """Re-graded rather than asserted, so the offer is never one that fails too."""
    fit = fit_for_pass(
        _profile(total_gb=256, gpu=_small_card()),
        seconds=400, fps=5, width=1376, height=768,
        mapping_backend="loger_star", seg_model=_SEG,
    )

    assert fit.verdict.limit_is_fixed
    assert fit.suggested_backend == "scsfmlearner"
    assert "scsfmlearner" in fit.advice
    # And the offer is good: the same pass on that backend is not refused.
    assert grade(
        _profile(total_gb=256, gpu=_small_card()),
        _shape(2000, backend="scsfmlearner"),
    ).level != "block"


def test_a_length_block_is_not_reported_as_a_fixed_one() -> None:
    """A card LoGeR fits on, with a clip too long for it, is still about length."""
    big_gpu = GpuInfo(GPU_CUDA, "RTX", total_vram_bytes=24 * _GB, free_vram_bytes=24 * _GB)
    # Long enough that the sequence tensor overflows the card, short enough that
    # RAM is not the thing that ran out first.
    verdict = grade(_profile(total_gb=256, gpu=big_gpu), _shape(10000))

    assert verdict.level == "block"
    assert verdict.limit == "vram"
    assert not verdict.limit_is_fixed


def test_the_card_is_graded_on_what_it_has_not_what_is_free() -> None:
    """A verdict that moves because something else opened cannot be acted on."""
    busy = GpuInfo(GPU_CUDA, "RTX", total_vram_bytes=24 * _GB, free_vram_bytes=2 * _GB)
    idle = GpuInfo(GPU_CUDA, "RTX", total_vram_bytes=24 * _GB, free_vram_bytes=24 * _GB)

    assert (
        machine_budget(_profile(gpu=busy)).vram_bytes
        == machine_budget(_profile(gpu=idle)).vram_bytes
    )


def test_a_recorded_peak_can_disprove_the_tabled_fixed_cost() -> None:
    """The tabled 9 GB is inferred, and is what refuses an 8 GB card outright.

    VRAM is one line, so a single recorded peak identifies its intercept and can
    move it down as well as up. Without that the figure could never be wrong.
    """
    tabled = estimate_cost(_shape(2000))
    measured = estimate_cost(
        _shape(2000),
        recorded={"vram_bytes": 5 * _GB, "vram_frames": 2000},
    )

    assert measured.fixed_vram_bytes < tabled.fixed_vram_bytes
    assert measured.vram_source == "measured"
    assert tabled.vram_source == "estimated"


def test_unified_memory_counts_the_gpu_against_ram() -> None:
    """On MPS the GPU draws from system RAM, so one pool serves both."""
    mps = GpuInfo(GPU_MPS, "Apple GPU", None, None)
    assert grade(_profile(total_gb=8, gpu=mps), _shape(1500)).level == "block"


def test_the_frame_ceiling_is_the_largest_run_that_fits() -> None:
    profile = _profile(total_gb=64)
    ceiling = max_frames(profile, _shape(0))
    assert grade(profile, _shape(ceiling)).level != "block"
    assert grade(profile, _shape(int(ceiling * 1.5))).level == "block"


def test_a_recorded_peak_raises_the_level_without_rescaling_its_baseline() -> None:
    """Scenario: a run recorded at 1000 frames peaked well above the model.

    Expected behaviour: the shortfall lifts the fixed term, so an estimate at
    twice the length does not also double that constant.
    """
    recorded = {"ram_bytes": 60 * _GB, "frames": 1000}
    plain = estimate_cost(_shape(2000))
    lifted = estimate_cost(_shape(2000), recorded=recorded)
    assert lifted.source == "measured"
    shortfall = 60 * _GB - plain.stages[0].bytes_at(1000)
    assert lifted.ram_bytes < plain.ram_bytes + 2 * shortfall


def test_a_pass_that_fits_is_told_so_without_advice() -> None:
    fit = fit_for_pass(
        _profile(total_gb=64), seconds=120.0, fps=5, width=1376, height=768,
        mapping_backend="loger_star", seg_model=_SEG,
    )
    assert fit.fits
    assert fit.advice == ""
    assert fit.max_seconds > fit.seconds


def test_a_pass_that_does_not_fit_names_a_frame_rate_that_would() -> None:
    fit = fit_for_pass(
        _profile(total_gb=64), seconds=1419.0, fps=5, width=1376, height=768,
        mapping_backend="loger_star", seg_model=_SEG,
    )
    assert not fit.fits
    assert fit.suggested_fps is not None
    assert fit.suggested_fps < fit.fps
    # The suggestion has to actually fit the pass as it stands.
    assert fit.seconds * fit.suggested_fps <= fit.verdict.max_frames
    assert f"FPS to {fit.suggested_fps}" in fit.advice


def test_advice_offers_a_trim_when_the_machine_can_still_do_a_useful_length() -> None:
    fit = fit_for_pass(
        _profile(total_gb=64), seconds=3600.0, fps=5, width=1376, height=768,
        mapping_backend="loger_star", seg_model=_SEG,
    )
    assert "trim" in fit.advice
    assert fit.max_seconds >= 60


def test_no_trim_is_suggested_that_barely_shortens_the_pass() -> None:
    """A pass told to trim to its own length has been given nothing to do."""
    profile = _profile(total_gb=64)
    # Just over the ceiling: it does not fit, but trimming would barely move it.
    ceiling = max_frames(profile, _shape(0))
    fit = fit_for_pass(
        profile, seconds=(ceiling / 5) / 0.95, fps=5, width=1376, height=768,
        mapping_backend="loger_star", seg_model=_SEG,
    )
    assert not fit.fits
    assert fit.suggested_seconds is None
    assert "trim" not in fit.advice


def test_a_machine_too_small_for_any_length_still_advises() -> None:
    """No frame rate and no trim helps once the model alone will not load."""
    fit = fit_for_pass(
        _profile(total_gb=8), seconds=600.0, fps=5, width=1376, height=768,
        mapping_backend="loger_star", seg_model=_SEG,
    )
    assert fit.verdict.max_frames == 0
    assert fit.advice


def test_a_segmentation_overflow_names_the_segmenter_not_the_backend() -> None:
    """The two fixed terms do not add, so only one of them is the whole number.

    Blaming the mapping backend for the segmenter's batch sent the user to a
    control that could not help, and then told them nothing could.
    """
    card = GpuInfo(GPU_CUDA, "RTX", total_vram_bytes=6 * _GB, free_vram_bytes=6 * _GB)
    fit = fit_for_pass(
        _profile(total_gb=64, gpu=card),
        seconds=300, fps=5, width=1376, height=768,
        mapping_backend="scsfmlearner", seg_model="segformer-b5", batch_size=4,
    )

    assert fit.verdict.limit == "vram_fixed"
    assert fit.verdict.cost.vram_fixed_from == "segmentation"
    assert "segformer-b5" in fit.headline
    assert "scsfmlearner" not in fit.headline
    # And the fix is the control that actually moves it.
    assert fit.suggested_batch_size is not None
    assert "batch size" in fit.advice


def test_a_backend_overflow_still_names_the_backend() -> None:
    card = GpuInfo(GPU_CUDA, "GTX", total_vram_bytes=8 * _GB, free_vram_bytes=8 * _GB)
    fit = fit_for_pass(
        _profile(total_gb=256, gpu=card),
        seconds=400, fps=5, width=1376, height=768,
        mapping_backend="loger_star", seg_model=_SEG, batch_size=4,
    )

    assert fit.verdict.cost.vram_fixed_from == "mapping"
    assert "loger_star" in fit.headline
    assert "scsfmlearner" in fit.advice


def test_a_card_reporting_only_free_vram_is_still_graded() -> None:
    """A VRAM budget of None grades every run "ok" however large it is."""
    partial = GpuInfo(GPU_CUDA, "RTX", total_vram_bytes=None, free_vram_bytes=8 * _GB)
    budget = machine_budget(_profile(gpu=partial))

    assert budget.vram_bytes is not None
    assert grade(_profile(total_gb=256, gpu=partial), _shape(2000)).level == "block"
