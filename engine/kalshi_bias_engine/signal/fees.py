"""Kalshi fee schedule.

As of writing, Kalshi's per-contract trading fee follows a p*(1-p) curve:

    fee_per_contract = ceil(rate * price * (1 - price) * 100) / 100 dollars

where ``rate`` is currently 0.07 (7%) for standard markets and lower for
certain event categories. Verify against Kalshi docs before Phase 2 — the
rate has changed historically. Do NOT hardcode the rate anywhere else.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class KalshiFeeSchedule:
    rate: float = 0.07  # verify current per Kalshi docs

    def per_contract(self, price_dollars: float) -> float:
        p = max(min(price_dollars, 1.0), 0.0)
        raw = self.rate * p * (1.0 - p)
        # Kalshi rounds up to the next cent per contract.
        return math.ceil(raw * 100.0) / 100.0

    def round_trip(self, entry_price: float, exit_price: float) -> float:
        return self.per_contract(entry_price) + self.per_contract(exit_price)
