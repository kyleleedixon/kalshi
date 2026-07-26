"""BiasModel interface.

Maps (raw oracle probability, contract features) -> adjusted probability.

Design invariants:
  * Biases are VENUE-level phenomena (Kalshi retail flow), not asset-level.
    The favorite-longshot curve is fit on Kalshi price bands, pooled across
    domains, with per-domain offsets. In V1 with one domain the hierarchy
    collapses to a single fit, but the fitting code is written against the
    hierarchical structure so a second domain slots in without a refactor.
  * Each feature transform is INDEPENDENT and TOGGLEABLE. A feature that
    fails its out-of-sample evidence gate is turned off automatically —
    the code, not the modeler, makes that call.
  * Domain-specific features (round-number strikes are a crypto thing) live
    in the domain module and register themselves. Core fitting code never
    imports anything crypto-specific.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable

from .contract import Contract
from .estimate import ProbEstimate


@dataclass(frozen=True)
class BiasAdjustment:
    """A single feature's adjustment to raw probability.

    The BiasModel applies transforms in sequence; each returns its own
    adjustment for provenance, and the final adjusted probability is the
    composed result. Ledger stores the full list so any adjusted probability
    is reconstructable.
    """

    feature_name: str
    delta: float                      # additive in log-odds space (see notes)
    params_snapshot: dict = field(default_factory=dict)
    evidence_ok: bool = True          # False -> feature was toggled off


class BiasFeature(ABC):
    """One toggleable, independently-fit bias feature.

    Implementations should compose additively in log-odds space so that
    features can be enabled/disabled independently without re-fitting the
    others. (Additive-in-logit means adjusted logit(p) = raw logit(p) + sum
    of feature deltas; this is the well-behaved default.)
    """

    name: str = "abstract"

    #: Domains this feature applies to. Use ("*",) for venue-wide features
    #: like the longshot curve. Domain-specific features (round-number
    #: strike distance) list their domain explicitly.
    applies_to_domains: tuple[str, ...] = ("*",)

    @abstractmethod
    def compute(
        self,
        contract: Contract,
        raw: ProbEstimate,
        kalshi_price: float,
    ) -> BiasAdjustment:
        """Return this feature's contribution to the log-odds adjustment."""

    @abstractmethod
    def fit(self, dataset) -> None:  # dataset shape defined by calibration module
        """Fit parameters from settled contracts. MUST enforce time-based
        in-sample/out-of-sample split — see the calibration module."""

    @abstractmethod
    def evidence_ok(self) -> bool:
        """True if latest out-of-sample evidence supports keeping this feature
        on. If False, ``compute`` should still return a zero-delta adjustment
        with evidence_ok=False so the ledger records that the feature was
        considered but disabled."""


class BiasModel(ABC):
    """Composes a set of BiasFeatures into a single adjusted probability."""

    @abstractmethod
    def register(self, feature: BiasFeature) -> None:
        """Register a feature. Domain modules call this at import time."""

    @abstractmethod
    def features(self) -> Iterable[BiasFeature]: ...

    @abstractmethod
    def adjust(
        self,
        contract: Contract,
        raw: ProbEstimate,
        kalshi_price: float,
    ) -> tuple[float, list[BiasAdjustment]]:
        """Return (adjusted_p, per-feature adjustments) for ledger provenance."""
