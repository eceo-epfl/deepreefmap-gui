from pathlib import Path

from deepreefmap.config.classes import ClassConfig, SemanticClass

from deepreefmap_gui.cover import aggregate_cover, group_color_for_name, group_name_for_id


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
