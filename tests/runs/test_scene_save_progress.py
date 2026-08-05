"""Writing the scene file, and reporting it on the progress bars.

The scene file used to be written on a detached daemon thread during the first
*load* of a run, while `scene_save` carried 8% of the reconstruction bar and 14%
of the ETA's weight. Nothing ever reported it, so the reconstruction bar stopped
at 92.6% and every prediction reserved time for a stage that never ran.

The write now happens at the end of the reconstruction, from data already in
memory. These pin the two halves that make that visible: every stage the writer
emits is routed to the `scene_save` phase, and the phase is weighted so the bar
actually finishes.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
from _factories import make_classes_config, make_scene
from deepreefmap.pipeline.artifacts import SemanticPointCloud

from deepreefmap_gui.io.scene_file import find_scene_file, load_scene_file
from deepreefmap_gui.runs.loaded_run import scene_file_pending, write_scene_file
from deepreefmap_gui.runs.progress import (
    _LOAD_PHASES,
    _LOAD_STAGE_TO_PHASE,
    _RECON_PHASES,
    _SCENE_SAVE_STAGES,
    ProgressModel,
)

H, W, N_FRAMES = 4, 6, 3
CLASS_ID = 1

# The merged manifest instrumented_reconstruction hands the writer: the run name
# and survey block are folded in by then, and the scene embeds this, not the file
# the pipeline left on disk.
MANIFEST = {"mode": "semantic", "name": "reef north", "survey": {"pass": {"direction": "forward"}}}


@pytest.fixture
def run_data(tmp_path):
    """A finished run's set_data payload, plus the manifest it wrote."""
    scene = make_scene(frame_indices=tuple(range(N_FRAMES)), size=(W, H), class_ids=(CLASS_ID,))
    rng = np.random.default_rng(1)
    n_points = 20
    cloud = SemanticPointCloud(
        xyz=rng.random((n_points, 3)).astype(np.float32),
        rgb=rng.integers(0, 255, (n_points, 3), dtype=np.uint8),
        labels=np.full(n_points, CLASS_ID, dtype=np.int32),
        frame_indices=rng.integers(0, N_FRAMES, n_points).astype(np.int32),
    )
    (tmp_path / "run_manifest.json").write_text(json.dumps({"mode": "semantic"}))
    return tmp_path, {
        "frame_batch": scene.frame_batch,
        "mapping_result": scene.mapping,
        "reference_cloud": cloud,
        "classes_config": make_classes_config((CLASS_ID,)),
    }


# --- writing it ---------------------------------------------------------


def _write(run_dir, data, manifest, **kwargs):
    """Write a scene from a set_data payload, the way the loader does."""
    return write_scene_file(
        run_dir,
        manifest=manifest,
        classes_config=data["classes_config"],
        mapping_result=data["mapping_result"],
        frame_batch=data["frame_batch"],
        reference_cloud=data["reference_cloud"],
        **kwargs,
    )


def test_a_finished_run_writes_a_scene_file_the_loader_accepts(run_data):
    """End of the reconstruction, straight from memory: the point of doing it
    here is that the run's first open takes the fast path."""
    run_dir, data = run_data

    out = _write(run_dir, data, MANIFEST)

    assert out is not None and out.exists()
    assert find_scene_file(run_dir) == out
    scene = load_scene_file(out, run_dir=run_dir)
    assert scene is not None
    assert scene.manifest["name"] == "reef north"
    assert scene.manifest["survey"]["pass"]["direction"] == "forward"
    assert len(scene.frame_indices) == N_FRAMES


def test_a_geometry_only_run_is_never_owed_a_scene_file(run_data):
    """No reference cloud, so there is no semantic scene to cache.

    Asserted on the gate rather than the writer: `scene_file_pending` is what
    the loader checks before starting a write at all.
    """
    from types import SimpleNamespace

    _run_dir, data = run_data
    geometry_only = SimpleNamespace(
        from_scene_file=False,
        mode="semantic",
        reference_cloud=SemanticPointCloud.empty(),
    )
    semantic = SimpleNamespace(
        from_scene_file=False,
        mode="semantic",
        reference_cloud=data["reference_cloud"],
    )

    assert not scene_file_pending(geometry_only)
    assert scene_file_pending(semantic)


def test_the_write_reports_progress_the_bar_can_follow(run_data):
    """Per-frame ticks are what give the ETA a rate to measure; a write that
    only reported start and finish would leave the bar frozen throughout."""
    run_dir, data = run_data
    seen: list[tuple[str, int, int]] = []

    _write(run_dir, data, MANIFEST, progress_cb=lambda s, c, t: seen.append((s, c, t)))

    stages = [s for s, _c, _t in seen]
    assert stages[0] == "scene_index"
    assert stages[-1] == "scene_done"
    # The index build is the whole cost now that no pixels are written, so it is
    # the stage that has to report both ends for the bar to move at all.
    assert ("scene_index", 0, 1) in seen and ("scene_index", 1, 1) in seen


# --- showing it ---------------------------------------------------------


def test_every_stage_the_writer_emits_drives_the_scene_save_phase(run_data):
    """A stage name with no phase falls through _LOAD_STAGE_TO_PHASE as its own
    unknown key, which no bar has weight for -- the fill would just stop."""
    run_dir, data = run_data
    seen: set[str] = set()

    _write(run_dir, data, MANIFEST, progress_cb=lambda s, _c, _t: seen.add(s))

    assert seen == set(_SCENE_SAVE_STAGES)
    for stage in seen:
        assert _LOAD_STAGE_TO_PHASE.get(stage) == "scene_save"


@pytest.mark.parametrize("phases", [_RECON_PHASES, _LOAD_PHASES], ids=["recon", "load"])
def test_the_bar_reaches_a_hundred_percent(phases):
    """scene_save holds real weight on both bars. While nothing reported it the
    reconstruction bar's ceiling was 92.6%."""
    model = ProgressModel(phases)
    for key, _weight in phases:
        model.update(key, 1, 1)

    assert model.total_percent() == 100


def test_the_scene_write_is_the_last_thing_the_bar_shows():
    model = ProgressModel(_RECON_PHASES)
    for key, _weight in _RECON_PHASES:
        if key == "scene_save":
            break
        model.update(key, 1, 1)
    before = model.total_percent()

    model.update("scene_save", 1, 2)
    halfway = model.total_percent()
    model.update("scene_save", 2, 2)

    assert before < halfway < model.total_percent() == 100
