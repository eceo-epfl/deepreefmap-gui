"""Keeping the on-disk tile cache inside a budget.

Scenario: tiles reach disk by being displayed and were never removed, so the
cache grew for the life of the install.

Expected behaviour: once the cache exceeds its budget, the oldest tiles go until
it fits again. A dropped tile is re-fetched the next time it is displayed, so
the only cost is a refetch.
"""

from __future__ import annotations

import pytest

from deepreefmap_gui.map import tile_cache as tile_cache_mod
from deepreefmap_gui.map.tile_cache import TileCache


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    root = tmp_path / "tiles"
    monkeypatch.setattr(tile_cache_mod, "tile_cache_dir", lambda: root)
    return root


@pytest.fixture
def cache(qapp, cache_root):
    from deepreefmap_gui.map.layers import OSM_LAYER

    return TileCache(OSM_LAYER)


def _write_tiles(cache, cache_root, count, size_bytes, start_mtime=1000.0):
    """Write `count` tiles, oldest first, one second apart."""
    import os

    layer_dir = cache_root / cache.layer.id
    paths = []
    for i in range(count):
        path = layer_dir / "5" / str(i) / "0.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size_bytes)
        stamp = start_mtime + i
        os.utime(path, (stamp, stamp))
        paths.append(path)
    return paths


def _total(cache_root) -> int:
    return sum(p.stat().st_size for p in cache_root.rglob("*.png"))


def test_a_cache_inside_its_budget_is_left_alone(cache, cache_root, monkeypatch):
    monkeypatch.setattr(tile_cache_mod, "_DISK_BUDGET_BYTES", 10_000)
    paths = _write_tiles(cache, cache_root, count=5, size_bytes=100)

    cache._prune_disk_cache()

    assert all(p.exists() for p in paths)


def test_an_oversized_cache_is_brought_back_under_budget(cache, cache_root, monkeypatch):
    monkeypatch.setattr(tile_cache_mod, "_DISK_BUDGET_BYTES", 500)
    _write_tiles(cache, cache_root, count=20, size_bytes=100)
    assert _total(cache_root) == 2000

    cache._prune_disk_cache()

    assert _total(cache_root) <= 500


def test_the_oldest_tiles_go_first(cache, cache_root, monkeypatch):
    monkeypatch.setattr(tile_cache_mod, "_DISK_BUDGET_BYTES", 300)
    paths = _write_tiles(cache, cache_root, count=10, size_bytes=100)

    cache._prune_disk_cache()

    survivors = [p for p in paths if p.exists()]
    assert survivors == paths[-len(survivors):], "eviction did not follow write order"


def test_pruning_an_empty_cache_is_harmless(cache, cache_root):
    cache._prune_disk_cache()


def test_a_missing_cache_directory_is_harmless(cache, cache_root):
    """The tree does not exist until the first tile is written."""
    assert not cache_root.exists()

    cache._prune_disk_cache()
