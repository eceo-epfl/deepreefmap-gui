"""Reconcile run folders on disk with the survey database for browsing.

The filesystem is the source of truth for which runs exist; the database is
the source of truth for how they relate to transects. Manifests are never
rewritten here: they snapshot what the run believed when it executed, and
rebuild_from_scan depends on that staying intact.
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from deepreefmap_gui.io.atomic import atomic_write_json
from deepreefmap_gui.survey.models.run_record import RunRecord
from deepreefmap_gui.survey.models.survey_batch import SurveyBatch
from deepreefmap_gui.survey.models.transect import Transect
from deepreefmap_gui.survey.models.transect_pass import TransectPass
from deepreefmap_gui.survey.models.video_asset import VideoAsset
from deepreefmap_gui.survey.statuses import (
    CLIP_FAILED,
    CLIP_PENDING,
    CLIP_PROCESSED,
    CLIP_UNPROCESSED,
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
    OUTCOME_UNFINISHED,
    status_outcome,
)
from deepreefmap_gui.survey.store import SurveyStore

logger = logging.getLogger(__name__)

UNASSIGNED_TITLE = "Not assigned yet"

# Runs from before sessions were recorded, or copied in from elsewhere without
# a manifest that names one. Not an error: they process and compare like any
# other, they just cannot be read as a day's work.
UNFILED_SESSION_TITLE = "No session recorded"

# Re-exported from statuses.py under the names the browser uses for them.
RUN_SUCCEEDED, RUN_FAILED, RUN_UNFINISHED = (
    OUTCOME_SUCCEEDED,
    OUTCOME_FAILED,
    OUTCOME_UNFINISHED,
)
VIDEO_UNPROCESSED, VIDEO_PENDING, VIDEO_FAILED, VIDEO_PROCESSED = (
    CLIP_UNPROCESSED,
    CLIP_PENDING,
    CLIP_FAILED,
    CLIP_PROCESSED,
)


def entry_status(entry: RunEntry) -> str:
    """The run's own status. A directory holding a manifest is a finished run."""
    return "succeeded" if not entry.incomplete else entry.status_label


def entry_outcome(entry: RunEntry) -> str:
    """Which filter bucket a run falls into."""
    return status_outcome(entry_status(entry))


@dataclass(slots=True)
class RunEntry:
    """One run directory plus everything known about it from manifest and database."""

    run_dir: Path
    dir_name: str
    manifest: dict
    display_name: str
    sort_key: float
    video_hashes: list[str]
    video_name: str | None
    begin_s: float | None
    end_s: float | None
    duration_s: float | None
    points: int | None
    manifest_run_id: uuid.UUID | None
    manifest_pass_id: uuid.UUID | None
    manifest_transect_id: uuid.UUID | None
    manifest_transect_name: str | None
    manifest_direction: str | None
    manifest_batch_id: uuid.UUID | None = None
    manifest_batch_name: str | None = None
    db_run: RunRecord | None = None
    db_pass: TransectPass | None = None
    db_transect_name: str | None = None
    moved_from: str | None = None
    size_bytes: int | None = None
    # A run directory that never wrote a manifest: crashed, cancelled, or still
    # in flight. Carries an empty manifest, so the fields above stay at defaults.
    incomplete: bool = False

    @property
    def status_label(self) -> str:
        """What to show for an incomplete run: the recorded status, or a generic
        marker when the database has no row for it."""
        if self.db_run is not None:
            return self.db_run.status
        return "incomplete"

    @property
    def transect_id(self) -> uuid.UUID | None:
        if self.db_pass is not None:
            return self.db_pass.transect_id
        return self.manifest_transect_id

    @property
    def session_id(self) -> uuid.UUID | None:
        """The session this run was queued in, database first then manifest.

        The manifest is the fallback rather than the source: a run folder copied
        off another machine carries its session in the manifest and has no row
        here until rebuild_from_scan writes one.
        """
        if self.db_pass is not None and self.db_pass.batch_id is not None:
            return self.db_pass.batch_id
        return self.manifest_batch_id

    @property
    def transect_name(self) -> str | None:
        return self.db_transect_name or self.manifest_transect_name

    @property
    def direction(self) -> str | None:
        if self.db_pass is not None:
            return self.db_pass.direction
        return self.manifest_direction


@dataclass(slots=True)
class GroupStats:
    run_count: int
    total_bytes: int | None
    duration_range: tuple[float, float] | None
    point_range: tuple[int, int] | None


@dataclass(slots=True)
class FacetGroup:
    key: tuple
    title: str
    entries: list[RunEntry] = field(default_factory=list)
    children: list[FacetGroup] = field(default_factory=list)

    def all_entries(self) -> list[RunEntry]:
        collected = list(self.entries)
        for child in self.children:
            collected.extend(child.all_entries())
        return collected


def parse_run_timestamp(ts: object) -> datetime | None:
    """An ISO-8601 run timestamp as an aware datetime, or None if unusable.

    A timestamp with no offset is read as UTC, which is what writes it: the
    pipeline uses datetime.now(timezone.utc) and the survey store records the
    same value. Left alone, fromisoformat gives a naive datetime whose
    .timestamp() assumes local time, so an older naive manifest sorts against a
    newer offset-carrying one by the local UTC offset -- hours out, and in the
    wrong direction depending on which side of UTC the machine sits.
    """
    if not isinstance(ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def run_sort_key(manifest: dict, mtime: float) -> float:
    """Prefer the recorded run timestamp; fall back to the manifest file mtime."""
    parsed = parse_run_timestamp(manifest.get("run_timestamp"))
    return parsed.timestamp() if parsed is not None else mtime


def run_duration_s(manifest: dict) -> float | None:
    """Total wall-clock run time; sums stage_durations for pre-field manifests."""
    v = manifest.get("run_duration_s")
    if isinstance(v, (int, float)) and v > 0:
        return float(v)
    stages = manifest.get("stage_durations") or {}
    total = sum(s for s in stages.values() if isinstance(s, (int, float)))
    return total if total > 0 else None


def scan_out_root(out_root: Path) -> list[RunEntry]:
    """Every child directory with a run manifest, newest first."""
    entries: list[RunEntry] = []
    if not out_root.is_dir():
        return entries
    for child in out_root.iterdir():
        manifest_path = child / "run_manifest.json"
        if not (child.is_dir() and manifest_path.exists()):
            continue
        manifest: dict = {}
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Unreadable manifest in %s", child)
        entries.append(_entry_from_manifest(child, manifest, manifest_path.stat().st_mtime))
    entries.sort(key=lambda e: e.sort_key, reverse=True)
    return entries


def _entry_from_manifest(run_dir: Path, manifest: dict, mtime: float) -> RunEntry:
    name = manifest.get("name")
    # The folder only when it adds something. A run left unnamed takes its
    # timestamp as a name, so spelling both printed "20260716-135235
    # (20260716-135235)" and spent half the table's widest column saying it
    # twice.
    display = f"{name}  ({run_dir.name})" if name and name != run_dir.name else run_dir.name
    videos = manifest.get("input_videos") or []
    survey = _dict_or_empty(manifest.get("survey"))
    pass_block = _dict_or_empty(survey.get("pass"))
    transect_block = _dict_or_empty(survey.get("transect"))
    points = manifest.get("semantic_reference_points") or manifest.get("metric_points")
    return RunEntry(
        run_dir=run_dir,
        dir_name=run_dir.name,
        manifest=manifest,
        display_name=display,
        sort_key=run_sort_key(manifest, mtime),
        video_hashes=[h for h in (manifest.get("video_hashes") or []) if h],
        video_name=Path(videos[0]).name if videos else None,
        begin_s=_as_float(manifest.get("begin_s")),
        end_s=_as_float(manifest.get("end_s")),
        duration_s=run_duration_s(manifest),
        points=int(points) if isinstance(points, (int, float)) else None,
        manifest_run_id=_as_uuid(survey.get("run_id")),
        manifest_pass_id=_as_uuid(pass_block.get("id")),
        manifest_transect_id=_as_uuid(transect_block.get("id")),
        manifest_transect_name=transect_block.get("name"),
        manifest_direction=pass_block.get("direction"),
        manifest_batch_id=_as_uuid(survey.get("batch_id")),
        manifest_batch_name=survey.get("batch_name"),
    )


def scan_incomplete_runs(
    out_root: Path, store: SurveyStore | None, known: set[str]
) -> list[RunEntry]:
    """Child directories that look like runs but never wrote a manifest.

    ``scan_out_root`` skips these, which hides crashed or interrupted runs from
    the browser. A folder counts as an incomplete run when the database still has
    a run record for it (the survey batch writes one before the pipeline starts)
    or when it holds a ``run.log`` a run left behind. ``known`` names the folders
    already surfaced as complete runs, so nothing is listed twice.
    """
    entries: list[RunEntry] = []
    if not out_root.is_dir():
        return entries
    records = {r.run_dir_name for r in store.list_runs()} if store is not None else set()
    for child in out_root.iterdir():
        if not child.is_dir() or child.name in known:
            continue
        if (child / "run_manifest.json").exists():
            continue
        if child.name not in records and not (child / "run.log").exists():
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            mtime = 0.0
        entries.append(_incomplete_entry(child, mtime))
    return entries


def _incomplete_entry(run_dir: Path, mtime: float) -> RunEntry:
    return RunEntry(
        run_dir=run_dir,
        dir_name=run_dir.name,
        manifest={},
        display_name=run_dir.name,
        sort_key=mtime,
        video_hashes=[],
        video_name=None,
        begin_s=None,
        end_s=None,
        duration_s=None,
        points=None,
        manifest_run_id=None,
        manifest_pass_id=None,
        manifest_transect_id=None,
        manifest_transect_name=None,
        manifest_direction=None,
        incomplete=True,
    )


def _dict_or_empty(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _as_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _as_uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def reconcile(entries: list[RunEntry], store: SurveyStore) -> None:
    """Attach database rows to scanned entries. The database wins over the
    manifest snapshot; a disagreement records where the run was originally filed."""
    runs = {r.run_dir_name: r for r in store.list_runs()}
    passes = {p.id: p for p in store.list_passes()}
    transects = {t.id: t for t in store.list_transects()}
    for entry in entries:
        run = runs.get(entry.dir_name)
        if run is None:
            continue
        entry.db_run = run
        pass_ = passes.get(run.pass_id)
        if pass_ is None:
            continue
        entry.db_pass = pass_
        transect = (
            transects.get(pass_.transect_id) if pass_.transect_id is not None else None
        )
        entry.db_transect_name = transect.name if transect is not None else None
        if (
            entry.manifest_transect_name
            and entry.db_transect_name
            and entry.db_transect_name != entry.manifest_transect_name
        ):
            entry.moved_from = entry.manifest_transect_name


def group_key(entry: RunEntry) -> tuple:
    """Runs of the same footage and trim share a key, so reruns stay together."""
    if entry.db_pass is not None:
        return ("pass", str(entry.db_pass.id))
    if entry.manifest_pass_id is not None:
        return ("pass", str(entry.manifest_pass_id))
    video = entry.video_hashes[0] if entry.video_hashes else None
    return ("adhoc", video, _rounded(entry.begin_s), _rounded(entry.end_s))


def _rounded(value: float | None) -> float | None:
    # Two decimals, matching reproducibility_groups in survey/analysis.py.
    return None if value is None else round(value, 2)


def group_stats(entries: list[RunEntry]) -> GroupStats:
    sizes = [e.size_bytes for e in entries if e.size_bytes is not None]
    durations = [e.duration_s for e in entries if e.duration_s is not None]
    points = [e.points for e in entries if e.points is not None]
    return GroupStats(
        run_count=len(entries),
        total_bytes=sum(sizes) if sizes else None,
        duration_range=(min(durations), max(durations)) if durations else None,
        point_range=(min(points), max(points)) if points else None,
    )


def transects_facet(
    entries: list[RunEntry], transects: Iterable[Transect] = ()
) -> list[FacetGroup]:
    """Transect groups over pass groups, with unassigned runs surfaced first.

    ``transects`` may list known transects so ones without runs still appear.
    """
    by_transect: dict[str, FacetGroup] = {}
    unassigned = FacetGroup(key=("unassigned",), title=UNASSIGNED_TITLE)
    for transect in transects:
        by_transect[transect.name] = FacetGroup(key=("transect", str(transect.id)), title=transect.name)
    for entry in entries:
        name = entry.transect_name
        if name is None:
            unassigned.entries.append(entry)
            continue
        group = by_transect.get(name)
        if group is None:
            tid = str(entry.transect_id) if entry.transect_id else name
            group = by_transect[name] = FacetGroup(key=("transect", tid), title=name)
        _child_for(group, group_key(entry), _pass_title(entry)).entries.append(entry)
    groups = [g for name, g in sorted(by_transect.items())]
    if unassigned.entries:
        groups.insert(0, unassigned)
    return groups


def session_group_key(batch_id: uuid.UUID | None) -> tuple:
    """The facet key a run is filed under by session.

    Same two-sided-join contract as video_group_key: a run reaches this from its
    pass or from its manifest, and a session reaches it from the store, so both
    have to arrive at the same key or one session would appear twice.
    """
    return ("unfiled_session",) if batch_id is None else ("session", str(batch_id))


def sessions_facet(
    entries: list[RunEntry], batches: Iterable[SurveyBatch] = ()
) -> list[FacetGroup]:
    """Session groups over pass groups, newest session first.

    The session is the only container that spans transects: everything else here
    groups by one line, one clip or one pass, so a day's work had no way to be
    read as a day's work. It is recorded on every run already, in the manifest
    and on the pass, and this is what finally shows it.

    ``batches`` may list known sessions so one whose runs have all been deleted
    still appears, the same courtesy transects_facet extends to a transect with
    no runs.
    """
    known = {b.id: b for b in batches}
    by_session: dict[tuple, FacetGroup] = {}
    unfiled = FacetGroup(key=session_group_key(None), title=UNFILED_SESSION_TITLE)
    for batch in known.values():
        by_session[session_group_key(batch.id)] = FacetGroup(
            key=session_group_key(batch.id), title=batch.name
        )
    for entry in entries:
        batch_id = entry.session_id
        if batch_id is None:
            unfiled.entries.append(entry)
            continue
        key = session_group_key(batch_id)
        group = by_session.get(key)
        if group is None:
            named = known.get(batch_id)
            title = named.name if named is not None else entry.manifest_batch_name
            group = by_session[key] = FacetGroup(key=key, title=title or str(batch_id))
        _child_for(group, group_key(entry), _pass_title(entry)).entries.append(entry)
    # Newest first: a session is a day's work, and the one you want is almost
    # always the one you just did. Sessions with no runs sort by name alone,
    # which puts a freshly created one at the top under the date default.
    groups = sorted(by_session.values(), key=_session_sort_key, reverse=True)
    if unfiled.entries:
        groups.insert(0, unfiled)
    return groups


def _session_sort_key(group: FacetGroup) -> tuple:
    entries = group.all_entries()
    return (max(e.sort_key for e in entries) if entries else 0.0, group.title)


def session_summary(group: FacetGroup) -> str:
    """"6 runs · 3 transects", the two counts that say what a session covered."""
    entries = group.all_entries()
    transects = {e.transect_name for e in entries if e.transect_name}
    parts = [_plural(len(entries), "run")]
    if transects:
        parts.append(_plural(len(transects), "transect"))
    return " · ".join(parts)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


# The bucket for runs that name no video at all: no hash, and no file name in
# the manifest. A run that crashed before writing one lands here, so on a machine
# that has seen a few failures this is most of them.
NO_VIDEO_TITLE = "No video recorded"

# The window label for a run that took the whole clip. Named because
# _fold_lone_window keys off it: a single such child is the one window level
# that tells you nothing its parent has not already said.
_WHOLE_VIDEO = "whole video"


def video_group_key(video_hash: str | None, fallback: str | None) -> tuple:
    """The facet key a clip is filed under, from either side of the join.

    A run knows its video by hash; the library knows the clip itself. Both have
    to arrive at the same key or an imported clip and the runs cut from it would
    appear as two separate groups.

    The fallback is the video's *file name* on both sides. It is a weak identity
    (every card's first clip is GX010001.MP4), which is why hashing is repaired
    rather than the fallback relied on.

    With neither, the run is anonymous, and every anonymous run is equally so:
    they share the one `None` key rather than each landing in a group of its own.
    Keying those on something unique per run -- the run directory, say -- is what
    turned twelve crashed runs into twelve identically titled groups of one.
    """
    return ("video", video_hash or fallback or None)


def videos_facet(
    entries: list[RunEntry], library: Iterable[VideoLibraryEntry] = ()
) -> list[FacetGroup]:
    """Video-file groups over time-window groups. One file can hold several
    transect passes, so the window level is what keeps them apart.

    ``library`` lists every clip the survey has imported, so a clip that has
    never been processed still gets a group. Without it the facet can only show
    footage that already produced a run -- exactly the wrong half when the
    question being asked is what still needs doing.
    """
    by_video: dict[tuple, FacetGroup] = {}
    # Kept beside the groups rather than spelled into the title as they are
    # built: whether a checksum is worth showing depends on the other groups,
    # which are not all known until the loops below have run.
    hashes: dict[tuple, str | None] = {}
    for entry in entries:
        video_hash = entry.video_hashes[0] if entry.video_hashes else None
        key = video_group_key(video_hash, entry.video_name)
        group = by_video.get(key)
        if group is None:
            group = by_video[key] = FacetGroup(
                key=key, title=entry.video_name or NO_VIDEO_TITLE
            )
            hashes[key] = video_hash
        _child_for(group, group_key(entry), _window_title(entry)).entries.append(entry)
    for clip in library:
        key = video_group_key(clip.video.hash, clip.video.file_name)
        if key in by_video:
            continue
        by_video[key] = FacetGroup(key=key, title=clip.video.file_name)
        hashes[key] = clip.video.hash
    for group in by_video.values():
        _fold_lone_window(group)
    _name_clips_apart(by_video.values(), hashes)
    return sorted(by_video.values(), key=lambda g: g.title)


def _fold_lone_window(group: FacetGroup) -> None:
    """Drop a window level that restates the clip row above it.

    A clip processed once, whole and unassigned, gets a single child reading
    "whole video" beneath a parent already counting one run. That is a tree row,
    an indent and a shortened parent title spent saying nothing new.

    Clips holding several passes keep their children, and so does a lone pass
    that names a transect or a time window: there the window is exactly what
    tells the runs apart.
    """
    if len(group.children) != 1:
        return
    child = group.children[0]
    if child.title != _WHOLE_VIDEO or child.children:
        return
    group.entries.extend(child.entries)
    group.children.clear()


def _name_clips_apart(
    groups: Iterable[FacetGroup], hashes: dict[tuple, str | None]
) -> None:
    """Spell the checksum into a clip title only where the file name is ambiguous.

    Every card's first clip is GX010001.MP4, so the hash is what tells two of
    them apart -- but only while both are on screen. Carried on every row it
    costs ~70px of a rail that is already eliding the names the hash exists to
    disambiguate, which is the opposite of the point.
    """
    groups = list(groups)
    shared = Counter(group.title for group in groups)
    for group in groups:
        digest = hashes.get(group.key)
        if digest and shared[group.title] > 1:
            group.title += f" · #{digest[:8]}"


# Whether the file a clip names is still where the survey last saw it. Checked
# off the paint path, so "unknown" is the honest answer until it has been: an
# entry that assumed "linked" would show a link on a clip that is not there.
LINK_LINKED, LINK_MISSING, LINK_UNKNOWN = "linked", "missing", "unknown"


@dataclass(slots=True)
class VideoLibraryEntry:
    """One imported clip, with how much of the survey hangs off it.

    A video the browser can show even with no runs: an orphan is a clip imported
    but never turned into a pass, which the run-oriented facets cannot surface.
    """

    video: VideoAsset
    pass_count: int
    run_count: int
    # The passes cut from this clip and every run they produced, so a detail
    # pane can show what became of the footage without re-querying per row.
    passes: list[TransectPass] = field(default_factory=list)
    runs: list[RunRecord] = field(default_factory=list)
    # Whether the file is still there. Filled in by a background pass rather
    # than at construction, because this is built while the rail is being drawn
    # and a stat per clip on a sleeping external drive is not a paint-time cost.
    link_state: str = LINK_UNKNOWN

    @property
    def orphan(self) -> bool:
        return self.pass_count == 0

    @property
    def last_run_at(self) -> str | None:
        """When this clip was last processed, whatever the outcome."""
        return max((run.created_at for run in self.runs), default=None)


    @property
    def outcome(self) -> str:
        """Where this clip stands: unprocessed, failing, or done.

        A clip is only ``processed`` once every pass cut from it has a run that
        succeeded; anything short of that is work still owed.
        """
        if self.orphan:
            return VIDEO_UNPROCESSED
        if any(run.status == "failed" for run in self.runs):
            return VIDEO_FAILED
        succeeded = {run.pass_id for run in self.runs if run.status == "succeeded"}
        if len(succeeded) >= self.pass_count:
            return VIDEO_PROCESSED
        return VIDEO_PENDING


def resolve_link_states(entries: Iterable[VideoLibraryEntry]) -> dict[str, str]:
    """Stat each clip once and say whether it is still there.

    Keyed by path rather than by clip id so the answer survives the library
    being rebuilt, which happens on every refresh. Blocking, and meant for a
    worker thread: a missing network mount can take seconds to admit it.
    """
    states: dict[str, str] = {}
    for entry in entries:
        path = entry.video.path
        if path in states:
            continue
        if not path:
            states[path] = LINK_UNKNOWN
            continue
        try:
            states[path] = LINK_LINKED if Path(path).is_file() else LINK_MISSING
        except OSError:
            # A path that cannot even be asked about is not the same as one that
            # answered no, and offering to relocate a clip on a mount that is
            # merely slow would be wrong.
            states[path] = LINK_UNKNOWN
    return states


def video_library(
    videos: Iterable[VideoAsset],
    passes: Iterable[TransectPass],
    runs: Iterable[RunRecord] = (),
) -> list[VideoLibraryEntry]:
    """Every imported clip with its pass and run counts, orphans included.

    Orphan detection is the point: a clip referenced by no pass never appears in
    the transect or video facets, which only know clips that produced a run.
    """
    passes = list(passes)
    # Every chapter of a pass counts, or the second half of a swim the camera
    # split at 4 GB would read as a clip nothing has ever used.
    passes_per_video: Counter[uuid.UUID] = Counter()
    videos_by_pass: dict[uuid.UUID, list[uuid.UUID]] = {}
    for p in passes:
        videos_by_pass[p.id] = p.video_ids()
        passes_per_video.update(videos_by_pass[p.id])
    runs_per_video: Counter[uuid.UUID] = Counter()
    passes_by_video: dict[uuid.UUID, list[TransectPass]] = {}
    runs_by_video: dict[uuid.UUID, list[RunRecord]] = {}
    for p in passes:
        for video_id in videos_by_pass[p.id]:
            passes_by_video.setdefault(video_id, []).append(p)
    for run in runs:
        for video_id in videos_by_pass.get(run.pass_id, []):
            runs_per_video[video_id] += 1
            runs_by_video.setdefault(video_id, []).append(run)
    return [
        VideoLibraryEntry(
            video=video,
            pass_count=passes_per_video.get(video.id, 0),
            run_count=runs_per_video.get(video.id, 0),
            passes=passes_by_video.get(video.id, []),
            runs=runs_by_video.get(video.id, []),
        )
        for video in videos
    ]


def _child_for(parent: FacetGroup, key: tuple, title: str) -> FacetGroup:
    for child in parent.children:
        if child.key == key:
            return child
    child = FacetGroup(key=key, title=title)
    parent.children.append(child)
    return child


def _window_title(entry: RunEntry) -> str:
    if entry.begin_s is None and entry.end_s is None:
        label = _WHOLE_VIDEO
    else:
        begin = f"{entry.begin_s:g}" if entry.begin_s is not None else "0"
        end = f"{entry.end_s:g}" if entry.end_s is not None else "end"
        label = f"{begin}–{end} s"
    name = entry.transect_name
    return f"{label} · {name}" if name else label


def _pass_title(entry: RunEntry) -> str:
    parts = [entry.video_name or "unknown video", _window_title(entry).split(" · ")[0]]
    if entry.direction:
        parts.append(entry.direction)
    return " · ".join(parts)


def dir_size_bytes(run_dir: Path) -> int:
    total = 0
    for p in run_dir.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def rename_run(run_dir: Path, new_name: str) -> dict:
    """Set the display name in the run manifest, atomically."""
    manifest_path = run_dir / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["name"] = new_name.strip()
    atomic_write_json(manifest_path, manifest)
    return manifest


def delete_run(out_root: Path, run_dir: Path, store: SurveyStore | None) -> None:
    """Remove a finished run directory and its database row. Only direct children
    of the output root that carry a manifest are ever deleted."""
    if not (run_dir.resolve() / "run_manifest.json").exists():
        raise ValueError(f"{run_dir} has no run manifest")
    delete_run_dir(out_root, run_dir, store)


def delete_run_dir(out_root: Path, run_dir: Path, store: SurveyStore | None) -> None:
    """Remove a run directory that may have no manifest (a crashed run) plus any
    database row. The direct-child guard still holds: only folders sitting
    directly under the output root are ever removed."""
    resolved = run_dir.resolve()
    if resolved.parent != out_root.resolve():
        raise ValueError(f"{run_dir} is not directly under {out_root}")
    shutil.rmtree(resolved)
    if store is not None:
        run = store.run_by_dir_name(resolved.name)
        if run is not None:
            store.delete_run(run.id)


def assign_to_transect(
    store: SurveyStore,
    entries: list[RunEntry],
    transect_id: uuid.UUID,
    direction: str = "forward",
) -> None:
    """File runs under a transect. Runs with a database pass are moved (sibling
    reruns of the pass move with them); runs the database has never seen are
    adopted with manifest ids kept, so a later rebuild_from_scan stays idempotent."""
    updated: set[uuid.UUID] = set()
    orphans: dict[tuple, list[RunEntry]] = {}
    for entry in entries:
        if entry.db_pass is not None:
            if entry.db_pass.transect_id != transect_id and entry.db_pass.id not in updated:
                entry.db_pass.transect_id = transect_id
                store.update_pass(entry.db_pass)
                updated.add(entry.db_pass.id)
        else:
            orphans.setdefault(group_key(entry), []).append(entry)
    for group in orphans.values():
        _adopt_group(store, group, transect_id, direction)


def _adopt_group(
    store: SurveyStore,
    group: list[RunEntry],
    transect_id: uuid.UUID,
    direction: str,
) -> None:
    first = group[0]
    manifest = first.manifest
    videos = manifest.get("input_videos") or [""]
    hashes = manifest.get("video_hashes") or [None]
    sizes = manifest.get("video_sizes") or [None]
    mtimes = manifest.get("video_mtimes") or [None]
    video = store.upsert_video(VideoAsset(
        file_name=Path(videos[0]).name or "unknown",
        path=videos[0],
        hash=hashes[0],
        size_bytes=sizes[0],
        mtime=mtimes[0],
    ))
    begin, end = _pass_window(first)
    pass_ = store.get_pass(first.manifest_pass_id) if first.manifest_pass_id else None
    if pass_ is None:
        pass_ = TransectPass(
            id=first.manifest_pass_id or uuid.uuid4(),
            transect_id=transect_id,
            video_id=video.id,
            begin_s=begin,
            end_s=end,
            direction=first.manifest_direction or direction,
        )
        store.add_pass(pass_)
    elif pass_.transect_id != transect_id:
        pass_.transect_id = transect_id
        store.update_pass(pass_)
    for entry in group:
        if store.run_by_dir_name(entry.dir_name) is None:
            run_id = entry.manifest_run_id
            if run_id is not None and store.get_run(run_id) is not None:
                run_id = None
            store.add_run(RunRecord(
                id=run_id or uuid.uuid4(),
                pass_id=pass_.id,
                run_dir_name=entry.dir_name,
                status="succeeded",
                started_at=entry.manifest.get("run_timestamp"),
            ))


def _pass_window(entry: RunEntry) -> tuple[float, float]:
    """A pass needs a concrete time range; untrimmed runs derive it from the
    processed frame count."""
    begin = entry.begin_s or 0.0
    if entry.end_s is not None and entry.end_s > begin:
        return begin, entry.end_s
    frames = entry.manifest.get("frames_processed")
    fps = entry.manifest.get("fps")
    if isinstance(frames, (int, float)) and isinstance(fps, (int, float)) and fps > 0:
        return begin, begin + float(frames) / float(fps)
    raise ValueError(f"Cannot work out the video time range for {entry.dir_name}")
