"""Local durable state: clip index and outbound message queue (SQLite).

* **clips** — every finalized event clip with integrity hash, size, expiry,
  and admin-preserve flag. Retention decisions read this table.
* **outbox** — messages for the cloud (incident events, heartbeats), kept
  durably so they survive restarts and internet outages. ``dedup_key`` makes
  enqueueing idempotent (incident ``event_id``). The sender arrives in
  Phase 3; until then messages simply accumulate within a bound.

One connection guarded by a lock; safe to share between the capture loop and
the housekeeping thread. WAL mode keeps writes cheap on an SD card.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS clips (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    incident_ids TEXT NOT NULL,          -- JSON array
    kind TEXT NOT NULL,                  -- incident | shadow
    started_at TEXT NOT NULL,            -- RFC 3339 UTC
    ended_at TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    preserved_at TEXT                    -- NULL = ordinary retention
);
CREATE INDEX IF NOT EXISTS clips_created ON clips(created_at);
CREATE TABLE IF NOT EXISTS outbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                  -- incident_event | heartbeat
    dedup_key TEXT UNIQUE,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(sent_at, seq);
"""


def utc_iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class ClipRecord:
    id: str
    path: str
    incident_ids: tuple[str, ...]
    kind: str
    started_at: datetime
    ended_at: datetime
    bytes: int
    sha256: str
    created_at: datetime
    expires_at: datetime
    preserved_at: datetime | None = None

    @property
    def preserved(self) -> bool:
        return self.preserved_at is not None


@dataclass(frozen=True)
class OutboxMessage:
    seq: int
    kind: str
    dedup_key: str | None
    payload: dict[str, Any]
    created_at: datetime
    attempts: int


class LocalStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._tx() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.executescript(_SCHEMA)
            db.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._db

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ clips

    def add_clip(self, clip: ClipRecord) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO clips(id, path, incident_ids, kind, started_at, ended_at, bytes, "
                "sha256, created_at, expires_at, preserved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    clip.id,
                    clip.path,
                    json.dumps(list(clip.incident_ids)),
                    clip.kind,
                    utc_iso(clip.started_at),
                    utc_iso(clip.ended_at),
                    clip.bytes,
                    clip.sha256,
                    utc_iso(clip.created_at),
                    utc_iso(clip.expires_at),
                    utc_iso(clip.preserved_at) if clip.preserved_at else None,
                ),
            )

    @staticmethod
    def _clip(row: sqlite3.Row) -> ClipRecord:
        return ClipRecord(
            id=row["id"],
            path=row["path"],
            incident_ids=tuple(json.loads(row["incident_ids"])),
            kind=row["kind"],
            started_at=parse_iso(row["started_at"]),
            ended_at=parse_iso(row["ended_at"]),
            bytes=int(row["bytes"]),
            sha256=row["sha256"],
            created_at=parse_iso(row["created_at"]),
            expires_at=parse_iso(row["expires_at"]),
            preserved_at=parse_iso(row["preserved_at"]) if row["preserved_at"] else None,
        )

    def list_clips(self) -> list[ClipRecord]:
        with self._tx() as db:
            rows = db.execute("SELECT * FROM clips ORDER BY created_at").fetchall()
        return [self._clip(r) for r in rows]

    def get_clip(self, clip_id: str) -> ClipRecord | None:
        with self._tx() as db:
            row = db.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        return self._clip(row) if row else None

    def set_preserved(self, clip_id: str, preserved: bool, at: datetime) -> bool:
        with self._tx() as db:
            cur = db.execute(
                "UPDATE clips SET preserved_at = ? WHERE id = ?",
                (utc_iso(at) if preserved else None, clip_id),
            )
        return cur.rowcount == 1

    def delete_clip(self, clip_id: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM clips WHERE id = ?", (clip_id,))

    def clips_bytes(self) -> int:
        with self._tx() as db:
            (total,) = db.execute("SELECT COALESCE(SUM(bytes), 0) FROM clips").fetchone()
        return int(total)

    # ----------------------------------------------------------------- outbox

    def enqueue(
        self, kind: str, payload: dict[str, Any], at: datetime, dedup_key: str | None = None
    ) -> bool:
        """Queue a message. Returns False if ``dedup_key`` was already queued."""
        with self._tx() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO outbox(kind, dedup_key, payload, created_at) "
                "VALUES (?,?,?,?)",
                (kind, dedup_key, json.dumps(payload, separators=(",", ":")), utc_iso(at)),
            )
        return cur.rowcount == 1

    def pending(self, limit: int = 100, max_attempts: int | None = None) -> list[OutboxMessage]:
        """Unsent messages, oldest first. ``max_attempts`` skips messages the server has
        rejected that many times (they stay in the outbox and in ``queue_depth``)."""
        cap = max_attempts if max_attempts is not None else 2**31
        with self._tx() as db:
            rows = db.execute(
                "SELECT * FROM outbox WHERE sent_at IS NULL AND attempts < ? ORDER BY seq LIMIT ?",
                (cap, limit),
            ).fetchall()
        return [
            OutboxMessage(
                r["seq"],
                r["kind"],
                r["dedup_key"],
                json.loads(r["payload"]),
                parse_iso(r["created_at"]),
                r["attempts"],
            )
            for r in rows
        ]

    def mark_sent(self, seqs: list[int], at: datetime) -> None:
        with self._tx() as db:
            db.executemany(
                "UPDATE outbox SET sent_at = ? WHERE seq = ?", [(utc_iso(at), s) for s in seqs]
            )

    def record_attempt(self, seqs: list[int]) -> None:
        with self._tx() as db:
            db.executemany(
                "UPDATE outbox SET attempts = attempts + 1 WHERE seq = ?", [(s,) for s in seqs]
            )

    def queue_depth(self) -> int:
        with self._tx() as db:
            (n,) = db.execute("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL").fetchone()
        return int(n)

    def trim_outbox(self, max_rows: int) -> int:
        """Bound the outbox. Drops sent rows first, then the oldest unsent heartbeats
        (superseded by newer ones). Incident events are never dropped here."""
        removed = 0
        with self._tx() as db:
            (total,) = db.execute("SELECT COUNT(*) FROM outbox").fetchone()
            excess = int(total) - max_rows
            for where in ("sent_at IS NOT NULL", "sent_at IS NULL AND kind = 'heartbeat'"):
                if excess <= 0:
                    break
                cur = db.execute(
                    f"DELETE FROM outbox WHERE seq IN (SELECT seq FROM outbox WHERE {where} "
                    "ORDER BY seq LIMIT ?)",
                    (excess,),
                )
                removed += cur.rowcount
                excess -= cur.rowcount
        return removed
