"""Which part of a run directory each artefact belongs to, and what it weighs.

Qt-free: these describe a directory tree in a tmp_path and nothing else.
"""

from __future__ import annotations

import pytest
from _factories import KB, write_run_tree

from deepreefmap_gui.storage import tiers as tiers_mod
from deepreefmap_gui.storage.tiers import (
    ALL_TIERS,
    DELETABLE_TIERS,
    TIER_CACHE,
    TIER_KEEP,
    TIER_RESULTS,
    TIER_UNKNOWN,
    TIER_WORKING,
    measure_run,
    tier_for,
)


@pytest.mark.parametrize(
    ("name", "is_dir", "expected"),
    [
        ("frames", True, TIER_WORKING),
        ("labels", True, TIER_WORKING),
        ("masks", True, TIER_WORKING),
        (".cache", True, TIER_WORKING),
        ("mapping_outputs.npz", False, TIER_WORKING),
        # Nothing rebuilds this one, so it is not a result somebody can shed.
        ("geometry_cloud.ply", False, TIER_WORKING),
        ("run_manifest.json", False, TIER_KEEP),
        ("run.log", False, TIER_KEEP),
        ("dive.scene.zarr.zip", False, TIER_CACHE),
        ("dive.scene.zarr.zip.tmp", False, TIER_CACHE),
        ("dive.drm.zarr.zip", False, TIER_CACHE),
        ("semantic_reference_cloud.ply", False, TIER_RESULTS),
        ("ortho.npz", False, TIER_RESULTS),
        ("videos", True, TIER_RESULTS),
        ("ortho2.npz", False, TIER_UNKNOWN),
        ("frame_00299.png", False, TIER_UNKNOWN),
        ("notes", True, TIER_UNKNOWN),
    ],
)
def test_every_artefact_lands_where_its_loss_says_it_should(name, is_dir, expected) -> None:
    assert tier_for(name, is_dir=is_dir) == expected


def test_legacy_labels_are_working_data_by_their_folder(tmp_path) -> None:
    """Pre-PNG runs hold labels/*.npy at 8 MB a frame. No rule of their own."""
    run_dir = write_run_tree(tmp_path)
    for png in (run_dir / "labels").glob("*.png"):
        png.rename(png.with_suffix(".npy"))

    breakdown = measure_run(run_dir)
    assert "labels" in breakdown.tier_entries(TIER_WORKING)


def test_the_tiers_account_for_the_whole_directory(tmp_path) -> None:
    breakdown = measure_run(write_run_tree(tmp_path))
    assert sum(breakdown.tier_bytes(t) for t in ALL_TIERS) == breakdown.total_bytes
    assert breakdown.total_bytes > 0


def test_the_run_reads_as_openable_and_resumable(tmp_path) -> None:
    breakdown = measure_run(write_run_tree(tmp_path))
    assert breakdown.openable and breakdown.resumable


def test_losing_the_frames_ends_both(tmp_path) -> None:
    import shutil

    run_dir = write_run_tree(tmp_path)
    shutil.rmtree(run_dir / "frames")
    breakdown = measure_run(run_dir)
    assert not breakdown.openable
    assert not breakdown.resumable


def test_the_manifest_and_the_log_can_never_be_offered(tmp_path) -> None:
    breakdown = measure_run(write_run_tree(tmp_path))
    kept = breakdown.tier_entries(TIER_KEEP)
    assert set(kept) == {"run_manifest.json", "run.log"}
    assert TIER_KEEP not in DELETABLE_TIERS
    assert TIER_UNKNOWN not in DELETABLE_TIERS


def test_a_symlinked_entry_is_unknown_whatever_it_is_named(tmp_path) -> None:
    """No tier may ever delete through a link into somewhere else."""
    run_dir = write_run_tree(tmp_path, "linked")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "big.bin").write_bytes(b"x" * 64 * KB)
    (run_dir / "frames").rename(run_dir / "frames-real")
    (run_dir / "frames").symlink_to(elsewhere, target_is_directory=True)

    breakdown = measure_run(run_dir)
    assert "frames" in breakdown.tier_entries(TIER_UNKNOWN)
    # Measured as the link, not as the 64 KB it points at.
    assert breakdown.tier_bytes(TIER_UNKNOWN) < 64 * KB


def test_a_directory_that_cannot_be_read_measures_as_empty(tmp_path) -> None:
    assert measure_run(tmp_path / "not-here").total_bytes == 0


def test_an_unreadable_file_is_reported_rather_than_counted_as_zero(tmp_path, monkeypatch) -> None:
    """A partial total has to say it is partial, or it reads as an empty folder."""
    run_dir = write_run_tree(tmp_path)
    real_lstat = tiers_mod.os.lstat

    def refuse(path, *args, **kwargs):
        if str(path).endswith("00000000.png"):
            raise OSError("no")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(tiers_mod.os, "lstat", refuse)
    breakdown = measure_run(run_dir)
    assert breakdown.unmeasured_items == 3
