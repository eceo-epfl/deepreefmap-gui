import json
from pathlib import Path

from PySide6.QtCore import QEvent, QUrl

from deepreefmap_gui.runs.run_cards import RUN_META_ROLE
from deepreefmap_gui.survey.catalogue import UNASSIGNED_TITLE
from deepreefmap_gui.survey.models import Transect, VideoAsset
from deepreefmap_gui.survey.store import SurveyStore

from _factories import seed_survey_run, write_run
from _qt_wait import wait_until


class _FakeMime:
    def __init__(self, urls):
        self._urls = urls

    def hasUrls(self):
        return bool(self._urls)

    def urls(self):
        return self._urls


class _FakeDropEvent:
    """Duck-typed drag/drop event, so the filter can be tested without a real drag."""

    def __init__(self, etype, urls):
        self._type = etype
        self._mime = _FakeMime([QUrl.fromLocalFile(u) for u in urls])
        self.accepted = False

    def type(self):
        return self._type

    def mimeData(self):
        return self._mime

    def acceptProposedAction(self):
        self.accepted = True


def add_library_video(root: Path, path: str, content_hash: str) -> None:
    """Register a clip in the store the window will reopen, then release it."""
    store = SurveyStore(root / "survey.db")
    store.upsert_video(VideoAsset(file_name=Path(path).name, path=path, hash=content_hash))
    store.close()


def write_survey_run(root: Path, dir_name: str) -> Transect:
    """Seed a survey run through a store the window will reopen at root/survey.db.

    The window owns its own connection, so this one is closed before handing back.
    """
    store = SurveyStore(root / "survey.db")
    transect, _pass, _run = seed_survey_run(store, root, dir_name)
    store.close()
    return transect


def test_runs_facet_lists_all_runs(tmp_path, make_window):
    root = tmp_path / "out"
    write_run(root, "run_a")
    write_run(root, "run_b", run_timestamp="2026-07-02T10:00:00+00:00")
    window = make_window()
    assert window._data_facet == "runs"
    assert window._data_run_list.count() == 2
    assert window._data_run_list.item(0).text() == "run_b"


def test_transects_facet_groups_and_buckets_unassigned(tmp_path, make_window):
    root = tmp_path / "out"
    write_survey_run(root, "assigned")
    write_run(root, "loose", video_hashes=["cd" * 16])
    window = make_window()
    window._data_facet_buttons["transects"].click()
    titles = [
        window._data_tree.topLevelItem(i).text(0)
        for i in range(window._data_tree.topLevelItemCount())
    ]
    assert titles[0].startswith(UNASSIGNED_TITLE)
    assert any(t.startswith("T1") for t in titles)


def test_videos_facet_splits_time_windows(tmp_path, make_window):
    root = tmp_path / "out"
    write_run(root, "first", begin_s=0.0, end_s=60.0)
    write_run(root, "second", begin_s=60.0, end_s=120.0)
    window = make_window()
    window._data_facet_buttons["videos"].click()
    assert window._data_tree.topLevelItemCount() == 1
    assert window._data_tree.topLevelItem(0).childCount() == 2


def test_tree_selection_filters_run_list(tmp_path, make_window):
    root = tmp_path / "out"
    write_run(root, "first", begin_s=0.0, end_s=60.0)
    write_run(root, "second", begin_s=60.0, end_s=120.0)
    window = make_window()
    window._data_facet_buttons["videos"].click()
    assert window._data_run_list.count() == 2
    child = window._data_tree.topLevelItem(0).child(0)
    window._data_tree.setCurrentItem(child)
    assert window._data_run_list.count() == 1


def test_open_routes_through_auto_load(tmp_path, make_window, monkeypatch):
    root = tmp_path / "out"
    run_dir = write_run(root, "run_a")
    window = make_window()
    loaded = []
    monkeypatch.setattr(window, "_auto_load_run", loaded.append)
    window._data_run_list.setCurrentRow(0)
    window._on_data_open_clicked()
    assert loaded == [run_dir]
    assert window._run_meta_banner.isVisibleTo(window)


def test_data_panel_moves_between_hosts(make_window):
    window = make_window()
    assert window._data_panel.parentWidget() is window._data_host_simple
    window._mode_buttons["advanced"].click()
    assert window._data_panel.parentWidget() is window._data_tab
    window._mode_buttons["simple"].click()
    assert window._data_panel.parentWidget() is window._data_host_simple


def test_data_tab_and_nav_registered(make_window):
    """One widget, one name: both modes call the run browser Browse."""
    window = make_window()
    assert window._sidebar_tabs.tabText(window._TAB_DATA) == "Browse"
    assert window._workspace_buttons["browse"].text() == "Browse"
    assert window._sidebar_tabs.tabText(window._TAB_SYSTEM) == "System"
    assert list(window._simple_nav_buttons) == ["plan", "run"]
    assert list(window._workspace_buttons) == ["survey", "browse"]
    assert window._data_host_simple.isAncestorOf(window._data_panel)


def test_rename_updates_manifest_and_card(tmp_path, make_window, monkeypatch):
    root = tmp_path / "out"
    run_dir = write_run(root, "run_a")
    window = make_window()
    window._data_run_list.setCurrentRow(0)
    monkeypatch.setattr(
        "deepreefmap_gui.runs.data_manager.QInputDialog.getText",
        staticmethod(lambda *a, **k: ("reef north", True)),
    )
    window._on_data_rename_clicked()
    on_disk = json.loads((run_dir / "run_manifest.json").read_text())
    assert on_disk["name"] == "reef north"
    assert window._data_run_list.item(0).text().startswith("reef north")


def test_delete_removes_run_after_confirmation(tmp_path, make_window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    root = tmp_path / "out"
    run_dir = write_run(root, "doomed")
    window = make_window()
    window._data_run_list.setCurrentRow(0)
    monkeypatch.setattr(
        "deepreefmap_gui.runs.data_manager.QMessageBox.question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    window._on_data_delete_clicked()
    assert not run_dir.exists()
    assert window._data_run_list.count() == 0


def test_delete_refuses_open_run(tmp_path, make_window, monkeypatch):
    root = tmp_path / "out"
    run_dir = write_run(root, "active")
    window = make_window()
    window._active_run_dir = run_dir
    window._data_run_list.setCurrentRow(0)
    warnings = []
    monkeypatch.setattr(
        "deepreefmap_gui.runs.data_manager.QMessageBox.information",
        staticmethod(lambda *a, **k: warnings.append(a)),
    )
    window._on_data_delete_clicked()
    assert run_dir.exists()
    assert warnings


def test_assign_moves_loose_run_under_transect(tmp_path, make_window, monkeypatch):
    root = tmp_path / "out"
    transect = write_survey_run(root, "assigned")
    write_run(root, "loose", video_hashes=["cd" * 16])
    window = make_window()
    window._data_run_list.setCurrentRow(0 if window._data_run_list.item(0).text() == "loose" else 1)
    monkeypatch.setattr(
        window, "_ask_assign_target", lambda transects: (transect.id, "forward")
    )
    window._on_data_assign_clicked()
    entries = {e.dir_name: e for e in window._data_entries}
    assert entries["loose"].transect_name == "T1"


def test_size_scan_slot_updates_label_and_cards(tmp_path, make_window):
    root = tmp_path / "out"
    write_run(root, "run_a")
    window = make_window()
    window._apply_run_sizes({"run_a": 2_000_000_000})
    assert "2.00 GB" in window._data_disk_label.text()
    assert "Space used" in window._data_disk_label.text()
    meta = window._data_run_list.item(0).data(RUN_META_ROLE)
    assert "2.00 GB" in meta["facts"]


def test_rescan_reruns_the_manifest_rebuild(tmp_path, make_window, monkeypatch):
    """Scenario: a colleague's run is copied in while the app is open.

    Expected behaviour: the once-per-session rebuild gate does not re-link it,
    but Rescan resets the gate and reads the folder back.
    """
    root = tmp_path / "out"
    write_run(root, "first")
    window = make_window()

    calls = []
    real = SurveyStore.rebuild_from_scan

    def spy(self, scanned_root):
        calls.append(scanned_root)
        return real(self, scanned_root)

    monkeypatch.setattr(SurveyStore, "rebuild_from_scan", spy)
    window._refresh_data_manager()
    assert calls == []  # gated to once per root

    window._on_data_rescan_clicked()
    assert calls == [root]
    assert "Rescanned" in window._status_label.text()


def test_results_tab_has_no_rename_row(make_window):
    window = make_window()
    assert not hasattr(window, "_rename_btn")
    assert not hasattr(window, "_rename_edit")


# --- T1.1 open run folder from disk ---


def test_open_run_folder_routes_through_auto_load(tmp_path, make_window, monkeypatch):
    external = write_run(tmp_path / "external", "run_x")
    window = make_window()
    monkeypatch.setattr(
        "deepreefmap_gui.runs.data_manager.QFileDialog.getExistingDirectory",
        staticmethod(lambda *a, **k: str(external)),
    )
    loaded = []
    monkeypatch.setattr(window, "_auto_load_run", loaded.append)
    window._on_data_open_folder_clicked()
    assert loaded == [external]


def test_open_run_folder_refused_while_running(make_window, monkeypatch):
    window = make_window()
    monkeypatch.setattr(window, "_run_in_flight", lambda: True)
    steps = []
    monkeypatch.setattr(
        "deepreefmap_gui.runs.data_manager.QFileDialog.getExistingDirectory",
        staticmethod(lambda *a, **k: steps.append("dialog") or ""),
    )
    monkeypatch.setattr(window, "_auto_load_run", lambda p: steps.append("load"))
    window._on_data_open_folder_clicked()
    assert steps == []
    assert "Wait for the batch" in window._status_label.text()


# --- T1.2 show in folder ---


def test_show_in_folder_opens_the_run_dir(tmp_path, make_window, monkeypatch):
    root = tmp_path / "out"
    run_dir = write_run(root, "run_a")
    window = make_window()
    opened = []
    monkeypatch.setattr(
        "deepreefmap_gui.runs.data_manager.QDesktopServices.openUrl",
        staticmethod(lambda url: opened.append(url.toLocalFile())),
    )
    window._data_run_list.setCurrentRow(0)
    window._on_data_show_in_folder_clicked()
    assert opened == [str(run_dir)]


# --- T1.4 drag and drop ---


def test_drag_enter_with_urls_is_accepted(make_window):
    window = make_window()
    event = _FakeDropEvent(QEvent.Type.DragEnter, ["/data/reef.mp4"])
    assert window._data_drop_event_filter(window._data_run_list, event) is True
    assert event.accepted


def test_dropped_video_queues_a_pass(tmp_path, make_window, monkeypatch):
    clip = tmp_path / "reef.mp4"
    clip.write_bytes(b"x" * 4096)
    window = make_window()
    monkeypatch.setattr(
        "deepreefmap_gui.simple.batch._probe_video", lambda _p: (60.0, 30.0)
    )
    before = window._survey_pass_table.rowCount()
    window._handle_data_drop([clip])
    # Probing runs on a worker thread, so the row arrives with a queued signal.
    assert wait_until(lambda: window._survey_pass_table.rowCount() == before + 1)
    assert "Queued 1 pass from 1 video." in window._status_label.text()


def test_dropped_run_folder_opens_it(tmp_path, make_window, monkeypatch):
    run_dir = write_run(tmp_path / "out", "dropped")
    window = make_window()
    loaded = []
    monkeypatch.setattr(window, "_auto_load_run", loaded.append)
    window._handle_data_drop([run_dir])
    assert loaded == [run_dir]


def test_dropped_unsupported_file_reports(tmp_path, make_window):
    junk = tmp_path / "notes.txt"
    junk.write_text("hi")
    window = make_window()
    window._handle_data_drop([junk])
    assert "Drop video files or a run folder" in window._status_label.text()


# --- T1.5 failed / incomplete runs ---


def test_incomplete_run_is_listed_distinctly_and_deletable(tmp_path, make_window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    root = tmp_path / "out"
    write_run(root, "done")
    crashed = root / "crashed"
    crashed.mkdir(parents=True)
    (crashed / "run.log").write_text("boom")
    window = make_window()

    rows = {window._data_run_list.item(i).text(): i for i in range(window._data_run_list.count())}
    assert "crashed" in rows
    entry = next(e for e in window._data_entries if e.dir_name == "crashed")
    assert entry.incomplete
    meta = window._data_run_list.item(rows["crashed"]).data(RUN_META_ROLE)
    assert meta["status"] == "incomplete"

    window._data_run_list.setCurrentRow(rows["crashed"])
    monkeypatch.setattr(
        "deepreefmap_gui.runs.data_manager.QMessageBox.question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    window._on_data_delete_clicked()
    assert not crashed.exists()


# --- T1.7 multi-select actions ---


def _select_rows(window, names):
    for i in range(window._data_run_list.count()):
        item = window._data_run_list.item(i)
        item.setSelected(item.text() in names)


def test_multi_select_delete_removes_every_selected_run(tmp_path, make_window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    root = tmp_path / "out"
    write_run(root, "a")
    write_run(root, "b")
    write_run(root, "c")
    window = make_window()
    window._data_run_list.selectAll()
    monkeypatch.setattr(
        "deepreefmap_gui.runs.data_manager.QMessageBox.question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    window._on_data_delete_clicked()
    assert window._data_run_list.count() == 0


def test_multi_select_delete_refused_when_one_is_open(tmp_path, make_window, monkeypatch):
    root = tmp_path / "out"
    run_a = write_run(root, "a")
    write_run(root, "b")
    window = make_window()
    window._active_run_dir = run_a
    window._data_run_list.selectAll()
    warnings = []
    monkeypatch.setattr(
        "deepreefmap_gui.runs.data_manager.QMessageBox.information",
        staticmethod(lambda *a, **k: warnings.append(a)),
    )
    window._on_data_delete_clicked()
    assert warnings
    assert run_a.exists()


def test_multi_select_assign_moves_all_selected(tmp_path, make_window, monkeypatch):
    root = tmp_path / "out"
    transect = write_survey_run(root, "assigned")
    write_run(root, "loose_a", video_hashes=["cd" * 16])
    write_run(root, "loose_b", video_hashes=["ef" * 16])
    window = make_window()
    _select_rows(window, {"loose_a", "loose_b"})
    monkeypatch.setattr(
        window, "_ask_assign_target", lambda transects: (transect.id, "forward")
    )
    window._on_data_assign_clicked()
    entries = {e.dir_name: e for e in window._data_entries}
    assert entries["loose_a"].transect_name == "T1"
    assert entries["loose_b"].transect_name == "T1"


# --- T1.6 video library facet ---


def test_library_facet_surfaces_orphan_video(tmp_path, make_window):
    root = tmp_path / "out"
    root.mkdir(parents=True)
    add_library_video(root, "/data/orphan.mp4", "ff" * 16)
    window = make_window()
    window._data_facet_buttons["library"].click()
    assert window._data_facet == "library"
    assert window._data_run_stack.currentWidget() is window._data_video_list
    labels = [
        window._data_video_list.item(i).text()
        for i in range(window._data_video_list.count())
    ]
    assert labels == ["orphan.mp4  ·  not yet processed"]


def test_library_queue_as_pass_adds_a_row(tmp_path, make_window, monkeypatch):
    root = tmp_path / "out"
    root.mkdir(parents=True)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x" * 4096)
    add_library_video(root, str(clip), "aa" * 16)
    window = make_window()
    monkeypatch.setattr(
        "deepreefmap_gui.simple.batch._probe_video", lambda _p: (60.0, 30.0)
    )
    window._data_facet_buttons["library"].click()
    window._data_video_list.setCurrentRow(0)
    before = window._survey_pass_table.rowCount()
    window._on_data_queue_video_clicked()
    assert wait_until(lambda: window._survey_pass_table.rowCount() == before + 1)
