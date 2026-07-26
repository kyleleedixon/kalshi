"""Heartbeat writer.

Every ``heartbeat_interval_sec`` the engine spools a heartbeat row. The
dashboard reads staleness; a stale heartbeat is the dead-man's-switch signal
that the engine is dead (Oracle Cloud reclaim, crash). Positions remain safe
by construction — they live on Kalshi and settle on their own, and the kill
switch lives in Neon not on the box.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from .. import __version__
from .spool import SpooledWriter


class Heartbeater:
    def __init__(self, writer: SpooledWriter, engine_id: str, phase: str,
                 interval_sec: float) -> None:
        self._writer = writer
        self._engine_id = engine_id
        self._phase = phase
        self._interval = interval_sec
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._loop(), name="heartbeat")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            await self._task

    async def beat_once(self, notes: str | None = None) -> None:
        await self._writer.enqueue("heartbeat", {
            "engine_id": self._engine_id,
            "last_beat": datetime.now(timezone.utc).isoformat(),
            "phase": self._phase,
            "version": __version__,
            "notes": notes,
        })

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            await self.beat_once()
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
