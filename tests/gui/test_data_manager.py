import json
from pathlib import Path

from _factories import seed_survey_run, write_run
from _qt_wait import wait_until
from PySide6.QtCore import QEvent, Qt, QUrl
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QSizePolicy

from deepreefmap_gui.runs.run_detail import OrthoDialog
from deepreefmap_gui.runs.run_table import COL_NAME, COL_POINTS, COL_SIZE, COL_STATUS
from deepreefmap_gui.survey.catalogue import UNASSIGNED_TITLE
from deepreefmap_gui.survey.models import Transect, VideoAsset
from deepreefmap_gui.survey.store import SurveyStore


def listed_runs(window) -> list[str]:
    """Run names in the order the table currently shows them."""
    table = window._data_run_table
    return [table.item(row, COL_NAME).text() for row in range(table.rowCount())]


def cell(window, row: int, column: int) -> str:
    return window._data_run_table.item(row, column).text()


def select_run(window, row: int) -> None:
    window._data_run_table.setCurrentCell(row, COL_NAME)


def row_of(window, name: str) -> int:
    return listed_runs(window).index(name)


def pane_stretch(window, index: int) -> int:
    """QSplitter has no stretch getter; it lives on the child's size policy."""
    return window._data_split.widget(index).sizePolicy().horizontalStretch()


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


def write_crashed_run(root: Path, dir_name: str) -> Path:
    """A run folder with no manifest: the shape a crash or a kill leaves behind."""
    run_dir = root / dir_name
    run_dir.mkdir(parents=True)
    (run_dir / "run.log").write_text("started\n", encoding="utf-8")
    return run_dir


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
    assert listed_runs(window) == ["run_b", "run_a"]


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
    assert len(listed_runs(window)) == 2
    child = window._data_tree.topLevelItem(0).child(0)
    window._data_tree.setCurrentItem(child)
    assert len(listed_runs(window)) == 1


def test_open_routes_through_auto_load(tmp_path, make_window, monkeypatch):
    root = tmp_path / "out"
    run_dir = write_run(root, "run_a")
    window = make_window()
    loaded = []
    monkeypatch.setattr(window, "_auto_load_run", loaded.append)
    select_run(window, 0)
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
    assert list(window._workspace_buttons) == ["survey", "browse", "videos"]
    assert window._data_host_simple.isAncestorOf(window._data_panel)


def test_rename_updates_manifest_and_card(tmp_path, make_window, monkeypatch):
    root = tmp_path / "out"
    run_dir = write_run(root, "run_a")
    window = make_window()
    select_run(window, 0)
    monkeypatch.setattr(
        "deepreefmap_gui.runs.data_manager.QInputDialog.getText",
        staticmethod(lambda *a, **k: ("reef north", True)),
    )
    window._on_data_rename_clicked()
    on_disk = json.loads((run_dir / "run_manifest.json").read_text())
    assert on_disk["name"] == "reef north"
    assert listed_runs(window) == ["reef north  (run_a)"]


def test_delete_removes_run_after_confirmation(tmp_path, make_window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    root = tmp_path / "out"
    run_dir = write_run(root, "doomed")
    window = make_window()
    select_run(window, 0)
    monkeypatch.setattr(
        "deepreefmap_gui.runs.data_manager.QMessageBox.question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    window._on_data_delete_clicked()
    assert not run_dir.exists()
    assert listed_runs(window) == []


def test_delete_refuses_open_run(tmp_path, make_window, monkeypatch):
    root = tmp_path / "out"
    run_dir = write_run(root, "active")
    window = make_window()
    window._active_run_dir = run_dir
    select_run(window, 0)
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
    select_run(window, row_of(window, "loose"))
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
    assert "1.9 GB" in window._data_disk_label.text()
    assert "Space used" in window._data_disk_label.text()
    assert cell(window, 0, COL_SIZE) == "1.9 GB"


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
    select_run(window, 0)
    window._on_data_show_in_folder_clicked()
    assert opened == [str(run_dir)]


# --- T1.4 drag and drop ---


def test_drag_enter_with_urls_is_accepted(make_window):
    window = make_window()
    event = _FakeDropEvent(QEvent.Type.DragEnter, ["/data/reef.mp4"])
    assert window._data_drop_event_filter(window._data_run_table, event) is True
    assert event.accepted


def test_dropped_video_queues_a_pass(tmp_path, make_window, monkeypatch):
    clip = tmp_path / "reef.mp4"
    clip.write_bytes(b"x" * 4096)
    window = make_window()
    monkeypatch.setattr(
        "deepreefmap_gui.simple.batch._probe_video", lambda _p: (60.0, 30.0)
    )
    before = len(window._survey_rows)
    window._handle_data_drop([clip])
    # Probing runs on a worker thread, so the row arrives with a queued signal.
    assert wait_until(lambda: len(window._survey_rows) == before + 1)
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

    assert "crashed" in listed_runs(window)
    entry = next(e for e in window._data_entries if e.dir_name == "crashed")
    assert entry.incomplete
    row = row_of(window, "crashed")
    assert cell(window, row, COL_STATUS) == "Incomplete"

    select_run(window, row)
    monkeypatch.setattr(
        "deepreefmap_gui.runs.data_manager.QMessageBox.question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    window._on_data_delete_clicked()
    assert not crashed.exists()


# --- T1.7 multi-select actions ---


def _select_rows(window, names):
    for row in range(window._data_run_table.rowCount()):
        selected = window._data_run_table.item(row, COL_NAME).text() in names
        for column in range(window._data_run_table.columnCount()):
            window._data_run_table.item(row, column).setSelected(selected)


def test_multi_select_delete_removes_every_selected_run(tmp_path, make_window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    root = tmp_path / "out"
    write_run(root, "a")
    write_run(root, "b")
    write_run(root, "c")
    window = make_window()
    window._data_run_table.selectAll()
    monkeypatch.setattr(
        "deepreefmap_gui.runs.data_manager.QMessageBox.question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    window._on_data_delete_clicked()
    assert listed_runs(window) == []


def test_multi_select_delete_refused_when_one_is_open(tmp_path, make_window, monkeypatch):
    root = tmp_path / "out"
    run_a = write_run(root, "a")
    write_run(root, "b")
    window = make_window()
    window._active_run_dir = run_a
    window._data_run_table.selectAll()
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


def test_videos_workspace_surfaces_orphan_video(tmp_path, make_window):
    """A clip nobody has processed is invisible to every run-shaped view, which
    is why the clip library is its own workspace rather than a facet here."""
    root = tmp_path / "out"
    root.mkdir(parents=True)
    add_library_video(root, "/data/orphan.mp4", "ff" * 16)
    window = make_window()
    window._refresh_videos_page()
    labels = [window._video_list.item(i).text() for i in range(window._video_list.count())]
    assert len(labels) == 1
    assert labels[0].startswith("orphan.mp4")
    assert "library" not in window._data_facet_buttons


def test_videos_queue_as_pass_adds_a_row(tmp_path, make_window, monkeypatch):
    root = tmp_path / "out"
    root.mkdir(parents=True)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x" * 4096)
    add_library_video(root, str(clip), "aa" * 16)
    window = make_window()
    monkeypatch.setattr(
        "deepreefmap_gui.simple.batch._probe_video", lambda _p: (60.0, 30.0)
    )
    window._refresh_videos_page()
    window._video_list.setCurrentRow(0)
    before = len(window._survey_rows)
    window._on_video_queue_clicked()
    assert wait_until(lambda: len(window._survey_rows) == before + 1)


def test_status_chips_count_and_filter_runs(tmp_path, make_window):
    """Scenario: a batch left two finished runs and one that crashed.

    Expected behaviour: the chip says how many of each before it is clicked, and
    clicking one narrows the list to those.
    """
    root = tmp_path / "out"
    write_run(root, "good_a")
    write_run(root, "good_b")
    write_crashed_run(root, "crashed")
    window = make_window()
    chips = window._data_status_chips
    assert chips._buttons["all"].text().endswith("3")
    assert chips._buttons["succeeded"].text().endswith("2")
    assert chips._buttons["unfinished"].text().endswith("1")

    chips.set_current("unfinished")
    assert listed_runs(window) == ["crashed"]
    chips.set_current("succeeded")
    assert len(listed_runs(window)) == 2


def test_search_narrows_the_run_list(tmp_path, make_window):
    root = tmp_path / "out"
    write_run(root, "north_reef")
    write_run(root, "south_lagoon")
    window = make_window()
    window._data_search.setText("lagoon")
    assert listed_runs(window) == ["south_lagoon"]
    window._data_search.setText("")
    assert len(listed_runs(window)) == 2


def test_filtered_empty_state_says_why(tmp_path, make_window):
    root = tmp_path / "out"
    write_run(root, "north_reef")
    window = make_window()
    window._data_search.setText("nothing matches this")
    assert window._data_run_stack.currentWidget() is window._data_empty_state
    assert "No runs match" in window._data_empty_state._message.text()


def test_detail_pane_follows_the_selection(tmp_path, make_window):
    """Nothing transect-shaped appears until a transect is what is selected."""
    root = tmp_path / "out"
    write_survey_run(root, "assigned")
    window = make_window()

    select_run(window, 0)
    assert window._data_detail_stack.currentIndex() == 1
    assert "assigned" in window._run_detail.title.text()

    window._data_facet_buttons["transects"].click()
    window._data_run_table.setCurrentCell(-1, -1)
    for i in range(window._data_tree.topLevelItemCount()):
        item = window._data_tree.topLevelItem(i)
        if item.text(0).startswith("T1"):
            window._data_tree.setCurrentItem(item)
    window._update_data_actions()
    assert window._data_detail_stack.currentIndex() == 2


def test_unfinished_run_detail_carries_its_reason(tmp_path, make_window):
    """The status bar loses a failure on the next event; the pane keeps it."""
    root = tmp_path / "out"
    write_crashed_run(root, "crashed")
    window = make_window()
    select_run(window, 0)
    assert not window._run_detail.error.isHidden()
    assert "did not finish" in window._run_detail.error.text()
    # A crashed run cannot be opened, only inspected on disk.
    assert not window._data_open_btn.isEnabled()
    assert window._data_show_btn.isEnabled()


# --- Sorting, and the map that drives the transect facet ---


def test_columns_sort_by_value_not_by_their_formatting(tmp_path, make_window):
    """"988k pts" is smaller than "1.2M pts", and every string comparison disagrees."""
    root = tmp_path / "out"
    write_run(root, "small", semantic_reference_points=988_000)
    write_run(root, "large", semantic_reference_points=1_200_000)
    window = make_window()

    table = window._data_run_table
    table.sortItems(COL_POINTS, Qt.SortOrder.AscendingOrder)
    assert listed_runs(window) == ["small", "large"]
    table.sortItems(COL_POINTS, Qt.SortOrder.DescendingOrder)
    assert listed_runs(window) == ["large", "small"]


def test_runs_missing_a_fact_sort_last_in_both_directions(tmp_path, make_window):
    """A blank is not a zero: it sinks whichever way the column is pointed."""
    root = tmp_path / "out"
    write_run(root, "counted", semantic_reference_points=500)
    write_crashed_run(root, "crashed")
    window = make_window()

    table = window._data_run_table
    for order in (Qt.SortOrder.AscendingOrder, Qt.SortOrder.DescendingOrder):
        table.sortItems(COL_POINTS, order)
        assert listed_runs(window)[-1] == "crashed"


def test_map_click_narrows_the_table_to_that_transect(tmp_path, make_window):
    root = tmp_path / "out"
    transect = write_survey_run(root, "assigned")
    write_run(root, "loose", video_hashes=["cd" * 16])
    window = make_window()
    window._data_facet_buttons["transects"].click()
    assert len(listed_runs(window)) == 2

    window._on_data_map_transect_clicked(str(transect.id))
    assert listed_runs(window) == ["assigned"]
    assert window._data_selected_key == ("transect", str(transect.id))


def test_the_map_draws_transects_only_while_grouping_by_them(tmp_path, make_window):
    """Grouping by video says nothing about where anything is."""
    root = tmp_path / "out"
    write_survey_run(root, "assigned")
    window = make_window()

    window._data_facet_buttons["transects"].click()
    assert window._data_map.isVisibleTo(window._data_rail)
    assert [o.label for o in window._data_map._transects] == ["T1"]

    window._data_facet_buttons["videos"].click()
    assert not window._data_map.isVisibleTo(window._data_rail)


def test_opening_a_run_lands_in_view_mode(tmp_path, make_window, monkeypatch):
    """Browsing and viewing each get the whole window, never half of it each."""
    root = tmp_path / "out"
    run_dir = write_run(root, "run_a")
    window = make_window()
    monkeypatch.setattr(window._viewer, "_ensure_plotter", lambda: None)

    window._enter_view_mode(run_dir)
    assert window._current_section() == "view"
    assert window._viewer.isVisibleTo(window)
    assert "run_a" in window._view_title.text()
    assert "run_a" in window._view_detail.title.text()

    window._set_simple_section("browse")
    assert window._viewer.isHidden()


def test_the_table_gets_the_bulk_of_browse(tmp_path, make_window):
    """The table is the page; the detail pane holds one run's facts beside it.

    Asserted on the stretch factors rather than the laid-out pixels: a test
    window is narrow enough that Qt clamps the requested sizes, but the stretch
    is what survives a resize and so is the rule being set.
    """
    write_run(tmp_path / "out", "run_a")
    window = make_window()
    assert (pane_stretch(window, 1), pane_stretch(window, 2)) == (7, 3)


def test_grouping_by_transect_widens_the_detail_pane(tmp_path, make_window):
    """A chart and a stats table need more room than a metadata card."""
    write_survey_run(tmp_path / "out", "assigned")
    window = make_window()
    window._data_facet_buttons["transects"].click()
    assert pane_stretch(window, 2) > 3

    window._data_facet_buttons["runs"].click()
    assert pane_stretch(window, 2) == 3


def test_detail_pane_shows_the_ortho_a_run_produced(tmp_path, make_window):
    root = tmp_path / "out"
    run_dir = write_run(root, "with_ortho")
    write_run(root, "no_ortho")
    QImage(60, 20, QImage.Format.Format_RGB32).save(str(run_dir / "ortho.png"))
    window = make_window()

    select_run(window, row_of(window, "with_ortho"))
    assert not window._run_detail.ortho.isHidden()
    assert not window._run_detail.ortho.pixmap().isNull()

    select_run(window, row_of(window, "no_ortho"))
    assert window._run_detail.ortho.isHidden()


def test_the_ortho_never_widens_the_detail_pane(tmp_path, make_window):
    """A pixmap's size hint is its own width; the pane must not follow it."""
    root = tmp_path / "out"
    run_dir = write_run(root, "wide_ortho")
    QImage(2000, 400, QImage.Format.Format_RGB32).save(str(run_dir / "ortho.png"))
    window = make_window()

    before = window._data_split.sizes()
    select_run(window, 0)
    assert window._data_split.sizes() == before

    strip = window._run_detail.ortho
    assert strip.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Ignored
    assert strip.pixmap().width() <= window._run_detail.width()


def test_clicking_the_ortho_opens_it_full_size(tmp_path, make_window, monkeypatch):
    root = tmp_path / "out"
    run_dir = write_run(root, "clickable")
    QImage(800, 200, QImage.Format.Format_RGB32).save(str(run_dir / "ortho.png"))
    window = make_window()
    select_run(window, 0)

    opened = []
    monkeypatch.setattr(OrthoDialog, "exec", lambda self: opened.append(self))
    window._run_detail.ortho.clicked.emit()
    assert len(opened) == 1
    assert opened[0].windowTitle() == "clickable"
    # Fitted to the dialog, not pasted at native size.
    assert opened[0]._image.pixmap().width() <= 800
