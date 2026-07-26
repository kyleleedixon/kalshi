"""ComposedBiasModel.

Applies registered BiasFeatures additively in log-odds space. Order does not
change the composed result (addition commutes), but it does change the
sequence stored in ledger provenance — we keep insertion order for
readability.
"""

from __future__ import annotations

import math
from typing import Iterable

from ..core.bias import BiasAdjustment, BiasFeature, BiasModel
from ..core.contract import Contract
from ..core.estimate import ProbEstimate


def _logit(p: float) -> float:
    eps = 1e-9
    p = min(max(p, eps), 1.0 - eps)
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


class ComposedBiasModel(BiasModel):
    def __init__(self) -> None:
        self._features: list[BiasFeature] = []

    def register(self, feature: BiasFeature) -> None:
        if any(f.name == feature.name for f in self._features):
            raise ValueError(f"bias feature already registered: {feature.name}")
        self._features.append(feature)

    def features(self) -> Iterable[BiasFeature]:
        return tuple(self._features)

    def adjust(
        self,
        contract: Contract,
        raw: ProbEstimate,
        kalshi_price: float,
    ) -> tuple[float, list[BiasAdjustment]]:
        applicable = [
            f for f in self._features
            if "*" in f.applies_to_domains or contract.domain in f.applies_to_domains
        ]
        adjustments: list[BiasAdjustment] = []
        z = _logit(raw.p)
        for f in applicable:
            adj = f.compute(contract, raw, kalshi_price)
            adjustments.append(adj)
            z += adj.delta
        return _sigmoid(z), adjustments
