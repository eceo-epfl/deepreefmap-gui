"""Auditing what settings past runs were made with.

Scenario throughout: an administrator comes back from a field season and wants to
know whether every run used the blessed configuration.
"""

import json

from deepreefmap_gui.survey.config_audit import (
    DEVIATED,
    DIFFERENT,
    STANDARD,
    UNRECORDED,
    audit_out_root,
    audit_row,
    audit_summary,
)
from deepreefmap_gui.survey.preset import load_org_preset, manifest_config_block


def manifest(config=None, *, name="Dive 1"):
    survey = {"provenance": {"gui_version": "1.0.0"}}
    if config is not None:
        survey["provenance"]["config"] = config
    return {"name": name, "mode": "semantic", "survey": survey}


def test_standard_run_reads_as_standard():
    org = load_org_preset()
    row = audit_row("t1__p01", manifest(manifest_config_block(org, {})), org)
    assert row.verdict == STANDARD
    assert row.preset_label == org.label
    assert row.preset_hash == org.content_hash
    assert row.note == "Standard settings."
    assert row.changed_summary == ""
    assert row.display_name == "Dive 1"


def test_deviating_run_names_what_changed():
    org = load_org_preset()
    config = manifest_config_block(org, {"preprocess_batch_size": 1})
    row = audit_row("t1__p01", manifest(config), org)
    assert row.verdict == DEVIATED
    assert "frames processed at once" in row.note
    assert row.changed_summary == "frames processed at once"


def test_run_on_another_standard_is_flagged_against_the_current_one():
    org = load_org_preset()
    config = {**manifest_config_block(org, {}), "preset_hash": "0000deadbeef", "preset_version": 1}
    row = audit_row("t1__p01", manifest(config), org)
    assert row.verdict == DIFFERENT
    assert "not the standard in force now" in row.note


def test_a_different_standard_outranks_a_deviation():
    """Both are true at once, and the bigger difference is the one to lead with."""
    org = load_org_preset()
    config = {
        **manifest_config_block(org, {"preprocess_batch_size": 1}),
        "preset_hash": "0000deadbeef",
    }
    row = audit_row("t1__p01", manifest(config), org)
    assert row.verdict == DIFFERENT
    assert "frames processed at once" in row.note


def test_run_with_no_recorded_config_says_so_rather_than_claiming_standard():
    """A run from before configuration was recorded must not be reported as
    standard: nothing is known about it either way."""
    org = load_org_preset()
    row = audit_row("old_run", manifest(), org)
    assert row.verdict == UNRECORDED
    assert row.preset_hash is None
    assert "cannot be checked" in row.note


def test_unnamed_run_falls_back_to_the_folder():
    org = load_org_preset()
    row = audit_row("t1__p01", {"survey": {}}, org)
    assert row.display_name == "t1__p01"


def test_audit_out_root_reads_every_run_folder(tmp_path):
    org = load_org_preset()
    for dir_name, config in (
        ("run_standard", manifest_config_block(org, {})),
        ("run_changed", manifest_config_block(org, {"mapping_name": "scsfmlearner"})),
        ("run_old", None),
    ):
        run_dir = tmp_path / dir_name
        run_dir.mkdir()
        (run_dir / "run_manifest.json").write_text(json.dumps(manifest(config, name=dir_name)))
    # A folder with no manifest at all is not a run the audit can speak about.
    (tmp_path / "not_a_run").mkdir()

    rows = {row.dir_name: row for row in audit_out_root(tmp_path, org)}
    assert set(rows) == {"run_standard", "run_changed", "run_old"}
    assert rows["run_standard"].verdict == STANDARD
    assert rows["run_changed"].verdict == DEVIATED
    assert rows["run_old"].verdict == UNRECORDED


def test_audit_out_root_of_a_missing_folder_is_empty(tmp_path):
    assert audit_out_root(tmp_path / "nope", load_org_preset()) == []


def test_summary_counts_each_verdict():
    org = load_org_preset()
    rows = [
        audit_row("a", manifest(manifest_config_block(org, {})), org),
        audit_row("b", manifest(manifest_config_block(org, {"mapping_name": "x"})), org),
        audit_row("c", manifest(), org),
    ]
    summary = audit_summary(rows)
    assert summary.startswith("3 runs checked")
    assert "1 on standard settings" in summary
    assert "1 with changes" in summary
    assert "1 with nothing recorded" in summary


def test_summary_of_an_empty_audit():
    assert audit_summary([]) == "No processed runs to check yet."
