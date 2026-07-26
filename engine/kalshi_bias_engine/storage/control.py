"""Control-plane reads: kill switch and per-domain phase gates.

READS fail closed. If we cannot reach Neon to freshly read the kill-switch
row, the engine treats the switch as ACTIVE (no new orders). This is the
dead-man's-switch semantics section 0 of the spec calls for.

Reads are cached with a short TTL so we don't hammer Neon; the TTL is
short enough (default 3s) that a dashboard toggle propagates before the
next order decision.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import select

from .db import session_scope
from .models import Control

log = structlog.get_logger(__name__)


@dataclass
class ControlState:
    kill_switch_active: bool
    phase_gates: dict[str, dict[str, Any]]
    fetched_at: float
    fresh: bool  # False when we fell back to fail-closed defaults


_FAIL_CLOSED = ControlState(
    kill_switch_active=True,   # <- fail closed
    phase_gates={},
    fetched_at=0.0,
    fresh=False,
)


class ControlReader:
    def __init__(self, ttl_seconds: float = 3.0) -> None:
        self._ttl = ttl_seconds
        self._cache: ControlState | None = None

    def get(self) -> ControlState:
        now = time.time()
        if self._cache and (now - self._cache.fetched_at) < self._ttl and self._cache.fresh:
            return self._cache

        try:
            with session_scope() as sess:
                rows = sess.execute(select(Control)).scalars().all()
            data = {r.key: r.value for r in rows}
            kill = bool(data.get("kill_switch", {}).get("active", True))
            gates = data.get("phase_gates", {}) or {}
            self._cache = ControlState(
                kill_switch_active=kill,
                phase_gates=gates,
                fetched_at=now,
                fresh=True,
            )
            return self._cache
        except Exception as e:
            log.warning("control.read_failed_failing_closed", error=str(e))
            return _FAIL_CLOSED

    def domain_unlocked(self, domain: str) -> bool:
        state = self.get()
        if not state.fresh:
            return False
        gate = state.phase_gates.get(domain) or {}
        return bool(gate.get("unlocked", False))
