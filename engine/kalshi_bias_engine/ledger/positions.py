"""Live position accumulator.

Reads ``paper_orders`` + ``paper_fills`` from Neon and folds them into a
:class:`PositionSnapshot` scoped per-market / per-underlying / per-domain
/ aggregate. The :class:`PaperExecutionPolicy` reads this at decide time
via a callable so risk-limit checks are always against ledger truth, not
against something the loop remembered.

Cache
-----
A short TTL (default 2s) keeps decide-time under one round trip when the
loop is fast. Cache misses on Neon transience fall back to the last
snapshot rather than an empty one — an empty snapshot would let the
policy blow through per-market limits during a Neon blip.

Sign convention
---------------
YES OPEN → +size (long the event happening).
YES CLOSE → -size.
NO  OPEN → -size (short the event happening, stored as negative YES).
NO  CLOSE → +size.

Storing NO as negative YES means a single integer per market captures net
exposure and the policy's limits (which are symmetric) work uniformly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import structlog

from sqlalchemy import select

from ..storage.db import session_scope
from ..storage.models import Contract, PaperFill, PaperOrder
from .paper_policy import PositionSnapshot

log = structlog.get_logger(__name__)


def _signed_delta(side: str, action: str, filled: int) -> int:
    """Convert (side, action, filled_qty) into a signed YES-equivalent
    position delta."""
    sign = 1 if side.upper() == "YES" else -1
    direction = 1 if action.upper() == "OPEN" else -1
    return sign * direction * filled


def build_snapshot() -> PositionSnapshot:
    """Fold all fills into a fresh PositionSnapshot.

    Uses filled quantity (join paper_fills → paper_orders) rather than
    trusting ``hypothetical_fill_size`` on the order alone — the fill
    row is what a real venue would give us in Phase 2, and we want the
    paper ledger to match that boundary exactly.
    """

    snap = PositionSnapshot()
    with session_scope() as sess:
        # Sum of fills grouped by order.
        rows = sess.execute(
            select(
                PaperOrder.side,
                PaperOrder.action,
                Contract.contract_id,
                Contract.underlying,
                Contract.domain,
                PaperFill.size_contracts,
            )
            .join(Contract, Contract.id == PaperOrder.contract_pk)
            .join(PaperFill, PaperFill.paper_order_pk == PaperOrder.id)
        ).all()

    for side, action, contract_id, underlying, domain, size in rows:
        delta = _signed_delta(side, action, int(size))
        snap.per_market[contract_id] = snap.per_market.get(contract_id, 0) + delta
        snap.per_underlying[underlying] = snap.per_underlying.get(underlying, 0) + delta
        snap.per_domain[domain] = snap.per_domain.get(domain, 0) + delta
        snap.aggregate += delta
    return snap


@dataclass
class _Cached:
    snap: PositionSnapshot
    at: float


class CachingPositionsProvider:
    """Callable that returns a PositionSnapshot; caches for ``ttl``.

    Falls back to the last good snapshot on a read error so a Neon blip
    can't reset the policy's view of live exposure to zero. Only the
    initial call (before we have any snapshot) can return an empty
    PositionSnapshot on failure — the loop will already be logging the
    root-cause error via structlog in that case.
    """

    def __init__(self, ttl_seconds: float = 2.0) -> None:
        self._ttl = ttl_seconds
        self._cached: _Cached | None = None

    def __call__(self) -> PositionSnapshot:
        now = time.monotonic()
        if self._cached and (now - self._cached.at) < self._ttl:
            return self._cached.snap
        try:
            snap = build_snapshot()
        except Exception as e:
            log.warning("positions.refresh_failed", error=str(e))
            if self._cached:
                return self._cached.snap
            return PositionSnapshot()
        self._cached = _Cached(snap=snap, at=now)
        return snap
