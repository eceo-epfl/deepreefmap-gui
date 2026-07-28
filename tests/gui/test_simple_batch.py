import json
import time

import pytest

from deepreefmap_gui.simple.batch import _COL_STATUS, _COL_TRANSECT

from _factories import make_transect


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


def add_video(window, tmp_path, monkeypatch, name="GX010001.MP4"):
    path = tmp_path / name
    path.write_bytes(name.encode() * 4096)
    monkeypatch.setattr(
        "deepreefmap_gui.simple.batch._probe_video", lambda _path: (60.0, 30.0)
    )
    monkeypatch.setattr(
        "deepreefmap_gui.simple.batch.QFileDialog.getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(path)], "")),
    )
    window._on_survey_add_videos()
    return path


def assign_transect(window, row_index):
    combo = window._survey_pass_table.cellWidget(row_index, _COL_TRANSECT)
    combo.setCurrentIndex(1)


def add_second_transect(window, name="T2"):
    window._survey_store().add_transect(
        make_transect(name, start_lat=-17.6, start_lon=177.2, end_lat=-17.6005, end_lon=177.2005)
    )
    window._refresh_survey_batch_tab()


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
    assert batch_window._survey_pass_table.item(0, _COL_STATUS).text() == "succeeded"


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
