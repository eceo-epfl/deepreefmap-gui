"""The simple-mode run settings dialog borrows the real advanced form.

The invariant every test guards: the form goes back to the sidebar, whatever
way the dialog closes.
"""

from PySide6.QtWidgets import QDialog, QDialogButtonBox

from deepreefmap_gui.simple.settings_dialog import RunSettingsDialog


def per_run_widgets(window):
    return [
        window._video_row_widget,
        window._range_row_widget,
        window._run_name_widget,
        window._transect_length_widget,
    ]


def edit_then(window, monkeypatch, result, edit):
    """Drive one dialog session: make an edit, then close with `result`."""

    def _exec(_dialog):
        edit()
        return result

    monkeypatch.setattr(RunSettingsDialog, "exec", _exec)
    window._on_edit_run_settings()


def test_dialog_borrows_the_form_and_hands_it_back(simple_window):
    window = simple_window
    dialog = RunSettingsDialog(window, window._setup_page, per_run_widgets(window))
    assert dialog.isAncestorOf(window._setup_page)
    assert not window._video_row_widget.isVisibleTo(dialog)

    dialog.restore_form()
    assert window._run_tab.isAncestorOf(window._setup_page)
    assert not window._video_row_widget.isHidden()
    assert not window._run_name_widget.isHidden()


def test_every_exit_path_restores_the_form(simple_window):
    window = simple_window
    for close in (lambda d: d.accept(), lambda d: d.reject(), lambda d: d.close()):
        dialog = RunSettingsDialog(window, window._setup_page, per_run_widgets(window))
        close(dialog)
        assert window._run_tab.isAncestorOf(window._setup_page)


def test_restoring_twice_is_harmless(simple_window):
    window = simple_window
    dialog = RunSettingsDialog(window, window._setup_page, per_run_widgets(window))
    dialog.restore_form()
    dialog.restore_form()
    assert window._run_tab.isAncestorOf(window._setup_page)


def edit_in_dialog(window, monkeypatch, change):
    """Drive a dialog session that ends in OK, so the edit is adopted."""

    def _exec(_dialog):
        change()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(RunSettingsDialog, "exec", _exec)
    window._on_edit_run_settings()


def test_settings_edited_in_the_dialog_reach_the_run(simple_window, monkeypatch):
    window = simple_window
    edit_in_dialog(window, monkeypatch, lambda: window._grid_bins_spin.setValue(1234))
    assert window._run_tab.isAncestorOf(window._setup_page)
    assert window._survey_preset["grid_bins"] == 1234
    assert window._collect_run_settings()["grid_bins"] == 1234


def test_only_machine_settings_are_written_back(simple_window, monkeypatch, machine_preset_path):
    """Expected behaviour: the dialog persists what the computer owns and nothing
    else, so editing settings cannot rewrite the organisation preset."""
    import yaml

    window = simple_window

    def change():
        window._batch_size_spin.setValue(1)
        window._grid_bins_spin.setValue(1234)

    edit_in_dialog(window, monkeypatch, change)
    assert yaml.safe_load(machine_preset_path.read_text())["overrides"] == {
        "preprocess_batch_size": 1
    }


def test_restore_standard_settings_drops_the_machine_override(
    simple_window, monkeypatch, machine_preset_path
):
    window = simple_window
    standard = window._active_preset.org.settings["preprocess_batch_size"]
    edit_in_dialog(window, monkeypatch, lambda: window._batch_size_spin.setValue(1))
    assert machine_preset_path.exists()

    window._restore_standard_settings()
    assert not machine_preset_path.exists()
    assert window._batch_size_spin.value() == standard
    assert window._survey_deviations() == {}
    assert "back to Standard reef survey (v1)" in window._status_label.text()


def test_restore_says_so_when_there_is_nothing_to_restore(simple_window):
    simple_window._restore_standard_settings()
    assert "nothing to restore" in simple_window._status_label.text()


def test_restore_is_refused_mid_batch(simple_window, monkeypatch, machine_preset_path):
    window = simple_window
    edit_in_dialog(window, monkeypatch, lambda: window._batch_size_spin.setValue(1))
    window._survey_worker_running = True
    window._restore_standard_settings()
    assert machine_preset_path.exists()
    assert "Unavailable while processing" in window._status_label.text()


def test_cancel_abandons_the_edit(simple_window, monkeypatch):
    """The dialog edits the live form, so Cancel has to put the values back."""
    window = simple_window
    before = window._grid_bins_spin.value()
    edit_then(
        window,
        monkeypatch,
        QDialog.DialogCode.Rejected,
        lambda: window._grid_bins_spin.setValue(1234),
    )
    assert window._grid_bins_spin.value() == before
    assert window._collect_run_settings()["grid_bins"] == before
    assert window._survey_preset["grid_bins"] == before


def test_cancel_does_not_write_the_preset_file(simple_window, monkeypatch, tmp_path):
    target = tmp_path / "cancelled.yaml"
    monkeypatch.setattr("deepreefmap_gui.survey.preset.survey_preset_path", lambda: target)
    window = simple_window
    edit_then(
        window,
        monkeypatch,
        QDialog.DialogCode.Rejected,
        lambda: window._fps_spin.setValue(window._fps_spin.value() + 1),
    )
    assert not target.exists()


def test_cancel_undoes_reset_defaults(simple_window, monkeypatch):
    """Reset writes into the same live form, so it is part of the edit."""
    window = simple_window
    window._grid_bins_spin.setValue(1234)
    edit_then(window, monkeypatch, QDialog.DialogCode.Rejected, window._reset_form_defaults)
    assert window._grid_bins_spin.value() == 1234


def test_cancel_restores_a_custom_processing_size(simple_window, monkeypatch):
    """The preset stores no size unless the resolution is Custom, so an undo
    built on the preset would lose a Custom size on the way back."""
    window = simple_window
    window._resolution_preset_combo.setCurrentText("Custom")
    window._proc_width_spin.setValue(800)
    window._proc_height_spin.setValue(600)

    def switch_to_native():
        window._resolution_preset_combo.setCurrentText("Native")

    edit_then(window, monkeypatch, QDialog.DialogCode.Rejected, switch_to_native)
    assert window._resolution_preset_combo.currentText() == "Custom"
    assert (window._proc_width_spin.value(), window._proc_height_spin.value()) == (800, 600)


def test_the_dialog_offers_ok_cancel_and_reset(simple_window):
    window = simple_window
    dialog = RunSettingsDialog(window, window._setup_page, per_run_widgets(window))
    box = dialog.findChild(QDialogButtonBox)
    offered = {
        box.standardButton(button)
        for button in box.buttons()
    }
    assert offered == {
        QDialogButtonBox.StandardButton.Ok,
        QDialogButtonBox.StandardButton.Cancel,
        QDialogButtonBox.StandardButton.Reset,
    }
    dialog.restore_form()


def test_reset_defaults_button_restores_the_form_values(simple_window):
    window = simple_window
    window._grid_bins_spin.setValue(1234)
    window._reset_form_defaults()
    assert window._grid_bins_spin.value() == window._form_defaults["_grid_bins_spin"]


def test_mode_switch_is_blocked_while_the_dialog_holds_the_form(simple_window):
    window = simple_window
    window._settings_dialog_open = True
    window._request_ui_mode("advanced")
    assert window._ui_mode == "simple"
    assert "Close the run settings" in window._status_label.text()
    window._settings_dialog_open = False
    window._request_ui_mode("advanced")
    assert window._ui_mode == "advanced"


def test_settings_cannot_be_edited_mid_batch(simple_window):
    window = simple_window
    window._survey_worker_running = True
    window._on_edit_run_settings()
    assert window._run_tab.isAncestorOf(window._setup_page)
    assert "Unavailable while processing" in window._status_label.text()
