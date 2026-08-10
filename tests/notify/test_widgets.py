"""The bell and its popover. Standalone widgets, so a QApplication is enough."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QLabel, QToolButton

from deepreefmap_gui.core.icons import ICON_SM
from deepreefmap_gui.core.theme import BLOCK, PRIMARY, WARNING
from deepreefmap_gui.notify.widgets import (
    _SEVERITY_COLOUR,
    BellButton,
    NotificationPopover,
    NotificationRow,
    relative_age,
)
from deepreefmap_gui.survey.models.notification import (
    BLOCKER,
    CONDITION,
    EVENT,
    INFO,
    SURVEY,
    Notification,
)
from deepreefmap_gui.survey.models.notification import WARNING as SEVERITY_WARNING

pytestmark = pytest.mark.usefixtures("qapp")

CAPTIONS = {"BLOCKING", "NEEDS ATTENTION", "RECENT"}


def note(severity: str = SEVERITY_WARNING, **overrides) -> Notification:
    kwargs = {
        "fingerprint": "videos.missing_clips",
        "kind": CONDITION,
        "severity": severity,
        "scope": SURVEY,
        "title": "10 clips cannot be found",
        "body": "10 clips cannot be found. Plug the drive back in.",
        "section": "videos",
    }
    kwargs.update(overrides)
    return Notification(**kwargs)


def click(widget) -> None:
    widget.mousePressEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(1, 1),
            QPointF(1, 1),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )


def captions(popover: NotificationPopover) -> list[str]:
    return [w.text() for w in popover._list.findChildren(QLabel) if w.text() in CAPTIONS]


def test_a_quiet_bell_carries_no_badge():
    bell = BellButton()
    bell.set_state(0, "")

    assert "0 unread" in bell.accessibleName()
    assert "Nothing needs your attention" in bell.toolTip()


def test_the_badge_counts_unread_and_takes_the_top_severity_colour():
    bell = BellButton()
    bell.set_state(3, BLOCKER, active=5)

    assert bell._badge_text() == "3"
    assert bell._severity == BLOCKER
    assert "5 active" in bell.accessibleName()


def test_a_hundred_messages_stop_counting():
    bell = BellButton()
    bell.set_state(140, SEVERITY_WARNING)

    assert bell._badge_text() == "99+"


def test_a_read_blocker_keeps_its_colour_without_a_number():
    """Expected behaviour: one glance at the list must not silence a blocker."""
    bell = BellButton()
    bell.set_state(0, BLOCKER, active=1)

    assert bell._unread == 0
    assert bell._severity == BLOCKER
    assert "all seen" in bell.toolTip()


@pytest.mark.parametrize(
    ("severity", "colour"), [(BLOCKER, BLOCK), (SEVERITY_WARNING, WARNING), (INFO, PRIMARY)]
)
def test_every_severity_takes_its_colour_from_the_theme(severity, colour):
    assert _SEVERITY_COLOUR[severity] == colour


def test_a_warning_can_be_put_away_or_silenced():
    """Two glyphs, so the tooltip is what says which is which."""
    row = NotificationRow(note(), "4 min")

    tips = [b.toolTip() for b in row.findChildren(QToolButton)]
    assert any(t.startswith("Clear this") for t in tips)
    assert any(t.startswith("Never show") for t in tips)


def test_a_blocker_offers_neither():
    """Start processing is already disabled; hiding why leaves a dead button."""
    row = NotificationRow(note(BLOCKER), "just now")

    assert row.findChildren(QToolButton) == []


def test_the_popover_groups_by_how_loudly_a_message_asks():
    popover = NotificationPopover()
    popover.set_notifications([note(BLOCKER), note(), note(INFO)], lambda n: "just now")

    assert captions(popover) == ["BLOCKING", "NEEDS ATTENTION", "RECENT"]


def test_an_empty_band_shows_no_caption():
    popover = NotificationPopover()
    popover.set_notifications([note()], lambda n: "just now")

    assert captions(popover) == ["NEEDS ATTENTION"]


def test_an_empty_popover_says_so():
    popover = NotificationPopover()
    popover.set_notifications([], lambda n: "")

    said = [w.text() for w in popover._list.findChildren(QLabel)]
    assert any("Nothing needs your attention" in text for text in said)


def test_the_panel_shrinks_when_a_message_is_cleared():
    """Scenario: two messages, one cleared.

    Expected behaviour: the panel is as tall as one message. Held at the height
    it opened at, the emptied band leaves a hole and the remaining row stretches
    into it.
    """
    popover = NotificationPopover()
    popover.set_notifications([note(), note(INFO, fingerprint="other")], lambda n: "just now")
    tall = popover.sizeHint().height()

    popover.set_notifications([note()], lambda n: "just now")

    assert popover.sizeHint().height() < tall


def test_a_wrapped_message_is_measured_at_the_panel_width():
    """A wrapped label reports a few pixels until it is told how wide it may be,
    which is what collapsed the panel to its header."""
    popover = NotificationPopover()
    long_note = note(body="A sentence of advice long enough that it has to wrap onto a second line.")
    popover.set_notifications([long_note], lambda n: "just now")

    assert popover._scroll.height() > ICON_SM


def test_repainting_does_not_stack_rows():
    popover = NotificationPopover()
    for _ in range(3):
        popover.set_notifications([note()], lambda n: "just now")

    assert len(popover._list.findChildren(NotificationRow)) == 1


def test_clicking_a_row_names_where_to_go():
    row = NotificationRow(note(), "4 min")
    seen = []
    row.activated.connect(seen.append)

    click(row)

    assert seen == ["videos"]


def test_an_event_with_nowhere_to_go_is_not_a_link():
    row = NotificationRow(note(INFO, kind=EVENT, section=""), "1 h")
    seen = []
    row.activated.connect(seen.append)

    click(row)

    assert seen == []


def test_the_row_buttons_report_what_they_act_on():
    row = NotificationRow(note(), "4 min")
    dismissed, muted = [], []
    row.dismissed.connect(dismissed.append)
    row.muted.connect(muted.append)

    clear, never = row.findChildren(QToolButton)
    clear.click()
    never.click()

    assert dismissed == [row._note.id]
    assert muted == ["videos.missing_clips"]


@pytest.mark.parametrize(
    ("then", "expected"),
    [
        ("2026-08-10T12:00:00+00:00", "just now"),
        ("2026-08-10T11:56:00+00:00", "4 min"),
        ("2026-08-10T09:00:00+00:00", "3 h"),
        ("2026-08-08T12:00:00+00:00", "2 d"),
    ],
)
def test_age_is_coarse_enough_to_read_at_a_glance(then, expected):
    assert relative_age(then, "2026-08-10T12:00:00+00:00") == expected


def test_an_unreadable_timestamp_is_left_blank():
    assert relative_age("", "2026-08-10T12:00:00+00:00") == ""
