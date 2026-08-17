import re
from pathlib import Path

from deepreefmap_gui.core.theme import WARNING
from deepreefmap_gui.runs.run_cards import related_run_counts, run_facts_line


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


def _manifest(**overrides) -> dict:
    manifest = {
        "frames_processed": 1240,
        "fps": 4,
        "semantic_reference_points": 8_200_000,
        "geometry_source": "world_points",
        "camera_profile": "gopro11_wide",
        "mode": "semantic",
        "mapping_backend": "loger_star",
        "run_timestamp": "2026-07-01T10:00:00+00:00",
    }
    manifest.update(overrides)
    return manifest


def _plain(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


def test_the_facts_line_is_the_size_of_the_run_and_nothing_else() -> None:
    """Everything else in the manifest is a row in the pane beside the cloud."""
    assert _plain(run_facts_line(_manifest())) == "1,240 @ 4 fps · 8.2M points"


def test_a_depth_fallback_names_itself_in_the_facts_line() -> None:
    line = run_facts_line(_manifest(geometry_source="depth_unprojection"))
    assert _plain(line).endswith(" · ⚠ depth-unprojection")
    assert WARNING in line


def test_a_solved_run_says_nothing_about_its_geometry() -> None:
    line = run_facts_line(_manifest())
    assert "world points" not in line
    assert "⚠" not in line


def test_the_facts_line_drops_what_a_manifest_never_recorded() -> None:
    assert run_facts_line({}) == ""
    assert _plain(run_facts_line({"frames_processed": 12})) == "12"
    assert _plain(run_facts_line({"metric_points": 988_000})) == "988k points"
