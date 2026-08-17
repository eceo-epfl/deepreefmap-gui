"""Translation between the desktop's rows and the registry's wire shape.

Every difference between the two models is handled here: the fields that never
leave the device, the join table the registry keeps for a pass's chapters, the
cover rows it keeps that this side holds only as JSON in a run directory, and the
timestamp format. Nothing here talks to a network or to sqlite, so all of it is
testable on plain dicts.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepreefmap_gui.survey.analysis import LongCoverRow, _run_manifest_provenance
from deepreefmap_gui.survey.models.convert import to_row
from deepreefmap_gui.survey.models.run_record import RunRecord
from deepreefmap_gui.survey.models.transect_pass import TransectPass
from deepreefmap_gui.survey.store import SYNC_SECTIONS

logger = logging.getLogger(__name__)

PASS_VIDEOS = "pass_videos"
COVER_ROWS = "cover_rows"

# The only estimator that travels. The pooled figure is a pure function of the
# per-pass counts, denominators and the latest-run-per-pass rule, so storing it
# centrally would invite two disagreeing numbers.
PER_PASS = "per_pass"

# Foreign-key order, which is the order a push document has to present. The two
# derived sections sit behind the rows they hang off: chapters after their pass,
# cover after its run.
WIRE_SECTIONS: tuple[str, ...] = (
    "sites",
    "campaigns",
    "transects",
    "videos",
    "passes",
    PASS_VIDEOS,
    "runs",
    COVER_ROWS,
)

# Fields that stay on the device. A path and an mtime describe this laptop's disk,
# so sending them would put absolute paths in a shared registry; probed_at and
# batch_id are local workflow. video_id and extra_video_ids leave as pass_video
# rows instead.
_DEVICE_LOCAL: dict[str, tuple[str, ...]] = {
    "videos": ("path", "mtime", "probed_at"),
    "passes": ("batch_id", "video_id", "extra_video_ids"),
    "runs": ("batch_id",),
}

# Every column the registry types as a timestamp. campaign.begin_date and
# end_date are days, not moments, and are deliberately not in here.
_TIMESTAMPS = frozenset({
    "created_at",
    "updated_at",
    "deleted_at",
    "started_at",
    "finished_at",
    "captured_at",
})

# Fixed namespaces, so a derived id is the same id on every device and across
# every push: uuid5 makes it a function of what it describes rather than of when
# it was built, which is what makes re-pushing idempotent.
_PASS_VIDEO_NAMESPACE = uuid.UUID("6b1f4a52-0f8e-5c7d-9a3b-2ad4c8e17f01")
_COVER_ROW_NAMESPACE = uuid.UUID("c04e7d19-3b52-5f68-8d21-9e7b41ac6d02")

_PROVENANCE_FIELDS = (
    "gui_version",
    "library_version",
    "segmentation_model",
    "mapping_backend",
    "taxonomy_version",
    "taxonomy_hash",
    "model_revisions",
    "preset_name",
    "preset_deviations",
)


# --- Identity ---


def pass_video_id(pass_id: Any, video_id: Any) -> uuid.UUID:
    """The join row's id, derived from the pair it joins."""
    return uuid.uuid5(_PASS_VIDEO_NAMESPACE, f"{pass_id}:{video_id}")


def cover_row_id(run_id: Any, level: str, class_group: str, estimator: str) -> uuid.UUID:
    """The cover row's id, derived from the registry's own unique key for it."""
    return uuid.uuid5(_COVER_ROW_NAMESPACE, f"{run_id}:{level}:{class_group}:{estimator}")


# --- Timestamps ---


def to_wire_time(value: str | None) -> str | None:
    """A stored stamp as the registry writes them: UTC, RFC 3339, ``Z`` suffix."""
    moment = _parse_time(value)
    return value if moment is None else moment.isoformat().replace("+00:00", "Z")


def from_wire_time(value: str | None) -> str | None:
    """A wire stamp as ``utc_now_iso`` writes them: UTC, ``+00:00``.

    Last-write-wins compares these as strings, so a stamp carrying an offset would
    compare wrongly against everything this side has ever written.
    """
    moment = _parse_time(value)
    return value if moment is None else moment.isoformat()


def _parse_time(value: str | None) -> datetime | None:
    """The moment a stamp names, or None for an empty or unreadable one."""
    if not value:
        return None
    text = str(value)
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        logger.warning("Leaving unreadable timestamp %r as it stands", value)
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _restamp(row: dict[str, Any], convert: Callable[[str | None], str | None]) -> dict[str, Any]:
    return {k: convert(v) if k in _TIMESTAMPS else v for k, v in row.items()}


# --- Outbound ---


def rows_to_wire(section: str, models: Iterable[Any]) -> list[dict[str, Any]]:
    """Desktop models as wire rows for one of the sections backed by a table.

    ``created_by`` and ``device_id`` travel even though the registry stamps them
    from the credential and discards what it was sent, because pull output is
    guaranteed to be valid push input field for field.
    """
    if section not in SYNC_SECTIONS:
        raise KeyError(f"{section!r} is not a section with a table behind it")
    dropped = _DEVICE_LOCAL.get(section, ())
    return [
        _restamp({k: v for k, v in to_row(model).items() if k not in dropped}, to_wire_time)
        for model in models
    ]


def run_rows_to_wire(runs: Sequence[RunRecord], out_root: Path) -> list[dict[str, Any]]:
    """Run rows with the provenance the database does not hold, read per run."""
    return [
        {**row, **run_provenance(out_root, run.run_dir_name)}
        for run, row in zip(runs, rows_to_wire("runs", runs), strict=True)
    ]


def pass_video_rows(pass_: TransectPass) -> list[dict[str, Any]]:
    """A pass's chapters as the registry's join table, ordinal zero-based.

    The relationship is the pass's, so the rows carry the pass's own stamps and go
    to a tombstone with it.
    """
    stamps = {
        "created_at": to_wire_time(pass_.created_at),
        "updated_at": to_wire_time(pass_.updated_at),
        "deleted_at": to_wire_time(pass_.deleted_at),
    }
    return [
        {
            "id": str(pass_video_id(pass_.id, video_id)),
            "pass_id": str(pass_.id),
            "video_id": str(video_id),
            "ordinal": ordinal,
            **stamps,
        }
        for ordinal, video_id in enumerate(pass_.video_ids())
    ]


def cover_rows_to_wire(
    rows: Iterable[LongCoverRow],
    runs: Mapping[str, RunRecord],
    out_root: Path,
) -> list[dict[str, Any]]:
    """Per-pass cover as wire rows, for the runs travelling in the same document.

    A row for a run the registry has never seen is a 409, so anything outside
    ``runs`` is left for the push that carries its run. Cover is a function of the
    run, so a row takes the run's stamps: a recomputed figure lands only when the
    run row itself is newer, which is the same limitation the run's own provenance
    columns already have.
    """
    sources: dict[str, str | None] = {}
    wire_rows = []
    for row in rows:
        if row.estimator != PER_PASS:
            continue
        run = runs.get(row.run_id)
        if run is None:
            continue
        if row.run_dir_name not in sources:
            sources[row.run_dir_name] = metric_source(out_root, row.run_dir_name)
        wire_rows.append({
            "id": str(cover_row_id(row.run_id, row.level, row.group, row.estimator)),
            "run_id": row.run_id,
            "level": row.level,
            # LongCoverRow calls these two `group` and `count`.
            "class_group": row.group,
            "estimator": row.estimator,
            "fraction": row.fraction,
            "point_count": row.count,
            "denominator": row.denominator,
            "metric_source": sources[row.run_dir_name],
            "created_at": to_wire_time(run.created_at),
            "updated_at": to_wire_time(run.updated_at),
            "deleted_at": to_wire_time(run.deleted_at),
        })
    return wire_rows


# --- Run manifests ---


def run_provenance(out_root: Path, run_dir_name: str) -> dict[str, Any]:
    """What produced a run, from its manifest, as nulls where it cannot be read.

    The database holds none of this. It is written by
    ``convert.survey_manifest_block``, three fields at the manifest's top level
    and the rest under ``survey.provenance``. A pruned or half-written run
    directory degrades to nulls rather than stopping the whole push.
    """
    provenance: dict[str, Any] = dict.fromkeys(_PROVENANCE_FIELDS)
    top = _run_manifest_provenance(out_root, run_dir_name)
    provenance["library_version"] = top["deepreefmap_version"] or None
    provenance["segmentation_model"] = top["segmentation_model"] or None
    provenance["mapping_backend"] = top["mapping_backend"] or None

    survey = _block(_manifest(out_root, run_dir_name), "survey")
    block = _block(survey, "provenance")
    config = _block(block, "config")
    provenance["gui_version"] = _text(block.get("gui_version"))
    provenance["taxonomy_version"] = _whole(block.get("taxonomy_version"))
    provenance["taxonomy_hash"] = _text(block.get("taxonomy_hash"))
    provenance["model_revisions"] = block.get("model_versions") or None
    provenance["preset_name"] = _text(config.get("preset_name")) or _text(survey.get("preset_name"))
    # An empty deviations map means "nothing departed from the preset", which is a
    # different fact from a run that recorded no configuration at all.
    if "deviations" in config:
        provenance["preset_deviations"] = config["deviations"]
    return provenance


def metric_source(out_root: Path, run_dir_name: str) -> str | None:
    """Which cloud the run's cover was measured on, or None when unrecorded.

    Read from the manifest's ``enable_tsdf``: fusion is what swaps the metric
    cloud. A run that enabled fusion and produced no fused points fell back to
    the unprojected cloud, and nothing in the manifest says so.
    """
    manifest = _manifest(out_root, run_dir_name)
    if "enable_tsdf" not in manifest:
        return None
    return "tsdf" if manifest["enable_tsdf"] else "unprojected"


def _manifest(out_root: Path, run_dir_name: str) -> dict[str, Any]:
    path = out_root / run_dir_name / "run_manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.info("No readable run manifest for %s", run_dir_name)
        return {}
    return manifest if isinstance(manifest, dict) else {}


def _block(document: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = document.get(key)
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return str(value) if value else None


def _whole(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --- Inbound ---


def rows_from_wire(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Wire rows as this side names things, each left as partial as it arrived.

    Partial matters: the store writes only the fields a row carried, so a clip's
    path and a run's session survive an update from a registry that holds neither.
    ``server_seq`` is the registry's own cursor and is not a column here.
    """
    return [
        _restamp({k: v for k, v in row.items() if k != "server_seq"}, from_wire_time)
        for row in rows
    ]


def fold_pass_videos(
    pass_rows: Iterable[Mapping[str, Any]],
    pass_video_rows_in: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[uuid.UUID]]]:
    """Collapse the join table back onto the passes it belongs to.

    Returns the pass rows with ``video_id`` and ``extra_video_ids`` filled, and the
    chapter lists whose pass was not among them, keyed by pass id, for the caller
    to apply to a pass it already holds.

    A pass with no live chapter row gets neither field, so a pass this device
    already has keeps the chapters it knows. A pass it has never seen cannot be
    built at all, because the model has no representation for a pass with no
    video, and the caller reports it rather than inventing one.
    """
    by_pass: dict[str, list[Mapping[str, Any]]] = {}
    for row in pass_video_rows_in:
        by_pass.setdefault(str(row["pass_id"]), []).append(row)
    folded = []
    for row in pass_rows:
        video_ids = video_ids_from_pass_videos(by_pass.pop(str(row["id"]), []))
        chapters = (
            {
                "video_id": str(video_ids[0]),
                "extra_video_ids": [str(v) for v in video_ids[1:]],
            }
            if video_ids
            else {}
        )
        folded.append({**row, **chapters})
    return folded, {
        pass_id: video_ids_from_pass_videos(rows) for pass_id, rows in by_pass.items()
    }


def video_ids_from_pass_videos(rows: Iterable[Mapping[str, Any]]) -> list[uuid.UUID]:
    """The chapters of one pass, in playing order.

    A tombstoned row is a chapter that was taken off the pass. Ordinals only
    order, so a gap in them means nothing; a repeat should be impossible and is
    broken by row id so two devices reading the same rows agree. A clip named
    twice keeps its first place, because the pass model refuses a chapter that is
    also its first video.
    """
    live = [row for row in rows if not row.get("deleted_at")]
    ordered = sorted(live, key=lambda row: (_whole(row.get("ordinal")) or 0, str(row.get("id", ""))))
    video_ids: list[uuid.UUID] = []
    for row in ordered:
        video_id = uuid.UUID(str(row["video_id"]))
        if video_id not in video_ids:
            video_ids.append(video_id)
    return video_ids
