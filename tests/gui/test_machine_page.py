"""Setup: its header button, its four views, and the panels it borrows.

The header button and the Run step read one verdict module, so most of what is
checked here is the two of them agreeing. A header reporting a ready machine
beside a Process button that refuses to start is the contradiction that shared
module exists to prevent.
"""

from __future__ import annotations

import pytest
from _factories import clip_pass, make_profile, make_transect

from deepreefmap_gui.simple import setup as setup_mod
from deepreefmap_gui.simple.machine import MACHINE_VIEWS
from deepreefmap_gui.simple.section_state import ATTENTION, BLOCKED, FIX_MACHINE, OK

_GB = 1024**3


_profile = make_profile


@pytest.fixture
def ready_machine(window, monkeypatch):
    """A window whose machine passes every readiness check.

    Every input to the verdict is faked so it belongs to the test rather than to
    the runner: a full disk, a missing card, an uncached model or a modest amount
    of RAM would each otherwise decide it. Grading no pass is what leaves the
    memory advisory off, since the grade probes the real machine's RAM.
    """
    monkeypatch.setattr(setup_mod, "probe_system", lambda *_a, **_k: _profile())
    monkeypatch.setattr(window, "_survey_missing_models", list)
    monkeypatch.setattr(window, "_current_fit", lambda: None)
    window._update_memory_profile_warning()
    window._refresh_readiness_view()
    return window


@pytest.fixture
def queued_machine(ready_machine, tmp_path, monkeypatch):
    """The ready machine with one transect and one queued pass, so the gate runs.

    Without a queued pass the Run step is on "no videos yet", which is neither
    ready nor blocked and so says nothing about the machine.
    """
    window = ready_machine
    window._survey_store().add_transect(make_transect())
    window._refresh_survey_batch_tab()

    video = tmp_path / "GX010001.MP4"
    video.write_bytes(b"x" * 4096)
    window._add_pass_to_cart(clip_pass(window._survey_store(), video).id)
    assert len(window._survey_rows) == 1
    return window


# --- the header cannot disagree with the Process button ----------------------


def test_the_header_and_the_run_gate_agree_about_a_missing_model(queued_machine, monkeypatch):
    """Scenario: a model these settings need never made it onto the disk.

    Expected behaviour: the Run step blocks and names Setup as the place
    to fix it, and the header button that opens Setup reports a blocked
    machine. Both verdicts come from one module for exactly this reason.
    """
    window = queued_machine
    assert window._machine_verdict().state == OK

    monkeypatch.setattr(window, "_survey_missing_models", lambda: ["coralscapes-vit-b-dpt"])
    window._recompute_survey_start()

    assert window._survey_gate.state == BLOCKED
    assert window._survey_gate.fix == FIX_MACHINE
    assert window._machine_verdict().state == BLOCKED


def test_the_header_and_the_run_gate_agree_about_a_missing_graphics_card(
    queued_machine, monkeypatch
):
    """The same agreement for the other blocker that is fixed on this machine."""
    window = queued_machine
    monkeypatch.setattr(setup_mod, "probe_system", lambda *_a, **_k: _profile(gpu_name=None))
    monkeypatch.setattr(window, "_gpu_available", lambda: False)
    window._recompute_survey_start()

    assert window._survey_gate.state == BLOCKED
    assert window._survey_gate.fix == FIX_MACHINE
    assert window._machine_verdict().state == BLOCKED


def test_clearing_the_blocker_returns_the_header_to_ready(queued_machine, monkeypatch):
    window = queued_machine
    monkeypatch.setattr(window, "_survey_missing_models", lambda: ["scsfmlearner"])
    assert window._machine_verdict().state == BLOCKED

    monkeypatch.setattr(window, "_survey_missing_models", list)
    assert window._machine_verdict().state == OK


# --- the button says in words what it says in glyphs -------------------------


def test_the_header_button_labels_a_ready_machine_for_a_reader(ready_machine):
    window = ready_machine
    window._update_available = ""
    window._refresh_machine_button()
    button = window._machine_nav_button

    assert button.accessibleName().strip()
    assert "Ready" in button.accessibleName()
    assert button.accessibleDescription() == window._machine_verdict().reason
    assert button.toolTip() == window._machine_verdict().reason


def test_the_header_button_says_a_blocker_rather_than_only_painting_it(
    ready_machine, monkeypatch
):
    """An unlabelled red dot is nothing at all to a screen reader."""
    window = ready_machine
    monkeypatch.setattr(window, "_survey_missing_models", lambda: ["scsfmlearner"])
    window._refresh_machine_button()
    button = window._machine_nav_button

    assert window._machine_verdict().state == BLOCKED
    assert "1 requirement not met" in button.accessibleName()
    assert "not met" in button.accessibleDescription()


def test_the_header_button_names_an_available_update_in_words(ready_machine):
    window = ready_machine
    window._update_available = "2.1.0"
    window._refresh_machine_button()
    button = window._machine_nav_button

    assert window._machine_verdict().state == OK
    assert "2.1.0" in button.accessibleName()
    assert "2.1.0" in button.accessibleDescription()


def test_the_header_button_paints_one_slot_per_thing_it_has_to_report(
    ready_machine, monkeypatch
):
    """At most two glyphs: what is stopping work, and whether an update waits.

    A badge that is always lit is a badge nobody reads, so a ready machine with
    nothing waiting paints none at all.
    """
    window = ready_machine
    button = window._machine_nav_button

    window._update_available = ""
    window._refresh_machine_button()
    assert button._badges == []

    window._update_available = "2.1.0"
    window._refresh_machine_button()
    assert len(button._badges) == 1

    monkeypatch.setattr(window, "_survey_missing_models", lambda: ["scsfmlearner"])
    window._refresh_machine_button()
    assert len(button._badges) == 2


def test_a_memory_advisory_reaches_the_header_without_blocking(ready_machine):
    window = ready_machine
    window._memory_advisory = "This session may exhaust memory on this machine."
    window._refresh_machine_button()

    assert window._machine_verdict().state == ATTENTION
    assert "exhaust memory" in window._machine_nav_button.accessibleDescription()


def test_the_header_button_opens_this_machine(window):
    window._set_simple_section("transects")
    window._machine_nav_button.click()
    assert window._current_section() == "machine"


# --- the views of one computer ---------------------------------------------


@pytest.mark.parametrize("view", MACHINE_VIEWS)
def test_every_view_is_reachable_and_checks_only_its_own_button(window, view):
    window._set_simple_section("machine")
    window._set_machine_view(view)

    assert window._machine_view == view
    assert window._machine_stack.currentIndex() == MACHINE_VIEWS.index(view)
    checked = [name for name, button in window._machine_view_buttons.items() if button.isChecked()]
    assert checked == [view]


def test_the_segmented_control_switches_the_view(window):
    window._set_simple_section("machine")
    window._machine_view_buttons["models"].click()

    assert window._machine_view == "models"


def test_an_unknown_view_is_rejected(window):
    with pytest.raises(ValueError):
        window._set_machine_view("gauges")


# --- the panels are lent, not rebuilt ----------------------------------------


@pytest.mark.parametrize(
    "widget_attr, host_attr",
    [
        ("_models_page", "_machine_models_host"),
        ("_system_page", "_machine_system_host"),
        ("_updates_page", "_machine_updates_host"),
        ("_out_root_widget", "_machine_out_root_host"),
    ],
)
def test_each_panel_is_lent_to_the_machine_page_rather_than_rebuilt(
    window, widget_attr, host_attr
):
    """One widget per panel, so a download cannot finish against only one copy.

    Each is built in a home of its own and moved here once the shell is up, so
    the check is that the move happened and that repeating it neither rebuilds
    nor re-parents anything.
    """
    widget = getattr(window, widget_attr)
    host = getattr(window, host_attr)

    assert widget.parentWidget() is host

    window._host_machine_panels()

    assert getattr(window, widget_attr) is widget
    assert widget.parentWidget() is host


def _on_view(window, widget, view):
    """Whether a widget is somewhere inside the given view of the machine page."""
    page = window._machine_stack.widget(MACHINE_VIEWS.index(view))
    while widget is not None:
        if widget is page:
            return True
        widget = widget.parentWidget()
    return False


@pytest.mark.parametrize(
    "widget_attr, view",
    [("_out_root_widget", "readiness"), ("_updates_page", "updates")],
)
def test_a_lent_panel_lands_on_the_view_that_claims_it(window, widget_attr, view):
    """The output root is set where the disk-space row that measures it is read,
    and the updater is the one view that changes the software rather than
    describing the computer."""
    assert _on_view(window, getattr(window, widget_attr), view)


# --- the gauge poll follows the gauges ---------------------------------------


def test_the_gauges_poll_only_while_the_performance_view_is_on_screen(window):
    """A 1 Hz tick against widgets nobody is looking at is a battery cost."""
    window._set_simple_section("machine")

    window._set_machine_view("performance")
    assert window._sys_timer.isActive()

    window._set_machine_view("readiness")
    assert not window._sys_timer.isActive()


def test_leaving_this_machine_stops_the_gauge_poll(window):
    """Scenario: the gauges are on screen, then the diver goes back to the work.

    Expected behaviour: the poll stops and resumes on return. The selected view
    does not change on the way out, so leaving the destination is the only thing
    that can notice the gauges have gone; when nothing asked, the poll leaked.
    """
    window._set_simple_section("machine")
    window._set_machine_view("performance")
    assert window._sys_timer.isActive()

    window._set_simple_section("transects")

    assert window._machine_view == "performance"
    assert not window._sys_timer.isActive()

    window._set_simple_section("machine")
    assert window._sys_timer.isActive()
