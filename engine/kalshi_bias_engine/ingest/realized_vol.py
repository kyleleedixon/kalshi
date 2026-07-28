"""Rolling realized-vol estimator with per-horizon sample tracking.

Feeds ``KrakenDigitalOptionOracle``. The tick-count field is critical: the
oracle widens its confidence interval where the vol sample is thin, and
refuses to emit (staleness flag) rather than emit garbage. That refusal
cannot happen if the vol layer silently drops sample-size information.

Vol is annualized: sigma_annual = sqrt( sum(log_returns^2) / horizon_seconds
                                       * SECONDS_PER_YEAR ).
Crypto trades 24/7 so we use 365.25 * 86_400.

Insufficient-window guard: annualizing a small observed window (e.g. 60s
of ticks projected to a 24h horizon) produces wild sigma. A single 3%
tick in a 60s window annualizes to >2000% vol. If the collected window
covers less than ``MIN_WINDOW_FRACTION`` of the requested horizon we
return sigma_annualized=0 so the oracle marks the estimate stale rather
than pricing off garbage. As a belt-and-suspenders check we also cap the
returned sigma at ``MAX_SANE_ANNUAL_SIGMA``.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

SECONDS_PER_YEAR = 365.25 * 86_400.0

# Require the observed window to cover at least this fraction of the
# horizon before we trust the annualized number.
MIN_WINDOW_FRACTION = 0.25

# Hard sanity cap on annualized sigma. BTC/ETH realised vol in extreme
# stress rarely exceeds ~200% annualized; anything above this is almost
# certainly a data spike or short-window artifact.
MAX_SANE_ANNUAL_SIGMA = 5.0


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
        return self._snapshot(ts_epoch)

    def snapshot(self) -> VolPoint | None:
        """Compute a VolPoint from current state without mutating."""
        if not self._points:
            return None
        return self._snapshot(self._points[-1][0])

    def _snapshot(self, ts_epoch: float) -> VolPoint:
        n = len(self._points)
        if n < 2:
            return VolPoint(
                underlying=self.underlying,
                horizon_seconds=self.horizon_seconds,
                sigma_annualized=0.0,
                tick_count=n,
                data_ts=datetime.fromtimestamp(ts_epoch, tz=timezone.utc),
            )

        pts = self._points
        window = pts[-1][0] - pts[0][0]

        # Insufficient window: refuse to annualize noise.
        if window < MIN_WINDOW_FRACTION * self.horizon_seconds:
            return VolPoint(
                underlying=self.underlying,
                horizon_seconds=self.horizon_seconds,
                sigma_annualized=0.0,
                tick_count=n,
                data_ts=datetime.fromtimestamp(ts_epoch, tz=timezone.utc),
            )

        s2 = 0.0
        prev_lp = pts[0][1]
        for i in range(1, n):
            lp = pts[i][1]
            r = lp - prev_lp
            s2 += r * r
            prev_lp = lp
        sigma_per_sec = math.sqrt(s2 / max(window, 1e-9))
        sigma_annual = min(sigma_per_sec * math.sqrt(SECONDS_PER_YEAR), MAX_SANE_ANNUAL_SIGMA)

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
        return r.snapshot()
