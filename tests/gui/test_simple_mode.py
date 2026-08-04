from deepreefmap_gui.simple.mode import SIMPLE_SECTIONS


def test_the_run_form_is_kept_off_screen_in_its_holder(window):
    """The form is only ever shown inside the run settings dialog.

    It sits in a hidden holder owned by the window the rest of the time, so none
    of it reaches the screen. The way to start work is the Run step's own button.
    """
    assert window._form_home.isAncestorOf(window._setup_page)
    assert not window._form_home.isVisibleTo(window)
    assert not window._setup_page.isVisibleTo(window)


def test_bundled_preset_reaches_the_run_settings(window):
    """A fresh window must carry the saved preset into the run.

    The batch runs from _collect_run_settings(), so populating only the preset
    dict is not enough: the form has to hold the preset's values on startup, and
    since the form is never on screen nothing else would catch it keeping its
    constructor defaults. transect_crop_width is the tell, because the form
    default is 0.0, which _collect_run_settings maps to None (crop disabled),
    while the bundled preset asks for 1.0.
    """
    settings = window._collect_run_settings()
    assert settings["transect_crop_width"] == window._survey_preset["transect_crop_width"]
    assert settings["transect_crop_width"] == 1.0
    assert settings["fps"] == window._survey_preset["fps"]
    assert settings["segmentation_name"] == window._survey_preset["segmentation_name"]


def test_simple_nav_switches_sections(window):
    window._set_simple_section("run")
    assert window._simple_stack.currentIndex() == 1
    window._set_simple_section("browse")
    assert window._simple_stack.currentIndex() == 2
    window._simple_nav_buttons["plan"].click()
    assert window._simple_stack.currentIndex() == 0


def test_browse_is_a_workspace_beside_the_flow(window):
    """Scenario: Browse is a place, not the end of the Plan -> Run sequence.

    Expected behaviour: it has its own workspace button, the step controls
    disappear while it is showing, and returning to Survey lands on the step
    that was left rather than always the first one.
    """
    window._set_simple_section("run")
    window._workspace_buttons["browse"].click()
    assert window._simple_stack.currentIndex() == 2
    assert not window._simple_nav_buttons["plan"].isVisibleTo(window)
    window._workspace_buttons["survey"].click()
    assert window._simple_stack.currentIndex() == 1
    assert window._simple_nav_buttons["plan"].isVisibleTo(window)


def test_browse_stays_reachable_while_a_batch_runs(window):
    """A batch takes tens of minutes; reading finished runs meanwhile is the point."""
    window._set_wizard_navigation_enabled(False)
    assert not window._simple_nav_buttons["run"].isEnabled()
    assert not window._workspace_buttons["survey"].isEnabled()
    assert window._workspace_buttons["browse"].isEnabled()


def test_only_a_starting_batch_relocates_the_user(window, monkeypatch):
    """Opening a run never moves the page; a batch starting is the one exception."""
    monkeypatch.setattr(window._viewer, "_ensure_plotter", lambda: None)
    window._set_app_mode("RUNNING")
    assert window._simple_stack.currentIndex() == 1
    window._set_simple_section("browse")
    window._set_app_mode("VIEWING")
    assert window._simple_stack.currentIndex() == 2
    window._set_app_mode("SETUP")
    assert window._simple_stack.currentIndex() == 2


def test_the_viewer_pane_follows_the_section(window, monkeypatch):
    """The cloud belongs to View: no app mode brings it onto another page."""
    monkeypatch.setattr(window._viewer, "_ensure_plotter", lambda: None)
    assert not window._viewer.isVisibleTo(window)
    for section in (name for name in SIMPLE_SECTIONS if name != "view"):
        for mode in ("RUNNING", "VIEWING", "SETUP"):
            window._set_app_mode(mode)
            window._set_simple_section(section)
            assert not window._viewer.isVisibleTo(window)
    window._set_simple_section("view")
    assert window._viewer.isVisibleTo(window)


def test_leaving_view_mode_takes_the_viewer_with_it(window):
    window._set_simple_section("view")
    window._set_simple_section("browse")
    assert window._viewer.isHidden()


def test_view_mode_gives_the_viewport_the_window(window, monkeypatch):
    """The info column is off by default, so the cloud starts at full width."""
    monkeypatch.setattr(window._viewer, "_ensure_plotter", lambda: None)
    window._set_simple_section("view")
    assert window._work_hsplitter.sizes()[0] == 0
    assert window._view_bar.isVisibleTo(window)
    assert not window._simple_header.isVisibleTo(window)

    window._view_info_btn.setChecked(True)
    assert window._work_hsplitter.sizes()[0] > 0


def test_revealing_the_canvas_needs_no_permission(window, monkeypatch):
    """The canvas gate is gone: scene data arriving is what reveals the cloud."""
    viewer = window._viewer
    monkeypatch.setattr(viewer, "_ensure_plotter", lambda: None)
    viewer._reveal_canvas()
    assert viewer._canvas_stack.currentWidget() is viewer._canvas_container
    viewer._hide_canvas()
    assert viewer._canvas_stack.currentWidget() is viewer._placeholder_container


def test_the_survey_db_is_created_under_the_output_root(window):
    from pathlib import Path

    from deepreefmap_gui.survey.store import SURVEY_DB_NAME

    root = Path(window._out_root_input.text()).expanduser()
    assert (root / SURVEY_DB_NAME).exists()


def test_survey_is_two_numbered_steps(window):
    assert list(window._simple_nav_buttons) == ["plan", "run"]
    assert window._simple_nav_buttons["plan"].text() == "Plan"
    assert window._simple_nav_buttons["run"].text() == "Run"
    # The step number lives in the badge icon beside the label.
    assert not window._simple_nav_buttons["plan"].icon().isNull()


def test_next_and_back_walk_the_steps(window):
    window._go_to_step("run")
    assert window._simple_stack.currentIndex() == 1
    window._wizard_back_buttons["run"].click()
    assert window._simple_stack.currentIndex() == 0
    window._wizard_next_buttons["plan"].click()
    assert window._simple_stack.currentIndex() == 1


def test_run_is_the_last_step(window):
    """Run's forward action is the run button itself, so it has no Next."""
    assert "run" not in window._wizard_next_buttons
    assert "plan" not in window._wizard_back_buttons
    assert "browse" not in window._wizard_next_buttons
    assert "browse" not in window._wizard_back_buttons


def test_navigation_locks_while_a_run_is_in_flight(window):
    window._set_wizard_navigation_enabled(False)
    assert not window._simple_nav_buttons["plan"].isEnabled()
    assert not window._wizard_next_buttons["plan"].isEnabled()
    window._set_wizard_navigation_enabled(True)
    assert window._simple_nav_buttons["plan"].isEnabled()
    assert window._wizard_next_buttons["plan"].isEnabled()
