"""Run metadata formatting, shared by every widget that describes a run.

Formatting only: the widgets that arrange these strings (the run table, the
detail pane, the top banner) each own their own layout.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from deepreefmap_gui.core.theme import (
    BANNER_TEXT,
    FONT_LG,
)
from deepreefmap_gui.profiling.eta import format_duration
from deepreefmap_gui.profiling.system_probe import format_bytes
from deepreefmap_gui.survey.catalogue import run_duration_s

_GEOMETRY_LABELS = {
    "world_points": "world points (full)",
    "depth_unprojection": "depth-unprojection",
}


def format_timestamp(value: object) -> str:
    """Render an ISO-8601 run_timestamp as a short local date/time, else passthrough."""
    if not isinstance(value, str):
        return ""
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def related_run_counts(entries: list[tuple[Path, dict]]) -> dict[Path, int]:
    """Per run dir, how many sibling runs share a video_hash with it.

    Old manifests have no video_hashes and never count as related.
    """
    dirs_by_hash: dict[str, set[Path]] = {}
    for run_dir, manifest in entries:
        for h in manifest.get("video_hashes") or []:
            if h:
                dirs_by_hash.setdefault(h, set()).add(run_dir)
    counts: dict[Path, int] = {}
    for run_dir, manifest in entries:
        related: set[Path] = set()
        for h in manifest.get("video_hashes") or []:
            if h:
                related |= dirs_by_hash[h]
        related.discard(run_dir)
        counts[run_dir] = len(related)
    return counts


def points_label(n: int) -> str:
    """A point count at reading precision: "4.6M", "988k", "14"."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def format_disk_size(run_dir: Path) -> str | None:
    try:
        total = sum(p.stat().st_size for p in run_dir.rglob("*") if p.is_file())
    except Exception:
        return None
    return format_bytes(total)


def video_details(manifest: dict, index: int = 0) -> list[str]:
    """Short hash, size, and recording date for one input video, where known."""
    details: list[str] = []
    hashes = manifest.get("video_hashes") or []
    sizes = manifest.get("video_sizes") or []
    mtimes = manifest.get("video_mtimes") or []
    if index < len(hashes) and hashes[index]:
        details.append(f"#{str(hashes[index])[:8]}")
    if index < len(sizes) and sizes[index]:
        details.append(format_bytes(float(sizes[index])))
    if index < len(mtimes) and mtimes[index]:
        stamp = format_timestamp(mtimes[index])
        if stamp:
            details.append(stamp)
    return details


def format_trim_range(manifest: dict) -> str | None:
    """The processed slice of the video, shown only when the run was trimmed."""
    begin = manifest.get("begin_s")
    end = manifest.get("end_s")
    if begin is None and end is None:
        return None
    begin_txt = f"{float(begin):.1f}" if begin is not None else "0"
    end_txt = f"{float(end):.1f}s" if end is not None else "end"
    return f"{begin_txt}–{end_txt}"


def _disk_label(run_dir: Path, disk_bytes: int | None) -> str | None:
    if disk_bytes is not None:
        return format_bytes(disk_bytes)
    return format_disk_size(run_dir)


def format_run_metadata(
    manifest: dict,
    run_dir: Path,
    *,
    include_disk_size: bool,
    disk_bytes: int | None = None,
) -> str:
    """Multi-line format used in tooltips and the results block."""
    lines: list[str] = []
    name = (manifest.get("name") or "").strip() or run_dir.name
    lines.append(f"<b>{name}</b>  <i>({run_dir.name})</i>")
    mode = manifest.get("mode")
    if mode:
        lines.append(f"Mode: {mode}")
    seg = manifest.get("segmentation_model")
    if seg:
        lines.append(f"Segmentation: {seg}")
    mapping = manifest.get("mapping_backend")
    if mapping:
        lines.append(f"Mapping: {mapping}")
    mopts = manifest.get("mapping_options") or {}
    if mopts.get("window_size") is not None:
        lines.append(
            f"LoGeR window/overlap: {mopts.get('window_size')}/{mopts.get('overlap_size')}"
        )
    if manifest.get("refine_intrinsics_from_mapper"):
        lines.append("Intrinsics: refined from mapper")
    geom = manifest.get("geometry_source")
    if geom:
        lines.append(f"Geometry: {_GEOMETRY_LABELS.get(geom, geom)}")
    profile = manifest.get("camera_profile")
    if profile:
        lines.append(f"Camera profile: {profile}")
    frames = manifest.get("frames_processed")
    if frames is not None:
        fps = manifest.get("fps")
        lines.append(f"Frames: {frames}" + (f" @ {fps} fps" if fps else ""))
    pw, ph = manifest.get("processing_width"), manifest.get("processing_height")
    if pw and ph:
        lines.append(f"Processing size: {pw}×{ph}")
    sem_pts = manifest.get("semantic_reference_points")
    if sem_pts:
        lines.append(f"Semantic points: {int(sem_pts):,}")
    metric_pts = manifest.get("metric_points")
    if metric_pts:
        lines.append(f"Metric points: {int(metric_pts):,}")
    for i, v in enumerate(manifest.get("input_videos") or []):
        details = video_details(manifest, i)
        suffix = f" ({', '.join(details)})" if details else ""
        lines.append(f"Input: {Path(v).name}{suffix}")
    trim = format_trim_range(manifest)
    if trim:
        lines.append(f"Range: {trim}")
    dur = run_duration_s(manifest)
    if dur:
        lines.append(f"Runtime: {format_duration(dur)}")
    created = format_timestamp(manifest.get("run_timestamp"))
    if created:
        lines.append(f"Created: {created}")
    if include_disk_size:
        disk = _disk_label(run_dir, disk_bytes)
        if disk:
            lines.append(f"Disk: {disk}")
    return "<br>".join(lines)


def format_run_metadata_compact(
    manifest: dict,
    run_dir: Path,
    *,
    include_disk_size: bool,
    disk_bytes: int | None = None,
) -> str:
    """Single-line wrapping format used in the inline top banner."""
    name = (manifest.get("name") or "").strip() or run_dir.name
    header = (
        f'<b style="font-size:{FONT_LG}">{name}</b>'
        f'&nbsp;<span style="color:#7a8a99">({run_dir.name})</span>'
    )
    facts: list[str] = []
    for label, key, fmt in (
        ("Mode", "mode", str),
        ("Frames", "frames_processed", str),
        ("FPS", "fps", str),
        ("Segmentation", "segmentation_model", str),
        ("Mapping", "mapping_backend", str),
        ("Geometry", "geometry_source", lambda v: _GEOMETRY_LABELS.get(v, str(v))),
        ("Camera", "camera_profile", str),
        ("Semantic pts", "semantic_reference_points", lambda v: f"{int(v):,}"),
        ("Metric pts", "metric_points", lambda v: f"{int(v):,}"),
        ("Input", "input_videos", lambda v: ", ".join(Path(p).name for p in v) if v else ""),
        ("Created", "run_timestamp", format_timestamp),
    ):
        v = manifest.get(key)
        if v is not None and v not in ("", []):
            facts.append(
                f'<span style="color:#8aa0b8">{label}:</span>&nbsp;'
                f'<span style="color:{BANNER_TEXT}">{fmt(v)}</span>'
            )
    trim = format_trim_range(manifest)
    if trim:
        facts.append(
            f'<span style="color:#8aa0b8">Range:</span>&nbsp;'
            f'<span style="color:{BANNER_TEXT}">{trim}</span>'
        )
    dur = run_duration_s(manifest)
    if dur:
        facts.append(
            f'<span style="color:#8aa0b8">Runtime:</span>&nbsp;'
            f'<span style="color:{BANNER_TEXT}">{format_duration(dur)}</span>'
        )
    if include_disk_size:
        disk = _disk_label(run_dir, disk_bytes)
        if disk:
            facts.append(
                f'<span style="color:#8aa0b8">Disk:</span>&nbsp;'
                f'<span style="color:{BANNER_TEXT}">{disk}</span>'
            )
    sep = '&nbsp;<span style="color:#4a5f74">·</span>&nbsp;'
    return f"{header}&nbsp;&nbsp;{sep.join(facts)}"


def provenance_rows(manifest: dict) -> list[tuple[str, str]]:
    """What produced a run's numbers, as detail-pane rows.

    A cover figure is only usable if what made it can be named: the models, the
    class taxonomy those models' outputs were grouped under, and whether the
    settings deviated from the organisation standard. All three are already
    recorded in every manifest by ``survey_manifest_block``; until now none of
    them was shown anywhere, so a figure and its method lived in different files.

    Omitted rather than filled with a placeholder when a manifest predates the
    provenance block: "not recorded" and "nothing changed" are different claims,
    and only one of them is true of an old run.
    """
    survey = manifest.get("survey")
    provenance = survey.get("provenance") if isinstance(survey, dict) else None
    if not isinstance(provenance, dict):
        return []

    rows: list[tuple[str, str]] = []
    models = " + ".join(
        str(manifest[key])
        for key in ("segmentation_model", "mapping_backend")
        if manifest.get(key) and manifest.get(key) != "__skip__"
    )
    if models:
        rows.append(("Models", models))

    taxonomy = provenance.get("taxonomy_version")
    taxonomy_hash = provenance.get("taxonomy_hash")
    if taxonomy:
        # The hash is what actually pins the grouping; the version is what a
        # person can say out loud. Both, because a version can be edited in
        # place and the hash is what would catch it.
        label = str(taxonomy)
        if taxonomy_hash:
            label += f" · #{str(taxonomy_hash)[:8]}"
        rows.append(("Taxonomy", label))

    config = provenance.get("config")
    if isinstance(config, dict):
        rows.append(("Settings", _settings_line(config)))

    versions = [
        v for v in (manifest.get("deepreefmap_version"), provenance.get("gui_version")) if v
    ]
    if versions:
        rows.append(("Version", " · ".join(str(v) for v in versions)))
    return rows


def _settings_line(config: dict) -> str:
    """The preset a run used, and how far it strayed from it."""
    from deepreefmap_gui.survey.preset import describe_keys

    name = str(config.get("preset_name") or "Unnamed")
    deviations = config.get("deviations")
    if isinstance(deviations, dict) and deviations:
        return f"{name}, changed: {describe_keys(list(deviations))}"
    return f"{name}, as standard"


def summarise_run_provenance(run_dir_name: str, out_root: str) -> str:
    """"coralscapes-vit-b-dpt + loger_star, taxonomy v1" for one run, or "".

    Short enough to sit on one line beside a cover figure, and specific enough
    that two runs made differently do not read as the same measurement. Empty
    when the manifest is unreadable or predates the provenance block.
    """
    import json

    path = Path(out_root).expanduser() / run_dir_name / "run_manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(manifest, dict):
        return ""
    rows = dict(provenance_rows(manifest))
    parts = [rows[key] for key in ("Models", "Taxonomy") if rows.get(key)]
    if not parts:
        return ""
    # The taxonomy hash pins the grouping but is noise in a sentence; the
    # version is the part a person can compare at a glance.
    return f"{parts[0]}, taxonomy {parts[-1].split(' · ')[0]}" if len(parts) > 1 else parts[0]
