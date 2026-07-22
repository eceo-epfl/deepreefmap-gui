"""Stage timing marks and resource sampling for one reconstruction run."""

from __future__ import annotations

import time
from pathlib import Path

from deepreefmap_gui.profiling.perf_sampler import ResourceSampler, peaks_from_marks

# Coarse pipeline stages, in order, that the timing marks bracket. The GUI reads
# these durations back to build a per-machine cost profile
# (profiling/run_history.py), so the keys match profiling/eta.py's stage keys.
STAGE_SPANS: tuple[tuple[str, str, str], ...] = (
    ("start", "preprocess", "startup"),
    ("preprocess", "mapping", "preprocess"),
    ("mapping", "cloud", "mapping"),
    ("cloud", "ortho", "cloud"),
    ("ortho", "save", "ortho"),
    ("save", "end", "save_view"),
    ("end", "scene_end", "scene_save"),
)


def durations_from_marks(marks: dict[str, float]) -> dict[str, float]:
    """Wall-clock seconds per coarse stage from sequential monotonic marks."""
    durations: dict[str, float] = {}
    for begin, end, stage in STAGE_SPANS:
        if begin in marks and end in marks and marks[end] >= marks[begin]:
            durations[stage] = marks[end] - marks[begin]
    return durations


class RunInstrumentation:
    """One run's stage marks, memory sampling and machine profile.

    The real per-stage peaks land in the manifest and feed the pre-run memory check.
    """

    def __init__(self, output_dir: Path) -> None:
        from deepreefmap_gui.profiling.system_probe import probe_system

        self.marks: dict[str, float] = {"start": time.monotonic()}
        self.system_profile: dict = probe_system(output_dir).to_dict()
        self._sampler = ResourceSampler()
        self._sampler.start()

    def mark(self, name: str) -> None:
        self.marks[name] = time.monotonic()

    def stage_durations(self) -> dict[str, float]:
        return durations_from_marks(self.marks)

    def total_seconds(self) -> float:
        """Wall-clock seconds from run start to the latest mark."""
        return max(self.marks.values()) - self.marks["start"]

    def stage_peaks(self) -> dict[str, dict[str, int | None]]:
        return peaks_from_marks(self._sampler.samples, STAGE_SPANS, self.marks)

    def stop(self) -> None:
        """Join the sampler thread, which otherwise polls for the life of the process."""
        self._sampler.stop()


def apply_manifest_timings(output_dir: Path, instr: RunInstrumentation) -> dict | None:
    """Fold measured durations/peaks into run_manifest.json; returns the manifest."""
    import json

    manifest_path = output_dir / "run_manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception:
        return None
    manifest["stage_durations"] = instr.stage_durations()
    manifest["stage_peaks"] = instr.stage_peaks()
    manifest["run_duration_s"] = instr.total_seconds()
    manifest["system_profile"] = instr.system_profile
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest


def instrumented_reconstruction(**kwargs) -> None:
    """run_reconstruction with stage timing + memory sampling, folded into the
    run manifest and the local run-history profile afterwards."""
    from deepreefmap.pipeline.orchestrator import run_reconstruction

    from deepreefmap_gui.profiling.run_history import record_run_from_manifest

    output_dir = Path(kwargs["output_dir"])
    instr = RunInstrumentation(output_dir)
    try:
        run_reconstruction(on_mark=instr.mark, **kwargs)
    finally:
        instr.stop()
    manifest = apply_manifest_timings(output_dir, instr)
    if manifest is not None:
        record_run_from_manifest(manifest)
