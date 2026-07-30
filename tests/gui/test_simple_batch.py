import json
import threading
import time

import pytest
from PySide6.QtWidgets import QDialog, QMessageBox

from deepreefmap_gui.simple.batch import (
    _COL_DIRECTION,
    _COL_STATUS,
    _COL_TRANSECT,
    _COL_TRIM,
    _COL_VIDEO,
    _diagnose_failure,
    _rough_batch_time,
)

from _factories import make_transect
from _qt_wait import wait_until


def test_diagnose_failure_speaks_plainly_and_advises():
    assert "graphics memory" in _diagnose_failure("RuntimeError: CUDA out of memory")
    assert "model is missing" in _diagnose_failure("FileNotFoundError: checkpoint not found")
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
def batch_window(simple_window, tmp_path, monkeypatch):
    window = simple_window
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
    rows = window._survey_pass_table.rowCount()
    window._on_survey_add_videos()
    assert wait_until(lambda: window._survey_pass_table.rowCount() > rows), "probe never landed"
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
    rows = window._survey_pass_table.rowCount()
    window._add_video_paths(paths)
    assert wait_until(lambda: window._survey_pass_table.rowCount() > rows), "probe never landed"
    return paths


def assign_transect(window, row_index):
    combo = window._survey_pass_table.cellWidget(row_index, _COL_TRANSECT)
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
    assert batch_window._survey_pass_table.rowCount() == 1
    assert batch_window._survey_rows[0].transect_id is None
    assert batch_window._survey_rows[0].end_s == 60.0
    assert not batch_window._survey_start_btn.isEnabled()
    assert batch_window._survey_start_btn.text() == "Assign transects first (1 to do)"
    combo = batch_window._survey_pass_table.cellWidget(0, _COL_TRANSECT)
    assert combo.currentText() == "Not assigned yet"
    assert combo.styleSheet() != ""
    assert batch_window._survey_store().list_passes() == []


def test_add_video_preselects_the_only_transect(batch_window, tmp_path, monkeypatch):
    add_video(batch_window, tmp_path, monkeypatch)
    assert batch_window._survey_rows[0].transect_id is not None
    assert len(batch_window._survey_store().list_passes()) == 1
    combo = batch_window._survey_pass_table.cellWidget(0, _COL_TRANSECT)
    assert combo.currentText() == "T1"
    assert combo.styleSheet() == ""
    assert batch_window._survey_start_btn.isEnabled()


def test_assigning_transect_persists_pass(batch_window, tmp_path, monkeypatch):
    add_second_transect(batch_window)
    add_video(batch_window, tmp_path, monkeypatch)
    assert batch_window._survey_store().list_passes() == []
    assign_transect(batch_window, 0)
    passes = batch_window._survey_store().list_passes()
    assert len(passes) == 1
    assert passes[0].begin_s == 0.0
    assert passes[0].end_s == 60.0
    assert passes[0].direction == "forward"
    assert passes[0].batch_id is not None
    assert batch_window._survey_start_btn.isEnabled()
    assert batch_window._survey_start_btn.text() == "Next: Process (1) →"


def test_split_pass_duplicates_row(batch_window, tmp_path, monkeypatch):
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._survey_pass_table.setCurrentCell(0, 0)
    batch_window._on_survey_split_pass()
    assert batch_window._survey_pass_table.rowCount() == 2
    assert len(batch_window._survey_store().list_passes()) == 2


def test_run_batch_records_success_and_links_manifest(
    batch_window, tmp_path, monkeypatch, qapp
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
    assert kwargs["output_dir"].parent == tmp_path
    # run_name and the survey block are no longer passed to run_reconstruction;
    # they land in the manifest instrumented_reconstruction writes afterward.
    manifest = json.loads((kwargs["output_dir"] / "run_manifest.json").read_text())
    assert manifest["name"] == kwargs["output_dir"].name
    survey = manifest["survey"]
    assert survey["transect"]["name"] == "T1"
    assert survey["pass"]["direction"] == "forward"
    runs = batch_window._survey_store().list_runs()
    assert [r.status for r in runs] == ["succeeded"]
    # Nothing left to process, so the step points at the archive instead.
    assert batch_window._survey_start_btn.text() == "Open Browse →"
    assert batch_window._survey_pass_table.item(0, _COL_STATUS).text() == "Succeeded"


def test_manifest_records_the_configuration_and_any_deviation(
    batch_window, tmp_path, monkeypatch, qapp
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

    run_dir = next(p for p in tmp_path.iterdir() if p.is_dir() and p.name.startswith("T1__"))
    survey = json.loads((run_dir / "run_manifest.json").read_text())["survey"]
    config = survey["provenance"]["config"]
    assert config["preset_name"] == org.name
    assert config["preset_version"] == org.version
    assert config["preset_hash"] == org.content_hash
    assert config["deviated"] is True
    assert config["deviations"] == {"preprocess_batch_size": 1}
    assert survey["preset_name"] == org.name


def test_manifest_of_a_standard_run_records_no_deviation(
    batch_window, tmp_path, monkeypatch, qapp
):
    def fake_run(**kwargs):
        (kwargs["output_dir"] / "run_manifest.json").write_text(json.dumps({"mode": "semantic"}))

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fake_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    run_dir = next(p for p in tmp_path.iterdir() if p.is_dir() and p.name.startswith("T1__"))
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


def test_batch_stays_on_run_when_done(batch_window, tmp_path, monkeypatch, qapp):
    """Expected behaviour: a finished batch reports where the work happened
    rather than relocating the user to another section."""
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction", lambda **k: None
    )
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    assert batch_window._app_mode == "RUNNING"
    assert batch_window._simple_stack.currentIndex() == 1
    await_batch(batch_window, qapp)
    assert batch_window._app_mode == "SETUP"
    assert batch_window._simple_stack.currentIndex() == 1
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
    assert batch_window._survey_start_btn.text() == "Next: Process (1) →"


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

    item = batch_window._survey_pass_table.item(0, _COL_STATUS)
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

    item = batch_window._survey_pass_table.item(0, _COL_STATUS)
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

    item = batch_window._survey_pass_table.item(0, _COL_STATUS)
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
    batch_window._on_survey_pass_activated(0, 0)
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
    batch_window._survey_pass_table.setCurrentCell(0, 0)
    batch_window._on_survey_remove_pass()
    assert "cannot be removed" in batch_window._status_label.text()
    assert batch_window._survey_pass_table.rowCount() == 1


def test_refresh_restores_batch_from_store(batch_window, tmp_path, monkeypatch):
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_name = batch_window._survey_batch_name.text()
    batch_window._survey_batch = None
    batch_window._survey_rows = []
    batch_window._survey_pass_table.setRowCount(0)
    batch_window._refresh_survey_batch_tab()
    assert batch_window._survey_pass_table.rowCount() == 1
    assert batch_window._survey_batch_name.text() == batch_name
    assert batch_window._survey_rows[0].transect_id is not None


def test_survey_run_can_be_paused_and_stopped(batch_window, tmp_path, monkeypatch, qapp):
    """Scenario: a field worker pauses a batch, then stops it while paused.

    Expected behaviour: the worker is released rather than wedged in wait().
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

    batch_window._pause_event.clear()
    batch_window._on_survey_stop()
    assert batch_window._pause_event.is_set()
    assert batch_window._survey_cancel_event.is_set()


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


def test_double_click_opens_the_run_that_succeeded(batch_window, tmp_path, monkeypatch):
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
        (tmp_path / name).mkdir()

    opened = []
    monkeypatch.setattr(batch_window, "_auto_load_run", opened.append)
    batch_window._on_survey_pass_activated(0, 0)
    assert opened == [tmp_path / "run_good"]


def test_double_click_is_refused_while_a_batch_runs(batch_window, tmp_path, monkeypatch):
    """The live run owns the viewer; an old one must not take it away."""
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    opened = []
    monkeypatch.setattr(batch_window, "_auto_load_run", opened.append)
    monkeypatch.setattr(batch_window, "_run_in_flight", lambda: True)
    batch_window._on_survey_pass_activated(0, 0)
    assert opened == []
    assert "Wait for the batch" in batch_window._status_label.text()


def test_unprocessed_row_says_so_rather_than_doing_nothing(batch_window, tmp_path, monkeypatch):
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    opened = []
    monkeypatch.setattr(batch_window, "_auto_load_run", opened.append)
    batch_window._on_survey_pass_activated(0, 0)
    assert opened == []
    assert "no successful run" in batch_window._status_label.text()


def test_gate_summary_and_run_settings_agree(simple_window, monkeypatch):
    """The gate, the summary label and the run all derive from the form.

    With one shared source they cannot disagree: the models the gate blocks on
    are exactly those _collect_run_settings() names, and the label describes the
    same run.
    """
    from deepreefmap_gui.models import manager

    monkeypatch.setattr(manager, "is_model_cached", lambda info: False)
    simple_window._skip_seg_check.setChecked(False)
    simple_window._seg_combo.setCurrentText("segformer-b2")
    simple_window._map_combo.setCurrentText("scsfmlearner")
    simple_window._recompute_survey_start()

    settings = simple_window._collect_run_settings()
    required = simple_window._required_model_names()
    # Nothing is cached, so every required model is missing and nothing else is.
    assert set(simple_window._survey_missing_models()) == required
    assert settings["mapping_name"] in required
    assert settings["segmentation_name"] in required
    label = simple_window._survey_preset_label.text()
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
    window._out_root_input.setText(str(tmp_path))
    window._set_ui_mode("simple")
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
        combo = table.cellWidget(index, _COL_TRANSECT)
        assert combo.currentText() == target.name
        # The amber "not assigned" variant is a per-widget stylesheet.
        assert combo.styleSheet() == ""
    assert batch_window._survey_start_btn.text() == "Next: Process (3) →"


def test_pass_table_allows_a_multi_row_selection(batch_window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QTableWidget

    add_second_transect(batch_window)
    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    table = batch_window._survey_pass_table
    assert table.selectionMode() == QTableWidget.SelectionMode.ExtendedSelection
    assert not batch_window._survey_assign_btn.isEnabled()
    table.selectRow(1)
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
    assert table.cellWidget(1, _COL_DIRECTION).currentText() == "reverse"


def test_one_way_transect_asks_whether_that_is_right(batch_window, tmp_path, monkeypatch):
    """Nothing downstream can tell a one-way survey from forgotten dropdowns."""
    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    notice = batch_window._survey_direction_notice
    assert not notice.isHidden()
    assert notice.text() == "T1: 2 passes, all forward. Is that right?"

    batch_window._survey_pass_table.cellWidget(1, _COL_DIRECTION).setCurrentText("reverse")
    assert notice.isHidden()


def test_one_pass_of_a_transect_is_not_flagged(batch_window, tmp_path, monkeypatch):
    add_video(batch_window, tmp_path, monkeypatch)
    assert batch_window._survey_direction_notice.isHidden()


# --- Clip identity ---


def test_video_cell_names_the_clip_by_time_and_length(batch_window, tmp_path, monkeypatch):
    add_video(batch_window, tmp_path, monkeypatch, name="GX010012.MP4", duration_s=401.0)
    text = batch_window._survey_pass_table.item(0, _COL_VIDEO).text()
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
    assert "time unknown" in batch_window._survey_pass_table.item(2, _COL_VIDEO).text()


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
    assert wait_until(lambda: batch_window._survey_pass_table.rowCount() == 1)
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
    assert wait_until(lambda: batch_window._survey_pass_table.rowCount() == 1)
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
        batch_window._survey_rows[0], table.cellWidget(0, _COL_TRIM)
    )

    assert [(row.begin_s, row.end_s) for row in batch_window._survey_rows] == [
        (5.0, 40.0), (5.0, 40.0)
    ]
    store = batch_window._survey_store()
    assert {
        (store.get_pass(row.pass_id).begin_s, store.get_pass(row.pass_id).end_s)
        for row in batch_window._survey_rows
    } == {(5.0, 40.0)}
    assert table.cellWidget(1, _COL_TRIM).text() == "0:05-0:40"


def test_declining_the_bulk_trim_leaves_the_other_rows_alone(
    batch_window, tmp_path, monkeypatch
):
    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    stub_trim_dialog(monkeypatch, 5.0, 40.0)
    answer_question(monkeypatch, QMessageBox.StandardButton.No)
    batch_window._on_survey_row_trim(
        batch_window._survey_rows[0],
        batch_window._survey_pass_table.cellWidget(0, _COL_TRIM),
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
        batch_window._survey_pass_table.cellWidget(0, _COL_TRIM),
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
        batch_window._survey_pass_table.cellWidget(0, _COL_TRIM),
    )
    assert asked == []


# --- Retrying a pass ---


def test_retrying_a_pass_reuses_its_run_dir(batch_window, tmp_path, monkeypatch, qapp):
    """Scenario: a pass fails, and the batch is run again.

    Expected behaviour: the retry lands in the directory the first attempt
    created, so the frames it already decoded are there to resume from.
    """
    dirs = []

    def failing(**kwargs):
        dirs.append(kwargs["output_dir"])
        raise RuntimeError("boom")

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", failing)
    add_video(batch_window, tmp_path, monkeypatch)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    assert len(dirs) == 2
    assert dirs[0] == dirs[1]
    assert "__p01__" in dirs[0].name


def test_survey_worker_seeds_from_a_matching_run(batch_window, tmp_path, monkeypatch, qapp):
    """A pass of a clip another run already preprocessed skips preprocessing."""
    from deepreefmap.pipeline import resume as resume_mod

    from deepreefmap_gui.runs.seeding import preprocess_key_for_settings

    clip = add_video(batch_window, tmp_path, monkeypatch)
    prior = tmp_path / "earlier-run"
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
    assert batch_window._survey_pass_table.rowCount() == 1
    row = batch_window._survey_rows[0]
    assert [video.file_name for video in row.videos] == ["GX010012.MP4", "GX020012.MP4"]
    assert (row.begin_s, row.end_s) == (0.0, 600.0)

    text = batch_window._survey_pass_table.item(0, _COL_VIDEO).text()
    assert text.startswith("GX010012.MP4 +1 chapter · ")
    assert text.endswith(" · 10m 00s")

    pass_ = batch_window._survey_store().get_pass(row.pass_id)
    assert pass_.video_ids() == [video.id for video in row.videos]


def test_separate_recordings_stay_separate_passes(batch_window, tmp_path, monkeypatch):
    add_videos(
        batch_window, tmp_path, monkeypatch, ["GX010012.MP4", "GX010013.MP4", "reef.mp4"]
    )
    assert batch_window._survey_pass_table.rowCount() == 3


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

    assert batch_window._survey_pass_table.rowCount() == 1
    assert [video.file_name for video in batch_window._survey_rows[0].videos] == [
        "GX010012.MP4", "GX020012.MP4"
    ]


def test_splitting_a_chaptered_pass_keeps_every_chapter(batch_window, tmp_path, monkeypatch):
    add_videos(batch_window, tmp_path, monkeypatch, ["GX010012.MP4", "GX020012.MP4"])
    batch_window._survey_pass_table.setCurrentCell(0, 0)
    batch_window._on_survey_split_pass()

    store = batch_window._survey_store()
    assert batch_window._survey_pass_table.rowCount() == 2
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
    assert wait_until(lambda: batch_window._survey_pass_table.rowCount() == 1)

    row = batch_window._survey_rows[0]
    assert len(row.videos) == 1
    assert row.end_s == 60.0
