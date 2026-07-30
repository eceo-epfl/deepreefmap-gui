"""What settings past runs were actually made with, checked against the current standard.

Answers the question an administrator asks after a field season: did every run in
this folder use the blessed configuration, and where it did not, what differed.
The answer comes from each run's own manifest, which is never rewritten, so the
audit reads history rather than reconstructing it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepreefmap_gui.survey.preset import OrgPreset, describe_keys

# Worst verdict wins, so this order is also the sort order for the summary.
UNRECORDED = "unrecorded"
DIFFERENT = "different"
DEVIATED = "deviated"
STANDARD = "standard"

_SEVERITY = (UNRECORDED, DIFFERENT, DEVIATED, STANDARD)


@dataclass(frozen=True)
class ConfigAuditRow:
    """One run's recorded configuration identity, judged against the current one."""

    dir_name: str
    run_name: str | None
    preset_label: str
    preset_hash: str | None
    deviations: dict[str, Any]
    verdict: str
    note: str

    @property
    def display_name(self) -> str:
        return self.run_name or self.dir_name

    @property
    def changed_summary(self) -> str:
        return describe_keys(self.deviations) if self.deviations else ""


def audit_row(dir_name: str, manifest: Mapping[str, Any], org: OrgPreset) -> ConfigAuditRow:
    """Judge one run's manifest against the organisation preset in force now."""
    survey = _mapping(manifest.get("survey"))
    config = _mapping(_mapping(survey.get("provenance")).get("config"))
    run_name = manifest.get("name") if isinstance(manifest.get("name"), str) else None
    if not config:
        return ConfigAuditRow(
            dir_name=dir_name,
            run_name=run_name,
            preset_label="Not recorded",
            preset_hash=None,
            deviations={},
            verdict=UNRECORDED,
            note="This run recorded no settings, so what it used cannot be checked.",
        )

    name = str(config.get("preset_name") or "Unnamed")
    version = config.get("preset_version")
    label = f"{name} (v{version})" if isinstance(version, int) else name
    preset_hash = config.get("preset_hash") if isinstance(config.get("preset_hash"), str) else None
    deviations = _mapping(config.get("deviations"))

    same_config = preset_hash == org.content_hash
    verdict = STANDARD if same_config else DIFFERENT
    notes = []
    if not same_config:
        notes.append(f"Ran on {label}, not the standard in force now.")
    if deviations:
        verdict = _worst(verdict, DEVIATED)
        notes.append(f"Changed on that computer: {describe_keys(deviations)}.")
    if not notes:
        notes.append("Standard settings.")
    return ConfigAuditRow(
        dir_name=dir_name,
        run_name=run_name,
        preset_label=label,
        preset_hash=preset_hash,
        deviations=dict(deviations),
        verdict=verdict,
        note=" ".join(notes),
    )


def audit_out_root(out_root: Path, org: OrgPreset) -> list[ConfigAuditRow]:
    """Every run under the output root, newest first, with its configuration verdict."""
    from deepreefmap_gui.survey.catalogue import scan_out_root

    return [audit_row(entry.dir_name, entry.manifest, org) for entry in scan_out_root(out_root)]


def audit_summary(rows: list[ConfigAuditRow]) -> str:
    """One sentence for the top of the audit view."""
    if not rows:
        return "No processed runs to check yet."
    counts = {verdict: sum(1 for row in rows if row.verdict == verdict) for verdict in _SEVERITY}
    total = len(rows)
    parts = [f"{total} run{'' if total == 1 else 's'} checked"]
    if counts[STANDARD]:
        parts.append(f"{counts[STANDARD]} on standard settings")
    if counts[DEVIATED]:
        parts.append(f"{counts[DEVIATED]} with changes")
    if counts[DIFFERENT]:
        parts.append(f"{counts[DIFFERENT]} on a different standard")
    if counts[UNRECORDED]:
        parts.append(f"{counts[UNRECORDED]} with nothing recorded")
    return ", ".join(parts) + "."


def _worst(*verdicts: str) -> str:
    return min(verdicts, key=_SEVERITY.index)


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}
