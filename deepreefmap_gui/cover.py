"""Benthic-cover group roll-up and CSV export.

The class group taxonomy (fine -> intermediate -> coarse) moved out of the
library's ClassConfig into a GUI-owned resource (`resources/configs/class_groups.yaml`),
so the roll-up and cover-CSV helpers that depended on it live here now.
"""

from __future__ import annotations

import csv
import hashlib
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import SupportsFloat, cast

import yaml

from deepreefmap.config.classes import ClassConfig

COVER_LEVELS = ("fine", "intermediate", "coarse")

_FALLBACK_COLOR = (128, 128, 128)

# Top-level key in class_groups.yaml that carries the taxonomy version, kept out
# of the id -> groups mapping. Bump the file's `version` whenever a grouping
# changes so a recomputed number can be told apart from an older one.
_VERSION_KEY = "version"


@lru_cache(maxsize=1)
def _class_groups_text() -> str:
    """Raw bytes of the bundled taxonomy, so its hash traces a grouped number."""
    return (
        resources.files("deepreefmap_gui.resources")
        .joinpath("configs/class_groups.yaml")
        .read_text(encoding="utf-8")
    )


@lru_cache(maxsize=1)
def _group_taxonomy() -> dict[int, dict[str, str]]:
    """Read the bundled class-group taxonomy: id -> {intermediate, coarse}."""
    payload = yaml.safe_load(_class_groups_text()) or {}
    taxonomy: dict[int, dict[str, str]] = {}
    for cid, groups in payload.items():
        if cid == _VERSION_KEY or not isinstance(groups, dict):
            continue
        taxonomy[int(cid)] = {
            "intermediate": str(groups.get("intermediate", "")),
            "coarse": str(groups.get("coarse", "")),
        }
    return taxonomy


def taxonomy_version() -> int:
    """The taxonomy's declared version, or 0 when the file predates versioning."""
    payload = yaml.safe_load(_class_groups_text()) or {}
    try:
        return int(payload.get(_VERSION_KEY, 0))
    except (TypeError, ValueError):
        return 0


def taxonomy_hash() -> str:
    """SHA-256 of the taxonomy file, so an exported number names the grouping used."""
    return hashlib.sha256(_class_groups_text().encode("utf-8")).hexdigest()


def group_name_for_id(classes_config: ClassConfig, class_id: int, level: str) -> str:
    """The group bucket a class rolls into at `level`.

    `fine` is the class name itself; `intermediate`/`coarse` come from the
    taxonomy. A missing/empty group falls back to the class name so each class
    becomes its own bucket.
    """
    cid = int(class_id)
    if level == "fine":
        return classes_config.name_for_id(cid)
    if level in ("intermediate", "coarse"):
        return _group_taxonomy().get(cid, {}).get(level) or classes_config.name_for_id(cid)
    raise ValueError(f"Unknown cover level: {level!r}")


def group_color_for_name(
    classes_config: ClassConfig, group_name: str, level: str
) -> tuple[int, int, int]:
    """Color for a group: the first class color whose group matches at `level`."""
    for cls in classes_config.classes:
        if group_name_for_id(classes_config, cls.id, level) == group_name:
            return cls.color
    return _FALLBACK_COLOR


def aggregate_cover(
    cover: dict[str, object],
    classes_config: ClassConfig,
    level: str,
) -> dict[str, dict[str, float]]:
    """Roll a per-class cover dict up to a group level (fine/intermediate/coarse).

    Fractions are re-normalized over the input's own denominator so coarse and
    intermediate sums match the fine total.
    """
    if level not in COVER_LEVELS:
        raise ValueError(f"Unknown cover level: {level!r}")
    classes_block = cover.get("classes") if isinstance(cover, dict) else None
    if not isinstance(classes_block, dict):
        return {}
    denom_raw = cover.get("denominator", 0.0) if isinstance(cover, dict) else 0.0
    denom = float(cast(SupportsFloat, denom_raw))
    grouped: dict[str, float] = {}
    for class_id_str, entry in classes_block.items():
        try:
            class_id = int(class_id_str)
        except (TypeError, ValueError):
            continue
        count = float(entry.get("count", 0.0))
        group = group_name_for_id(classes_config, class_id, level)
        grouped[group] = grouped.get(group, 0.0) + count
    if denom <= 0:
        return {name: {"count": cnt, "fraction": 0.0} for name, cnt in grouped.items()}
    return {
        name: {"count": cnt, "fraction": cnt / denom}
        for name, cnt in grouped.items()
    }


def save_cover_csv(path: Path, cover: dict[str, object]) -> None:
    """Write the fine-grained cover dict produced by compute_benthic_cover."""
    classes_block = cover.get("classes") if isinstance(cover, dict) else None
    rows: list[tuple[int, str, float, float]] = []
    if isinstance(classes_block, dict):
        for class_id_str, entry in classes_block.items():
            try:
                cid = int(class_id_str)
            except (TypeError, ValueError):
                continue
            rows.append(
                (
                    cid,
                    str(entry.get("name", f"class_{cid}")),
                    float(entry.get("fraction", 0.0)),
                    float(entry.get("count", 0.0)),
                )
            )
    rows.sort(key=lambda r: r[2], reverse=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["class_id", "name", "fraction", "count"])
        for cid, name, frac, count in rows:
            writer.writerow([cid, name, f"{frac:.6f}", f"{count:.4f}"])


def save_cover_csv_levels(
    out_dir: Path,
    cover: dict[str, object],
    classes_config: ClassConfig,
    prefix: str = "benthic_cover",
) -> dict[str, Path]:
    """Write fine / intermediate / coarse CSVs of the cover dict to `out_dir`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    fine_path = out_dir / f"{prefix}_fine.csv"
    save_cover_csv(fine_path, cover)
    written["fine"] = fine_path
    for level in ("intermediate", "coarse"):
        grouped = aggregate_cover(cover, classes_config, level)
        rows = sorted(
            ((name, payload["fraction"], payload["count"]) for name, payload in grouped.items()),
            key=lambda r: r[1],
            reverse=True,
        )
        path = out_dir / f"{prefix}_{level}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["name", "fraction", "count"])
            for name, frac, count in rows:
                writer.writerow([name, f"{frac:.6f}", f"{count:.4f}"])
        written[level] = path
    return written
