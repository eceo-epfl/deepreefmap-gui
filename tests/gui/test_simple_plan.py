import pytest
from _factories import make_transect

from deepreefmap_gui.survey.models import TransectPass, VideoAsset
from deepreefmap_gui.survey.models.exporters import save_transects_csv


def type_coord(window, which, text):
    """Type a coordinate the way a field worker pastes one off a GPS."""
    edit = window._tr_start_coord if which == "start" else window._tr_end_coord
    edit.setText(text)
    window._on_coords_edited()


def test_transect_autosaves_once_complete(simple_window):
    w = simple_window
    w._tr_name_input.setText("T1")
    type_coord(w, "start", "-17.5 177.1")
    assert w._survey_store().list_transects() == []
    type_coord(w, "end", "-17.5005, 177.1005")
    transects = w._survey_store().list_transects()
    assert len(transects) == 1
    assert transects[0].name == "T1"
    assert transects[0].start_lat == -17.5
    assert transects[0].end_lon == 177.1005
    w._tr_length.setValue(50.0)
    w._maybe_autosave()
    assert w._survey_store().list_transects()[0].length_m == 50.0
    assert w._transect_list.count() == 1
    # A typed tape length is the cable actually laid, so it stands in the row in
    # place of the straight-line distance between the GPS endpoints.
    assert "50 m tape" in w._transect_list.item(0).text()


def test_draft_row_tracks_typing_before_save(simple_window):
    from PySide6.QtCore import Qt

    w = simple_window
    w._refresh_transect_list()
    assert w._transect_list.count() == 0
    w._tr_name_input.setText("Nor")
    assert w._transect_list.count() == 1
    item = w._transect_list.item(0)
    assert item.data(Qt.ItemDataRole.UserRole) == "draft"
    assert "Nor" in item.text()
    assert w._transect_list.currentRow() == 0
    w._tr_name_input.setText("North reef")
    assert "North reef" in w._transect_list.item(0).text()
    assert w._survey_store().list_transects() == []


def test_out_of_range_coordinate_is_reported(simple_window):
    """The range check that used to live on the Quick field still applies."""
    w = simple_window
    w._tr_name_input.setText("T1")
    type_coord(w, "start", "-17.5, 177.1")
    type_coord(w, "end", "95.0, 177.1")
    assert w._survey_store().list_transects() == []
    with pytest.raises(ValueError, match="out of range"):
        w._form_coordinates()


def test_garbage_coordinate_never_saves(simple_window):
    w = simple_window
    w._tr_name_input.setText("T1")
    type_coord(w, "start", "junk")
    type_coord(w, "end", "-17.5, 177.1")
    assert w._survey_store().list_transects() == []


def test_map_buttons_set_endpoints_one_shot(simple_window):
    w = simple_window
    w._map_start_btn.setChecked(True)
    w._plan_map.map_clicked.emit(-17.5, 177.1)
    assert w._tr_start_coord.text() == "-17.500000, 177.100000"
    assert not w._map_start_btn.isChecked()
    w._map_end_btn.setChecked(True)
    w._plan_map.map_clicked.emit(-17.5005, 177.1005)
    assert w._tr_end_coord.text() == "-17.500500, 177.100500"
    assert not w._map_end_btn.isChecked()


def test_map_buttons_are_mutually_exclusive(simple_window):
    w = simple_window
    w._map_start_btn.setChecked(True)
    w._map_end_btn.setChecked(True)
    assert not w._map_start_btn.isChecked()
    w._map_start_btn.setChecked(True)
    assert not w._map_end_btn.isChecked()


def test_copy_endpoint_puts_latlon_on_clipboard(simple_window, monkeypatch):
    # Stubbed clipboard: the real X11 selection is shared with the desktop and
    # races with clipboard managers.
    captured = []

    class _Clipboard:
        def setText(self, text):
            captured.append(text)

    class _App:
        @staticmethod
        def clipboard():
            return _Clipboard()

    monkeypatch.setattr("deepreefmap_gui.simple.plan.QGuiApplication", _App)
    w = simple_window
    w._tr_start_coord.setText("-17.500000, 177.100000")
    w._copy_endpoint("start")
    assert captured == ["-17.500000, 177.100000"]
    w._copy_endpoint("end")
    assert "No end point to copy" in w._status_label.text()


def test_map_click_without_armed_button_is_ignored(simple_window):
    w = simple_window
    w._plan_map.map_clicked.emit(-17.5, 177.1)
    assert w._tr_start_coord.text() == ""
    assert w._tr_end_coord.text() == ""


def test_draft_line_appears_once_both_endpoints_set(simple_window):
    w = simple_window
    w._map_start_btn.setChecked(True)
    w._plan_map.map_clicked.emit(-17.5, 177.1)
    assert not any(t.id == "draft" for t in w._plan_map._transects)
    w._map_end_btn.setChecked(True)
    w._plan_map.map_clicked.emit(-17.5005, 177.1005)
    assert any(t.id == "draft" for t in w._plan_map._transects)
    w._tr_name_input.setText("T1")
    w._on_transect_save()
    assert not any(t.id == "draft" for t in w._plan_map._transects)
    assert len(w._plan_map._transects) == 1


def test_duplicate_name_reports_and_keeps_one(simple_window):
    w = simple_window
    w._survey_store().add_transect(make_transect())
    w._tr_name_input.setText("T1")
    type_coord(w, "start", "-17.6 177.2")
    type_coord(w, "end", "-17.6005 177.2005")
    w._on_transect_save()
    assert "already exists" in w._status_label.text()
    assert len(w._survey_store().list_transects()) == 1


def test_edit_selected_transect_updates_row(simple_window):
    w = simple_window
    w._survey_store().add_transect(make_transect())
    w._refresh_transect_list()
    w._transect_list.setCurrentRow(0)
    assert w._tr_name_input.text() == "T1"
    w._tr_end_coord.setText("-17.502, 177.1005")
    w._on_transect_save()
    stored = w._survey_store().list_transects()
    assert len(stored) == 1
    assert stored[0].end_lat == -17.502


def test_delete_with_passes_is_blocked(simple_window):
    w = simple_window
    store = w._survey_store()
    transect = make_transect()
    video = VideoAsset(file_name="a.mp4", path="/a.mp4", hash="cd" * 16)
    store.add_transect(transect)
    store.upsert_video(video)
    store.add_pass(
        TransectPass(transect_id=transect.id, video_id=video.id, begin_s=0.0, end_s=30.0)
    )
    w._refresh_transect_list()
    w._transect_list.setCurrentRow(0)
    w._on_transect_delete()
    assert "cannot be deleted" in w._status_label.text()
    assert len(store.list_transects()) == 1


def test_import_csv_skips_existing(simple_window, tmp_path, monkeypatch):
    w = simple_window
    existing = make_transect()
    w._survey_store().add_transect(existing)
    csv_path = tmp_path / "in.csv"
    save_transects_csv(csv_path, [existing, make_transect("T2")])
    monkeypatch.setattr(
        "deepreefmap_gui.simple.plan.QFileDialog.getOpenFileName",
        staticmethod(lambda *a, **k: (str(csv_path), "")),
    )
    w._on_transects_import()
    assert "Imported 1" in w._status_label.text()
    assert "Skipped 1" in w._status_label.text()
    assert [t.name for t in w._survey_store().list_transects()] == ["T1", "T2"]


def test_export_csv_round_trip(simple_window, tmp_path, monkeypatch):
    w = simple_window
    w._survey_store().add_transect(make_transect())
    out_path = tmp_path / "out.csv"
    monkeypatch.setattr(
        "deepreefmap_gui.simple.plan.QFileDialog.getSaveFileName",
        staticmethod(lambda *a, **k: (str(out_path), "")),
    )
    w._on_transects_export()
    assert "T1" in out_path.read_text()


def test_pick_both_walks_start_then_end(simple_window):
    """Scenario: a new transect drawn entirely on the map.

    Expected behaviour: one button, two clicks, then it disarms itself.
    """
    w = simple_window
    w._pick_both_btn.setChecked(True)
    assert w._plan_map._pick_mode
    w._plan_map.map_clicked.emit(-17.5, 177.1)
    assert w._tr_start_coord.text() == "-17.500000, 177.100000"
    assert w._tr_end_coord.text() == ""
    assert "end" in w._status_label.text().lower()
    w._plan_map.map_clicked.emit(-17.5005, 177.1005)
    assert w._tr_end_coord.text() == "-17.500500, 177.100500"
    assert not w._pick_both_btn.isChecked()
    assert not w._plan_map._pick_mode


def test_single_endpoint_pick_disarms_pick_both(simple_window):
    w = simple_window
    w._pick_both_btn.setChecked(True)
    w._map_end_btn.setChecked(True)
    assert not w._pick_both_btn.isChecked()
    assert w._pick_stage is None
    w._plan_map.map_clicked.emit(-17.6, 177.2)
    assert w._tr_end_coord.text() == "-17.600000, 177.200000"
    assert w._tr_start_coord.text() == ""


def test_notes_round_trip_through_the_store(simple_window):
    w = simple_window
    w._tr_name_input.setText("T1")
    type_coord(w, "start", "-17.5, 177.1")
    type_coord(w, "end", "-17.5005, 177.1005")
    w._tr_description.setPlainText("tape run W→E\nviz ~12 m")
    w._maybe_autosave()
    stored = w._survey_store().list_transects()[0]
    assert stored.description == "tape run W→E\nviz ~12 m"
    w._on_transect_new()
    w._refresh_transect_list()
    w._transect_list.setCurrentRow(0)
    assert w._tr_description.toPlainText() == "tape run W→E\nviz ~12 m"


def test_selecting_a_transect_filters_the_browser(simple_window):
    w = simple_window
    transect = make_transect()
    w._survey_store().add_transect(transect)
    w._refresh_transect_list()
    w._transect_list.setCurrentRow(0)
    assert w._data_facet == "transects"
    assert w._data_selected_key == ("transect", str(transect.id))


def test_save_offline_area_reports_the_size_saved(simple_window, monkeypatch):
    w = simple_window
    monkeypatch.setattr(w._plan_map, "save_visible_area", lambda: (12, 2_400_000))
    w._on_save_offline_area()
    text = w._status_label.text()
    assert "12 map tiles" in text
    assert "2.4 MB" in text


def test_save_offline_area_says_so_when_nothing_is_cached(simple_window, monkeypatch):
    w = simple_window
    monkeypatch.setattr(w._plan_map, "save_visible_area", lambda: (0, 0))
    w._on_save_offline_area()
    assert "Nothing to save yet" in w._status_label.text()
