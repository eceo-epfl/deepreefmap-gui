"""A CSV of the day's dives fills the survey queue rather than starting a second one.

Scenario: the batch CSV and the pass table were two separate queues over the same
videos, reached by two different buttons.

Expected behaviour: importing queues passes in the one table, honouring each
row's trim and the optional transect column, and blocking the same way a dropped
video does when the transect is unknown.
"""

from __future__ import annotations

import pytest
from _factories import make_transect

from deepreefmap_gui.simple.batch import _COL_TRANSECT, _COL_TRIM


@pytest.fixture
def import_window(simple_window, monkeypatch):
    window = simple_window
    monkeypatch.setattr(window, "_survey_missing_models", list)
    monkeypatch.setattr("deepreefmap_gui.simple.batch._probe_video", lambda _p: (60.0, 30.0))
    return window


def write_csv(tmp_path, rows, header="videos,timestamps,transect_length,crop_width"):
    for name in {row.split(",")[0] for row in rows}:
        (tmp_path / name).write_bytes(b"x" * 512)
    csv_path = tmp_path / "dives.csv"
    csv_path.write_text(header + "\n" + "".join(f"{row}\n" for row in rows))
    return csv_path


def do_import(window, csv_path, monkeypatch):
    monkeypatch.setattr(
        "deepreefmap_gui.simple.batch.QFileDialog.getOpenFileName",
        staticmethod(lambda *a, **k: (str(csv_path), "")),
    )
    window._on_survey_import_csv()


def test_import_queues_one_pass_per_row(import_window, tmp_path, monkeypatch):
    window = import_window
    window._survey_store().add_transect(make_transect())
    window._refresh_survey_batch_tab()
    csv_path = write_csv(
        tmp_path, [f"{tmp_path / 'a.mp4'},,10,2", f"{tmp_path / 'b.mp4'},,10,2"]
    )

    do_import(window, csv_path, monkeypatch)

    assert len(window._survey_rows) == 2
    assert "Queued 2 pass" in window._status_label.text()
    # One planned transect, so both rows land assigned and the batch can run.
    assert all(row.transect_id is not None for row in window._survey_rows)
    assert window._survey_start_btn.isEnabled()


def test_import_honours_the_timestamp_range(import_window, tmp_path, monkeypatch):
    window = import_window
    window._survey_store().add_transect(make_transect())
    window._refresh_survey_batch_tab()
    csv_path = write_csv(tmp_path, [f"{tmp_path / 'a.mp4'},12-34,10,2"])

    do_import(window, csv_path, monkeypatch)

    row = window._survey_rows[0]
    assert (row.begin_s, row.end_s) == (12.0, 34.0)
    assert window._survey_pass_table.cellWidget(window._table_row_of(0), _COL_TRIM).text() == "0:12-0:34"


def test_a_range_past_the_end_is_clamped(import_window, tmp_path, monkeypatch):
    """The probe knows the real duration, so a stale spreadsheet cannot overrun it."""
    window = import_window
    window._survey_store().add_transect(make_transect())
    window._refresh_survey_batch_tab()
    csv_path = write_csv(tmp_path, [f"{tmp_path / 'a.mp4'},10-9999,10,2"])

    do_import(window, csv_path, monkeypatch)

    assert window._survey_rows[0].end_s == 60.0


def test_the_transect_column_assigns_the_pass(import_window, tmp_path, monkeypatch):
    window = import_window
    store = window._survey_store()
    store.add_transect(make_transect("T1"))
    store.add_transect(
        make_transect("Reef North", start_lat=-17.6, start_lon=177.2, end_lat=-17.6005, end_lon=177.2005)
    )
    window._refresh_survey_batch_tab()
    csv_path = write_csv(
        tmp_path,
        [f"{tmp_path / 'a.mp4'},,10,2,reef north"],
        header="videos,timestamps,transect_length,crop_width,transect",
    )

    do_import(window, csv_path, monkeypatch)

    combo = window._survey_pass_table.cellWidget(window._table_row_of(0), _COL_TRANSECT)
    assert combo.currentText() == "Reef North"
    assert len(store.list_passes()) == 1


def test_an_unknown_transect_lands_unassigned_and_says_so(import_window, tmp_path, monkeypatch):
    window = import_window
    window._survey_store().add_transect(make_transect("T1"))
    window._survey_store().add_transect(
        make_transect("T2", start_lat=-17.6, start_lon=177.2, end_lat=-17.6005, end_lon=177.2005)
    )
    window._refresh_survey_batch_tab()
    csv_path = write_csv(
        tmp_path,
        [f"{tmp_path / 'a.mp4'},,10,2,Nowhere"],
        header="videos,timestamps,transect_length,crop_width,transect",
    )

    do_import(window, csv_path, monkeypatch)

    assert window._survey_rows[0].transect_id is None
    assert "not yet planned" in window._status_label.text()
    assert not window._survey_start_btn.isEnabled()


def test_a_bad_csv_queues_nothing(import_window, tmp_path, monkeypatch):
    window = import_window
    csv_path = tmp_path / "wrong.csv"
    csv_path.write_text("video,start\na.mp4,0\n")

    do_import(window, csv_path, monkeypatch)

    assert len(window._survey_rows) == 0
    assert "Nothing imported" in window._status_label.text()


def test_import_is_refused_mid_batch(import_window, tmp_path, monkeypatch):
    window = import_window
    window._survey_worker_running = True
    csv_path = write_csv(tmp_path, [f"{tmp_path / 'a.mp4'},,10,2"])

    do_import(window, csv_path, monkeypatch)

    assert len(window._survey_rows) == 0
    assert "Unavailable while processing" in window._status_label.text()
