from pathlib import Path

from deepreefmap_gui.runs.run_cards import related_run_counts


def _entry(run_dir: str, hashes: list[str | None] | None) -> tuple[Path, dict]:
    manifest: dict = {} if hashes is None else {"video_hashes": hashes}
    return (Path(run_dir), manifest)


def test_runs_sharing_a_hash_count_each_other() -> None:
    counts = related_run_counts([
        _entry("run_a", ["aaaa"]),
        _entry("run_b", ["aaaa"]),
        _entry("run_c", ["cccc"]),
    ])
    assert counts[Path("run_a")] == 1
    assert counts[Path("run_b")] == 1
    assert counts[Path("run_c")] == 0


def test_multi_clip_runs_relate_through_any_shared_hash() -> None:
    counts = related_run_counts([
        _entry("run_a", ["aaaa", "bbbb"]),
        _entry("run_b", ["bbbb"]),
        _entry("run_c", ["aaaa"]),
    ])
    assert counts[Path("run_a")] == 2
    assert counts[Path("run_b")] == 1
    assert counts[Path("run_c")] == 1


def test_old_manifests_and_failed_hashes_never_relate() -> None:
    counts = related_run_counts([
        _entry("run_a", None),
        _entry("run_b", [None]),
        _entry("run_c", [None]),
    ])
    assert counts == {Path("run_a"): 0, Path("run_b"): 0, Path("run_c"): 0}
