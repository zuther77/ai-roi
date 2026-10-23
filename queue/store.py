"""Postgres queue store. Unit tests run the same SQL on SQLite.

Timestamps are ISO-8601 text and UUIDs are generated in Python (text columns)
so one statement works on both engines.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS prompts (
        id TEXT PRIMARY KEY,
        user_ref TEXT,
        raw_text TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'manual',
        moderation_status TEXT NOT NULL DEFAULT 'approved',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tracks (
        id TEXT PRIMARY KEY,
        file_path TEXT NOT NULL,
        duration_sec REAL,
        source TEXT NOT NULL,
        generation_time_sec REAL,
        is_filler INTEGER NOT NULL DEFAULT 0,
        validated INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS queue_items (
        id TEXT PRIMARY KEY,
        prompt_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        queue_position INTEGER NOT NULL,
        source_assigned TEXT,
        target_duration_sec INTEGER NOT NULL DEFAULT 30,
        deadline_at TEXT,
        track_id TEXT,
        created_at TEXT NOT NULL,
        assigned_at TEXT
    )
    """,
    # Paid-provider usage. Only relevant in hybrid/paid_only; a local_only
    # clone never writes a row. window_type is 'credit_balance' (ElevenLabs)
    # or 'spend' (Gemini Lyra, when a cap is configured).
    """
    CREATE TABLE IF NOT EXISTS provider_quota (
        id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        window_start TEXT NOT NULL,
        window_type TEXT NOT NULL,
        used REAL NOT NULL DEFAULT 0,
        quota_limit REAL,
        last_error TEXT,
        updated_at TEXT NOT NULL
    )
    """,
)

# Used only to fill deadline_at at enqueue time; routing does not read it.
ASSUMED_TRACK_SEC = 180


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.isoformat()


class QueueStore:
    def __init__(self, conn, placeholder: str = "?"):
        self.conn = conn
        self.placeholder = placeholder
        # sqlite3 opens a transaction on the first DML statement unless this is
        # None, and a later BEGIN IMMEDIATE then fails. Autocommit also matches
        # the Postgres connection, which is opened with autocommit=True.
        if hasattr(conn, "isolation_level"):
            conn.isolation_level = None
        for statement in SCHEMA:
            self._exec(statement)

    def _sql(self, sql: str) -> str:
        return sql if self.placeholder == "?" else sql.replace("?", self.placeholder)

    def _exec(self, sql: str, params: tuple = ()):
        return self.conn.execute(self._sql(sql), params)

    def _one(self, sql: str, params: tuple = ()):
        return self._exec(sql, params).fetchone()

    def _update_status(self, item_id: str, new: str, expected: str) -> bool:
        """Status transition guarded on the current status. True if it applied."""
        cur = self._exec(
            "UPDATE queue_items SET status = ? WHERE id = ? AND status = ?",
            (new, item_id, expected),
        )
        return cur.rowcount == 1

    # -- writes ------------------------------------------------------------

    def enqueue(self, prompt_text: str, target_duration_sec: int = 30,
                source: str = "manual") -> dict:
        now = _now()
        prompt_id = str(uuid.uuid4())
        item_id = str(uuid.uuid4())
        created = _iso(now)
        row = self._one(
            "SELECT COALESCE(MAX(queue_position), 0) + 1 AS pos FROM queue_items")
        position = int(row["pos"])
        deadline = _iso(now + timedelta(seconds=position * ASSUMED_TRACK_SEC))
        self._exec(
            "INSERT INTO prompts (id, raw_text, source, moderation_status, created_at)"
            " VALUES (?, ?, ?, 'approved', ?)",
            (prompt_id, prompt_text, source, created),
        )
        self._exec(
            "INSERT INTO queue_items"
            " (id, prompt_id, status, queue_position, target_duration_sec,"
            "  deadline_at, created_at)"
            " VALUES (?, ?, 'queued', ?, ?, ?, ?)",
            (item_id, prompt_id, position, int(target_duration_sec), deadline, created),
        )
        return {
            "id": item_id,
            "status": "queued",
            "queue_position": position,
            "prompt": prompt_text,
            "target_duration_sec": int(target_duration_sec),
        }

    def log_provider_usage(self, provider: str, *, window_type: str,
                           used: float, quota_limit: float | None = None,
                           last_error: str | None = None) -> str:
        """Append a provider_quota row after a paid-provider call.

        One row per call keeps it auditable; a rolling balance is derived by
        reading the latest row per provider.
        """
        now = _iso(_now())
        row_id = str(uuid.uuid4())
        self._exec(
            "INSERT INTO provider_quota"
            " (id, provider, window_start, window_type, used, quota_limit,"
            "  last_error, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (row_id, provider, now, window_type, float(used),
             quota_limit, last_error, now),
        )
        return row_id

    def record_rejected_prompt(self, prompt_text: str, source: str = "manual") -> str:
        """Persist a moderation-rejected prompt; no queue_items row is created.

        The prompt row keeps moderation_status='rejected' rather than the
        backend's raw response: the text plus the reason logged by intake is
        what is useful for reviewing policy, and keeping less is the safer
        default for a public project.
        """
        prompt_id = str(uuid.uuid4())
        self._exec(
            "INSERT INTO prompts (id, raw_text, source, moderation_status, created_at)"
            " VALUES (?, ?, ?, 'rejected', ?)",
            (prompt_id, prompt_text, source, _iso(_now())),
        )
        return prompt_id

    def mark_generating(self, item_id: str, worker: str) -> None:
        self._exec(
            "UPDATE queue_items SET status = 'generating', source_assigned = ?,"
            " assigned_at = ? WHERE id = ?",
            (worker, _iso(_now()), item_id),
        )

    def mark_ready(self, item_id: str, file_path: str, source: str,
                   generation_time_sec: float,
                   duration_sec: float | None = None) -> bool:
        """First generating -> ready wins; a hedge loser finds it already ready."""
        if not self._update_status(item_id, "ready", "generating"):
            return False
        track_id = str(uuid.uuid4())
        self._exec(
            "INSERT INTO tracks"
            " (id, file_path, duration_sec, source, generation_time_sec, is_filler, validated)"
            " VALUES (?, ?, ?, ?, ?, 0, 0)",
            (track_id, file_path, duration_sec, source, float(generation_time_sec)),
        )
        self._exec("UPDATE queue_items SET track_id = ? WHERE id = ?",
                   (track_id, item_id))
        return True

    def mark_failed(self, item_id: str) -> bool:
        """Fail only a row that is still generating; a ready hedge winner stays ready."""
        return self._update_status(item_id, "failed", "generating")

    def mark_played(self, item_id: str) -> None:
        self._update_status(item_id, "played", "playing")

    def release_to_ready(self, item_id: str) -> None:
        """File was claimed but is not on disk yet. Try again next boundary."""
        self._update_status(item_id, "ready", "playing")

    def requeue_interrupted(self) -> None:
        """A crash mid-play must not drop the item; the next start airs it again."""
        self._exec("UPDATE queue_items SET status = 'ready' WHERE status = 'playing'")

    def set_deadline(self, item_id: str, deadline_at: str) -> None:
        self._exec("UPDATE queue_items SET deadline_at = ? WHERE id = ?",
                   (deadline_at, item_id))

    # -- reads -------------------------------------------------------------

    def next_queued(self) -> dict | None:
        row = self._one(
            "SELECT q.id, q.status, q.queue_position, q.target_duration_sec,"
            "       p.raw_text AS prompt"
            " FROM queue_items q JOIN prompts p ON p.id = q.prompt_id"
            " WHERE q.status = 'queued'"
            " ORDER BY q.queue_position ASC, q.created_at ASC LIMIT 1"
        )
        if row is None:
            return None
        return {
            "id": row["id"],
            "status": row["status"],
            "queue_position": row["queue_position"],
            "target_duration_sec": row["target_duration_sec"],
            "prompt": row["prompt"],
        }

    def claim_next_ready(self) -> dict | None:
        if self.placeholder == "?":
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                claimed = self._claim_unlocked()
                self.conn.execute("COMMIT")
                return claimed
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        with self.conn.transaction():
            return self._claim_unlocked()

    def _claim_unlocked(self) -> dict | None:
        row = self._one(
            "SELECT q.id, t.file_path, t.duration_sec"
            " FROM queue_items q JOIN tracks t ON t.id = q.track_id"
            " WHERE q.status = 'ready'"
            " ORDER BY q.queue_position ASC, q.created_at ASC LIMIT 1"
        )
        if row is None:
            return None
        if not self._update_status(row["id"], "playing", "ready"):
            return None
        return {
            "queue_item_id": row["id"],
            "file_path": row["file_path"],
            "duration_sec": row["duration_sec"],
            "status": "playing",
        }

    def open_depth(self) -> int:
        """Queued, generating and ready — the buffer. Played and failed are out."""
        row = self._one(
            "SELECT COUNT(*) AS n FROM queue_items"
            " WHERE status IN ('queued', 'generating', 'ready')"
        )
        return int(row["n"])

    def average_track_sec(self) -> float | None:
        """Mean measured duration of generated tracks. None until one exists."""
        row = self._one(
            "SELECT AVG(duration_sec) AS avg_sec FROM tracks"
            " WHERE duration_sec IS NOT NULL AND duration_sec > 0 AND is_filler = 0"
        )
        if row is None or row["avg_sec"] is None:
            return None
        return float(row["avg_sec"])

    def last_assigned_worker(self) -> str | None:
        row = self._one(
            "SELECT source_assigned FROM queue_items"
            " WHERE source_assigned IS NOT NULL AND assigned_at IS NOT NULL"
            " ORDER BY assigned_at DESC LIMIT 1"
        )
        return None if row is None else row["source_assigned"]


def connect(url: str) -> QueueStore:
    """Open the production store. Imported only when DATABASE_URL is set."""
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    return QueueStore(conn, placeholder="%s")
