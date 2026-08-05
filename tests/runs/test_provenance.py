"""A cover figure is only usable if what produced it can be named.

Every manifest already records the models, the class taxonomy and how far the
settings strayed from the standard. None of it was shown anywhere, so a number
and its method lived in different files.
"""

from __future__ import annotations

from deepreefmap_gui.runs.run_cards import provenance_rows, summarise_run_provenance


def manifest(**survey_overrides) -> dict:
    provenance = {
        "gui_version": "0.3.0",
        "taxonomy_version": "coralscapes-1",
        "taxonomy_hash": "abcdef0123456789",
        "config": {
            "preset_name": "Standard reef survey",
            "deviations": {},
        },
    }
    provenance.update(survey_overrides)
    return {
        "segmentation_model": "coralscapes-vit-b-dpt",
        "mapping_backend": "loger_star",
        "deepreefmap_version": "1.2.3",
        "survey": {"provenance": provenance},
    }


def test_rows_name_the_models_taxonomy_and_settings():
    rows = dict(provenance_rows(manifest()))
    assert rows["Models"] == "coralscapes-vit-b-dpt + loger_star"
    assert rows["Taxonomy"] == "coralscapes-1 · #abcdef01"
    assert rows["Settings"] == "Standard reef survey, as standard"
    assert rows["Version"] == "1.2.3 · 0.3.0"


def test_a_deviated_run_names_what_changed():
    rows = dict(
        provenance_rows(
            manifest(
                config={
                    "preset_name": "Standard reef survey",
                    "deviations": {"fps": 3},
                }
            )
        )
    )
    assert "changed" in rows["Settings"]
    assert "frames per second" in rows["Settings"]


def test_a_geometry_only_run_does_not_claim_a_segmentation_model():
    data = manifest()
    data["segmentation_model"] = "__skip__"
    assert dict(provenance_rows(data))["Models"] == "loger_star"


def test_a_manifest_without_provenance_says_nothing_rather_than_guessing():
    """"not recorded" and "nothing changed" are different claims."""
    assert provenance_rows({"segmentation_model": "x"}) == []
    assert provenance_rows({"survey": {}}) == []


def test_summary_is_one_line_and_empty_when_unreadable(tmp_path):
    import json

    run_dir = tmp_path / "run_a"
    run_dir.mkdir()
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest()), encoding="utf-8")
    assert summarise_run_provenance("run_a", str(tmp_path)) == (
        "coralscapes-vit-b-dpt + loger_star, taxonomy coralscapes-1"
    )
    assert summarise_run_provenance("missing", str(tmp_path)) == ""
