"""Rolling realized-vol estimator with per-horizon sample tracking.

Feeds ``KrakenDigitalOptionOracle``. The tick-count field is critical: the
oracle widens its confidence interval where the vol sample is thin, and
refuses to emit (staleness flag) rather than emit garbage. That refusal
cannot happen if the vol layer silently drops sample-size information.

Vol is annualized: sigma_annual = sqrt( sum(log_returns^2) / horizon_seconds
                                       * SECONDS_PER_YEAR ).
Crypto trades 24/7 so we use 365.25 * 86_400.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

SECONDS_PER_YEAR = 365.25 * 86_400.0


@dataclass
class VolPoint:
    underlying: str
    horizon_seconds: int
    sigma_annualized: float
    tick_count: int
    data_ts: datetime


class RollingVol:
    """One (underlying, horizon) rolling estimator.

    We keep (timestamp, log_price) pairs and drop points older than the
    horizon. Log-returns are computed adjacent-pair. Cheap and correct for
    the tick rates we deal with; if it gets expensive we swap in a
    Welford-style online variance keyed by horizon buckets.
    """

    def __init__(self, underlying: str, horizon_seconds: int) -> None:
        self.underlying = underlying
        self.horizon_seconds = horizon_seconds
        self._points: deque[tuple[float, float]] = deque()  # (ts, log_price)

    def update(self, ts_epoch: float, price: float) -> VolPoint:
        if price <= 0:
            raise ValueError(f"non-positive price: {price!r}")
        self._points.append((ts_epoch, math.log(price)))
        cutoff = ts_epoch - self.horizon_seconds
        while self._points and self._points[0][0] < cutoff:
            self._points.popleft()

        n = len(self._points)
        if n < 2:
            return VolPoint(
                underlying=self.underlying,
                horizon_seconds=self.horizon_seconds,
                sigma_annualized=0.0,
                tick_count=n,
                data_ts=datetime.fromtimestamp(ts_epoch, tz=timezone.utc),
            )

        s2 = 0.0
        pts = list(self._points)
        for i in range(1, len(pts)):
            r = pts[i][1] - pts[i - 1][1]
            s2 += r * r
        window = max(pts[-1][0] - pts[0][0], 1e-9)
        sigma_per_sec = math.sqrt(s2 / window)
        sigma_annual = sigma_per_sec * math.sqrt(SECONDS_PER_YEAR)

        return VolPoint(
            underlying=self.underlying,
            horizon_seconds=self.horizon_seconds,
            sigma_annualized=sigma_annual,
            tick_count=n,
            data_ts=datetime.fromtimestamp(ts_epoch, tz=timezone.utc),
        )


class MultiHorizonVol:
    """Per-underlying, per-horizon vol store."""

    DEFAULT_HORIZONS = (300, 900, 3600, 86_400)  # 5m, 15m, 1h, 24h

    def __init__(self, horizons: tuple[int, ...] = DEFAULT_HORIZONS) -> None:
        self.horizons = horizons
        self._roll: dict[tuple[str, int], RollingVol] = {}

    def update(self, underlying: str, ts_epoch: float, price: float) -> list[VolPoint]:
        out: list[VolPoint] = []
        for h in self.horizons:
            key = (underlying, h)
            r = self._roll.get(key)
            if r is None:
                r = RollingVol(underlying, h)
                self._roll[key] = r
            out.append(r.update(ts_epoch, price))
        return out

    def latest(self, underlying: str, horizon: int) -> VolPoint | None:
        r = self._roll.get((underlying, horizon))
        if r is None:
            return None
        # Peek: replay the last recorded point through a no-op update by
        # emitting a synthetic VolPoint from the current deque.
        if not r._points:  # noqa: SLF001
            return None
        ts, lp = r._points[-1]  # noqa: SLF001
        return r.update(ts, math.exp(lp))
