"""Pull, push, and the conflict reports both produce.

Pull runs before push. It narrows the window in which this laptop is working from
a stale copy, and it means a row two people edited is discovered before the local
edit is offered rather than after it was refused.

Both halves are resumable. The pull cursor is written after each page that landed,
and a section's push watermark moves only once the registry has accounted for
every row that section sent, so an interrupted sync repeats work rather than
losing it.

No Qt here, deliberately: the surface runs this on a worker thread and marshals
the reports back through signals.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from deepreefmap.config.classes import ClassConfig

from deepreefmap_gui.survey.analysis import collate_long_format
from deepreefmap_gui.survey.models.notification import SURVEY, WARNING
from deepreefmap_gui.survey.store import SYNC_SECTIONS, ApplyResult, SurveyStore
from deepreefmap_gui.sync import wire
from deepreefmap_gui.sync.client import PULL_LIMIT

logger = logging.getLogger(__name__)

# Machine-local sync position. The cursor is the registry's own monotonic
# server_seq; a watermark is the newest updated_at that section has had accepted.
CURSOR_KEY = "sync.cursor"
WATERMARK_PREFIX = "sync.push_watermark."

# A local edit the registry refused, and a local edit its copy replaced. Two
# fingerprints because they are two outcomes: one edit did not land and the other
# was overwritten by somebody else's.
CONFLICT_DISCARDED = "sync.local_edit_discarded"
CONFLICT_OVERWRITTEN = "sync.local_edit_overwritten"
# A pass the registry holds with no videos, which this side cannot represent.
PASS_WITHOUT_VIDEOS = "sync.pass_without_videos"
# A run whose pass the registry never sent, so it has no parent to hang off.
RUN_WITHOUT_PASS = "sync.run_without_pass"


class SyncTransport(Protocol):
    """The two calls the engine makes on a registry client.

    Enrolment, schema and status belong to the client and to whatever drives
    onboarding, not to a sync run.
    """

    def pull(self, since: int | None = ..., limit: int = ...) -> Mapping[str, Any]: ...

    def push(self, sections: Mapping[str, Sequence[Mapping[str, Any]]]) -> Mapping[str, Any]: ...


class ConflictSink(Protocol):
    """Where a conflict report goes.

    `NotificationCenter` is one. A surface running this on a worker thread cannot
    touch the centre directly, so it passes a bridge that emits instead.
    """

    def post(
        self,
        *,
        fingerprint: str,
        title: str,
        body: str = ...,
        severity: str = ...,
        scope: str = ...,
    ) -> Any: ...


@dataclass(frozen=True)
class SectionPush:
    """What the registry did with one section of a document."""

    received: int
    applied: int
    # Narrowed to rows edited here since the last accepted push: see _our_edits.
    skipped: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True)
class PushReport:
    """One push: what the registry took, what it already had newer, where we are."""

    sections: dict[str, SectionPush] = field(default_factory=dict)
    watermarks: dict[str, str] = field(default_factory=dict)
    cursor: int | None = None

    @property
    def sent(self) -> int:
        return sum(section.received for section in self.sections.values())

    @property
    def applied(self) -> int:
        return sum(section.applied for section in self.sections.values())

    @property
    def skipped(self) -> list[uuid.UUID]:
        """Rows the registry already held a newer copy of: information, not an error."""
        return [row_id for section in self.sections.values() for row_id in section.skipped]


@dataclass(frozen=True)
class PullReport:
    """One pull: what landed, and every place the two copies disagreed."""

    pages: int = 0
    cursor: int | None = None
    sections: dict[str, ApplyResult] = field(default_factory=dict)
    # Rows this device had edited since its last push, whose registry copy won.
    overwritten: tuple[uuid.UUID, ...] = ()
    # Rows the registry sent that lost to the copy held here.
    kept: tuple[uuid.UUID, ...] = ()
    # Passes the registry holds with no live videos. See _apply_page.
    passes_without_videos: tuple[uuid.UUID, ...] = ()
    # Runs whose pass never arrived, so they had nothing to hang off. See _hold_run.
    runs_without_passes: tuple[uuid.UUID, ...] = ()

    @property
    def applied(self) -> int:
        return sum(result.applied for result in self.sections.values())


@dataclass(frozen=True)
class SyncReport:
    pull: PullReport
    push: PushReport


class SyncEngine:
    """Bidirectional metadata sync for one survey database.

    ``out_root`` is where run directories live, since a run's provenance and its
    cover are read from its manifest and its benthic_cover.json rather than from
    the database. ``classes_config`` is what rolls raw class counts up into the
    groups a cover row names; without one, cover is left out of the document and
    everything else still syncs.
    """

    def __init__(
        self,
        store: SurveyStore,
        client: SyncTransport,
        out_root: Path | None = None,
        classes_config: ClassConfig | None = None,
        notifications: ConflictSink | None = None,
        limit: int = PULL_LIMIT,
    ) -> None:
        self._store = store
        self._client = client
        self._out_root = out_root if out_root is not None else store.path.parent
        self._classes_config = classes_config
        self._notify = notifications
        self._limit = limit

    # --- Position ---

    def cursor(self) -> int | None:
        """The last registry position this device has pulled up to."""
        stored = self._store.sync_state(CURSOR_KEY)
        if stored is None:
            return None
        try:
            return int(stored)
        except ValueError:
            logger.warning("Ignoring unreadable pull cursor %r", stored)
            return None

    def watermark(self, section: str) -> str | None:
        """The newest ``updated_at`` this section has had accepted, or None."""
        return self._store.sync_state(f"{WATERMARK_PREFIX}{section}")

    # --- Sync ---

    def sync(self) -> SyncReport:
        """Pull, then push. See the module docstring for why that order."""
        pulled = self.pull()
        return SyncReport(pull=pulled, push=self.push())

    # --- Pull ---

    def pull(self) -> PullReport:
        """Every page the registry has for us, landing each before asking for more.

        The cursor is written after a page lands, so an interrupted pull resumes
        at the page it failed on. Re-landing a page is harmless: last-write-wins
        makes an equal stamp a skip.
        """
        cursor = self.cursor()
        pages = 0
        sections: dict[str, ApplyResult] = {}
        overwritten: list[uuid.UUID] = []
        kept: list[uuid.UUID] = []
        # Rows that cannot land yet, by section. A pass whose chapters have not
        # arrived is the common one, since its pass_video rows sort after it and so
        # often come a page later; its runs wait with it or they would break the
        # foreign key.
        held: dict[str, dict[str, dict[str, Any]]] = {"passes": {}, "runs": {}}
        while True:
            page = self._client.pull(since=cursor, limit=self._limit)
            landed, page_overwritten, page_kept = self._apply_page(page, held)
            for name, result in landed.items():
                sections[name] = _merge(sections.get(name), result)
            overwritten.extend(page_overwritten)
            kept.extend(page_kept)
            pages += 1
            advanced = _page_cursor(page)
            if advanced is not None and (cursor is None or advanced > cursor):
                cursor = advanced
                self._store.set_sync_state(CURSOR_KEY, str(cursor))
            elif page.get("has_more"):
                logger.warning("Registry reported more rows at cursor %s and did not advance", cursor)
                break
            if not page.get("has_more"):
                break
        report = PullReport(
            pages=pages,
            cursor=cursor,
            sections=sections,
            overwritten=tuple(overwritten),
            kept=tuple(kept),
            passes_without_videos=_held_ids(held["passes"]),
            runs_without_passes=_held_ids(held["runs"]),
        )
        self._report_pull_conflicts(report)
        return report

    def _apply_page(
        self,
        page: Mapping[str, Any],
        held: dict[str, dict[str, dict[str, Any]]],
    ) -> tuple[dict[str, ApplyResult], list[uuid.UUID], list[uuid.UUID]]:
        """Land one page, in foreign-key order, and say where the copies disagreed."""
        raw = page.get("sections") or {}
        incoming = {
            name: wire.rows_from_wire(raw[name]) for name in SYNC_SECTIONS if raw.get(name)
        }
        # Cover lives in the run directories, so a pulled cover row has nowhere to
        # land. It is the registry's own aggregate of what this device produced.
        chapters = wire.rows_from_wire(raw.get(wire.PASS_VIDEOS) or [])
        folded, unattached = wire.fold_pass_videos(
            [*incoming.get("passes", []), *held["passes"].values()], chapters
        )
        held["passes"].clear()
        ready = []
        for row in folded:
            if "video_id" in row or self._store.holds_id("passes", uuid.UUID(str(row["id"]))):
                ready.append(row)
            else:
                held["passes"][str(row["id"])] = row
        if ready or "passes" in incoming:
            incoming["passes"] = ready
        runs = [*incoming.get("runs", []), *held["runs"].values()]
        held["runs"].clear()
        if runs:
            landing = {str(row["id"]) for row in incoming.get("passes", ())}
            incoming["runs"] = [
                row for row in runs if not self._hold_run(row, landing, held["runs"])
            ]

        landed: dict[str, ApplyResult] = {}
        overwritten: list[uuid.UUID] = []
        kept: list[uuid.UUID] = []
        for name in SYNC_SECTIONS:
            rows = incoming.get(name)
            if not rows:
                continue
            arriving = {uuid.UUID(str(row["id"])) for row in rows}
            pending = self._unpushed_ids(name) & arriving
            result = self._store.apply_from_server(name, rows)
            landed[name] = result
            kept.extend(result.skipped)
            overwritten.extend(sorted(pending - set(result.skipped), key=str))
        self._adopt_chapter_order(unattached)
        return landed, overwritten, kept

    def _hold_run(
        self,
        row: Mapping[str, Any],
        landing: set[str],
        waiting: dict[str, dict[str, Any]],
    ) -> bool:
        """Whether a run has to wait for the pass it belongs to.

        A run whose pass this device neither holds nor is about to insert would fail
        the foreign key and take the whole pull down with it, so it waits for a
        later page instead. Runs are the only section this can happen to: every
        other parent is a row the registry sends whole.
        """
        pass_id = str(row.get("pass_id"))
        if pass_id in landing or self._store.holds_id("passes", uuid.UUID(pass_id)):
            return False
        waiting[str(row["id"])] = dict(row)
        return True

    def _adopt_chapter_order(self, unattached: Mapping[str, list[uuid.UUID]]) -> None:
        """Apply chapter lists whose pass this device already holds.

        Written through ``update_pass`` rather than as a partial row, because the
        chapters are one relationship on this side and two rows on the registry's,
        so there is no incoming stamp that describes only them. The edit is pushed
        back on the next push and skipped there, since the ids are derived.
        """
        for pass_key, video_ids in unattached.items():
            pass_ = self._store.get_pass(uuid.UUID(pass_key))
            if pass_ is None or not video_ids or pass_.video_ids() == video_ids:
                continue
            pass_.video_id = video_ids[0]
            pass_.extra_video_ids = list(video_ids[1:])
            self._store.update_pass(pass_)

    def _unpushed_ids(self, section: str) -> set[uuid.UUID]:
        """Ids edited here since this section was last accepted.

        Strictly after the watermark, not at it: the watermark is the stamp the
        registry accepted, so a row carrying it is the row that was accepted.
        Stamps are second-precision, so an edit made in that same second cannot be
        told from the push, and calling it a conflict would report one on every
        sync of an unedited survey.
        """
        watermark = self.watermark(section)
        return {
            row.id
            for row in self._store.changed_since(section, watermark)
            if watermark is None or row.updated_at > watermark
        }

    # --- Push ---

    def push(self) -> PushReport:
        """Send everything edited here since the last accepted push.

        The document is a closed set: the registry refuses a child whose parent it
        has never seen, so every row travels with its ancestors whether or not they
        changed. A watermark moves only once the registry has accounted for the
        whole section, so a refused document leaves this device exactly where it
        was.
        """
        sections, watermarks = self.build_document()
        if not sections:
            return PushReport()
        edited = {name: self._unpushed_ids(name) for name in SYNC_SECTIONS}
        response = self._client.push(sections)
        outcomes = _push_outcomes(response)
        advanced: dict[str, str] = {}
        for name, stamp in watermarks.items():
            outcome = outcomes.get(name)
            if outcome is None or not _accounted(outcome, len(sections[name])):
                logger.warning("Holding the %s watermark: the registry did not account for it", name)
                continue
            self._store.set_sync_state(f"{WATERMARK_PREFIX}{name}", stamp)
            advanced[name] = stamp
        report = PushReport(
            sections=_our_edits(outcomes, edited),
            watermarks=advanced,
            # The registry's cursor after our own write, reported and never stored:
            # adopting it as the pull cursor would skip every row another device
            # wrote before it.
            cursor=_page_cursor(response),
        )
        self._report_push_conflicts(report)
        return report

    def build_document(self) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
        """The push sections in foreign-key order, and the watermark each would earn.

        A watermark is the newest stamp among the rows that actually changed, not
        among the ancestors dragged in with them: an unchanged parent travelling as
        a companion must not move a section forward.
        """
        changed: dict[str, list[Any]] = {}
        watermarks: dict[str, str] = {}
        for name in SYNC_SECTIONS:
            rows = self._store.changed_since(name, self.watermark(name))
            if rows:
                changed[name] = rows
                watermarks[name] = max(row.updated_at for row in rows)
        closure = self._closure(changed)
        sections: dict[str, list[dict[str, Any]]] = {}
        for name in SYNC_SECTIONS:
            models = closure.get(name)
            if not models:
                continue
            sections[name] = (
                wire.run_rows_to_wire(models, self._out_root)
                if name == "runs"
                else wire.rows_to_wire(name, models)
            )
            if name == "passes":
                sections[wire.PASS_VIDEOS] = [
                    row for model in models for row in wire.pass_video_rows(model)
                ]
        cover = self._cover_rows(closure)
        if cover:
            sections[wire.COVER_ROWS] = cover
        ordered = {name: sections[name] for name in wire.WIRE_SECTIONS if sections.get(name)}
        return ordered, watermarks

    def _closure(self, changed: Mapping[str, Sequence[Any]]) -> dict[str, list[Any]]:
        """Every changed row plus every ancestor it needs, deduplicated per section."""
        found: dict[str, dict[uuid.UUID, Any]] = {}
        for name, rows in changed.items():
            ancestry = self._store.dependency_closure(name, [row.id for row in rows])
            for section, models in ancestry.items():
                found.setdefault(section, {}).update({model.id: model for model in models})
        return {
            name: sorted(models.values(), key=lambda model: (model.created_at, str(model.id)))
            for name, models in found.items()
        }

    def _cover_rows(self, closure: Mapping[str, Sequence[Any]]) -> list[dict[str, Any]]:
        """Per-pass cover for the runs in this document, read from their run directories."""
        runs = closure.get("runs") or []
        transects = closure.get("transects") or []
        if self._classes_config is None or not runs or not transects:
            return []
        rows = collate_long_format(
            self._store,
            self._out_root,
            self._classes_config,
            transect_ids=[transect.id for transect in transects],
        )
        return wire.cover_rows_to_wire(rows, {str(run.id): run for run in runs}, self._out_root)

    # --- Conflicts ---

    def _report_pull_conflicts(self, report: PullReport) -> None:
        if report.overwritten:
            self._post(
                CONFLICT_OVERWRITTEN,
                f"The registry replaced {len(report.overwritten)} edited row(s)",
                "Somebody edited these rows more recently, so their version won. "
                "Whatever was typed here since the last sync is no longer in the survey.",
            )
        if report.passes_without_videos:
            self._post(
                PASS_WITHOUT_VIDEOS,
                f"{len(report.passes_without_videos)} section(s) arrived with no footage",
                "The registry holds these sections with no clips listed against them, "
                "and a section without footage cannot be recorded here. They were left "
                "out, and so was anything processed from them.",
            )
        if report.runs_without_passes and not report.passes_without_videos:
            self._post(
                RUN_WITHOUT_PASS,
                f"{len(report.runs_without_passes)} result(s) arrived with no section",
                "The registry did not send the sections these results belong to, so "
                "there was nothing to record them against. Sync again once it does.",
            )

    def _report_push_conflicts(self, report: PushReport) -> None:
        skipped = report.skipped
        if skipped:
            self._post(
                CONFLICT_DISCARDED,
                f"The registry already held {len(skipped)} row(s) newer than ours",
                "Those edits were not accepted. The next sync brings the registry's "
                "version down, which is what the survey will show.",
            )

    def _post(self, fingerprint: str, title: str, body: str) -> None:
        logger.info("%s: %s", fingerprint, title)
        if self._notify is not None:
            self._notify.post(
                fingerprint=fingerprint, title=title, body=body, severity=WARNING, scope=SURVEY
            )


def _held_ids(rows: Mapping[str, Mapping[str, Any]]) -> tuple[uuid.UUID, ...]:
    return tuple(uuid.UUID(str(row["id"])) for row in rows.values())


def _merge(existing: ApplyResult | None, result: ApplyResult) -> ApplyResult:
    """One section's tally across the pages of one pull."""
    if existing is None:
        return result
    return ApplyResult(
        received=existing.received + result.received,
        inserted=existing.inserted + result.inserted,
        updated=existing.updated + result.updated,
        skipped=[*existing.skipped, *result.skipped],
    )


def _page_cursor(payload: Mapping[str, Any]) -> int | None:
    raw = payload.get("cursor")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _push_outcomes(response: Mapping[str, Any]) -> dict[str, SectionPush]:
    outcomes = {}
    for name, outcome in (response.get("sections") or {}).items():
        outcomes[name] = SectionPush(
            received=int(outcome.get("received", 0)),
            applied=int(outcome.get("applied", 0)),
            skipped=tuple(_ids(outcome.get("skipped") or ())),
        )
    return outcomes


def _our_edits(
    outcomes: Mapping[str, SectionPush], edited: Mapping[str, set[uuid.UUID]]
) -> dict[str, SectionPush]:
    """The same outcomes with every skip that was not a local edit dropped.

    A document carries the ancestors of what changed and re-offers the row sitting
    exactly on each watermark, so a registry in step with this device skips most of
    what it is sent. Reporting that as a conflict would warn about a survey where
    nothing is wrong. The derived sections are not in ``edited`` at all: their ids
    are a function of the rows they hang off, so an equal stamp there is never news.
    """
    return {
        name: SectionPush(
            received=outcome.received,
            applied=outcome.applied,
            skipped=tuple(
                row_id for row_id in outcome.skipped if row_id in edited.get(name, ())
            ),
        )
        for name, outcome in outcomes.items()
    }


def _accounted(outcome: SectionPush, sent: int) -> bool:
    """Whether the registry said what became of every row this section sent.

    A skipped row counts as accounted for: the registry has told us it holds a
    newer copy, and the pull that follows brings it down. Holding the watermark
    back for it instead would re-offer the same row on every sync forever.
    """
    return outcome.received == sent and outcome.applied + len(outcome.skipped) == sent


def _ids(values: Iterable[Any]) -> list[uuid.UUID]:
    parsed = []
    for value in values:
        try:
            parsed.append(uuid.UUID(str(value)))
        except ValueError:
            logger.warning("Registry reported a skipped row with an unreadable id %r", value)
    return parsed
