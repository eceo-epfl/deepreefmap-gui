"""record_run_from_manifest: the only production path into the timing profile.

instrumented_reconstruction calls it with whatever the manifest happens to hold,
so it has to survive a manifest written by an older build or a partial run. Every
other run_history test calls record_run directly, which skips the key derivation
and the coercions that live here.
"""

from __future__ import annotations

import json

import pytest

from deepreefmap_gui.profiling.run_history import (
    history_key,
    load_priors,
    record_run_from_manifest,
)


def _manifest(**overrides) -> dict:
    manifest = {
        "mapping_backend": "loger_star",
        "segmentation_model": "coralscapes-vit-b-dpt",
        "processing_width": 1376,
        "processing_height": 768,
        "fps": 5,
        "frames_processed": 1134,
        "metric_points": 14_000_000,
        "stage_durations": {"preprocess": 120.0, "mapping": 300.0},
        "stage_peaks": {"mapping": {"ram_bytes": 30}},
        "system_profile": {"os_name": "Linux"},
    }
    manifest.update(overrides)
    return manifest


def test_a_finished_run_lands_under_its_history_key(timings) -> None:
    record_run_from_manifest(_manifest())

    stored = json.loads(timings.read_text())
    key = history_key("loger_star", "coralscapes-vit-b-dpt", 1376, 768, 5)
    assert key in stored
    entry = stored[key][0]
    assert entry["stage_durations"] == {"preprocess": 120.0, "mapping": 300.0}
    assert entry["frames"] == 1134
    assert entry["points"] == 14_000_000
    assert entry["params"]["mapping_backend"] == "loger_star"
    assert entry["stage_peaks"] == {"mapping": {"ram_bytes": 30}}
    assert entry["system_profile"] == {"os_name": "Linux"}


def test_the_recorded_run_becomes_a_prior(timings) -> None:
    """The whole point of recording: the next run estimates from this one."""
    record_run_from_manifest(_manifest())
    priors = load_priors(history_key("loger_star", "coralscapes-vit-b-dpt", 1376, 768, 5))
    assert priors["preprocess"] == pytest.approx(120.0 / 1134)


def test_a_run_with_no_timings_is_not_recorded(timings) -> None:
    record_run_from_manifest(_manifest(stage_durations={}))
    record_run_from_manifest(_manifest(stage_durations=None))
    assert not timings.exists()


@pytest.mark.parametrize(
    "overrides",
    [
        {"processing_width": "1376", "fps": "5"},           # strings from older manifests
        {"processing_width": 1376.0, "processing_height": 768.0},  # floats
        {"frames_processed": None},
        {"metric_points": None},
        {"stage_peaks": None, "system_profile": None},
    ],
)
def test_loosely_typed_manifests_are_coerced_not_rejected(timings, overrides) -> None:
    """A field the run did not measure is null, not absent.

    `.get(key, default)` only covers an absent key, so a null used to raise
    inside the blanket except below and lose the whole run's timings without a
    trace. The library writes ints today; this keeps that from being load-bearing.
    """
    record_run_from_manifest(_manifest(**overrides))
    assert timings.exists(), f"{overrides} silently dropped the run"


def test_a_manifest_that_cannot_be_read_is_swallowed(timings) -> None:
    """A recording failure must not take the run down with it: the run succeeded."""
    record_run_from_manifest(_manifest(processing_width="not a number"))
    assert not timings.exists()


def test_absent_optional_keys_stay_out_of_params(timings) -> None:
    record_run_from_manifest(_manifest(enable_tsdf=None, grid_bins=2000))
    entry = json.loads(timings.read_text())[
        history_key("loger_star", "coralscapes-vit-b-dpt", 1376, 768, 5)
    ][0]
    assert "enable_tsdf" not in entry["params"]
    assert entry["params"]["grid_bins"] == 2000
