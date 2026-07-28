"""Local SQLite write-spool.

Neon outages must NOT block the trading loop. Every write goes through
``SpooledWriter.enqueue``, which:

  1. Persists the write to a local SQLite journal.
  2. A background task drains the journal to Neon.
  3. On Neon reconnect, the drainer replays queued writes in order.

The trading loop only awaits the SQLite append, never the Neon round-trip.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import structlog

log = structlog.get_logger(__name__)


_DDL = """
CREATE TABLE IF NOT EXISTS spool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    enqueued_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS ix_spool_id ON spool(id);
"""


@dataclass
class SpoolItem:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int


class SpooledWriter:
    """Append-only journal + async drainer.

    ``sink`` is an async callable(kind, payload) that performs the actual
    Neon write. It MUST be idempotent per (kind, payload) — the drainer
    retries on failure.
    """

    def __init__(
        self,
        spool_path: str | Path,
        sink: Callable[[str, dict[str, Any]], "asyncio.Future | Any"],
        *,
        drain_interval_sec: float = 1.0,
        max_batch: int = 100,
    ) -> None:
        self.path = str(spool_path)
        self._sink = sink
        self._interval = drain_interval_sec
        self._max_batch = max_batch
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._conn.executescript(_DDL)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def enqueue(self, kind: str, payload: dict[str, Any]) -> None:
        """Append a write to the local journal. Never awaits Neon."""

        async with self._lock:
            self._conn.execute(
                "INSERT INTO spool(kind, payload, enqueued_at) VALUES (?, ?, ?)",
                (kind, json.dumps(payload, default=str), time.time()),
            )

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._drain_loop(), name="spool-drain")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            await self._task

    async def _drain_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                drained = await self._drain_once()
                if drained == 0:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                log.warning("spool.drain_error", error=str(e))
                await asyncio.sleep(self._interval)

    async def _drain_once(self) -> int:
        rows = self._conn.execute(
            "SELECT id, kind, payload, attempts FROM spool "
            "ORDER BY id ASC LIMIT ?",
            (self._max_batch,),
        ).fetchall()
        if not rows:
            return 0

        items: list[tuple[int, str, dict[str, Any], int]] = []
        for row_id, kind, payload_json, attempts in rows:
            try:
                items.append((row_id, kind, json.loads(payload_json), attempts))
            except Exception as e:
                self._conn.execute(
                    "UPDATE spool SET attempts = attempts + 1, last_error = ? "
                    "WHERE id = ?",
                    (f"payload_decode_error: {e}", row_id),
                )
                return 0

        # Fast path: one session for the whole batch. Neon round-trip
        # dominates single-row writes; batching amortizes it.
        batch = [(k, p) for (_id, k, p, _a) in items]
        apply_batch = getattr(self._sink, "apply_batch", None)
        if apply_batch is not None:
            try:
                t0 = time.time()
                await asyncio.to_thread(apply_batch, batch)
                elapsed_ms = int((time.time() - t0) * 1000)
                ids = [i for (i, _, _, _) in items]
                placeholders = ",".join("?" * len(ids))
                self._conn.execute(
                    f"DELETE FROM spool WHERE id IN ({placeholders})", ids
                )
                log.info("spool.batch_drained",
                         batch_size=len(items), elapsed_ms=elapsed_ms)
                return len(items)
            except Exception as e:
                log.info("spool.batch_failed_falling_back",
                         batch_size=len(items), error=str(e))

        # Slow path: per-row so a bad row can be isolated + retried.
        drained = 0
        for row_id, kind, payload, attempts in items:
            try:
                await asyncio.to_thread(self._sink, kind, payload)
                self._conn.execute("DELETE FROM spool WHERE id = ?", (row_id,))
                drained += 1
            except Exception as e:
                self._conn.execute(
                    "UPDATE spool SET attempts = attempts + 1, last_error = ? "
                    "WHERE id = ?",
                    (str(e), row_id),
                )
                log.info("spool.write_deferred", kind=kind,
                         attempts=attempts + 1, error=str(e))
                break
        return drained

    def pending_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM spool").fetchone()[0]
