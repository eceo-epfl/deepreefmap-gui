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
from deepreefmap_gui.map.tile_cache import TileCache


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


@pytest.fixture
def cache(qapp):
    from deepreefmap_gui.map.layers import OSM_LAYER

    return TileCache(OSM_LAYER)


KEY = (5, 3, 7)


def test_a_fresh_tile_may_be_fetched(cache, clock):
    assert cache._may_retry(KEY)


def test_a_failed_tile_is_held_off_briefly(cache, clock):
    cache._note_failure(KEY)

    assert not cache._may_retry(KEY)


def test_a_failed_tile_is_retried_once_the_wait_passes(cache, clock):
    """The blacklist used to be permanent, so reconnecting never helped."""
    cache._note_failure(KEY)

    clock.advance(tile_cache_mod._RETRY_BASE_S)

    assert cache._may_retry(KEY)


def test_the_wait_grows_with_consecutive_failures(cache, clock):
    cache._note_failure(KEY)
    clock.advance(tile_cache_mod._RETRY_BASE_S)
    cache._note_failure(KEY)

    clock.advance(tile_cache_mod._RETRY_BASE_S)
    assert not cache._may_retry(KEY), "the second failure waits longer than the first"

    clock.advance(tile_cache_mod._RETRY_BASE_S)
    assert cache._may_retry(KEY)


def test_the_wait_is_capped(cache, clock):
    for _ in range(40):
        cache._note_failure(KEY)

    clock.advance(tile_cache_mod._RETRY_MAX_S)

    assert cache._may_retry(KEY), "backoff grew past its ceiling"


def test_a_success_clears_the_failure_record(cache, clock):
    """Otherwise a tile that failed once carries its backoff for the session."""
    cache._note_failure(KEY)
    cache._failed.pop(KEY, None)

    assert cache._may_retry(KEY)
    assert KEY not in cache._failed


def test_a_held_off_tile_is_not_requested(cache, clock, monkeypatch):
    requested = []
    monkeypatch.setattr(cache, "_network", None)
    monkeypatch.setattr(
        type(cache), "_may_retry", lambda self, key: requested.append(key) or False
    )

    cache._fetch(KEY)

    assert requested == [KEY]
    assert KEY not in cache._in_flight
