"""Filing a section: the map, the list, the direction, and a new transect."""

import pytest
from _factories import make_transect

from deepreefmap_gui.simple.transect_picker import TransectPickerDialog
from deepreefmap_gui.survey.store import SurveyStore

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture
def store(tmp_path):
    return SurveyStore(tmp_path / "survey.db")


def test_a_section_can_be_filed_nowhere(store):
    """Unassigned stays a valid answer: a section is a cutout of a video first."""
    store.add_transect(make_transect("T1"))
    dialog = TransectPickerDialog(None, store)

    assert dialog.choice() == (None, "forward")


def test_picking_from_the_list_answers_with_that_transect(store):
    transect = make_transect("T1")
    store.add_transect(transect)
    dialog = TransectPickerDialog(None, store)

    dialog.list.setCurrentRow(1)
    dialog.direction.setCurrentIndex(1)

    assert dialog.choice() == (transect.id, "reverse")


def test_clicking_the_line_on_the_map_picks_it(store):
    """The whole reason there is a map: a transect is a place, not a name."""
    transect = make_transect("T1")
    store.add_transect(transect)
    dialog = TransectPickerDialog(None, store)

    dialog.map.transect_clicked.emit(str(transect.id))

    assert dialog.selected_transect_id() == transect.id


def test_direction_reads_as_the_heading_it_means(store):
    """"Forward" says nothing about the water until there is a line to swim."""
    store.add_transect(make_transect("T1", start_lat=-17.5, start_lon=177.1,
                                    end_lat=-17.5, end_lon=177.2))
    dialog = TransectPickerDialog(None, store)
    dialog.list.setCurrentRow(1)

    labels = [dialog.direction.itemText(i) for i in range(dialog.direction.count())]
    assert labels[0].startswith("Forward (090°")
    assert labels[1].startswith("Reverse (270°")


def test_the_section_it_is_editing_opens_on_its_own_answer(store):
    transect = make_transect("T1")
    store.add_transect(transect)
    dialog = TransectPickerDialog(None, store, transect_id=transect.id, direction="reverse")

    assert dialog.choice() == (transect.id, "reverse")


def test_a_new_transect_is_named_and_drawn_with_two_clicks(store):
    dialog = TransectPickerDialog(None, store)
    dialog._start_new_transect()

    assert dialog.name_input.text() == "Transect 1"
    assert dialog.map._pick_mode

    dialog.name_input.setText("Reef edge")
    dialog._on_map_clicked(-17.5, 177.1)
    dialog._on_map_clicked(-17.5005, 177.1005)
    dialog.length_input.setValue(25.0)
    dialog._save_new_transect()

    stored = store.list_transects()
    assert [t.name for t in stored] == ["Reef edge"]
    assert stored[0].length_m == 25.0
    # Saving files the section against it too, or the two clicks bought nothing.
    assert dialog.choice() == (stored[0].id, "forward")
    assert not dialog.map._pick_mode


def test_a_half_drawn_transect_names_what_is_missing(store):
    dialog = TransectPickerDialog(None, store)
    dialog._start_new_transect()
    dialog._on_map_clicked(-17.5, 177.1)
    dialog._save_new_transect()

    assert "Missing end point" in dialog.error.text()
    assert store.list_transects() == []


def test_a_duplicate_name_is_refused_rather_than_crashing(store):
    store.add_transect(make_transect("T1"))
    dialog = TransectPickerDialog(None, store)
    dialog._start_new_transect()
    dialog.name_input.setText("T1")
    dialog._on_map_clicked(-17.5, 177.1)
    dialog._on_map_clicked(-17.5005, 177.1005)
    dialog._save_new_transect()

    assert "already exists" in dialog.error.text()
    assert len(store.list_transects()) == 1


def test_the_arrow_hands_the_transect_to_the_page_and_stands_down(store):
    """Leaving for the page abandons the filing, so nothing is half applied."""
    transect = make_transect("T1")
    store.add_transect(transect)
    dialog = TransectPickerDialog(None, store, transect_id=transect.id)
    seen = []
    dialog.open_transect_requested.connect(seen.append)

    dialog._on_open_page()

    assert seen == [str(transect.id)]
    assert dialog.result() == int(TransectPickerDialog.DialogCode.Rejected)


def test_the_arrow_is_dead_while_nothing_is_picked(store):
    store.add_transect(make_transect("T1"))
    dialog = TransectPickerDialog(None, store)

    assert not dialog.open_btn.isEnabled()

    dialog.list.setCurrentRow(1)
    assert dialog.open_btn.isEnabled()
