from deepreefmap_gui.simple.section_dialog import NewTransectDialog, SectionAssignDialog
from deepreefmap_gui.survey.store import SurveyStore


def test_new_transect_dialog_saves_and_names_faults(qapp, tmp_path):
    store = SurveyStore(tmp_path / "survey.db")
    dialog = NewTransectDialog(None, store)
    assert dialog._name.text() == "Transect 1"

    dialog._on_save()
    assert "Missing start point" in dialog._error.text()
    assert store.list_transects() == []

    dialog._start.setText("-17.5, 177.1")
    dialog._end.setText("-17.5005, 177.1005")
    dialog._length.setValue(25.0)
    dialog._on_save()
    assert dialog.transect is not None
    stored = store.list_transects()
    assert [t.name for t in stored] == ["Transect 1"]
    assert stored[0].length_m == 25.0


def test_new_transect_dialog_refuses_a_duplicate_name(qapp, tmp_path):
    from _factories import make_transect

    store = SurveyStore(tmp_path / "survey.db")
    store.add_transect(make_transect("T1"))
    dialog = NewTransectDialog(None, store)
    dialog._name.setText("T1")
    dialog._start.setText("-17.5, 177.1")
    dialog._end.setText("-17.5005, 177.1005")
    dialog._on_save()
    assert "already exists" in dialog._error.text()
    assert len(store.list_transects()) == 1


def test_section_assign_defaults_to_no_transect(qapp, tmp_path):
    """Skip transect stays a valid answer: a section is a cutout first."""
    from _factories import make_transect

    store = SurveyStore(tmp_path / "survey.db")
    transect = make_transect("T1")
    store.add_transect(transect)
    dialog = SectionAssignDialog(None, store)
    assert dialog.choice() == (None, "forward")

    dialog._transects.setCurrentIndex(1)
    dialog._direction.setCurrentText("reverse")
    assert dialog.choice() == (transect.id, "reverse")
