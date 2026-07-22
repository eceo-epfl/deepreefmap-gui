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


def apply_manifest_timings(
    output_dir: Path,
    instr: RunInstrumentation,
    *,
    run_name: str | None = None,
    manifest_extra: dict | None = None,
) -> dict | None:
    """Fold measured durations/peaks (plus name + survey block) into run_manifest.json.

    ``run_reconstruction`` no longer stamps the run name or extra manifest blocks
    itself, so this is where they persist. Returns the merged manifest, or None
    when the run wrote no manifest to fold into.
    """
    import json

    manifest_path = output_dir / "run_manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception:
        return None
    if run_name is not None:
        manifest["name"] = run_name
    manifest.update(manifest_extra or {})
    manifest["stage_durations"] = instr.stage_durations()
    manifest["stage_peaks"] = instr.stage_peaks()
    manifest["run_duration_s"] = instr.total_seconds()
    manifest["system_profile"] = instr.system_profile
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest


class _MarkingViewer:
    """Wrap the run viewer so stage transitions drive instrumentation marks.

    Records marks using the same names durations_from_marks expects; inner may be
    None (headless/batch), in which case viewer methods are no-ops.
    """

    _STAGE_TO_MARK = {"preprocess": "preprocess", "mapping": "mapping", "outputs": "cloud"}

    def __init__(self, inner, instr):
        self._inner = inner
        self._instr = instr

    def start_run(self, run_label, output_dir):
        if self._inner is not None:
            self._inner.start_run(run_label, output_dir)

    def set_stage(self, stage, status, message=None):
        m = self._STAGE_TO_MARK.get(stage)
        if m and m not in self._instr.marks:
            self._instr.mark(m)
        if self._inner is not None:
            self._inner.set_stage(stage, status, message)

    def update_progress(self, *a, **k):
        if self._inner is not None:
            self._inner.update_progress(*a, **k)

    def set_data(self, **k):
        if self._inner is not None:
            self._inner.set_data(**k)

    def mark_outputs_ready(self, output_dir, output_files):
        if "end" not in self._instr.marks:
            self._instr.mark("end")
        if self._inner is not None:
            self._inner.mark_outputs_ready(output_dir, output_files)

    def fail_run(self, stage, error_message):
        if self._inner is not None:
            self._inner.fail_run(stage, error_message)

    def close(self):
        if self._inner is not None:
            self._inner.close()

    def wait_forever(self):
        if self._inner is not None:
            self._inner.wait_forever()


def instrumented_reconstruction(
    *,
    run_name: str | None = None,
    manifest_extra: dict | None = None,
    **kwargs,
) -> None:
    """run_reconstruction with stage timing + memory sampling, folded into the
    run manifest and the local run-history profile afterwards.

    ``run_name`` and ``manifest_extra`` are written to the manifest after the run
    (``run_reconstruction`` no longer accepts them). Stage marks are captured via
    a viewer proxy since the orchestrator no longer emits ``on_mark`` callbacks."""
    from deepreefmap.pipeline.orchestrator import run_reconstruction

    from deepreefmap_gui.profiling.run_history import record_run_from_manifest

    output_dir = Path(kwargs["output_dir"])
    instr = RunInstrumentation(output_dir)
    proxy = _MarkingViewer(kwargs.pop("viewer", None), instr)
    try:
        run_reconstruction(viewer=proxy, **kwargs)
    finally:
        instr.stop()
    manifest = apply_manifest_timings(
        output_dir, instr, run_name=run_name, manifest_extra=manifest_extra
    )
    if manifest is not None:
        record_run_from_manifest(manifest)
