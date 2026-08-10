"""SQLite persistence for survey data.

One database per output root (``<out_root>/survey.db``). Run directories stay
self-describing through their manifests, so a lost or stale database can always
be rebuilt with :meth:`SurveyStore.rebuild_from_scan`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deepreefmap_gui.survey.backup import write_backup
from deepreefmap_gui.survey.models.batch_item import BatchItem
from deepreefmap_gui.survey.models.common import utc_now_iso
from deepreefmap_gui.survey.models.convert import (
    build_document,
    from_row,
    parse_document,
    to_row,
)
from deepreefmap_gui.survey.models.exporters import load_survey_json, save_survey_json
from deepreefmap_gui.survey.models.notification import Notification
from deepreefmap_gui.survey.models.run_record import RUN_STATUSES, TERMINAL_STATUSES, RunRecord
from deepreefmap_gui.survey.models.survey_batch import SurveyBatch
from deepreefmap_gui.survey.models.transect import Transect
from deepreefmap_gui.survey.models.transect_pass import TransectPass
from deepreefmap_gui.survey.models.video_asset import VideoAsset

logger = logging.getLogger(__name__)

SURVEY_DB_NAME = "survey.db"

# Left on a run row the process abandoned. Short on purpose: it shows in the run
# list and the pass status, so it reads as a fact, not a stack trace.
_INTERRUPTED_REASON = "The app closed before this run finished."

@dataclass(frozen=True)
class Migration:
    """One forward step, applied to any database stamped below ``version``."""

    version: int
    name: str
    sql: str


SCHEMA_VERSION = 6

# The schema as it stands, written whole into a new database.
_BASELINE = """
CREATE TABLE transect (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    start_lat REAL NOT NULL,
    start_lon REAL NOT NULL,
    end_lat REAL NOT NULL,
    end_lon REAL NOT NULL,
    length_m REAL,
    depth_m REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- The tri-states default to 'unknown' rather than 'no': "the camera recorded no
-- gravity" is a different fact from "nobody looked". probed_at is what the
-- backfill selects on, so an unprobed row starts NULL.
CREATE TABLE video_asset (
    id TEXT PRIMARY KEY,
    file_name TEXT NOT NULL,
    path TEXT NOT NULL,
    hash TEXT,
    size_bytes INTEGER,
    mtime TEXT,
    duration_s REAL,
    fps REAL,
    created_at TEXT NOT NULL,
    captured_at TEXT,
    captured_source TEXT,
    width INTEGER,
    height INTEGER,
    codec TEXT,
    probed_at TEXT,
    gravity TEXT NOT NULL DEFAULT 'unknown',
    gps TEXT NOT NULL DEFAULT 'unknown'
);
CREATE INDEX video_asset_hash ON video_asset(hash);
CREATE TABLE survey_batch (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    preset_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- transect_id is nullable: footage worth processing is not always footage laid
-- against a tape, and such a run simply reports no tape length and is not
-- scaled. extra_video_ids is the GoPro chapters that follow the first video, a
-- JSON array because the list is short, ordered, and only ever read whole.
-- held belongs to the pass, not the session: a batch left half-run overnight
-- comes back with the same passes held.
CREATE TABLE transect_pass (
    id TEXT PRIMARY KEY,
    transect_id TEXT REFERENCES transect(id),
    video_id TEXT NOT NULL REFERENCES video_asset(id),
    batch_id TEXT REFERENCES survey_batch(id),
    direction TEXT NOT NULL CHECK (direction IN ('forward', 'reverse')),
    begin_s REAL NOT NULL,
    end_s REAL NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    extra_video_ids TEXT NOT NULL DEFAULT '[]',
    held INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE run_record (
    id TEXT PRIMARY KEY,
    pass_id TEXT NOT NULL REFERENCES transect_pass(id),
    run_dir_name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    batch_id TEXT REFERENCES survey_batch(id)
);
-- Worklist membership is a table of its own so a pass can be ordered in
-- several sessions.
CREATE TABLE batch_item (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES survey_batch(id),
    pass_id TEXT NOT NULL REFERENCES transect_pass(id),
    created_at TEXT NOT NULL,
    UNIQUE (batch_id, pass_id)
);
"""

# Released schema version -> the script that brings it to SCHEMA_VERSION. Only
# versions an installed build actually wrote appear here; the steps between them
# never outlived their own transaction.
_CARRY_FORWARD = {
    # 0.2.0. SQLite cannot relax NOT NULL in place, so transect_pass is rebuilt.
    # Foreign keys are off for the duration, set in _migrate. run_record.batch_id
    # and batch_item both backfill from the pass, the only session either had,
    # minting ids in the 8-4-4-4-12 form from_row parses back.
    3: """
    CREATE TABLE transect_pass_new (
        id TEXT PRIMARY KEY,
        transect_id TEXT REFERENCES transect(id),
        video_id TEXT NOT NULL REFERENCES video_asset(id),
        batch_id TEXT REFERENCES survey_batch(id),
        direction TEXT NOT NULL CHECK (direction IN ('forward', 'reverse')),
        begin_s REAL NOT NULL,
        end_s REAL NOT NULL,
        notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        extra_video_ids TEXT NOT NULL DEFAULT '[]',
        held INTEGER NOT NULL DEFAULT 0
    );
    INSERT INTO transect_pass_new
        SELECT id, transect_id, video_id, batch_id, direction, begin_s, end_s,
               notes, created_at, extra_video_ids, held
        FROM transect_pass;
    DROP TABLE transect_pass;
    ALTER TABLE transect_pass_new RENAME TO transect_pass;

    ALTER TABLE run_record ADD COLUMN batch_id TEXT REFERENCES survey_batch(id);
    UPDATE run_record SET batch_id =
        (SELECT batch_id FROM transect_pass WHERE transect_pass.id = run_record.pass_id);
    CREATE TABLE batch_item (
        id TEXT PRIMARY KEY,
        batch_id TEXT NOT NULL REFERENCES survey_batch(id),
        pass_id TEXT NOT NULL REFERENCES transect_pass(id),
        created_at TEXT NOT NULL,
        UNIQUE (batch_id, pass_id)
    );
    INSERT INTO batch_item (id, batch_id, pass_id, created_at)
        SELECT printf('%s-%s-%s-%s-%s',
                      lower(hex(randomblob(4))), lower(hex(randomblob(2))),
                      lower(hex(randomblob(2))), lower(hex(randomblob(2))),
                      lower(hex(randomblob(6)))),
               batch_id, id, created_at
        FROM transect_pass WHERE batch_id IS NOT NULL;

    ALTER TABLE video_asset ADD COLUMN captured_at TEXT;
    ALTER TABLE video_asset ADD COLUMN captured_source TEXT;
    ALTER TABLE video_asset ADD COLUMN width INTEGER;
    ALTER TABLE video_asset ADD COLUMN height INTEGER;
    ALTER TABLE video_asset ADD COLUMN codec TEXT;
    ALTER TABLE video_asset ADD COLUMN probed_at TEXT;
    ALTER TABLE video_asset ADD COLUMN gravity TEXT NOT NULL DEFAULT 'unknown';
    ALTER TABLE video_asset ADD COLUMN gps TEXT NOT NULL DEFAULT 'unknown';
    """,
}

_OLDEST_CARRIED = min(_CARRY_FORWARD)

# Steps taken after the baseline was cut. Appended to, never renumbered. Both
# the fresh path and the carry-forward path land on SCHEMA_VERSION first, so
# every step here runs on either kind of database.
_MIGRATIONS: list[Migration] = [
    # A cart row is a plan to process a pass, so it cannot outlive the pass.
    # Without the cascade, deleting a pass that sits in any cart fails on the
    # foreign key and the pass cannot be removed at all. SQLite cannot alter a
    # foreign key in place, so batch_item is rebuilt; nothing references
    # batch_item, so the rename repoints no other table. run_record.pass_id
    # stays restricting: a run is history, and deleting the pass under it would
    # leave a finished run naming nothing.
    Migration(
        7,
        "batch_item cascades on pass delete",
        """
        CREATE TABLE batch_item_new (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES survey_batch(id),
            pass_id TEXT NOT NULL REFERENCES transect_pass(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            UNIQUE (batch_id, pass_id)
        );
        INSERT INTO batch_item_new (id, batch_id, pass_id, created_at)
            SELECT id, batch_id, pass_id, created_at FROM batch_item;
        DROP TABLE batch_item;
        ALTER TABLE batch_item_new RENAME TO batch_item;
        """,
    ),
    # Everything that has asked for attention, and what became of it. No foreign
    # keys: a notification outlives whatever provoked it, and a log that
    # disappeared when the pass it complained about was deleted would lose
    # exactly the entry somebody went looking for.
    Migration(
        8,
        "notification log",
        """
        CREATE TABLE notification (
            id TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('condition', 'event')),
            severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'blocker')),
            scope TEXT NOT NULL CHECK (scope IN ('survey', 'machine')),
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            section TEXT NOT NULL DEFAULT '',
            subject_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT,
            read_at TEXT,
            dismissed_at TEXT
        );
        -- One open episode per condition. The centre keeps the open set in
        -- memory, so a store reopened under a second window would otherwise
        -- leave two rows for one fault and resolve only one of them.
        CREATE UNIQUE INDEX notification_open_condition
            ON notification(fingerprint)
            WHERE resolved_at IS NULL AND kind = 'condition';
        CREATE INDEX notification_created ON notification(created_at);
        """,
    ),
]


def latest_schema_version() -> int:
    return max([SCHEMA_VERSION, *(m.version for m in _MIGRATIONS)])


def resolved_path(path: str) -> str | None:
    """A path in the one form two records of the same file can be compared in.

    Never touches the filesystem: resolve(strict=False) is pure string work, so
    a clip on an unplugged drive still compares equal to itself. Returns None
    for an empty path, which is not a location and must not match another.
    """
    if not path:
        return None
    try:
        return str(Path(path).expanduser().resolve(strict=False))
    except (OSError, ValueError):
        return path


def _insert_sql(table: str, row: dict[str, Any]) -> str:
    columns = ", ".join(row)
    params = ", ".join(f":{c}" for c in row)
    return f"INSERT INTO {table} ({columns}) VALUES ({params})"


def _update_sql(table: str, row: dict[str, Any]) -> str:
    sets = ", ".join(f"{c} = :{c}" for c in row if c != "id")
    return f"UPDATE {table} SET {sets} WHERE id = :id"


@dataclass
class RebuildReport:
    transects: int = 0
    videos: int = 0
    batches: int = 0
    passes: int = 0
    runs: int = 0
    skipped: list[str] = field(default_factory=list)


class SurveyStore:
    """Thread-confined sqlite access: each thread gets its own connection.

    The GUI thread and the batch worker only run short transactions, so WAL plus
    a busy timeout is all the coordination needed.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()
        # A store is opened once per output root, on the GUI thread, before any
        # batch touches it, so opening is the one moment where every non-terminal
        # row is certain to be a leftover rather than live work. The count is
        # kept because the window reports it, and this is the only moment it can
        # be told apart from a run that is genuinely under way.
        self.interrupted_at_open = self.reconcile_interrupted_runs()
        self.prune_notifications()

    @property
    def path(self) -> Path:
        return self._db_path

    def close(self) -> None:
        """Close the calling thread's connection (worker threads close their own)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            self._local.conn = conn
        return conn

    def _migrate(self) -> None:
        conn = self._conn()
        conn.execute("PRAGMA journal_mode = WAL")
        # Foreign keys off for the duration. A migration that rebuilds a table
        # (the only way SQLite can relax a NOT NULL) has to drop it while
        # run_record still references it, and the pragma is a no-op inside a
        # transaction, so it has to be set out here.
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self._run_migrations(conn)
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        # Logged, never raised. Orphan rows predate the migration -- a database
        # written with foreign keys off can carry them -- and refusing to open a
        # survey over one would strand a field laptop on the thing least worth
        # stopping for. The log is enough to find it.
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            logger.warning(
                "survey.db has %d row(s) referencing something that is not there", len(broken)
            )

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        latest = latest_schema_version()
        # A database stamped newer than this build knows must not be opened: it
        # would run nothing and then read a schema whose columns this code does
        # not understand. This happens after an update is rolled back, so it
        # needs to say what to do, not fail obscurely later.
        if version > latest:
            raise RuntimeError(
                f"survey.db is schema v{version}, but this build knows up to "
                f"v{latest}. Update the app to open this survey."
            )
        if version == latest:
            return
        # Version 0 is the database _conn just brought into being; there is no
        # prior build's work in it to protect.
        if version > 0:
            self._backup_before_migrating(version)
        if version == 0:
            self._apply(conn, _BASELINE, SCHEMA_VERSION)
            version = SCHEMA_VERSION
        elif version < SCHEMA_VERSION:
            carry = _CARRY_FORWARD.get(version)
            if carry is None:
                raise RuntimeError(
                    f"survey.db is schema v{version}, which this build cannot carry "
                    f"forward. Open it once with 0.2.0, which brings it to v{_OLDEST_CARRIED}."
                )
            self._apply(conn, carry, SCHEMA_VERSION)
            version = SCHEMA_VERSION
        for migration in _MIGRATIONS:
            if migration.version > version:
                self._apply(conn, migration.sql, migration.version)

    @staticmethod
    def _apply(conn: sqlite3.Connection, script: str, version: int) -> None:
        with conn:
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {version}")

    def _backup_before_migrating(self, version: int) -> None:
        """Copy the database in the shape the previous build wrote it.

        Migrating is one-way -- an older build refuses a database stamped past
        what it knows -- so this copy is what a rolled-back update restores,
        instead of piecing the survey back together from run manifests. A backup
        that cannot be taken is not a reason to refuse to migrate; write_backup
        logs and returns None.
        """
        write_backup(self._db_path, version)

    def _add(self, table: str, model: Any) -> None:
        row = to_row(model)
        with self._conn() as conn:
            conn.execute(_insert_sql(table, row), row)

    def _update(self, table: str, model: Any) -> None:
        row = to_row(model)
        with self._conn() as conn:
            cursor = conn.execute(_update_sql(table, row), row)
        if cursor.rowcount == 0:
            raise KeyError(f"No {table} row with id {row['id']}")

    def _get(self, table: str, cls: type, item_id: uuid.UUID) -> Any:
        row = self._conn().execute(
            f"SELECT * FROM {table} WHERE id = ?", (str(item_id),)
        ).fetchone()
        return from_row(cls, row) if row is not None else None

    def _list(self, table: str, cls: type, order_by: str) -> list[Any]:
        rows = self._conn().execute(f"SELECT * FROM {table} ORDER BY {order_by}").fetchall()
        return [from_row(cls, r) for r in rows]

    # --- Transects ---

    def add_transect(self, transect: Transect) -> None:
        self._add("transect", transect)

    def update_transect(self, transect: Transect) -> None:
        transect.updated_at = utc_now_iso()
        self._update("transect", transect)

    def delete_transect(self, transect_id: uuid.UUID) -> None:
        """Delete a transect that nothing was swum against.

        transect_pass.transect_id restricts rather than cascading or nulling:
        the passes are the record of what was swum here, and a run manifest
        already written names this transect.
        """
        try:
            with self._conn() as conn:
                conn.execute("DELETE FROM transect WHERE id = ?", (str(transect_id),))
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                "This transect has passes recorded against it and cannot be deleted."
            ) from exc

    def get_transect(self, transect_id: uuid.UUID) -> Transect | None:
        return self._get("transect", Transect, transect_id)

    def list_transects(self) -> list[Transect]:
        return self._list("transect", Transect, "name")

    def transect_usage_counts(self) -> dict[uuid.UUID, tuple[int, int]]:
        """(passes, runs) per transect, for every transect that has either.

        Two grouped queries rather than a count per row: the plan list is
        rebuilt on every keystroke while a transect is being typed.

        A pass need not name a transect, so both queries drop the null group.
        """
        counts: dict[uuid.UUID, tuple[int, int]] = {}
        conn = self._conn()
        for row in conn.execute(
            """
            SELECT transect_id, COUNT(*) AS n
            FROM transect_pass
            WHERE transect_id IS NOT NULL
            GROUP BY transect_id
            """
        ):
            counts[uuid.UUID(row["transect_id"])] = (row["n"], 0)
        for row in conn.execute(
            """
            SELECT transect_pass.transect_id AS transect_id, COUNT(*) AS n
            FROM run_record
            JOIN transect_pass ON transect_pass.id = run_record.pass_id
            WHERE transect_pass.transect_id IS NOT NULL
            GROUP BY transect_pass.transect_id
            """
        ):
            transect_id = uuid.UUID(row["transect_id"])
            counts[transect_id] = (counts.get(transect_id, (0, 0))[0], row["n"])
        return counts

    # --- Videos ---

    def upsert_video(self, asset: VideoAsset) -> VideoAsset:
        """Insert ``asset``, or refresh and return the row that is already this clip.

        Matched on the content hash, and on the resolved path when there is no
        hash. The path fallback carries clips that cannot be hashed, off an
        unplugged drive or otherwise unreadable, which would otherwise insert a
        fresh row on every add.

        A path is only ever a fallback: two different files can sit at one path
        over time, so a hash wins when there is one on both sides.
        """
        existing = self.find_video_by_hash(asset.hash) if asset.hash else None
        if existing is None and not asset.hash:
            existing = self.find_video_by_path(asset.path)
            # Only adopt an unhashed row: one that already knows its hash is a
            # different, identified clip that happens to have lived here.
            if existing is not None and existing.hash:
                existing = None
        if existing is None:
            self._add("video_asset", asset)
            return asset
        existing.overlay_from(asset)
        self._update("video_asset", existing)
        return existing

    def find_video_by_hash(self, content_hash: str | None) -> VideoAsset | None:
        if not content_hash:
            return None
        row = self._conn().execute(
            "SELECT * FROM video_asset WHERE hash = ?", (content_hash,)
        ).fetchone()
        return from_row(VideoAsset, row) if row is not None else None

    def find_video_by_path(self, path: str) -> VideoAsset | None:
        """The clip recorded at this path, matched on the resolved location.

        Resolved on both sides so a relative path, a symlinked mount and the
        absolute path of the same file are one clip rather than three.
        """
        wanted = resolved_path(path)
        if wanted is None:
            return None
        for video in self.list_videos():
            if resolved_path(video.path) == wanted:
                return video
        return None

    def get_video(self, video_id: uuid.UUID) -> VideoAsset | None:
        return self._get("video_asset", VideoAsset, video_id)

    def list_videos(self) -> list[VideoAsset]:
        return self._list("video_asset", VideoAsset, "created_at, file_name")

    def update_video(self, asset: VideoAsset) -> None:
        self._update("video_asset", asset)

    def merge_videos(self, keeper_id: uuid.UUID, loser_ids: list[uuid.UUID]) -> int:
        """Fold duplicate clip rows into one, and repoint what pointed at them.

        Returns the number of passes moved. Both ends of a pass are repointed:
        ``video_id`` and the ``extra_video_ids`` a chaptered recording carries,
        or the second half of a swim the camera split at 4 GB would go on
        naming a row that no longer exists.

        A pass that would end up naming the keeper twice keeps it once. That is
        not hypothetical: a chaptered pass whose chapters were separately
        unhashed has every chapter merging into the same keeper, and
        TransectPass refuses to hold the same video in both places.
        """
        losers = {lid for lid in loser_ids if lid != keeper_id}
        if not losers:
            return 0
        moved = 0
        for pass_ in self.list_passes():
            ids = pass_.video_ids()
            if not losers.intersection(ids):
                continue
            remapped: list[uuid.UUID] = []
            for video_id in ids:
                mapped = keeper_id if video_id in losers else video_id
                if mapped not in remapped:
                    remapped.append(mapped)
            pass_.video_id = remapped[0]
            pass_.extra_video_ids = remapped[1:]
            self.update_pass(pass_)
            moved += 1
        with self._conn() as conn:
            conn.executemany(
                "DELETE FROM video_asset WHERE id = ?", [(str(lid),) for lid in losers]
            )
        return moved

    # --- Batches ---

    def add_batch(self, batch: SurveyBatch) -> None:
        self._add("survey_batch", batch)

    def get_batch(self, batch_id: uuid.UUID) -> SurveyBatch | None:
        return self._get("survey_batch", SurveyBatch, batch_id)

    def list_batches(self) -> list[SurveyBatch]:
        return self._list("survey_batch", SurveyBatch, "created_at DESC")

    def batch_run_count(self, batch_id: uuid.UUID) -> int:
        """How many runs this session has placed. Zero means it is still a cart."""
        row = self._conn().execute(
            "SELECT COUNT(*) AS n FROM run_record WHERE batch_id = ?", (str(batch_id),)
        ).fetchone()
        return row["n"]

    def runs_in_batch(self, batch_id: uuid.UUID) -> list[RunRecord]:
        """Every run the session placed, including ones recorded on the pass only.

        The fallback matches RunEntry.session_id: an early run row carries no
        batch_id of its own and belongs to the session its pass was catalogued
        in. delete_batch removes the same set, so what this lists is what goes.
        """
        rows = self._conn().execute(
            """
            SELECT * FROM run_record
            WHERE batch_id = ?
               OR (batch_id IS NULL AND pass_id IN
                   (SELECT id FROM transect_pass WHERE batch_id = ?))
            ORDER BY created_at
            """,
            (str(batch_id), str(batch_id)),
        ).fetchall()
        return [from_row(RunRecord, r) for r in rows]

    def delete_batch(self, batch_id: uuid.UUID) -> None:
        """Forget a session: its cart rows and run records go, everything shared
        stays. Passes catalogued in it survive with no session of their own, and
        clips and transects are never touched here."""
        key = str(batch_id)
        with self._conn() as conn:
            conn.execute("DELETE FROM batch_item WHERE batch_id = ?", (key,))
            conn.execute(
                """
                DELETE FROM run_record
                WHERE batch_id = ?
                   OR (batch_id IS NULL AND pass_id IN
                       (SELECT id FROM transect_pass WHERE batch_id = ?))
                """,
                (key, key),
            )
            conn.execute(
                "UPDATE transect_pass SET batch_id = NULL WHERE batch_id = ?", (key,)
            )
            conn.execute("DELETE FROM survey_batch WHERE id = ?", (key,))

    def current_cart(self) -> SurveyBatch | None:
        """The newest session, only while it has run nothing.

        Only the newest: an older empty session behind a started one is
        abandoned, not the cart. rowid breaks a same-second created_at tie.
        """
        row = self._conn().execute(
            "SELECT * FROM survey_batch ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        batch = from_row(SurveyBatch, row)
        return batch if self.batch_run_count(batch.id) == 0 else None

    # --- Batch items ---

    def add_batch_item(self, item: BatchItem) -> None:
        """Add a pass to a session's worklist; already a member is a no-op."""
        row = to_row(item)
        with self._conn() as conn:
            conn.execute(
                _insert_sql("batch_item", row).replace("INSERT", "INSERT OR IGNORE", 1), row
            )

    def remove_batch_item(self, batch_id: uuid.UUID, pass_id: uuid.UUID) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM batch_item WHERE batch_id = ? AND pass_id = ?",
                (str(batch_id), str(pass_id)),
            )

    def list_batch_items(self, batch_id: uuid.UUID) -> list[BatchItem]:
        # rowid is insertion order, which is the order the cart was filled in.
        rows = self._conn().execute(
            "SELECT * FROM batch_item WHERE batch_id = ? ORDER BY rowid", (str(batch_id),)
        ).fetchall()
        return [from_row(BatchItem, r) for r in rows]

    def list_all_batch_items(self) -> list[BatchItem]:
        rows = self._conn().execute("SELECT * FROM batch_item ORDER BY rowid").fetchall()
        return [from_row(BatchItem, r) for r in rows]

    def passes_in_batch(self, batch_id: uuid.UUID) -> list[TransectPass]:
        """The session's worklist, in the order it was filled."""
        rows = self._conn().execute(
            """
            SELECT transect_pass.* FROM transect_pass
            JOIN batch_item ON batch_item.pass_id = transect_pass.id
            WHERE batch_item.batch_id = ?
            ORDER BY batch_item.rowid
            """,
            (str(batch_id),),
        ).fetchall()
        return [from_row(TransectPass, r) for r in rows]

    # --- Passes ---

    def add_pass(self, pass_: TransectPass) -> None:
        self._add("transect_pass", pass_)

    def pass_with_window(
        self, video_id: uuid.UUID, begin_s: float, end_s: float | None
    ) -> TransectPass | None:
        """An existing section of this clip covering the same window, if there is one.

        A guard for the cut, not for ``add_pass``: restoring a run's pass from a
        manifest has to insert whatever window that run processed, duplicate or
        not, or the run is left describing a section nothing records. Windows are
        compared to a tenth of a second, well under a frame at any usable fps.
        """
        for stored in self.list_passes(video_id=video_id):
            if abs(stored.begin_s - begin_s) > 0.1:
                continue
            if stored.end_s is None or end_s is None:
                if stored.end_s is end_s:
                    return stored
                continue
            if abs(stored.end_s - end_s) <= 0.1:
                return stored
        return None

    def update_pass(self, pass_: TransectPass) -> None:
        self._update("transect_pass", pass_)

    def delete_pass(self, pass_id: uuid.UUID) -> None:
        """Delete a pass and the cart rows that ordered it.

        Only a run stops it: run_record.pass_id restricts, and the constraint
        failure is turned into a sentence the caller can show.
        """
        try:
            with self._conn() as conn:
                conn.execute("DELETE FROM transect_pass WHERE id = ?", (str(pass_id),))
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                "This pass has recorded runs and cannot be removed."
            ) from exc

    def get_pass(self, pass_id: uuid.UUID) -> TransectPass | None:
        return self._get("transect_pass", TransectPass, pass_id)

    def list_passes(
        self,
        transect_id: uuid.UUID | None = None,
        batch_id: uuid.UUID | None = None,
        video_id: uuid.UUID | None = None,
    ) -> list[TransectPass]:
        clauses, params = [], []
        if transect_id is not None:
            clauses.append("transect_id = ?")
            params.append(str(transect_id))
        if batch_id is not None:
            clauses.append("batch_id = ?")
            params.append(str(batch_id))
        if video_id is not None:
            clauses.append("video_id = ?")
            params.append(str(video_id))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        # created_at is second-precision, so passes queued in one action share it.
        # rowid breaks the tie by insertion order, which is the order the user
        # built the table in, and which a pass's first run-dir name is numbered
        # from (later attempts derive from that recorded name).
        rows = self._conn().execute(
            f"SELECT * FROM transect_pass{where} ORDER BY created_at, rowid", params
        ).fetchall()
        return [from_row(TransectPass, r) for r in rows]

    # --- Runs ---

    def add_run(self, run: RunRecord) -> None:
        self._add("run_record", run)

    def set_run_status(self, run_id: uuid.UUID, status: str, error: str = "") -> None:
        if status not in RUN_STATUSES:
            raise ValueError(f"status must be one of {RUN_STATUSES}, got {status!r}")
        sets = "status = ?, error = ?"
        params: list[Any] = [status, error]
        if status == "running":
            sets += ", started_at = ?"
            params.append(utc_now_iso())
        elif status in TERMINAL_STATUSES:
            sets += ", finished_at = ?"
            params.append(utc_now_iso())
        params.append(str(run_id))
        with self._conn() as conn:
            cursor = conn.execute(f"UPDATE run_record SET {sets} WHERE id = ?", params)
        if cursor.rowcount == 0:
            raise KeyError(f"No run_record row with id {run_id}")

    def reconcile_interrupted_runs(self) -> int:
        """Mark every non-terminal run row as interrupted; return how many moved.

        A crash or a quit-with-batch-running never gets to stamp a terminal
        status, so a row stays "running" (or "pending") forever, reading as live
        work that blocks nothing from being re-run. Reconciling on open turns
        those into a terminal, non-success state the gate treats as remaining.
        """
        non_terminal = [s for s in RUN_STATUSES if s not in TERMINAL_STATUSES]
        placeholders = ", ".join("?" for _ in non_terminal)
        with self._conn() as conn:
            cursor = conn.execute(
                f"UPDATE run_record SET status = ?, finished_at = ?, error = ? "
                f"WHERE status IN ({placeholders})",
                ["interrupted", utc_now_iso(), _INTERRUPTED_REASON, *non_terminal],
            )
        if cursor.rowcount:
            logger.info("Reconciled %d interrupted run(s) in %s", cursor.rowcount, self._db_path)
        return cursor.rowcount

    def delete_run(self, run_id: uuid.UUID) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM run_record WHERE id = ?", (str(run_id),))

    def get_run(self, run_id: uuid.UUID) -> RunRecord | None:
        return self._get("run_record", RunRecord, run_id)

    def run_by_dir_name(self, run_dir_name: str) -> RunRecord | None:
        row = self._conn().execute(
            "SELECT * FROM run_record WHERE run_dir_name = ? ORDER BY created_at DESC LIMIT 1",
            (run_dir_name,),
        ).fetchone()
        return from_row(RunRecord, row) if row is not None else None

    def runs_for_pass(self, pass_id: uuid.UUID) -> list[RunRecord]:
        rows = self._conn().execute(
            "SELECT * FROM run_record WHERE pass_id = ? ORDER BY created_at, rowid",
            (str(pass_id),),
        ).fetchall()
        return [from_row(RunRecord, r) for r in rows]

    def runs_for_transect(self, transect_id: uuid.UUID) -> list[RunRecord]:
        rows = self._conn().execute(
            """
            SELECT run_record.* FROM run_record
            JOIN transect_pass ON transect_pass.id = run_record.pass_id
            WHERE transect_pass.transect_id = ?
            ORDER BY run_record.created_at
            """,
            (str(transect_id),),
        ).fetchall()
        return [from_row(RunRecord, r) for r in rows]

    def succeeded_pass_ids(self, batch_id: uuid.UUID | None = None) -> set[uuid.UUID]:
        """Passes with at least one successful run, in one query.

        The Run table asks on every repaint, so it cannot afford a query per
        row. Scoped to a session when ``batch_id`` is given: a pass re-ordered
        in a new cart has succeeded before, but not yet in that session.
        """
        sql = "SELECT DISTINCT pass_id FROM run_record WHERE status = 'succeeded'"
        params: list[str] = []
        if batch_id is not None:
            sql += " AND batch_id = ?"
            params.append(str(batch_id))
        rows = self._conn().execute(sql, params).fetchall()
        return {uuid.UUID(row["pass_id"]) for row in rows}

    def list_runs(self) -> list[RunRecord]:
        return self._list("run_record", RunRecord, "created_at")

    # --- Notifications ---

    def add_notification(self, note: Notification) -> None:
        self._add("notification", note)

    def update_notification(self, note: Notification) -> None:
        self._update("notification", note)

    def open_notifications(self) -> list[Notification]:
        """Every episode still running, oldest first, so the centre can adopt them."""
        rows = self._conn().execute(
            "SELECT * FROM notification WHERE resolved_at IS NULL ORDER BY created_at"
        ).fetchall()
        return [from_row(Notification, r) for r in rows]

    def list_notifications(
        self, limit: int = 500, severity: str = "", scope: str = ""
    ) -> list[Notification]:
        """The log, newest first."""
        sql = "SELECT * FROM notification WHERE 1 = 1"
        params: list[Any] = []
        if severity:
            sql += " AND severity = ?"
            params.append(severity)
        if scope:
            sql += " AND scope = ?"
            params.append(scope)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn().execute(sql, params).fetchall()
        return [from_row(Notification, r) for r in rows]

    def resolve_notification(self, note_id: uuid.UUID, at: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE notification SET resolved_at = ?, updated_at = ? WHERE id = ?",
                (at, at, str(note_id)),
            )

    def prune_notifications(self, keep: int = 2000) -> int:
        """Drop all but the newest ``keep`` resolved rows, and return how many went.

        The only table here that grows without anybody asking it to: a condition
        that flickers over a long field season writes an episode each time. Open
        rows are never pruned, however old, because they are still true.
        """
        with self._conn() as conn:
            cursor = conn.execute(
                """
                DELETE FROM notification WHERE id IN (
                    SELECT id FROM notification WHERE resolved_at IS NOT NULL
                    ORDER BY created_at DESC LIMIT -1 OFFSET ?
                )
                """,
                (keep,),
            )
        return cursor.rowcount

    # --- Documents ---

    def export_json(self, path: Path) -> None:
        doc = build_document(
            transects=self.list_transects(),
            videos=self.list_videos(),
            batches=self.list_batches(),
            passes=self.list_passes(),
            runs=self.list_runs(),
            batch_items=self.list_all_batch_items(),
        )
        save_survey_json(path, doc)

    def import_json(self, path: Path) -> None:
        """Merge a survey document; rows whose ids already exist are left alone."""
        sections = parse_document(load_survey_json(path))
        tables = {
            "transects": "transect",
            "videos": "video_asset",
            "batches": "survey_batch",
            "passes": "transect_pass",
            "batch_items": "batch_item",
            "runs": "run_record",
        }
        with self._conn() as conn:
            for section, models in sections.items():
                table = tables[section]
                for model in models:
                    row = to_row(model)
                    conn.execute(_insert_sql(table, row).replace("INSERT", "INSERT OR IGNORE", 1), row)

    # --- Rebuild from manifests ---

    def rebuild_from_scan(self, out_root: Path) -> RebuildReport:
        """Recreate survey rows from run manifests; existing rows are kept as-is.

        The notification log is not among them: manifests do not carry it, so a
        rebuilt database starts with an empty history. It is a record of what the
        app said, not of what the survey is.
        """
        report = RebuildReport()
        for manifest_path in sorted(out_root.glob("*/run_manifest.json")):
            run_dir_name = manifest_path.parent.name
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                report.skipped.append(run_dir_name)
                continue
            survey = manifest.get("survey")
            if not isinstance(survey, dict):
                report.skipped.append(run_dir_name)
                continue
            try:
                self._restore_run(run_dir_name, manifest, survey, report)
            except (KeyError, TypeError, ValueError, sqlite3.Error):
                logger.warning("Could not restore run %s", run_dir_name, exc_info=True)
                report.skipped.append(run_dir_name)
        return report

    def _restore_run(
        self,
        run_dir_name: str,
        manifest: dict[str, Any],
        survey: dict[str, Any],
        report: RebuildReport,
    ) -> None:
        # A run written without a transect has no transect block to restore.
        transect_snapshot = survey.get("transect")
        transect_id = (
            self._restore_transect(transect_snapshot, report) if transect_snapshot else None
        )
        batch_id = self._restore_batch(survey, report)
        video_ids = self._restore_videos(manifest, report)
        pass_id = self._restore_pass(survey["pass"], transect_id, video_ids, batch_id, report)
        if batch_id is not None:
            # The manifest only knows the session the run executed in, so the
            # rebuilt pass's origin and membership default to that session.
            self.add_batch_item(BatchItem(batch_id=batch_id, pass_id=pass_id))
        run_id = uuid.UUID(survey["run_id"])
        if self.get_run(run_id) is None:
            self.add_run(RunRecord(
                id=run_id,
                pass_id=pass_id,
                run_dir_name=run_dir_name,
                status="succeeded",
                started_at=manifest.get("run_timestamp"),
                batch_id=batch_id,
            ))
            report.runs += 1

    def _restore_transect(self, snapshot: dict[str, Any], report: RebuildReport) -> uuid.UUID:
        transect_id = uuid.UUID(snapshot["id"])
        if self.get_transect(transect_id) is None:
            self.add_transect(Transect(
                id=transect_id,
                name=snapshot["name"],
                start_lat=snapshot["start_lat"],
                start_lon=snapshot["start_lon"],
                end_lat=snapshot["end_lat"],
                end_lon=snapshot["end_lon"],
                length_m=snapshot.get("length_m"),
                depth_m=snapshot.get("depth_m"),
            ))
            report.transects += 1
        return transect_id

    def _restore_batch(self, survey: dict[str, Any], report: RebuildReport) -> uuid.UUID | None:
        raw = survey.get("batch_id")
        if not raw:
            return None
        batch_id = uuid.UUID(raw)
        if self.get_batch(batch_id) is None:
            self.add_batch(SurveyBatch(
                id=batch_id,
                name=survey.get("batch_name") or "Recovered batch",
                preset_name=survey.get("preset_name") or "survey_preset",
            ))
            report.batches += 1
        return batch_id

    def _restore_videos(self, manifest: dict[str, Any], report: RebuildReport) -> list[uuid.UUID]:
        """Every input clip of the run, in order: a pass may span GoPro chapters."""
        paths = manifest.get("input_videos") or [""]
        hashes = manifest.get("video_hashes") or []
        sizes = manifest.get("video_sizes") or []
        mtimes = manifest.get("video_mtimes") or []

        def at(values: list, index: int) -> Any:
            return values[index] if index < len(values) else None

        video_ids = []
        for index, path in enumerate(paths):
            content_hash = at(hashes, index)
            existing = self.find_video_by_hash(content_hash)
            if existing is not None:
                video_ids.append(existing.id)
                continue
            asset = VideoAsset(
                file_name=Path(path).name or "unknown",
                path=path,
                hash=content_hash,
                size_bytes=at(sizes, index),
                mtime=at(mtimes, index),
            )
            self._add("video_asset", asset)
            report.videos += 1
            video_ids.append(asset.id)
        return video_ids

    def _restore_pass(
        self,
        snapshot: dict[str, Any],
        transect_id: uuid.UUID | None,
        video_ids: list[uuid.UUID],
        batch_id: uuid.UUID | None,
        report: RebuildReport,
    ) -> uuid.UUID:
        pass_id = uuid.UUID(snapshot["id"])
        if self.get_pass(pass_id) is None:
            self.add_pass(TransectPass(
                id=pass_id,
                transect_id=transect_id,
                video_id=video_ids[0],
                extra_video_ids=video_ids[1:],
                batch_id=batch_id,
                direction=snapshot["direction"],
                begin_s=snapshot["begin_s"],
                end_s=snapshot["end_s"],
            ))
            report.passes += 1
        return pass_id
