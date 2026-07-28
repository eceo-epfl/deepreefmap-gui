"""instrumented_reconstruction: the wrapper every run actually goes through.

Called from form/batch.py, runs/loading.py and simple/batch.py, and previously
untested end to end -- the existing tests all hand-build mark dicts and call
durations_from_marks directly, which is how the missing ortho/save marks went
unnoticed. These drive the real wrapper with a stubbed orchestrator and assert on
what lands in run_manifest.json and the timing profile.
"""

from __future__ import annotations

import json

import pytest

from deepreefmap_gui.profiling.instrumentation import (
    UNPRODUCIBLE_STAGES,
    RunInstrumentation,
    apply_manifest_timings,
    instrumented_reconstruction,
)


@pytest.fixture
def timings(tmp_path, monkeypatch):
    path = tmp_path / "run_timings.json"
    monkeypatch.setenv("DEEPREEFMAP_RUN_TIMINGS", str(path))
    return path


@pytest.fixture
def out_dir(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    return d


def _fake_run(**manifest_fields):
    """An orchestrator stub that reports stages like the real one and writes a manifest."""

    def run(*, viewer, output_dir, **_kwargs):
        viewer.start_run("run", str(output_dir))
        viewer.set_stage("startup", "running", "Loading camera + segmentation + mapping backends")
        viewer.set_stage("preprocess", "running", "Rectifying + segmenting + masking")
        viewer.set_stage("mapping", "running", "3D mapping pipeline in progress")
        viewer.update_progress("outputs", current=1, total=2, message="Building semantic cloud")
        viewer.set_stage("outputs", "running", "Computing PCA projection")
        viewer.set_stage("outputs", "running", "Saving ortho image")
        manifest = {
            "mode": "semantic",
            "mapping_backend": "loger_star",
            "segmentation_model": "coralscapes-vit-b-dpt",
            "processing_width": 1376,
            "processing_height": 768,
            "fps": 5,
            "frames_processed": 120,
            **manifest_fields,
        }
        (output_dir / "run_manifest.json").write_text(json.dumps(manifest))
        viewer.mark_outputs_ready(str(output_dir), [])

    return run


def test_a_run_folds_timings_and_name_into_the_manifest(out_dir, timings, monkeypatch) -> None:
    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", _fake_run())

    instrumented_reconstruction(
        output_dir=out_dir,
        run_name="reef north",
        manifest_extra={"survey": {"pass": {"direction": "forward"}}},
        viewer=None,
    )

    manifest = json.loads((out_dir / "run_manifest.json").read_text())
    assert manifest["name"] == "reef north"
    assert manifest["survey"]["pass"]["direction"] == "forward"
    assert manifest["mode"] == "semantic"          # the run's own fields survive
    assert manifest["run_duration_s"] >= 0.0
    assert manifest["system_profile"]["os_name"]

    # The whole point: every stage the pipeline can report is timed.
    measured = set(manifest["stage_durations"])
    assert measured == {"startup", "preprocess", "mapping", "cloud", "ortho", "save_view"}
    assert "scene_save" in UNPRODUCIBLE_STAGES and "scene_save" not in measured


def test_a_run_is_recorded_into_the_timing_profile(out_dir, timings, monkeypatch) -> None:
    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", _fake_run())

    instrumented_reconstruction(output_dir=out_dir, viewer=None)

    stored = json.loads(timings.read_text())
    assert len(stored) == 1
    entry = next(iter(stored.values()))[0]
    assert entry["frames"] == 120
    assert set(entry["stage_durations"]) >= {"preprocess", "mapping", "ortho"}


def test_the_viewer_proxy_forwards_everything_to_the_real_viewer(out_dir, timings, monkeypatch) -> None:
    """The proxy wraps the viewer, so a dropped delegation blanks the UI mid-run."""
    seen: list[str] = []

    class Recorder:
        def __getattr__(self, name):
            def record(*a, **k):
                seen.append(name)
            return record

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", _fake_run())
    instrumented_reconstruction(output_dir=out_dir, viewer=Recorder())

    assert seen.count("start_run") == 1
    assert seen.count("set_stage") == 5
    assert seen.count("update_progress") == 1
    assert seen.count("mark_outputs_ready") == 1


def test_a_headless_run_needs_no_viewer(out_dir, timings, monkeypatch) -> None:
    """Batch runs pass viewer=None; the proxy must no-op rather than crash."""
    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", _fake_run())
    instrumented_reconstruction(output_dir=out_dir, viewer=None)
    assert (out_dir / "run_manifest.json").exists()


def test_the_sampler_is_stopped_even_when_the_run_raises(out_dir, timings, monkeypatch) -> None:
    """Otherwise the polling thread outlives the run, for the life of the process."""
    created: list[RunInstrumentation] = []
    real_init = RunInstrumentation.__init__

    def spy(self, output_dir):
        real_init(self, output_dir)
        created.append(self)

    monkeypatch.setattr(RunInstrumentation, "__init__", spy)

    def boom(**_kwargs):
        raise RuntimeError("mapping backend died")

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", boom)

    with pytest.raises(RuntimeError):
        instrumented_reconstruction(output_dir=out_dir, viewer=None)

    assert created and created[0]._sampler._thread is None


def test_a_run_that_wrote_no_manifest_records_nothing(out_dir, timings, monkeypatch) -> None:
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction", lambda **k: None
    )
    instrumented_reconstruction(output_dir=out_dir, viewer=None)
    assert not (out_dir / "run_manifest.json").exists()
    assert not timings.exists()


def test_apply_manifest_timings_leaves_an_unreadable_manifest_alone(out_dir) -> None:
    (out_dir / "run_manifest.json").write_text("{not json")
    instr = RunInstrumentation(out_dir)
    try:
        assert apply_manifest_timings(out_dir, instr) is None
    finally:
        instr.stop()
