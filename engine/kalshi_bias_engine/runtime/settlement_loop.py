"""Settlement ingestion loop.

Periodically finds contracts whose ``settlement_time`` has passed but have
no row in ``settlements``, calls Kalshi's ``get_market`` for each, parses
the settled outcome, and enqueues a ``settlement`` write. Feeds calibration
and per-band Brier scoring.

Fail-open on individual market failures: if Kalshi returns 5xx on one
market, log and continue — the next tick will pick it up. Never let one
bad market block draining the queue.

We deliberately do NOT poll the whole markets endpoint here: for a small
number of open-but-past-close contracts, per-ticker GETs are cheaper and
avoid interfering with the trading loop's paginated discovery.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog

from sqlalchemy import select

from ..ingest.kalshi_client import KalshiClient
from ..storage.db import session_scope
from ..storage.models import Contract, Settlement
from ..storage.spool import SpooledWriter

log = structlog.get_logger(__name__)


def _pending_settlements(now: datetime, limit: int = 200) -> list[dict[str, Any]]:
    """Return contracts past settlement_time with no settlements row.

    LEFT JOIN via subquery so we don't materialize every settled row.
    """
    with session_scope() as sess:
        settled_pks = select(Settlement.contract_pk).subquery()
        rows = sess.execute(
            select(Contract.contract_id, Contract.settlement_time)
            .where(Contract.settlement_time.is_not(None))
            .where(Contract.settlement_time <= now)
            .where(Contract.id.not_in(select(settled_pks)))
            .order_by(Contract.settlement_time.asc())
            .limit(limit)
        ).all()
    return [
        {"contract_id": r[0], "settlement_time": r[1]}
        for r in rows
    ]


def _parse_settled(market: dict[str, Any]) -> tuple[str, float | None, datetime | None] | None:
    """Extract outcome + optional settlement value + settled_at.

    Returns None if the market is not yet settled (poll again later).
    Kalshi's shape varies over time; be permissive.
    """
    m = market.get("market") or market
    status = (m.get("status") or "").lower()
    if status not in ("settled", "finalized"):
        return None

    outcome: str | None = None
    for key in ("result", "settled_result", "settlement", "final_result"):
        v = m.get(key)
        if isinstance(v, str) and v:
            outcome = v.upper()
            break
    if outcome is None:
        return None
    if outcome not in ("YES", "NO", "VOID"):
        return None

    value: float | None = None
    for key in ("settlement_value", "result_value", "expiration_value"):
        v = m.get(key)
        if v is None:
            continue
        try:
            value = float(v)
            break
        except (TypeError, ValueError):
            continue

    settled_at: datetime | None = None
    for key in ("settled_time", "settlement_time", "expiration_time",
                "close_time"):
        v = m.get(key)
        if not v:
            continue
        try:
            settled_at = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            break
        except ValueError:
            continue

    return outcome, value, settled_at


async def _ingest_one(
    kalshi: KalshiClient, writer: SpooledWriter, contract_id: str
) -> bool:
    try:
        market = await kalshi.get_market(contract_id)
    except Exception as e:
        log.warning("settlement.fetch_failed", contract=contract_id, error=str(e))
        return False
    parsed = _parse_settled(market)
    if parsed is None:
        return False
    outcome, value, settled_at = parsed
    now = datetime.now(timezone.utc)
    await writer.enqueue("settlement", {
        "contract_id": contract_id,
        "outcome": outcome,
        "settlement_value": value,
        "settled_at": (settled_at or now).isoformat(),
        "ingest_ts": now.isoformat(),
    })
    log.info("settlement.recorded", contract=contract_id, outcome=outcome)
    return True


class SettlementIngestor:
    """Periodic settlement ingestion. Owned by the engine runtime."""

    def __init__(
        self,
        kalshi: KalshiClient,
        writer: SpooledWriter,
        *,
        interval_sec: float,
        batch_limit: int = 100,
    ) -> None:
        self._kalshi = kalshi
        self._writer = writer
        self._interval = interval_sec
        self._batch_limit = batch_limit
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name="settlement-ingest")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._once()
            except Exception as e:
                log.warning("settlement.loop_error", error=str(e))
            try:
                await asyncio.wait_for(self._stopping.wait(),
                                       timeout=self._interval)
            except asyncio.TimeoutError:
                pass

    async def _once(self) -> None:
        now = datetime.now(timezone.utc)
        pending = _pending_settlements(now, limit=self._batch_limit)
        if not pending:
            return
        log.info("settlement.pending", count=len(pending))
        for row in pending:
            await _ingest_one(self._kalshi, self._writer, row["contract_id"])
