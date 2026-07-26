"""Venue-level (domain-agnostic) bias features.

Each feature is independent, toggleable, and additive in log-odds space so
its parameters can be fit without disturbing the others.

The favorite-longshot curve is the load-bearing one for V1 — it captures
the retail tendency to overpay for cheap contracts and underpay for
near-certain contracts. Fit hierarchically: a pooled Kalshi-wide curve plus
per-domain offsets. With one domain the offset collapses to zero and the
model degenerates to a single-domain fit; adding sports later borrows
strength from the pooled curve until the sports sample settles.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..core.bias import BiasAdjustment, BiasFeature
from ..core.contract import Contract
from ..core.estimate import ProbEstimate


@dataclass
class LongshotCurveFeature(BiasFeature):
    """Additive log-odds correction as a smooth function of Kalshi price.

    Parameterization::

        delta = alpha_pool * f(kalshi_price) + alpha_domain[domain] * f(kalshi_price)

    where ``f(p)`` is a fixed basis chosen so ``delta`` is signed the right
    way at the extremes:

        f(p) = logit(p) - logit(0.5)  =  logit(p)

    The pooled slope + domain offset structure is a minimal
    partial-pooling scheme: with one domain, alpha_domain=0 and only the
    pooled coefficient does work. With two+ domains the fit updates both;
    a domain with thin sample stays close to the pooled slope.
    """

    name: str = "longshot_curve"
    applies_to_domains: tuple[str, ...] = ("*",)

    alpha_pool: float = 0.0
    alpha_domain: dict[str, float] = field(default_factory=dict)
    _evidence_ok: bool = False
    _params_meta: dict = field(default_factory=dict)

    def compute(
        self,
        contract: Contract,
        raw: ProbEstimate,
        kalshi_price: float,
    ) -> BiasAdjustment:
        f = _logit(kalshi_price)
        slope = self.alpha_pool + self.alpha_domain.get(contract.domain, 0.0)
        applied = slope * f if self._evidence_ok else 0.0
        return BiasAdjustment(
            feature_name=self.name,
            delta=applied,
            params_snapshot={
                "alpha_pool": self.alpha_pool,
                "alpha_domain": self.alpha_domain.get(contract.domain, 0.0),
                "kalshi_price": kalshi_price,
            },
            evidence_ok=self._evidence_ok,
        )

    def fit(self, dataset) -> None:  # noqa: D401
        raise NotImplementedError("Use bias.fit.fit_feature")

    def load_params(self, *, alpha_pool: float, alpha_domain: dict[str, float],
                    evidence_ok: bool, meta: dict) -> None:
        self.alpha_pool = alpha_pool
        self.alpha_domain = dict(alpha_domain)
        self._evidence_ok = evidence_ok
        self._params_meta = meta

    def evidence_ok(self) -> bool:
        return self._evidence_ok


@dataclass
class RecencyMomentumFeature(BiasFeature):
    """Adjusts for retail momentum-chasing on short-horizon crypto.

    Signal: signed log return of the underlying over the previous window
    (spec fits the window; default 60s). Applied additively in log-odds.
    Applies venue-wide but is only informative where the domain module
    supplies a ``recent_log_return`` provenance field.
    """

    name: str = "recency_momentum"
    applies_to_domains: tuple[str, ...] = ("*",)

    beta: float = 0.0
    _evidence_ok: bool = False
    _params_meta: dict = field(default_factory=dict)

    def compute(
        self,
        contract: Contract,
        raw: ProbEstimate,
        kalshi_price: float,
    ) -> BiasAdjustment:
        r = raw.provenance.get("recent_log_return")
        if r is None:
            return BiasAdjustment(self.name, 0.0,
                                  {"reason": "no_recent_return"},
                                  self._evidence_ok)
        applied = self.beta * float(r) if self._evidence_ok else 0.0
        return BiasAdjustment(self.name, applied,
                              {"beta": self.beta, "r": r},
                              self._evidence_ok)

    def fit(self, dataset) -> None:
        raise NotImplementedError("Use bias.fit.fit_feature")

    def load_params(self, *, beta: float, evidence_ok: bool, meta: dict) -> None:
        self.beta = beta
        self._evidence_ok = evidence_ok
        self._params_meta = meta

    def evidence_ok(self) -> bool:
        return self._evidence_ok


@dataclass
class SessionLiquidityFeature(BiasFeature):
    """Widens/narrows adjustment based on time-of-day liquidity regime.

    Uses ``session_bucket`` from provenance (an integer 0..N-1 the ingest
    layer stamps on each raw quote). Learns a per-bucket log-odds shift.
    """

    name: str = "session_liquidity"
    applies_to_domains: tuple[str, ...] = ("*",)

    shifts: dict[int, float] = field(default_factory=dict)
    _evidence_ok: bool = False
    _params_meta: dict = field(default_factory=dict)

    def compute(
        self,
        contract: Contract,
        raw: ProbEstimate,
        kalshi_price: float,
    ) -> BiasAdjustment:
        b = raw.provenance.get("session_bucket")
        if b is None:
            return BiasAdjustment(self.name, 0.0,
                                  {"reason": "no_session_bucket"},
                                  self._evidence_ok)
        shift = float(self.shifts.get(int(b), 0.0)) if self._evidence_ok else 0.0
        return BiasAdjustment(self.name, shift,
                              {"bucket": int(b), "shift": shift},
                              self._evidence_ok)

    def fit(self, dataset) -> None:
        raise NotImplementedError("Use bias.fit.fit_feature")

    def load_params(self, *, shifts: dict[int, float], evidence_ok: bool, meta: dict) -> None:
        self.shifts = {int(k): float(v) for k, v in shifts.items()}
        self._evidence_ok = evidence_ok
        self._params_meta = meta

    def evidence_ok(self) -> bool:
        return self._evidence_ok


def _logit(p: float) -> float:
    eps = 1e-9
    p = min(max(p, eps), 1.0 - eps)
    return math.log(p / (1.0 - p))
