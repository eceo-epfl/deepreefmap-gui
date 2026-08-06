import json
import threading
import time
from pathlib import Path

import pytest
from _factories import make_transect
from _qt_wait import wait_until
from deepreefmap.pipeline.orchestrator import ReconstructionCancelled
from PySide6.QtWidgets import QDialog, QMessageBox

from deepreefmap_gui.core.widgets import PASS_PERCENT_ROLE
from deepreefmap_gui.simple.batch import (
    _COL_ACTION,
    _COL_DIRECTION,
    _COL_STATUS,
    _COL_TRANSECT,
    _COL_TRIM,
    _COL_VIDEO,
    _diagnose_failure,
    _median_pass_seconds,
    _rough_batch_time,
)
from deepreefmap_gui.simple.batch_progress import BatchProgressCard
from deepreefmap_gui.simple.mode import SIMPLE_SECTIONS


def test_diagnose_failure_speaks_plainly_and_advises():
    assert "graphics memory" in _diagnose_failure("RuntimeError: CUDA out of memory")
    assert "not installed" in _diagnose_failure("FileNotFoundError: checkpoint not found")
    assert "could not be read" in _diagnose_failure("Failed to decode video stream")
    # An unrecognised error keeps its own first line rather than inventing advice.
    assert _diagnose_failure("Weird thing: 42\nsecond line") == "Weird thing: 42"
    assert _diagnose_failure("") == "The run failed. No cause was recorded."


def test_rough_batch_time_is_silent_without_history(monkeypatch):
    monkeypatch.setattr(
        "deepreefmap_gui.profiling.run_history.summarise_recorded_runs", list
    )
    assert _rough_batch_time(10) is None


def test_rough_batch_time_scales_with_the_pass_count(monkeypatch):
    monkeypatch.setattr(
        "deepreefmap_gui.profiling.run_history.summarise_recorded_runs",
        lambda: [{"run_seconds": 1200.0}],
    )
    assert _rough_batch_time(1) == "about 20 minutes"
    assert _rough_batch_time(6) == "about 2 hours"


@pytest.fixture
def batch_window(window, tmp_path, monkeypatch):
    window._survey_store().add_transect(make_transect())
    window._refresh_survey_batch_tab()
    monkeypatch.setattr(window, "_survey_missing_models", list)
    return window


def await_batch(window, qapp, timeout=10.0):
    """Wait for the batch worker, then drain the signals it queued.

    join(timeout=...) returns whether or not the thread finished, so an unchecked
    join turns a hang into a confusing assertion failure further down. Draining
    has to loop as well: each processEvents() pass delivers the queued
    cross-thread signals, and a slot may queue more.
    """
    thread = window._pipeline_thread
    thread.join(timeout=timeout)
    assert not thread.is_alive(), f"batch worker still running after {timeout}s"

    deadline = time.monotonic() + timeout
    while window._survey_worker_running and time.monotonic() < deadline:
        qapp.processEvents()
    qapp.processEvents()
    assert not window._survey_worker_running, "batch finished but the UI never caught up"


def add_video(window, tmp_path, monkeypatch, name="GX010001.MP4", duration_s=60.0):
    path = tmp_path / name
    path.write_bytes(name.encode() * 4096)
    monkeypatch.setattr(
        "deepreefmap_gui.simple.batch._probe_video", lambda _path: (duration_s, 30.0)
    )
    monkeypatch.setattr(
        "deepreefmap_gui.simple.batch.QFileDialog.getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(path)], "")),
    )
    rows = len(window._survey_rows)
    window._on_survey_add_videos()
    assert wait_until(lambda: len(window._survey_rows) > rows), "probe never landed"
    return path


def add_videos(window, tmp_path, monkeypatch, names, duration_s=60.0):
    """Add several clips in one action, as selecting a card's worth does."""
    paths = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(name.encode() * 4096)
        paths.append(str(path))
    monkeypatch.setattr(
        "deepreefmap_gui.simple.batch._probe_video", lambda _path: (duration_s, 30.0)
    )
    rows = len(window._survey_rows)
    window._add_video_paths(paths)
    assert wait_until(lambda: len(window._survey_rows) > rows), "probe never landed"
    return paths


def assign_transect(window, row_index):
    combo = window._survey_pass_table.cellWidget(window._table_row_of(row_index), _COL_TRANSECT)
    combo.setCurrentIndex(1)


def add_second_transect(window, name="T2"):
    window._survey_store().add_transect(
        make_transect(name, start_lat=-17.6, start_lon=177.2, end_lat=-17.6005, end_lon=177.2005)
    )
    window._refresh_survey_batch_tab()


def stub_trim_dialog(monkeypatch, begin_s, end_s, accepted=True):
    """Stand in for the scrub dialog, which needs a decodable file and a screen."""
    class _FakeScrub:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected

        def time_range(self):
            return (begin_s, end_s)

    monkeypatch.setattr("deepreefmap_gui.simple.batch.VideoScrubDialog", _FakeScrub)


def answer_question(monkeypatch, button):
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: button))


def stored_directions(window):
    store = window._survey_store()
    return [store.get_pass(row.pass_id).direction for row in window._survey_rows]


def test_add_video_stays_unassigned_between_transects(batch_window, tmp_path, monkeypatch):
    add_second_transect(batch_window)
    add_video(batch_window, tmp_path, monkeypatch)
    assert len(batch_window._survey_rows) == 1
    assert batch_window._survey_rows[0].transect_id is None
    assert batch_window._survey_rows[0].end_s == 60.0
    # Runnable as it stands: skipping the transect costs the comparison against
    # repeat passes, not the run.
    assert batch_window._survey_start_btn.isEnabled()
    combo = batch_window._survey_pass_table.cellWidget(batch_window._table_row_of(0), _COL_TRANSECT)
    assert combo.currentText() == "Skip transect"
    assert combo.styleSheet() != ""
    assert len(batch_window._survey_store().list_passes()) == 1
    assert batch_window._survey_store().list_passes()[0].transect_id is None


def test_add_video_preselects_the_only_transect(batch_window, tmp_path, monkeypatch):
    add_video(batch_window, tmp_path, monkeypatch)
    assert batch_window._survey_rows[0].transect_id is not None
    assert len(batch_window._survey_store().list_passes()) == 1
    combo = batch_window._survey_pass_table.cellWidget(batch_window._table_row_of(0), _COL_TRANSECT)
    assert combo.currentText() == "T1"
    assert combo.styleSheet() == ""
    assert batch_window._survey_start_btn.isEnabled()


def test_assigning_transect_persists_pass(batch_window, tmp_path, monkeypatch):
    add_second_transect(batch_window)
    add_video(batch_window, tmp_path, monkeypatch)
    # Queued before a transect is chosen, and updated in place when one is.
    assert [p.transect_id for p in batch_window._survey_store().list_passes()] == [None]
    assign_transect(batch_window, 0)
    passes = batch_window._survey_store().list_passes()
    assert len(passes) == 1
    assert passes[0].transect_id is not None
    assert passes[0].begin_s == 0.0
    assert passes[0].end_s == 60.0
    assert passes[0].direction == "forward"
    assert passes[0].batch_id is not None
    assert batch_window._survey_start_btn.isEnabled()
    assert batch_window._survey_start_btn.text() == "Start processing (1 pass)"


def test_split_pass_duplicates_row(batch_window, tmp_path, monkeypatch):
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._survey_pass_table.setCurrentCell(batch_window._table_row_of(0), 0)
    batch_window._on_survey_split_pass()
    assert len(batch_window._survey_rows) == 2
    assert len(batch_window._survey_store().list_passes()) == 2


def test_each_pass_leaves_a_log_beside_its_outputs(
    batch_window, tmp_path, out_root, monkeypatch, qapp
):
    """A batch runs unattended for hours and the log view is in memory, so a
    pass that failed overnight has to leave something on disk to read.

    RunDetailPanel looks for run.log in the run directory, so this is the file
    it looks for, not a file of our own choosing.
    """
    import logging

    def fake_run(**kwargs):
        logging.getLogger("deepreefmap").info("a line from inside the pass")

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fake_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    logs = list(out_root.glob("*/run.log"))
    assert len(logs) == 1
    assert "a line from inside the pass" in logs[0].read_text()


def test_run_batch_records_success_and_links_manifest(
    batch_window, tmp_path, out_root, monkeypatch, qapp
):
    calls = []

    def fake_run(**kwargs):
        calls.append(kwargs)
        # instrumented_reconstruction folds the run name + survey block into the
        # manifest after the run, so give it one to fold into.
        (kwargs["output_dir"] / "run_manifest.json").write_text(json.dumps({"mode": "semantic"}))

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fake_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    assert len(calls) == 1
    kwargs = calls[0]
    # instrumented_reconstruction wraps the viewer in a stage-marking proxy.
    assert kwargs["viewer"]._inner is batch_window._viewer
    assert kwargs["fps"] == 5
    assert kwargs["begin_s"] == 0.0
    assert kwargs["end_s"] == 60.0
    assert kwargs["transect_length"] == 50.0
    assert kwargs["output_dir"].parent == out_root
    # run_name and the survey block are no longer passed to run_reconstruction;
    # they land in the manifest instrumented_reconstruction writes afterward.
    manifest = json.loads((kwargs["output_dir"] / "run_manifest.json").read_text())
    assert manifest["name"] == kwargs["output_dir"].name
    survey = manifest["survey"]
    assert survey["transect"]["name"] == "T1"
    assert survey["pass"]["direction"] == "forward"
    runs = batch_window._survey_store().list_runs()
    assert [r.status for r in runs] == ["succeeded"]
    # Nothing left to process, so the start button has no action and says so.
    # The quiet route to the results is there for a return visit; the batch
    # itself has already taken the user to them.
    assert batch_window._survey_start_btn.text() == "Nothing left to process"
    assert not batch_window._survey_start_btn.isEnabled()
    batch_window._set_simple_section("process")
    assert batch_window._survey_results_btn.isVisibleTo(batch_window)
    assert batch_window._survey_pass_table.item(batch_window._table_row_of(0), _COL_STATUS).text() == "Succeeded"


def test_manifest_records_the_configuration_and_any_deviation(
    batch_window, tmp_path, out_root, monkeypatch, qapp
):
    """Scenario: a diver processes a pass after changing one setting.

    Expected behaviour: the manifest names the organisation preset behind the run
    and the setting that deviated, so the number can be audited later.
    """
    def fake_run(**kwargs):
        (kwargs["output_dir"] / "run_manifest.json").write_text(json.dumps({"mode": "semantic"}))

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fake_run)
    org = batch_window._active_preset.org
    batch_window._batch_size_spin.setValue(1)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    run_dir = next(p for p in out_root.iterdir() if p.is_dir() and p.name.startswith("T1__"))
    survey = json.loads((run_dir / "run_manifest.json").read_text())["survey"]
    config = survey["provenance"]["config"]
    assert config["preset_name"] == org.name
    assert config["preset_version"] == org.version
    assert config["preset_hash"] == org.content_hash
    assert config["deviated"] is True
    assert config["deviations"] == {"preprocess_batch_size": 1}
    assert survey["preset_name"] == org.name


def test_manifest_of_a_standard_run_records_no_deviation(
    batch_window, tmp_path, out_root, monkeypatch, qapp
):
    def fake_run(**kwargs):
        (kwargs["output_dir"] / "run_manifest.json").write_text(json.dumps({"mode": "semantic"}))

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fake_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    run_dir = next(p for p in out_root.iterdir() if p.is_dir() and p.name.startswith("T1__"))
    config = json.loads((run_dir / "run_manifest.json").read_text())["survey"]["provenance"]["config"]
    assert config["deviated"] is False
    assert config["deviations"] == {}


def test_advanced_settings_reach_a_survey_run(batch_window, tmp_path, monkeypatch, qapp):
    """A survey run honours the whole run form, not just the core preset keys."""
    calls = []
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction",
        lambda **kwargs: calls.append(kwargs),
    )
    batch_window._grid_bins_spin.setValue(1234)
    batch_window._require_gravity_check.setChecked(True)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    assert calls[0]["grid_bins"] == 1234
    assert calls[0]["require_gravity_telemetry"] is True


def test_a_finished_batch_lands_on_what_it_produced(batch_window, tmp_path, monkeypatch, qapp):
    """Scenario: a batch is walked away from, so the state it is found in
    should be its results.

    Expected behaviour: Browse, grouped by session and opened on this one. The
    summary is still written to the Process page for when it is returned to.
    """
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction", lambda **k: None
    )
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    assert batch_window._app_mode == "RUNNING"
    assert batch_window._simple_stack.currentIndex() == SIMPLE_SECTIONS.index("process")
    await_batch(batch_window, qapp)
    assert batch_window._app_mode == "SETUP"
    assert batch_window._simple_stack.currentIndex() == SIMPLE_SECTIONS.index("browse")
    assert batch_window._data_facet == "sessions"
    assert batch_window._data_detail_stack.currentWidget() is batch_window._session_detail

    summary = batch_window._survey_summary_label
    assert not summary.isHidden()
    assert "1 of 1 pass succeeded" in summary.text()


def test_batch_stops_cleanly_when_the_disk_runs_out(batch_window, tmp_path, monkeypatch, qapp):
    """Scenario: the drive fills before the batch can run its first pass.

    Expected behaviour: nothing is processed, the run is left not-started so it
    can be retried once space is freed, and the outcome says why.
    """
    import shutil
    from types import SimpleNamespace

    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(shutil, "disk_usage", lambda _p: SimpleNamespace(free=0, total=0, used=0))
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    ran = []
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction", lambda **k: ran.append(k)
    )
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    assert ran == []
    runs = batch_window._survey_store().list_runs()
    assert [r.status for r in runs] == ["cancelled"]
    # Cancelled evades the failure count, so the row itself carries the reason.
    assert "disk space" in runs[0].error
    assert "disk space" in batch_window._status_label.text().lower()


def test_failed_run_keeps_pass_remaining(batch_window, tmp_path, monkeypatch, qapp):
    def broken_run(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", broken_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    runs = batch_window._survey_store().list_runs()
    assert [r.status for r in runs] == ["failed"]
    assert "boom" in runs[0].error
    assert batch_window._survey_start_btn.isEnabled()
    # The pass has been through a run, so pressing again continues the batch
    # rather than starting it.
    assert batch_window._survey_start_btn.text() == "Continue processing (1 pass)"


def test_a_failure_before_the_first_pass_still_ends_the_batch(
    batch_window, tmp_path, monkeypatch, qapp
):
    """Scenario: something outside the per-pass handler raises, before any pass
    starts.

    Expected behaviour: the batch ends, the page un-freezes, and the summary
    says why. Previously the thread died silently and the whole page stayed
    frozen until restart.
    """
    def broken_versions(_names):
        raise OSError("cache unreadable")

    monkeypatch.setattr(
        "deepreefmap_gui.models.cache.resolve_model_versions", broken_versions
    )
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    assert not batch_window._survey_worker_running
    assert batch_window._app_mode == "SETUP"
    assert "cache unreadable" in batch_window._survey_summary_label.text()


def test_a_retry_lands_in_its_own_directory(batch_window, tmp_path, out_root, monkeypatch, qapp):
    """Scenario: a pass fails, the diver starts processing again.

    Expected behaviour: the retry gets a directory of its own, derived from the
    first attempt's recorded name, and the failed attempt's directory survives
    with its log. Repeats are the reproducibility data.
    """
    def broken_run(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", broken_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    store = batch_window._survey_store()
    first = store.list_runs()[0]
    first_dir = out_root / first.run_dir_name

    def fake_run(**kwargs):
        (kwargs["output_dir"] / "run_manifest.json").write_text(json.dumps({"mode": "semantic"}))

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fake_run)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    runs = {r.run_dir_name: r.status for r in store.list_runs()}
    assert runs == {
        first.run_dir_name: "failed",
        f"{first.run_dir_name}__r02": "succeeded",
    }
    assert first_dir.exists()
    assert (out_root / f"{first.run_dir_name}__r02" / "run_manifest.json").exists()


def test_attempt_names_survive_a_transect_rename(batch_window):
    """The stem comes from the first run's recorded name, not the live transect."""
    from _factories import make_video

    from deepreefmap_gui.survey.models import RunRecord, TransectPass

    store = batch_window._survey_store()
    transect = store.list_transects()[0]
    video = store.upsert_video(make_video())
    pass_ = TransectPass(transect_id=transect.id, video_id=video.id, begin_s=0.0, end_s=60.0)
    store.add_pass(pass_)
    store.add_run(
        RunRecord(pass_id=pass_.id, run_dir_name="T1__p01__abcd1234", status="failed")
    )
    transect.name = "Renamed"
    store.update_transect(transect)

    assert batch_window._pass_dir_name(pass_, transect, store) == "T1__p01__abcd1234__r02"

    store.add_run(
        RunRecord(pass_id=pass_.id, run_dir_name="T1__p01__abcd1234__r02", status="failed")
    )
    assert batch_window._pass_dir_name(pass_, transect, store) == "T1__p01__abcd1234__r03"


def test_the_worker_skips_a_pass_held_after_checkout(
    batch_window, tmp_path, monkeypatch, qapp
):
    """Hold on a not-yet-started row is the one per-item control a running
    order keeps, and it only works because the worker re-reads the store."""
    add_videos(batch_window, tmp_path, monkeypatch, ["GX010001.MP4", "GX010002.MP4"])
    assign_transect(batch_window, 0)
    assign_transect(batch_window, 1)
    store = batch_window._survey_store()
    second_pass_id = batch_window._survey_rows[1].pass_id
    calls = []

    def fake_run(**kwargs):
        if not calls:
            held = store.get_pass(second_pass_id)
            held.held = True
            store.update_pass(held)
        calls.append(kwargs)
        (kwargs["output_dir"] / "run_manifest.json").write_text(json.dumps({"mode": "semantic"}))

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fake_run)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    assert len(calls) == 1
    statuses = {r.pass_id: r for r in store.list_runs()}
    skipped = statuses[second_pass_id]
    assert skipped.status == "cancelled"
    assert "Held or removed" in skipped.error


def test_failed_pass_keeps_its_cause_on_the_row(batch_window, tmp_path, monkeypatch, qapp):
    """Scenario: a pass fails mid-batch.

    Expected behaviour: the failure stays with the pass. The Status cell reads
    Failed with the cause on hover, the batch summary names it by transect and
    pass rather than dumping the run-dir slug, and the full error is copyable.
    """
    def broken_run(**kwargs):
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", broken_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    item = batch_window._survey_pass_table.item(batch_window._table_row_of(0), _COL_STATUS)
    assert item.text() == "Failed"
    # The tooltip translates the error into plain advice; the raw text stays
    # available for "Copy error details".
    assert "graphics memory" in item.toolTip()
    assert "CUDA out of memory" in batch_window._survey_pass_error(batch_window._survey_rows[0])

    summary = batch_window._survey_summary_label.text()
    assert "T1 pass 1" in summary
    assert "__p" not in summary  # the run-dir slug never reaches the summary

    assert "CUDA out of memory" in batch_window._survey_pass_error(batch_window._survey_rows[0])


def test_succeeded_pass_flags_a_quality_warning(batch_window, tmp_path, monkeypatch, qapp):
    """Scenario: a pass succeeds but the camera pointed off-reef.

    Expected behaviour: the Status cell still reads succeeded, marked with a
    warning sign, and the manifest's quality_warnings show on hover.
    """
    warning = "Background class dominates 9/10 frames. Camera may be pointing away from the reef."

    def fake_run(**kwargs):
        (kwargs["output_dir"] / "run_manifest.json").write_text(
            json.dumps({"mode": "semantic", "quality_warnings": [warning]})
        )

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fake_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    item = batch_window._survey_pass_table.item(batch_window._table_row_of(0), _COL_STATUS)
    assert item.text() == "Succeeded ⚠"
    assert warning in item.toolTip()


def test_succeeded_pass_without_warnings_has_a_clean_pill(batch_window, tmp_path, monkeypatch, qapp):
    def fake_run(**kwargs):
        (kwargs["output_dir"] / "run_manifest.json").write_text(json.dumps({"mode": "semantic"}))

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fake_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    item = batch_window._survey_pass_table.item(batch_window._table_row_of(0), _COL_STATUS)
    assert item.text() == "Succeeded"
    assert item.toolTip() == ""


def test_double_click_failed_pass_reveals_the_cause(batch_window, tmp_path, monkeypatch, qapp):
    def broken_run(**kwargs):
        raise RuntimeError("gravity telemetry missing")

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", broken_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    opened = []
    monkeypatch.setattr(batch_window, "_auto_load_run", opened.append)
    batch_window._on_survey_pass_activated(batch_window._table_row_of(0), 0)
    assert opened == []
    assert "gravity telemetry missing" in batch_window._status_label.text()


def test_survey_progress_line_shows_transect_not_slug(batch_window, tmp_path, monkeypatch, qapp):
    """The live progress line names the place, never the run-dir slug."""
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction", lambda **k: None
    )
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    seen = []
    # A second slot alongside the real one: the emitted name is what we assert.
    batch_window._sig_survey_progress.connect(lambda _i, _t, name: seen.append(name))
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)
    assert seen == ["T1"]


def test_remove_pass_with_runs_is_blocked(batch_window, tmp_path, monkeypatch, qapp):
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction", lambda **k: None
    )
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)
    batch_window._survey_pass_table.setCurrentCell(batch_window._table_row_of(0), 0)
    batch_window._on_survey_remove_pass()
    assert "cannot be removed" in batch_window._status_label.text()
    assert len(batch_window._survey_rows) == 1


def test_refresh_restores_batch_from_store(batch_window, tmp_path, monkeypatch):
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_name = batch_window._survey_batch_name.text()
    batch_window._survey_batch = None
    batch_window._survey_rows = []
    batch_window._survey_pass_table.setRowCount(0)
    batch_window._refresh_survey_batch_tab()
    assert len(batch_window._survey_rows) == 1
    assert batch_window._survey_batch_name.text() == batch_name
    assert batch_window._survey_rows[0].transect_id is not None


def test_the_worker_is_handed_the_windows_own_pause_and_cancel_events(
    batch_window, tmp_path, monkeypatch, qapp
):
    """The two objects the transport controls set have to be the two the pass is
    waiting on, or pausing and stopping reach nothing.

    That they release a paused worker rather than wedging it is exercised
    against a batch that is genuinely still running, in
    test_stopping_a_batch_ends_it_before_the_next_pass.
    """
    seen = {}
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction",
        lambda **kwargs: seen.update(kwargs),
    )
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    assert seen["pause_event"] is batch_window._pause_event
    assert seen["cancel_event"] is batch_window._survey_cancel_event


def test_pause_button_drives_the_survey_pause_event(batch_window, tmp_path, monkeypatch, qapp):
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction", lambda **kwargs: None
    )
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    batch_window._on_pause_toggled(True)
    assert not batch_window._pause_event.is_set()
    batch_window._on_pause_toggled(False)
    assert batch_window._pause_event.is_set()


# --- Transport controls over a batch that is still running ---


class _BlockingPass:
    """Stands in for the pipeline, holding each pass open until it is released.

    Records the kwargs it was called with so a test can count the passes that
    ran, and honours the cancel event the way the real pipeline does.
    """

    def __init__(self):
        self.calls: list[dict] = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.entered.set()
        self.release.wait(timeout=5)
        cancel = kwargs.get("cancel_event")
        if cancel is not None and cancel.is_set():
            raise ReconstructionCancelled


@pytest.fixture
def blocking_pass(monkeypatch):
    """A pipeline that stays inside a pass, so a live batch can be inspected."""
    stub = _BlockingPass()
    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", stub)
    yield stub
    # Released whatever the test did with it, so a failed assertion leaves no
    # worker wedged in wait().
    stub.release.set()


def test_a_running_batch_puts_start_out_of_reach(
    batch_window, tmp_path, monkeypatch, blocking_pass, qapp
):
    """A second launch would share the viewer and overwrite _pipeline_thread, so
    while a batch runs there is no way to start another, and the transport
    controls stand in its place until it ends."""
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    assert batch_window._survey_start_btn.isEnabled()

    batch_window._on_survey_start()
    assert blocking_pass.entered.wait(timeout=5)

    assert not batch_window._survey_start_btn.isEnabled()
    assert not batch_window._pause_btn.isHidden()

    blocking_pass.release.set()
    await_batch(batch_window, qapp)

    # The one pass queued has now run, so the transport goes and the start
    # button stays out of reach for want of anything to do rather than because
    # something is in flight.
    assert not batch_window._survey_start_btn.isEnabled()
    assert batch_window._pause_btn.isHidden()


def test_stopping_a_batch_ends_it_before_the_next_pass(
    batch_window, tmp_path, monkeypatch, blocking_pass, qapp
):
    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    assign_transect(batch_window, 0)
    assign_transect(batch_window, 1)
    batch_window._on_survey_start()
    assert blocking_pass.entered.wait(timeout=5)

    batch_window._on_stop_clicked()
    blocking_pass.release.set()
    await_batch(batch_window, qapp)

    assert len(blocking_pass.calls) == 1, "the remaining passes ran after the stop"


def test_a_paused_batch_does_not_start_the_next_pass(
    batch_window, tmp_path, monkeypatch, blocking_pass, qapp
):
    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    assign_transect(batch_window, 0)
    assign_transect(batch_window, 1)
    batch_window._on_survey_start()
    assert blocking_pass.entered.wait(timeout=5)

    batch_window._pause_btn.setChecked(True)
    blocking_pass.entered.clear()
    blocking_pass.release.set()
    assert not blocking_pass.entered.wait(timeout=0.5), "a pass started while paused"

    batch_window._pause_btn.setChecked(False)
    await_batch(batch_window, qapp)
    assert len(blocking_pass.calls) == 2


def test_a_stopped_batch_is_not_reported_as_failures(
    batch_window, tmp_path, monkeypatch, qapp
):
    """One of two finishing reads identically whether the diver stopped the batch
    or the other pass blew up, so the outcome has to tell them apart."""
    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    assign_transect(batch_window, 0)
    assign_transect(batch_window, 1)

    def cancel_immediately(**kwargs):
        batch_window._survey_cancel_event.set()
        raise ReconstructionCancelled

    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction", cancel_immediately
    )
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    summary = batch_window._survey_summary_label.text()
    assert "0 of 2 passes succeeded" in summary
    assert "Failed" not in summary
    assert "Failed" not in batch_window._status_label.text()


def test_a_finished_batch_lands_in_browse(batch_window, tmp_path, monkeypatch, qapp):
    """What a batch wrote is listed as finished without anyone asking for a
    rescan. The out-root watcher notices the folder on its own, but only as the
    half-built directory the pass started in, so the status is what pins this."""
    from deepreefmap_gui.runs import run_table

    def fake_run(**kwargs):
        (kwargs["output_dir"] / "run_manifest.json").write_text(json.dumps({"mode": "semantic"}))

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fake_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    table = batch_window._data_run_table
    assert table.rowCount() == 1
    assert table.item(0, run_table.COL_STATUS).text() == "Succeeded"


def test_double_click_opens_the_run_that_succeeded(batch_window, tmp_path, out_root, monkeypatch):
    """Scenario: a pass failed, was retried, and succeeded the second time.

    Expected behaviour: the row opens the successful run. Taking the most
    recent record instead would open a directory with no manifest whenever a
    later attempt failed.
    """
    from deepreefmap_gui.survey.models import RunRecord

    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    store = batch_window._survey_store()
    pass_id = batch_window._survey_rows[0].pass_id

    for name, status in (("run_bad", "failed"), ("run_good", "succeeded")):
        record = RunRecord(pass_id=pass_id, run_dir_name=name)
        store.add_run(record)
        store.set_run_status(record.id, status)
        (out_root / name).mkdir()

    opened = []
    monkeypatch.setattr(batch_window, "_auto_load_run", opened.append)
    batch_window._on_survey_pass_activated(batch_window._table_row_of(0), 0)
    assert opened == [out_root / "run_good"]


def test_double_click_is_refused_while_a_batch_runs(batch_window, tmp_path, monkeypatch):
    """The live run owns the viewer; an old one must not take it away."""
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    opened = []
    monkeypatch.setattr(batch_window, "_auto_load_run", opened.append)
    monkeypatch.setattr(batch_window, "_run_in_flight", lambda: True)
    batch_window._on_survey_pass_activated(batch_window._table_row_of(0), 0)
    assert opened == []
    assert "Unavailable while processing" in batch_window._status_label.text()


def test_unprocessed_row_says_so_rather_than_doing_nothing(batch_window, tmp_path, monkeypatch):
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    opened = []
    monkeypatch.setattr(batch_window, "_auto_load_run", opened.append)
    batch_window._on_survey_pass_activated(batch_window._table_row_of(0), 0)
    assert opened == []
    assert "no successful run" in batch_window._status_label.text()


def test_gate_summary_and_run_settings_agree(window, monkeypatch):
    """The gate, the summary label and the run all derive from the form.

    With one shared source they cannot disagree: the models the gate blocks on
    are exactly those _collect_run_settings() names, and the label describes the
    same run.
    """
    from deepreefmap_gui.models import cache

    monkeypatch.setattr(cache, "is_model_cached", lambda info: False)
    window._skip_seg_check.setChecked(False)
    window._seg_combo.setCurrentText("segformer-b2")
    window._map_combo.setCurrentText("scsfmlearner")
    window._recompute_survey_start()

    settings = window._collect_run_settings()
    required = window._required_model_names()
    # Nothing is cached, so every required model is missing and nothing else is.
    assert set(window._survey_missing_models()) == required
    assert settings["mapping_name"] in required
    assert settings["segmentation_name"] in required
    label = window._survey_preset_label.text()
    assert settings["segmentation_name"] in label
    assert settings["mapping_name"] in label


def test_corrupt_user_preset_falls_back_and_does_not_block(make_window, tmp_path, monkeypatch):
    """A corrupt user preset must not leave the survey with no preset.

    load_survey_preset quarantines the bad file and returns the bundled
    defaults, so _survey_preset is not None and an assigned pass can still run.
    """
    bad = tmp_path / "survey_preset.yaml"
    bad.write_text("schema_version: 999\nfps: 3\n")
    monkeypatch.setattr("deepreefmap_gui.survey.preset.survey_preset_path", lambda: bad)

    window = make_window()
    window._survey_store().add_transect(make_transect())
    window._refresh_survey_batch_tab()
    monkeypatch.setattr(window, "_survey_missing_models", list)

    assert window._survey_preset is not None
    assert not bad.exists()
    assert bad.with_name(bad.name + ".corrupt-1").exists()
    assert "could not be loaded" not in window._survey_preset_label.text()

    add_video(window, tmp_path, monkeypatch)
    # One transect, so the video preselects it, the pass persists, and the only
    # thing that could still block the button is a missing preset.
    assert window._survey_start_btn.isEnabled()


# --- Bulk editing ---


def test_bulk_assign_sets_every_selected_row_and_gates_once(
    batch_window, tmp_path, monkeypatch
):
    """Scenario: a morning of clips, all of one transect, assigned in one action.

    Expected behaviour: every selected row takes the transect and writes its
    pass, and the run gate is rebuilt once rather than once per row.
    """
    add_second_transect(batch_window)
    for name in ("GX010001.MP4", "GX010002.MP4", "GX010003.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    table = batch_window._survey_pass_table
    table.selectAll()
    assert batch_window._selected_survey_rows() == [0, 1, 2]

    gates = []
    inner = batch_window._recompute_survey_start

    def counted():
        gates.append(1)
        inner()

    monkeypatch.setattr(batch_window, "_recompute_survey_start", counted)
    target = batch_window._survey_transects[0]
    batch_window._assign_rows_to_transect([0, 1, 2], target.id)

    assert len(gates) == 1
    assert all(row.transect_id == target.id for row in batch_window._survey_rows)
    assert len(batch_window._survey_store().list_passes()) == 3
    for index in range(3):
        combo = table.cellWidget(batch_window._table_row_of(index), _COL_TRANSECT)
        assert combo.currentText() == target.name
        # The amber "not assigned" variant is a per-widget stylesheet.
        assert combo.styleSheet() == ""
    assert batch_window._survey_start_btn.text() == "Start processing (3 passes)"


def test_pass_table_allows_a_multi_row_selection(batch_window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QTableWidget

    add_second_transect(batch_window)
    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    table = batch_window._survey_pass_table
    assert table.selectionMode() == QTableWidget.SelectionMode.ExtendedSelection
    assert not batch_window._survey_assign_btn.isEnabled()
    table.selectRow(batch_window._table_row_of(1))
    assert batch_window._selected_survey_rows() == [1]
    assert batch_window._survey_assign_btn.isEnabled()


def test_alternate_direction_walks_down_the_selection(batch_window, tmp_path, monkeypatch):
    """Passes swum out and back alternate, so one action can set the whole run."""
    add_second_transect(batch_window)
    for name in ("GX010001.MP4", "GX010002.MP4", "GX010003.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    batch_window._survey_alternate_check.setChecked(True)
    target = batch_window._survey_transects[0]
    batch_window._assign_rows_to_transect([0, 1, 2], target.id)

    assert [row.direction for row in batch_window._survey_rows] == [
        "forward", "reverse", "forward"
    ]
    assert stored_directions(batch_window) == ["forward", "reverse", "forward"]
    table = batch_window._survey_pass_table
    assert table.cellWidget(batch_window._table_row_of(1), _COL_DIRECTION).currentText() == "reverse"


def direction_combo(window, row_index):
    return window._survey_pass_table.cellWidget(
        window._table_row_of(row_index), _COL_DIRECTION
    )


def test_one_way_transect_is_flagged(batch_window, tmp_path, monkeypatch):
    """Nothing downstream can tell a one-way survey from forgotten dropdowns."""
    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    flagged = direction_combo(batch_window, 0)
    assert flagged.styleSheet() != ""
    assert "swum out and back" in flagged.toolTip()

    direction_combo(batch_window, 1).setCurrentText("reverse")
    assert direction_combo(batch_window, 0).styleSheet() == ""


def test_one_pass_of_a_transect_is_not_flagged(batch_window, tmp_path, monkeypatch):
    add_video(batch_window, tmp_path, monkeypatch)
    assert direction_combo(batch_window, 0).styleSheet() == ""


# --- Clip identity ---


def test_video_cell_names_the_clip_by_time_and_length(batch_window, tmp_path, monkeypatch):
    add_video(batch_window, tmp_path, monkeypatch, name="GX010012.MP4", duration_s=401.0)
    text = batch_window._survey_pass_table.item(batch_window._table_row_of(0), _COL_VIDEO).text()
    assert text.startswith("GX010012.MP4 · ")
    assert text.endswith(" · 6m 41s")
    assert "time unknown" not in text


def test_unreadable_clip_metadata_says_so():
    from deepreefmap_gui.simple.batch import _video_cell_text
    from deepreefmap_gui.survey.models import VideoAsset

    asset = VideoAsset(file_name="clip.mp4", path="/data/clip.mp4")
    assert _video_cell_text([asset]) == "clip.mp4 · time unknown · length unknown"


def test_sort_by_time_orders_the_day_as_it_happened(batch_window, tmp_path, monkeypatch):
    add_second_transect(batch_window)
    for name in ("GX010002.MP4", "GX010001.MP4", "GX010003.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    rows = batch_window._survey_rows
    rows[0].video.mtime = "2026-07-01T09:30:00+00:00"
    rows[1].video.mtime = "2026-07-01T08:15:00+00:00"
    # A clip whose timestamp could not be read sorts last, not first.
    rows[2].video.mtime = None

    batch_window._on_survey_sort_by_time()
    assert [row.video.file_name for row in batch_window._survey_rows] == [
        "GX010001.MP4", "GX010002.MP4", "GX010003.MP4"
    ]
    assert "time unknown" in batch_window._survey_pass_table.item(batch_window._table_row_of(2), _COL_VIDEO).text()


def test_probing_a_clip_stays_off_the_gui_thread(batch_window, tmp_path, monkeypatch):
    """A card of 4 GB clips must not freeze the window while cv2 reads them."""
    threads = []
    path = tmp_path / "GX010009.MP4"
    path.write_bytes(b"x" * 4096)

    def record(_path):
        threads.append(threading.current_thread())
        return (60.0, 30.0)

    monkeypatch.setattr("deepreefmap_gui.simple.batch._probe_video", record)
    batch_window._add_video_paths([str(path)])
    assert wait_until(lambda: len(batch_window._survey_rows) == 1)
    assert threads and threads[0] is not threading.main_thread()
    assert batch_window._survey_rows[0].end_s == 60.0


def test_unreadable_clips_are_counted_not_queued(batch_window, tmp_path, monkeypatch):
    good = tmp_path / "good.MP4"
    bad = tmp_path / "bad.MP4"
    for path in (good, bad):
        path.write_bytes(b"x" * 4096)
    monkeypatch.setattr(
        "deepreefmap_gui.simple.batch._probe_video",
        lambda path: (60.0, 30.0) if path.endswith("good.MP4") else None,
    )
    batch_window._add_video_paths([str(good), str(bad)])
    assert wait_until(lambda: len(batch_window._survey_rows) == 1)
    assert "Skipped 1 unreadable" in batch_window._status_label.text()


# --- Bulk trim ---


def test_trim_can_apply_to_every_pass_of_a_transect(batch_window, tmp_path, monkeypatch):
    """One transect filmed several times takes the same tape-in and tape-out cuts."""
    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    stub_trim_dialog(monkeypatch, 5.0, 40.0)
    answer_question(monkeypatch, QMessageBox.StandardButton.Yes)
    table = batch_window._survey_pass_table
    batch_window._on_survey_row_trim(
        batch_window._survey_rows[0], table.cellWidget(batch_window._table_row_of(0), _COL_TRIM)
    )

    assert [(row.begin_s, row.end_s) for row in batch_window._survey_rows] == [
        (5.0, 40.0), (5.0, 40.0)
    ]
    store = batch_window._survey_store()
    assert {
        (store.get_pass(row.pass_id).begin_s, store.get_pass(row.pass_id).end_s)
        for row in batch_window._survey_rows
    } == {(5.0, 40.0)}
    assert table.cellWidget(batch_window._table_row_of(1), _COL_TRIM).text() == "0:05-0:40"


def test_declining_the_bulk_trim_leaves_the_other_rows_alone(
    batch_window, tmp_path, monkeypatch
):
    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    stub_trim_dialog(monkeypatch, 5.0, 40.0)
    answer_question(monkeypatch, QMessageBox.StandardButton.No)
    batch_window._on_survey_row_trim(
        batch_window._survey_rows[0],
        batch_window._survey_pass_table.cellWidget(batch_window._table_row_of(0), _COL_TRIM),
    )
    assert [(row.begin_s, row.end_s) for row in batch_window._survey_rows] == [
        (5.0, 40.0), (0.0, 60.0)
    ]


def test_bulk_trim_keeps_a_short_clip_inside_its_own_length(
    batch_window, tmp_path, monkeypatch
):
    """A shorter clip of the same transect runs to its own end rather than past it."""
    add_video(batch_window, tmp_path, monkeypatch, name="GX010001.MP4")
    add_video(batch_window, tmp_path, monkeypatch, name="GX010002.MP4", duration_s=20.0)
    stub_trim_dialog(monkeypatch, 5.0, 40.0)
    answer_question(monkeypatch, QMessageBox.StandardButton.Yes)
    batch_window._on_survey_row_trim(
        batch_window._survey_rows[0],
        batch_window._survey_pass_table.cellWidget(batch_window._table_row_of(0), _COL_TRIM),
    )
    assert [(row.begin_s, row.end_s) for row in batch_window._survey_rows] == [
        (5.0, 40.0), (5.0, 20.0)
    ]


def test_a_lone_pass_is_never_asked_about_bulk_trim(batch_window, tmp_path, monkeypatch):
    add_video(batch_window, tmp_path, monkeypatch)
    stub_trim_dialog(monkeypatch, 5.0, 40.0)
    asked = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: asked.append(1) or QMessageBox.StandardButton.No),
    )
    batch_window._on_survey_row_trim(
        batch_window._survey_rows[0],
        batch_window._survey_pass_table.cellWidget(batch_window._table_row_of(0), _COL_TRIM),
    )
    assert asked == []


# --- Retrying a pass ---


def test_a_retry_seeds_from_the_failed_attempts_frames(
    batch_window, tmp_path, out_root, monkeypatch, qapp
):
    """Scenario: a pass fails after preprocessing, and the batch is run again.

    Expected behaviour: the retry gets its own directory but hard-links the
    frames the failed attempt already decoded, so the afternoon is not spent
    twice. Seeding scans every sibling, earlier attempts included.
    """
    from deepreefmap.pipeline import resume as resume_mod

    from deepreefmap_gui.runs.seeding import preprocess_key_for_settings

    dirs = []

    def failing(**kwargs):
        out_dir = kwargs["output_dir"]
        dirs.append(out_dir)
        # Preprocessing completed before the failure: frames plus the sidecar
        # that marks the stage done, which is what seeding matches on.
        clip = batch_window._survey_rows[0].videos[0].path
        for dirname in ("frames", "labels", "masks"):
            (out_dir / dirname).mkdir(parents=True, exist_ok=True)
            (out_dir / dirname / "000000.png").write_bytes(b"data")
        key = preprocess_key_for_settings(
            batch_window._collect_run_settings(), [Path(clip)], 0.0, 60.0
        )
        resume_mod.write_sidecar(out_dir, resume_mod.STAGE_PREPROCESS, key)
        raise RuntimeError("boom")

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", failing)
    add_video(batch_window, tmp_path, monkeypatch)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    assert len(dirs) == 2
    assert dirs[1] == Path(f"{dirs[0]}__r02")
    assert "__p01__" in dirs[0].name
    # The retry's frames arrived by seeding before the pipeline was called.
    assert (dirs[1] / "frames" / "000000.png").read_bytes() == b"data"


def test_survey_worker_seeds_from_a_matching_run(
    batch_window, tmp_path, out_root, monkeypatch, qapp
):
    """A pass of a clip another run already preprocessed skips preprocessing."""
    from deepreefmap.pipeline import resume as resume_mod

    from deepreefmap_gui.runs.seeding import preprocess_key_for_settings

    clip = add_video(batch_window, tmp_path, monkeypatch)
    # A sibling of the run the pass is about to create: seeding only searches
    # the output root.
    prior = out_root / "earlier-run"
    for dirname in ("frames", "labels", "masks"):
        (prior / dirname).mkdir(parents=True)
        (prior / dirname / "000000.png").write_bytes(b"data")
    key = preprocess_key_for_settings(batch_window._collect_run_settings(), [clip], 0.0, 60.0)
    resume_mod.write_sidecar(prior, resume_mod.STAGE_PREPROCESS, key)

    seen = []
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction",
        lambda **kwargs: seen.append(kwargs["output_dir"]),
    )
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    assert (seen[0] / "frames" / "000000.png").exists()
    sidecar = resume_mod.read_sidecar(seen[0], resume_mod.STAGE_PREPROCESS)
    assert sidecar is not None and sidecar["key"] == key


# --- Chaptered recordings ---


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("GX010012.MP4", ("0012", 1)),
        ("GX020012.MP4", ("0012", 2)),
        ("GP030012.MP4", ("0012", 3)),
        ("GOPR0012.MP4", ("0012", 0)),
        ("reef.mp4", None),
        ("GX01001.MP4", None),
    ],
)
def test_chapter_key_reads_gopro_file_names(name, expected):
    from deepreefmap_gui.simple.batch import _chapter_key

    assert _chapter_key(name) == expected


def test_chapters_of_one_swim_become_one_pass(batch_window, tmp_path, monkeypatch):
    """Scenario: a swim long enough that the camera split it at 4 GB.

    Expected behaviour: one pass covering both chapters played back to back,
    not two passes of half a transect each.
    """
    add_videos(
        batch_window, tmp_path, monkeypatch, ["GX020012.MP4", "GX010012.MP4"], duration_s=300.0
    )
    assert len(batch_window._survey_rows) == 1
    row = batch_window._survey_rows[0]
    assert [video.file_name for video in row.videos] == ["GX010012.MP4", "GX020012.MP4"]
    assert (row.begin_s, row.end_s) == (0.0, 600.0)

    text = batch_window._survey_pass_table.item(batch_window._table_row_of(0), _COL_VIDEO).text()
    assert text.startswith("GX010012.MP4 +1 chapter · ")
    assert text.endswith(" · 10m 00s")

    pass_ = batch_window._survey_store().get_pass(row.pass_id)
    assert pass_.video_ids() == [video.id for video in row.videos]


def test_separate_recordings_stay_separate_passes(batch_window, tmp_path, monkeypatch):
    add_videos(
        batch_window, tmp_path, monkeypatch, ["GX010012.MP4", "GX010013.MP4", "reef.mp4"]
    )
    assert len(batch_window._survey_rows) == 3


def test_a_chaptered_pass_runs_every_chapter(batch_window, tmp_path, monkeypatch, qapp):
    seen = {}
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction", lambda **kwargs: seen.update(kwargs)
    )
    paths = add_videos(batch_window, tmp_path, monkeypatch, ["GX010012.MP4", "GX020012.MP4"])
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    assert seen["video_paths"] == paths
    # begin_s/end_s are offsets into the chapters played back to back.
    assert (seen["begin_s"], seen["end_s"]) == (0.0, 120.0)


def test_reopening_a_batch_restores_the_chapters(batch_window, tmp_path, monkeypatch):
    add_videos(batch_window, tmp_path, monkeypatch, ["GX010012.MP4", "GX020012.MP4"])
    batch_window._survey_batch = None
    batch_window._survey_rows = []
    batch_window._survey_pass_table.setRowCount(0)
    batch_window._refresh_survey_batch_tab()

    assert len(batch_window._survey_rows) == 1
    assert [video.file_name for video in batch_window._survey_rows[0].videos] == [
        "GX010012.MP4", "GX020012.MP4"
    ]


def test_splitting_a_chaptered_pass_keeps_every_chapter(batch_window, tmp_path, monkeypatch):
    add_videos(batch_window, tmp_path, monkeypatch, ["GX010012.MP4", "GX020012.MP4"])
    batch_window._survey_pass_table.setCurrentCell(batch_window._table_row_of(0), 0)
    batch_window._on_survey_split_pass()

    store = batch_window._survey_store()
    assert len(batch_window._survey_rows) == 2
    assert [
        len(store.get_pass(row.pass_id).video_ids()) for row in batch_window._survey_rows
    ] == [2, 2]


def test_queue_report_counts_passes_and_videos_apart(batch_window, tmp_path, monkeypatch):
    """A chaptered recording is several files and one pass, so say both."""
    add_videos(batch_window, tmp_path, monkeypatch, ["GX010012.MP4", "GX020012.MP4"])
    assert "Queued 1 pass from 2 videos." in batch_window._status_label.text()


def test_the_same_recording_picked_twice_is_one_chapter(batch_window, tmp_path, monkeypatch):
    """Two folders holding one clip is still one file, so the pass names it once."""
    copy_dir = tmp_path / "backup"
    copy_dir.mkdir()
    original = tmp_path / "GX010012.MP4"
    original.write_bytes(b"same bytes" * 4096)
    duplicate = copy_dir / "GX010012.MP4"
    duplicate.write_bytes(original.read_bytes())
    monkeypatch.setattr(
        "deepreefmap_gui.simple.batch._probe_video", lambda _path: (60.0, 30.0)
    )
    batch_window._add_video_paths([str(original), str(duplicate)])
    assert wait_until(lambda: len(batch_window._survey_rows) == 1)

    row = batch_window._survey_rows[0]
    assert len(row.videos) == 1
    assert row.end_s == 60.0


def group_headings(window):
    """The section titles the pass table currently shows, top to bottom."""
    table = window._survey_pass_table
    return [
        table.item(row, 0).text()
        for row in range(table.rowCount())
        if window._model_index(row) is None
    ]


def test_held_pass_is_skipped_by_the_next_batch(batch_window, tmp_path, monkeypatch):
    """Scenario: one clip of the day is not ready to process yet.

    Expected behaviour: it stays in the batch, under its own heading, and
    processing counts only the rest.
    """
    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    assign_transect(batch_window, 0)
    assign_transect(batch_window, 1)
    assert batch_window._survey_start_btn.text() == "Start processing (2 passes)"

    batch_window._move_rows([1], hold=True)
    assert [row.held for row in batch_window._survey_rows] == [False, True]
    assert len(batch_window._survey_remaining_rows()) == 1
    assert batch_window._survey_start_btn.text() == "Start processing (1 pass)"
    assert group_headings(batch_window) == ["To process  (1)", "Held back  (1)"]
    status = batch_window._survey_pass_table.item(
        batch_window._table_row_of(1), _COL_STATUS
    )
    assert status.text() == "Held"


def test_holding_survives_reopening_the_batch(batch_window, tmp_path, monkeypatch):
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._move_rows([0], hold=True)
    batch_window._survey_batch = None
    batch_window._survey_rows = []
    batch_window._refresh_survey_batch_tab()
    assert [row.held for row in batch_window._survey_rows] == [True]
    assert batch_window._survey_remaining_rows() == []


def test_returning_a_held_pass_puts_it_back_in_the_batch(batch_window, tmp_path, monkeypatch):
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._move_rows([0], hold=True)
    batch_window._move_rows([0], hold=False)
    assert not batch_window._survey_rows[0].held
    assert len(batch_window._survey_remaining_rows()) == 1
    assert group_headings(batch_window) == ["To process  (1)"]


def test_a_processed_pass_leaves_the_batch_until_asked_for_again(
    batch_window, tmp_path, monkeypatch
):
    from deepreefmap_gui.survey.models import RunRecord

    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    row = batch_window._survey_rows[0]
    batch_window._survey_store().add_run(
        RunRecord(
            pass_id=row.pass_id,
            run_dir_name="run_001",
            status="succeeded",
            batch_id=batch_window._survey_batch.id,
        )
    )
    batch_window._rebuild_survey_table()
    batch_window._recompute_survey_start()
    assert group_headings(batch_window) == ["Already processed  (1)"]
    assert batch_window._survey_remaining_rows() == []

    # Process again: the same pass, ordered into a fresh cart.
    first_session = batch_window._survey_batch
    batch_window._move_rows([0], hold=False)
    assert batch_window._survey_batch.id != first_session.id
    assert group_headings(batch_window) == ["To process  (1)"]
    assert len(batch_window._survey_remaining_rows()) == 1
    assert "cart" in batch_window._status_label.text().lower()


def test_each_row_carries_the_one_move_it_can_make(batch_window, tmp_path, monkeypatch):
    """Scenario: a batch holding a queued, a held and a processed pass.

    Expected behaviour: each row offers its own move, with no selection first.
    """
    from deepreefmap_gui.survey.models import RunRecord

    for name in ("GX010001.MP4", "GX010002.MP4", "GX010003.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    for index in range(3):
        assign_transect(batch_window, index)
    batch_window._survey_store().add_run(
        RunRecord(
            pass_id=batch_window._survey_rows[2].pass_id,
            run_dir_name="run_001",
            status="succeeded",
            batch_id=batch_window._survey_batch.id,
        )
    )
    batch_window._move_rows([1], hold=True)

    labels = [
        batch_window._survey_pass_table.cellWidget(
            batch_window._table_row_of(index), _COL_ACTION
        ).text()
        for index in range(3)
    ]
    assert labels == ["Hold", "Return", "Process again"]

    # Clicking a row's own button moves that row, without selecting it first.
    batch_window._survey_pass_table.cellWidget(
        batch_window._table_row_of(0), _COL_ACTION
    ).click()
    assert batch_window._survey_rows[0].held


def test_holding_a_not_yet_started_row_mid_run_reaches_the_store(
    batch_window, tmp_path, monkeypatch
):
    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    assign_transect(batch_window, 0)
    batch_window._survey_worker_running = True
    batch_window._survey_running_batch = batch_window._survey_batch
    batch_window._survey_job_pass_ids = [r.pass_id for r in batch_window._survey_rows]
    batch_window._survey_running_index = 0

    batch_window._move_rows([1], hold=True)

    held = batch_window._survey_store().get_pass(batch_window._survey_rows[1].pass_id)
    assert held.held is True
    # The pass in flight cannot be held any more; the click does nothing.
    batch_window._move_rows([0], hold=True)
    in_flight = batch_window._survey_store().get_pass(batch_window._survey_rows[0].pass_id)
    assert in_flight.held is False
    batch_window._survey_worker_running = False
    batch_window._survey_running_batch = None


def test_a_finished_order_hands_the_page_to_the_cart(
    batch_window, tmp_path, monkeypatch, qapp
):
    """Scenario: passes were added to the cart while the order ran.

    Expected behaviour: once the order finishes, the page shows the cart, ready
    to start, and the order's rows are read in Browse instead.
    """
    def fake_run(**kwargs):
        (kwargs["output_dir"] / "run_manifest.json").write_text(json.dumps({"mode": "semantic"}))

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fake_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    order = batch_window._survey_batch
    batch_window._on_survey_start()
    # Queue a rerun of the same pass mid-run; it lands in the next cart.
    batch_window._add_pass_to_cart(batch_window._survey_rows[0].pass_id)
    cart = batch_window._survey_batch
    assert cart.id != order.id
    await_batch(batch_window, qapp)

    assert batch_window._survey_batch.id == cart.id
    assert batch_window._survey_running_batch is None
    assert [row.in_cart for row in batch_window._survey_rows] == [False]
    assert len(batch_window._survey_remaining_rows()) == 1
    assert batch_window._survey_start_btn.isEnabled()


def test_the_cart_badge_follows_the_queue(batch_window, tmp_path, monkeypatch):
    """Adds, holds and checkouts all funnel through _recompute_survey_start,
    which is what keeps the badge honest."""
    assert batch_window._cart_button._count == 0
    add_video(batch_window, tmp_path, monkeypatch)
    assert batch_window._cart_button._count == 1
    batch_window._move_rows([0], hold=True)
    assert batch_window._cart_button._count == 0
    batch_window._move_rows([0], hold=False)
    assert batch_window._cart_button._count == 1


def test_the_cart_badge_counts_next_session_rows_mid_run(
    batch_window, tmp_path, monkeypatch
):
    add_video(batch_window, tmp_path, monkeypatch)
    order = batch_window._survey_batch
    batch_window._survey_worker_running = True
    batch_window._survey_running_batch = order
    batch_window._survey_job_pass_ids = [batch_window._survey_rows[0].pass_id]
    batch_window._recompute_survey_start()
    # The running order's own queue is not the cart.
    assert batch_window._cart_button._count == 0

    add_video(batch_window, tmp_path, monkeypatch, name="GX010099.MP4")
    assert batch_window._cart_button._count == 1

    batch_window._survey_worker_running = False
    batch_window._survey_running_batch = None


def test_median_pass_seconds_is_silent_without_history(monkeypatch):
    monkeypatch.setattr(
        "deepreefmap_gui.profiling.run_history.summarise_recorded_runs", list
    )
    assert _median_pass_seconds() is None


def test_batch_card_spans_the_passes_still_queued():
    """Scenario: pass 2 of 10 is half done, and a pass has historically cost 20 min.

    Expected behaviour: the estimate covers the eight passes after this one, not
    just the remainder of the one in flight.
    """
    card = BatchProgressCard()
    card.set_batch_plan(10, 1200.0)
    card.set_batch_context(2, 10, "North_reef")
    card.set_percent(50)
    card.set_eta_seconds(600.0)
    assert card.batch_remaining_s() == 600.0 + 8 * 1200.0


def test_batch_card_infers_a_pass_cost_without_history():
    """A first-ever batch has no median, so the pass in flight supplies the scale."""
    card = BatchProgressCard()
    card.set_batch_plan(4, None)
    card.set_batch_context(1, 4, "North_reef")
    card.set_percent(50)
    card.set_eta_seconds(300.0)
    # Half a pass has 300s left, so a whole one is 600s, and three follow it.
    assert card.batch_remaining_s() == 300.0 + 3 * 600.0


def test_batch_card_says_nothing_too_early():
    card = BatchProgressCard()
    card.set_batch_plan(4, None)
    card.set_batch_context(1, 4, "North_reef")
    card.set_percent(1)
    card.set_eta_seconds(300.0)
    assert card.batch_remaining_s() is None


def test_batch_card_counts_the_last_pass_alone():
    card = BatchProgressCard()
    card.set_batch_plan(3, 1200.0)
    card.set_batch_context(3, 3, "North_reef")
    card.set_eta_seconds(90.0)
    assert card.batch_remaining_s() == 90.0


def test_running_row_carries_its_own_progress(batch_window, tmp_path, monkeypatch):
    """The queue is where a pass reports itself now that the Run step has no viewer."""
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    pass_id = batch_window._survey_rows[0].pass_id
    batch_window._survey_job_pass_ids = [pass_id]
    batch_window._survey_running_index = 0

    batch_window._on_pass_percent(42)
    item = batch_window._survey_pass_table.item(batch_window._table_row_of(0), _COL_STATUS)
    assert item.text() == "Running 42%"
    assert item.data(PASS_PERCENT_ROLE) == 42

    # A pass that has stopped running loses the fill rather than freezing at it.
    batch_window._survey_running_index = None
    batch_window._refresh_survey_pass_statuses()
    assert item.data(PASS_PERCENT_ROLE) is None


def test_stopping_a_batch_leaves_it_continuable(batch_window, tmp_path, monkeypatch, qapp):
    """Scenario: two passes queued, the batch is cancelled before either runs.

    Expected behaviour: nothing is lost. Both stay queued and the button offers to
    continue rather than pretending this is a fresh batch.
    """
    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    assign_transect(batch_window, 0)
    assign_transect(batch_window, 1)

    def cancel_immediately(**kwargs):
        batch_window._survey_cancel_event.set()
        raise ReconstructionCancelled

    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction", cancel_immediately
    )
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    assert [r.status for r in batch_window._survey_store().list_runs()] == [
        "cancelled",
        "cancelled",
    ]
    assert len(batch_window._survey_remaining_rows()) == 2
    assert batch_window._survey_start_btn.text() == "Continue processing (2 passes)"


def test_batch_standing_reports_what_is_behind_you(batch_window, tmp_path, monkeypatch, qapp):
    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    assign_transect(batch_window, 0)
    assign_transect(batch_window, 1)
    # A fresh batch has nothing behind it, so the line stays out of the way.
    assert not batch_window._survey_standing_label.isVisibleTo(batch_window)

    batch_window._move_rows([1], hold=True)
    batch_window._recompute_survey_start()
    text = batch_window._survey_standing_label.text()
    assert "1 remaining" in text
    assert "1 held back" in text


def test_a_running_order_freezes_but_keeps_hold_and_adding_open(
    batch_window, tmp_path, monkeypatch
):
    """Scenario: a batch is running and the diver reaches for the table.

    Expected behaviour: what the order *is* can no longer change -- its
    transect, direction and trim are frozen and the header acts on nothing.
    What stays open: Hold on a pass the worker has not reached, and adding
    videos, which go to the next session's cart.
    """
    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    assign_transect(batch_window, 0)
    table_row = batch_window._table_row_of(0)
    editors = [
        batch_window._survey_pass_table.cellWidget(table_row, column)
        for column in (_COL_TRANSECT, _COL_DIRECTION, _COL_TRIM)
    ]
    frozen_controls = [
        batch_window._survey_batch_name,
        batch_window._survey_new_batch_btn,
        batch_window._survey_settings_btn,
        batch_window._survey_audit_btn,
        batch_window._survey_sort_btn,
    ]
    open_controls = [batch_window._survey_add_btn, batch_window._survey_import_btn]
    move = batch_window._survey_pass_table.cellWidget(table_row, _COL_ACTION)
    assert all(w.isEnabled() for w in frozen_controls + open_controls + editors + [move])

    batch_window._survey_worker_running = True
    batch_window._survey_running_batch = batch_window._survey_batch
    batch_window._survey_job_pass_ids = [r.pass_id for r in batch_window._survey_rows]
    batch_window._survey_running_index = 0
    batch_window._recompute_row_actions()
    assert not any(w.isEnabled() for w in frozen_controls + editors)
    assert all(w.isEnabled() for w in open_controls)
    # Row 0 is the pass in flight, so its Hold can no longer take effect; the
    # pass behind it can still be held.
    assert not batch_window._survey_pass_table.cellWidget(
        batch_window._table_row_of(0), _COL_ACTION
    ).isEnabled()
    assert batch_window._survey_pass_table.cellWidget(
        batch_window._table_row_of(1), _COL_ACTION
    ).isEnabled()

    batch_window._survey_worker_running = False
    batch_window._survey_running_batch = None
    batch_window._recompute_row_actions()
    assert all(w.isEnabled() for w in frozen_controls + open_controls + editors)


def test_videos_added_mid_run_land_in_the_next_session(
    batch_window, tmp_path, monkeypatch
):
    """Scenario: a batch is running and more footage comes off the card.

    Expected behaviour: the clips queue into a fresh session, shown under the
    Next session divider, and the running order is untouched.
    """
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    order = batch_window._survey_batch
    batch_window._survey_worker_running = True
    batch_window._survey_running_batch = order
    batch_window._survey_job_pass_ids = [batch_window._survey_rows[0].pass_id]

    add_video(batch_window, tmp_path, monkeypatch, name="GX010099.MP4")

    assert batch_window._survey_batch.id != order.id
    states = batch_window._survey_row_states()
    assert states.count("next") == 1
    headings = group_headings(batch_window)
    assert any(h.startswith("Next session") for h in headings)
    assert not batch_window._survey_next_cart_label.isHidden()
    assert "Next session" in batch_window._survey_next_cart_label.text()
    # The pending cart never leaks into what the running order will process.
    assert len(batch_window._survey_remaining_rows()) == 1

    batch_window._survey_worker_running = False
    batch_window._survey_running_batch = None


def test_no_row_action_label_is_clipped(batch_window):
    """The action column holds widgets, so its width has to fit the longest of
    them. "Process again" is longer than the "Run again" it replaced and was
    rendering as "rocess agai"."""
    from PySide6.QtGui import QFontMetrics

    from deepreefmap_gui.simple.batch import _COL_ACTION, _MOVE_LABELS

    table = batch_window._survey_pass_table
    metrics = QFontMetrics(table.font())
    widest = max(metrics.horizontalAdvance(label) for label in _MOVE_LABELS.values())
    # Padding either side of a push button's text; generous rather than exact,
    # because the point is that the column is not sized to the text alone.
    assert table.columnWidth(_COL_ACTION) >= widest + 24
