"""Recovery after tile fetches fail.

Scenario: the app is launched with no network. Every visible tile fails at once.
The user then reconnects.

Expected behaviour: failures are retried, with the wait doubling per consecutive
failure so a reconnect is picked up without a burst at the tile server. Before
this, a failed tile was blacklisted permanently and the map stayed blank until
the app was restarted.
"""

from __future__ import annotations

import pytest

from deepreefmap_gui.map import tile_cache as tile_cache_mod


@pytest.fixture
def clock(monkeypatch):
    """A monotonic clock the test moves by hand."""

    class Clock:
        now = 1000.0

        def advance(self, seconds):
            self.now += seconds

    c = Clock()
    monkeypatch.setattr(tile_cache_mod.time, "monotonic", lambda: c.now)
    return c


KEY = (5, 3, 7)


def test_a_fresh_tile_may_be_fetched(tile_cache, clock):
    assert tile_cache._may_retry(KEY)


def test_a_failed_tile_is_held_off_briefly(tile_cache, clock):
    tile_cache._note_failure(KEY)

    assert not tile_cache._may_retry(KEY)


def test_a_failed_tile_is_retried_once_the_wait_passes(tile_cache, clock):
    """The blacklist used to be permanent, so reconnecting never helped."""
    tile_cache._note_failure(KEY)

    clock.advance(tile_cache_mod._RETRY_BASE_S)

    assert tile_cache._may_retry(KEY)


def test_the_wait_grows_with_consecutive_failures(tile_cache, clock):
    tile_cache._note_failure(KEY)
    clock.advance(tile_cache_mod._RETRY_BASE_S)
    tile_cache._note_failure(KEY)

    clock.advance(tile_cache_mod._RETRY_BASE_S)
    assert not tile_cache._may_retry(KEY), "the second failure waits longer than the first"

    clock.advance(tile_cache_mod._RETRY_BASE_S)
    assert tile_cache._may_retry(KEY)


def test_the_wait_is_capped(tile_cache, clock):
    for _ in range(40):
        tile_cache._note_failure(KEY)

    clock.advance(tile_cache_mod._RETRY_MAX_S)

    assert tile_cache._may_retry(KEY), "backoff grew past its ceiling"


def test_a_success_clears_the_failure_record(tile_cache, clock):
    """Otherwise a tile that failed once carries its backoff for the session."""
    tile_cache._note_failure(KEY)
    tile_cache._failed.pop(KEY, None)

    assert tile_cache._may_retry(KEY)
    assert KEY not in tile_cache._failed


def test_a_held_off_tile_is_not_requested(tile_cache, clock, monkeypatch):
    requested = []
    monkeypatch.setattr(tile_cache, "_network", None)
    monkeypatch.setattr(
        type(tile_cache), "_may_retry", lambda self, key: requested.append(key) or False
    )

    tile_cache._fetch(KEY)

    assert requested == [KEY]
    assert KEY not in tile_cache._in_flight
