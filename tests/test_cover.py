import csv
from pathlib import Path

import pytest
from deepreefmap.config.classes import ClassConfig, SemanticClass

from deepreefmap_gui.cover import (
    aggregate_cover,
    group_color_for_name,
    group_name_for_id,
    save_cover_csv,
    save_cover_csv_levels,
    taxonomy_hash,
    taxonomy_version,
)


def _classes() -> ClassConfig:
    # Real coralscapes ids so the bundled class_groups.yaml drives the roll-up:
    # 22 branching alive and 25 acropora alive both roll up to "coral alive".
    return ClassConfig(
        classes=(
            SemanticClass(22, "branching alive", (1, 1, 1), frozenset()),
            SemanticClass(25, "acropora alive", (2, 2, 2), frozenset()),
            SemanticClass(5, "sand", (3, 3, 3), frozenset()),
        ),
        path=Path("test"),
    )


def _cover() -> dict[str, object]:
    return {
        "classes": {
            "22": {"name": "branching alive", "count": 30.0, "fraction": 0.3},
            "25": {"name": "acropora alive", "count": 50.0, "fraction": 0.5},
            "5": {"name": "sand", "count": 20.0, "fraction": 0.2},
        },
        "denominator": 100.0,
    }


def test_aggregate_cover_rolls_classes_into_groups() -> None:
    classes_config = _classes()
    cover = _cover()

    coarse = aggregate_cover(cover, classes_config, "coarse")
    assert coarse["coral alive"]["count"] == 80.0
    assert coarse["coral alive"]["fraction"] == 0.8
    assert coarse["sand"]["fraction"] == 0.2

    intermediate = aggregate_cover(cover, classes_config, "intermediate")
    # Branching keeps its own bucket; acropora rolls up to "coral alive".
    assert intermediate["branching alive"]["fraction"] == 0.3
    assert intermediate["coral alive"]["fraction"] == 0.5


def test_group_name_for_id_levels() -> None:
    classes_config = _classes()
    assert group_name_for_id(classes_config, 22, "fine") == "branching alive"
    assert group_name_for_id(classes_config, 22, "intermediate") == "branching alive"
    assert group_name_for_id(classes_config, 22, "coarse") == "coral alive"
    # An id absent from the taxonomy falls back to its own class name.
    assert group_name_for_id(classes_config, 999, "coarse") == "class_999"


def test_group_color_for_name_uses_first_matching_class() -> None:
    classes_config = _classes()
    # First class whose coarse group is "coral alive" is id 22, color (1, 1, 1).
    assert group_color_for_name(classes_config, "coral alive", "coarse") == (1, 1, 1)
    assert group_color_for_name(classes_config, "sand", "coarse") == (3, 3, 3)
    assert group_color_for_name(classes_config, "nonexistent", "coarse") == (128, 128, 128)


@pytest.mark.parametrize("level", ["", "fine ", "medium", None])
def test_an_unknown_level_is_rejected_rather_than_guessed(level) -> None:
    """Silently falling back would mislabel an export's aggregation level."""
    classes_config = _classes()
    with pytest.raises(ValueError):
        group_name_for_id(classes_config, 22, level)
    with pytest.raises(ValueError):
        aggregate_cover(_cover(), classes_config, level)


@pytest.mark.parametrize("cover", [{}, {"denominator": 100.0}, None, [], "nope"])
def test_aggregate_of_a_coverless_payload_is_empty(cover) -> None:
    assert aggregate_cover(cover, _classes(), "coarse") == {}


def test_aggregate_with_no_denominator_reports_counts_without_fractions() -> None:
    """A zero denominator means nothing was measured; 0/0 must not divide."""
    cover = {"classes": {"5": {"name": "sand", "count": 20.0, "fraction": 0.2}}, "denominator": 0.0}
    grouped = aggregate_cover(cover, _classes(), "coarse")
    assert grouped["sand"]["count"] == 20.0
    assert grouped["sand"]["fraction"] == 0.0


def test_aggregate_skips_a_non_integer_class_key() -> None:
    cover = {
        "classes": {
            "5": {"name": "sand", "count": 20.0, "fraction": 0.2},
            "total": {"name": "junk", "count": 99.0, "fraction": 0.99},
        },
        "denominator": 100.0,
    }
    assert set(aggregate_cover(cover, _classes(), "coarse")) == {"sand"}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_save_cover_csv_orders_by_fraction_and_pins_precision(tmp_path) -> None:
    """The CSV is a published artefact, so column order and precision are contract."""
    path = tmp_path / "cover.csv"
    save_cover_csv(path, _cover())

    rows = _read_csv(path)
    assert [r["name"] for r in rows] == ["acropora alive", "branching alive", "sand"]
    assert list(rows[0]) == ["class_id", "name", "fraction", "count"]
    assert rows[0]["class_id"] == "25"
    assert rows[0]["fraction"] == "0.500000"  # 6dp
    assert rows[0]["count"] == "50.0000"      # 4dp


def test_save_cover_csv_of_an_empty_cover_still_writes_a_header(tmp_path) -> None:
    path = tmp_path / "cover.csv"
    save_cover_csv(path, {})
    assert path.read_text().strip() == "class_id,name,fraction,count"


def test_save_cover_csv_levels_writes_one_file_per_level(tmp_path) -> None:
    out = tmp_path / "exports"
    written = save_cover_csv_levels(out, _cover(), _classes())

    assert set(written) == {"fine", "intermediate", "coarse"}
    assert {p.name for p in written.values()} == {
        "benthic_cover_fine.csv",
        "benthic_cover_intermediate.csv",
        "benthic_cover_coarse.csv",
    }

    # Fine keeps every class; coarse rolls the two corals together.
    assert len(_read_csv(written["fine"])) == 3
    coarse = _read_csv(written["coarse"])
    assert [r["name"] for r in coarse] == ["coral alive", "sand"]
    assert coarse[0]["fraction"] == "0.800000"
    assert list(coarse[0]) == ["name", "fraction", "count"]


def test_save_cover_csv_levels_creates_a_missing_directory(tmp_path) -> None:
    written = save_cover_csv_levels(tmp_path / "a" / "b", _cover(), _classes(), prefix="run42")
    assert written["fine"].name == "run42_fine.csv"
    assert written["fine"].exists()


def test_taxonomy_version_and_hash_identify_the_grouping() -> None:
    """A grouped cover number is only reproducible if the taxonomy is pinned."""
    assert taxonomy_version() == 1
    digest = taxonomy_hash()
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)
    # A version key must not leak into the id -> group mapping.
    assert group_name_for_id(_classes(), 22, "coarse") == "coral alive"
