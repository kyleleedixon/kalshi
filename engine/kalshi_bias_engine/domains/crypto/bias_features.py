"""Crypto-specific bias features.

Round-number strike distance: retail on Kalshi disproportionately quotes
round strikes (60000, 65000, 100000, ...) — flow clusters there and the
implied probability at those strikes shows a signature deviation from the
lognormal model. Feature is domain=crypto only.

Fitting stub: parameters are set by the bias-fitting pipeline in the
``bias`` module. Here we implement compute + a placeholder fit that stores
whatever the fitting pipeline passes in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ...core.bias import BiasAdjustment, BiasFeature
from ...core.contract import Contract
from ...core.estimate import ProbEstimate
from ...oracles.kraken_digital import FEAT_STRIKE


def _log10_round_distance(strike: float) -> float:
    """0 for perfectly round (10^k) strikes, larger as strike moves off-round.

    We measure ``d = |log10(strike) - round(log10(strike))|`` which is small
    for values like 100, 1000, 10000 and grows monotonically as the strike
    deviates from a power-of-ten anchor.
    """

    if strike <= 0:
        return 0.0
    l = math.log10(strike)
    return abs(l - round(l))


@dataclass
class RoundNumberDistanceFeature(BiasFeature):
    name: str = "round_number_distance_crypto"
    applies_to_domains: tuple[str, ...] = ("crypto",)

    # additive log-odds slope * distance
    beta: float = 0.0
    _evidence_ok: bool = False
    _params_meta: dict = field(default_factory=dict)

    def compute(
        self,
        contract: Contract,
        raw: ProbEstimate,
        kalshi_price: float,
    ) -> BiasAdjustment:
        strike = contract.feature(FEAT_STRIKE)
        if strike is None:
            return BiasAdjustment(
                feature_name=self.name, delta=0.0,
                params_snapshot={"reason": "no_strike"},
                evidence_ok=self._evidence_ok,
            )
        d = _log10_round_distance(float(strike))
        # If the feature has no supporting evidence, emit zero delta but
        # still record what would have been applied for provenance.
        applied = self.beta * d if self._evidence_ok else 0.0
        return BiasAdjustment(
            feature_name=self.name,
            delta=applied,
            params_snapshot={"beta": self.beta, "distance": d},
            evidence_ok=self._evidence_ok,
        )

    def fit(self, dataset) -> None:
        # The actual fit is performed by ``bias.fit.fit_feature`` which
        # writes parameters via ``load_params``. This body is a placeholder
        # so the interface contract is satisfied; unit tests should exercise
        # the fitting pipeline, not this stub.
        raise NotImplementedError("Use bias.fit.fit_feature to fit this feature")

    def load_params(self, *, beta: float, evidence_ok: bool, meta: dict) -> None:
        self.beta = beta
        self._evidence_ok = evidence_ok
        self._params_meta = meta

    def evidence_ok(self) -> bool:
        return self._evidence_ok
