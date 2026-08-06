from deepreefmap_gui.simple.mode import DESTINATIONS, SIMPLE_SECTIONS
from deepreefmap_gui.simple.section_state import browse_state, transects_state


def _index(name: str) -> int:
    return SIMPLE_SECTIONS.index(name)


def test_the_run_form_is_kept_off_screen_in_its_holder(window):
    """The form is only ever shown inside the run settings dialog.

    It sits in a hidden holder owned by the window the rest of the time, so none
    of it reaches the screen. The way to start work is Process's own button.
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
    window._set_simple_section("process")
    assert window._simple_stack.currentIndex() == _index("process")
    window._set_simple_section("browse")
    assert window._simple_stack.currentIndex() == _index("browse")
    window._simple_nav_buttons["transects"].click()
    assert window._simple_stack.currentIndex() == _index("transects")


def test_one_destination_reads_as_live_at_a_time(window):
    """Scenario: the header used to hold a workspace switch and a step switch,
    both filled when active, so two pills said "here" at once.

    Expected behaviour: one exclusive group, so lighting one unlights the rest,
    and every destination stays visible from every other.
    """
    window._set_simple_section("process")
    assert [n for n, b in window._simple_nav_buttons.items() if b.isChecked()] == ["process"]
    window._simple_nav_buttons["browse"].click()
    assert window._simple_stack.currentIndex() == _index("browse")
    assert [n for n, b in window._simple_nav_buttons.items() if b.isChecked()] == ["browse"]
    assert window._simple_nav_buttons["transects"].isVisibleTo(window)


def test_this_machine_lights_no_destination(window):
    """It is a utility you visit and leave, so no pill should own it."""
    window._set_simple_section("browse")
    window._set_simple_section("machine")
    assert not any(b.isChecked() for b in window._simple_nav_buttons.values())


def test_browse_stays_reachable_while_a_batch_runs(window):
    """A batch takes tens of minutes; reading finished runs meanwhile is the point.

    Process is where the batch reports itself and Browse is where finished work
    lives, so only Transects is locked: editing a transect mid-batch would never
    reach the pass in flight.
    """
    window._set_navigation_enabled(False)
    assert not window._simple_nav_buttons["transects"].isEnabled()
    assert window._simple_nav_buttons["process"].isEnabled()
    assert window._simple_nav_buttons["browse"].isEnabled()


def test_only_a_starting_batch_relocates_the_user(window, monkeypatch):
    """Opening a run never moves the page; a batch starting is the one exception."""
    monkeypatch.setattr(window._viewer, "_ensure_plotter", lambda: None)
    window._set_app_mode("RUNNING")
    assert window._simple_stack.currentIndex() == _index("process")
    window._set_simple_section("browse")
    window._set_app_mode("VIEWING")
    assert window._simple_stack.currentIndex() == _index("browse")
    window._set_app_mode("SETUP")
    assert window._simple_stack.currentIndex() == _index("browse")


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


def test_an_opened_run_is_a_page_inside_browse(window, monkeypatch):
    """Scenario: opening a run used to hide the destination switch, so the app
    looked like it had changed mode and there was no way to Transects or
    Process without first backing out of a cloud.

    Expected behaviour: the header stays, Browse stays lit, and a breadcrumb
    says where inside Browse you are. The info column is still off by default,
    so the cloud starts at full width.
    """
    monkeypatch.setattr(window._viewer, "_ensure_plotter", lambda: None)
    window._set_simple_section("view")
    assert window._work_hsplitter.sizes()[0] == 0
    assert window._view_bar.isVisibleTo(window)
    assert window._simple_header.isVisibleTo(window)
    assert window._simple_nav_buttons["browse"].isChecked()

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


def test_every_destination_carries_a_name_and_a_glyph(window):
    assert list(window._simple_nav_buttons) == list(DESTINATIONS)
    assert [b.text() for b in window._simple_nav_buttons.values()] == [
        "Transects",
        "Videos",
        "Cart",
        "Browse",
    ]
    for button in window._simple_nav_buttons.values():
        assert not button.icon().isNull()


def test_no_page_carries_a_wizard_footer(window):
    """Scenario: "Next: Run" and "Next: Process" sat in the same place with the
    same fill, so navigation and the one action that commits the machine for
    hours were indistinguishable.

    Expected behaviour: no forward button exists at all, and the page a stack
    index holds is the page itself rather than a page wrapped in a footer.
    """
    assert not hasattr(window, "_wizard_next_buttons")
    assert not hasattr(window, "_wizard_back_buttons")
    assert window._simple_stack.widget(_index("browse")) is window._data_panel


def test_starting_is_named_for_what_it_does(window):
    """The start button never says "Next", and it counts what it will work."""
    text = window._survey_start_btn.text()
    assert "Next" not in text
    assert text.startswith(("Start processing", "Continue processing"))


def test_navigation_locks_while_a_run_is_in_flight(window):
    window._set_navigation_enabled(False)
    assert not window._simple_nav_buttons["transects"].isEnabled()
    window._set_navigation_enabled(True)
    assert window._simple_nav_buttons["transects"].isEnabled()


def test_a_quiet_header_shows_nothing_but_its_destinations(window):
    """A count belongs to the destination that owns it; the header's box appears
    only when something is wrong."""
    window._plan_state = transects_state(2, False)
    window._browse_state = browse_state(19, 0)
    window._survey_gate = None
    window._section_state_cache = None
    window._refresh_section_state()
    assert not window._section_alert.isVisibleTo(window)
    assert "2 transects" in window._simple_nav_buttons["transects"].toolTip()


def test_the_header_names_one_problem_and_goes_there(window):
    window._set_simple_section("transects")
    window._plan_state = transects_state(2, False)
    window._browse_state = browse_state(19, 17)
    window._survey_gate = None
    window._section_state_cache = None
    window._refresh_section_state()

    assert window._section_alert.isVisibleTo(window)
    assert window._section_alert._text.text() == "Browse: 17 runs belong to no transect"
    assert "Assign them" in window._section_alert.toolTip()

    window._section_alert.clicked.emit()
    assert window._simple_nav_buttons["browse"].isChecked()


def test_every_header_control_is_drawn_as_a_button(window):
    """One control for the whole band: the checked destination is filled, the
    rest are bordered exactly as the utilities are."""
    from deepreefmap_gui.core.widgets import utility_button_qss

    base = utility_button_qss()
    for button in window._simple_nav_buttons.values():
        assert button.styleSheet().startswith(base)
    assert window._log_toggle_btn.styleSheet().startswith(base)


def test_the_machine_destination_is_named_for_the_job(window):
    """Setup names what the page is for, not what it is about."""
    button = window._machine_nav_button
    assert button.text() == "Setup"
    assert not button.icon().isNull()
    assert not window._log_toggle_btn.icon().isNull()


def test_the_cart_pill_is_the_process_destination(window):
    """One button: the process pill says Cart and sits apart from the others."""
    button = window._cart_button
    assert button is window._simple_nav_buttons["process"]
    assert button.text() == "Cart"
    assert not button.icon().isNull()
    window._set_simple_section("browse")
    button.click()
    assert window._current_section() == "process"
    assert button.isChecked()


def test_the_cart_badge_counts_the_queue(window):
    button = window._cart_button
    assert button._count == 0
    button.set_count(3)
    assert button.accessibleName() == "Cart: 3 queued"
    button.set_count(0)
    assert button.accessibleName() == "Cart: 0 queued"
