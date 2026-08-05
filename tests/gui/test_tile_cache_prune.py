"""Keeping the on-disk tile cache inside a budget.

Scenario: tiles reach disk by being displayed and were never removed, so the
cache grew for the life of the install.

Expected behaviour: once the cache exceeds its budget, the oldest tiles go until
it fits again. A dropped tile is re-fetched the next time it is displayed, so
the only cost is a refetch.
"""

from __future__ import annotations

from deepreefmap_gui.map import tile_cache as tile_cache_mod


def _write_tiles(tile_cache, tile_cache_root, count, size_bytes, start_mtime=1000.0):
    """Write `count` tiles, oldest first, one second apart."""
    import os

    layer_dir = tile_cache_root / tile_cache.layer.id
    paths = []
    for i in range(count):
        path = layer_dir / "5" / str(i) / "0.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size_bytes)
        stamp = start_mtime + i
        os.utime(path, (stamp, stamp))
        paths.append(path)
    return paths


def _total(tile_cache_root) -> int:
    return sum(p.stat().st_size for p in tile_cache_root.rglob("*.png"))


def test_a_cache_inside_its_budget_is_left_alone(tile_cache, tile_cache_root, monkeypatch):
    monkeypatch.setattr(tile_cache_mod, "_DISK_BUDGET_BYTES", 10_000)
    paths = _write_tiles(tile_cache, tile_cache_root, count=5, size_bytes=100)

    tile_cache._prune_disk_cache()

    assert all(p.exists() for p in paths)


def test_an_oversized_cache_is_brought_back_under_budget(tile_cache, tile_cache_root, monkeypatch):
    monkeypatch.setattr(tile_cache_mod, "_DISK_BUDGET_BYTES", 500)
    _write_tiles(tile_cache, tile_cache_root, count=20, size_bytes=100)
    assert _total(tile_cache_root) == 2000

    tile_cache._prune_disk_cache()

    assert _total(tile_cache_root) <= 500


def test_the_oldest_tiles_go_first(tile_cache, tile_cache_root, monkeypatch):
    monkeypatch.setattr(tile_cache_mod, "_DISK_BUDGET_BYTES", 300)
    paths = _write_tiles(tile_cache, tile_cache_root, count=10, size_bytes=100)

    tile_cache._prune_disk_cache()

    survivors = [p for p in paths if p.exists()]
    assert survivors == paths[-len(survivors):], "eviction did not follow write order"


def test_pruning_an_empty_cache_is_harmless(tile_cache, tile_cache_root):
    tile_cache._prune_disk_cache()


def test_a_missing_cache_directory_is_harmless(tile_cache, tile_cache_root):
    """The tree does not exist until the first tile is written."""
    assert not tile_cache_root.exists()

    tile_cache._prune_disk_cache()
