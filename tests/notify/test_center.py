"""The reconcile engine. Pure and Qt-free, so these need no window."""

from __future__ import annotations

import itertools

import pytest

from deepreefmap_gui.notify.center import NotificationCenter, SetMuteBook
from deepreefmap_gui.notify.log import MemoryLog
from deepreefmap_gui.notify.model import Condition
from deepreefmap_gui.survey.models.notification import (
    BLOCKER,
    EVENT,
    INFO,
    SURVEY,
    WARNING,
)


class CountingLog(MemoryLog):
    """A log that says how often it was written to, for the hot-path guard."""

    def __init__(self) -> None:
        super().__init__()
        self.writes = 0

    def insert(self, note) -> None:
        self.writes += 1
        super().insert(note)

    def update(self, note) -> None:
        self.writes += 1
        super().update(note)

    def resolve(self, note_id, at) -> None:
        self.writes += 1
        super().resolve(note_id, at)


def clips(missing: int = 10, severity: str = WARNING) -> Condition:
    return Condition(
        fingerprint="videos.missing_clips",
        severity=severity,
        scope=SURVEY,
        title=f"{missing} clips cannot be found",
        body=f"{missing} clips cannot be found. Plug the drive back in.",
        section="videos",
        subject_count=missing,
    )


def gpu() -> Condition:
    return Condition(
        fingerprint="process.no_gpu",
        severity=BLOCKER,
        scope=SURVEY,
        title="No graphics card was detected",
        section="process",
    )


@pytest.fixture
def clock():
    ticks = itertools.count(1)
    return lambda: f"2026-08-10T12:00:{next(ticks):02d}+00:00"


@pytest.fixture
def centre(clock):
    return NotificationCenter(CountingLog(), SetMuteBook(), now=clock)


def test_a_new_fault_becomes_one_row(centre):
    assert centre.reconcile([clips()]) is True

    assert [n.title for n in centre.active()] == ["10 clips cannot be found"]
    assert len(centre.history()) == 1


def test_the_same_fault_said_again_writes_nothing(centre):
    centre.reconcile([clips()])
    writes = centre._log.writes

    assert centre.reconcile([clips()]) is False
    assert centre._log.writes == writes


def test_an_unchanged_set_never_reaches_the_log(centre):
    """The header recomputes its verdicts on every keystroke, so an unchanged
    set has to cost a tuple comparison and nothing else."""
    centre.reconcile([clips(), gpu()])
    writes = centre._log.writes

    for _ in range(100):
        centre.reconcile([clips(), gpu()])

    assert centre._log.writes == writes


def test_a_falling_count_updates_the_episode_and_does_not_interrupt_again(centre):
    """Expected behaviour: relocating ten clips one at a time is one problem
    getting smaller, not ten new ones."""
    centre.reconcile([clips(10)])
    centre.mark_all_read()

    for missing in (9, 8, 7):
        centre.reconcile([clips(missing)])

    assert len(centre.history()) == 1
    assert centre.active()[0].subject_count == 7
    assert centre.unread_count() == 0


def test_a_fault_that_worsens_asks_again(centre):
    centre.reconcile([clips()])
    centre.mark_all_read()
    centre.dismiss(centre.active()[0].id)
    assert centre.active() == []

    centre.reconcile([clips(severity=BLOCKER)])

    assert centre.unread_count() == 1
    assert len(centre.history()) == 1


def test_a_fault_that_goes_away_is_stamped_cleared(centre):
    centre.reconcile([clips()])
    centre.reconcile([])

    assert centre.active() == []
    assert centre.history()[0].resolved_at is not None


def test_a_fault_that_recurs_is_a_second_episode(centre):
    centre.reconcile([clips()])
    centre.reconcile([])
    centre.reconcile([clips()])

    assert len(centre.history()) == 2
    assert len(centre.active()) == 1


def test_a_first_paint_cannot_clear_what_it_has_not_read_yet(centre):
    """The shell paints before the video library is scanned, so an empty set at
    that moment says nothing about whether the clips are still missing."""
    centre.reconcile([clips()])
    centre.reconcile([], authoritative=False)

    assert len(centre.active()) == 1
    assert centre.history()[0].resolved_at is None


def test_dismissing_hides_the_occurrence_but_keeps_the_episode(centre):
    centre.reconcile([clips()])
    centre.dismiss(centre.active()[0].id)

    assert centre.active() == []
    assert centre.history()[0].resolved_at is None


def test_a_dismissed_fault_returns_when_it_recurs(centre):
    centre.reconcile([clips()])
    centre.dismiss(centre.active()[0].id)
    centre.reconcile([])
    centre.reconcile([clips()])

    assert len(centre.active()) == 1


def test_muting_hides_the_message_and_still_logs_it(centre):
    centre.mute("videos.missing_clips")
    centre.reconcile([clips()])

    assert centre.active() == []
    assert len(centre.history()) == 1

    centre.unmute("videos.missing_clips")
    assert len(centre.active()) == 1


def test_a_blocker_cannot_be_silenced(centre):
    """Start processing is already disabled; hiding the reason leaves a dead
    button and nowhere to find out why."""
    centre.mute("process.no_gpu")
    centre.reconcile([gpu()])

    assert [n.fingerprint for n in centre.active()] == ["process.no_gpu"]


def test_the_loudest_message_comes_first(centre):
    centre.reconcile([clips(), gpu()])

    assert [n.severity for n in centre.active()] == [BLOCKER, WARNING]
    assert centre.top_severity() == BLOCKER


def test_the_same_event_twice_is_two_things_that_happened(centre):
    for _ in range(2):
        centre.post(fingerprint="batch.finished", title="Session complete")

    assert len(centre.active()) == 2
    assert all(n.kind == EVENT for n in centre.active())


def test_an_event_is_muted_like_a_condition(centre):
    centre.mute("batch.finished")
    centre.post(fingerprint="batch.finished", title="Session complete")

    assert centre.active() == []


def test_reading_stops_the_badge_without_clearing_the_list(centre):
    centre.reconcile([clips(), gpu()])
    assert centre.unread_count() == 2

    centre.mark_all_read()

    assert centre.unread_count() == 0
    assert len(centre.active()) == 2
    assert centre.top_severity() == BLOCKER


def test_muted_lists_what_each_silenced_message_last_said(centre):
    centre.reconcile([clips()])
    centre.mute("videos.missing_clips")

    assert centre.muted() == [("videos.missing_clips", "10 clips cannot be found")]


def test_reopening_a_survey_picks_its_episodes_back_up(clock):
    log = CountingLog()
    first = NotificationCenter(log, SetMuteBook(), now=clock)
    first.reconcile([clips()])

    second = NotificationCenter(log, SetMuteBook(), now=clock)

    assert [n.title for n in second.active()] == ["10 clips cannot be found"]
    assert second.reconcile([clips()]) is False


def test_an_event_already_read_does_not_survive_a_restart(clock):
    """Nothing will ever come along to close an event, so reading is what ends
    it. Last week's finished session is not news."""
    log = CountingLog()
    first = NotificationCenter(log, SetMuteBook(), now=clock)
    first.post(fingerprint="batch.finished", title="Session complete")
    first.mark_all_read()

    second = NotificationCenter(log, SetMuteBook(), now=clock)

    assert second.active() == []


def test_an_event_nobody_saw_survives_a_restart(clock):
    """A run that failed while the app was closed is still worth hearing."""
    log = CountingLog()
    first = NotificationCenter(log, SetMuteBook(), now=clock)
    first.post(fingerprint="batch.failed", title="A pass failed", severity=WARNING)

    second = NotificationCenter(log, SetMuteBook(), now=clock)

    assert [n.title for n in second.active()] == ["A pass failed"]


def test_changing_survey_leaves_the_old_log_as_it_stands(centre, clock):
    centre.reconcile([clips()])
    old = centre._log

    centre.rebind(CountingLog())

    assert centre.active() == []
    assert old.history()[0].resolved_at is None


def test_an_info_message_ranks_below_a_warning(centre):
    unassigned = Condition(
        fingerprint="process.unassigned_passes",
        severity=INFO,
        scope=SURVEY,
        title="3 passes will run without a transect",
        section="process",
    )
    centre.reconcile([unassigned, clips()])

    assert [n.severity for n in centre.active()] == [WARNING, INFO]
