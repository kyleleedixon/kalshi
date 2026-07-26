"""ProbEstimate — an oracle's probability estimate WITH confidence attached.

A probability without a confidence measure is a bug, not an output. The
ExecutionPolicy sizes on calibrated edge — which requires knowing not just
what the oracle said, but how confident it was and whether its inputs were
usable. Downstream code must be able to distinguish "70% and I'm sure" from
"70% but my vol sample is thin and the settlement basis is noisy."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class StalenessReason(str, Enum):
    """Why an oracle refused to emit or flagged an estimate as stale.

    ExecutionPolicy treats staleness as an EXIT condition, not a HOLD.
    Missing/thin data is exactly the regime this strategy must not trade in.
    """

    FRESH = "FRESH"
    THIN_SAMPLE = "THIN_SAMPLE"
    STALE_QUOTE = "STALE_QUOTE"
    STALE_SPOT = "STALE_SPOT"
    ILLIQUID_REFERENCE = "ILLIQUID_REFERENCE"
    MODEL_OUT_OF_DOMAIN = "MODEL_OUT_OF_DOMAIN"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"


@dataclass(frozen=True)
class ProbEstimate:
    """An oracle probability estimate with confidence and provenance.

    Attributes
    ----------
    p
        Point probability in [0, 1] of the YES outcome for the contract.
    variance
        Variance of the point estimate (parameter uncertainty, NOT the
        variance of the underlying outcome — that is a Bernoulli with
        variance p*(1-p) and is redundant to report). Feeds sizing.
    effective_sample_size
        Rough count of independent observations backing the estimate. For a
        realized-vol-driven oracle this is the tick count in the horizon
        window. Zero => oracle should have raised staleness.
    data_timestamp
        Timestamp of the freshest input that fed this estimate. NOT the
        wall-clock time the estimate was computed.
    staleness
        FRESH means safe to trade on. Anything else means the ExecutionPolicy
        must refuse to open and MUST exit any existing position.
    provenance
        Free-form dict of upstream inputs (spot, vol, strike, tail-inflation
        parameter used, basis correction) so any live decision is
        reconstructable from the ledger.
    """

    p: float
    variance: float
    effective_sample_size: int
    data_timestamp: datetime
    staleness: StalenessReason = StalenessReason.FRESH
    provenance: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (0.0 <= self.p <= 1.0):
            raise ValueError(f"p out of [0,1]: {self.p!r}")
        if self.variance < 0.0:
            raise ValueError(f"variance must be non-negative: {self.variance!r}")
        if self.effective_sample_size < 0:
            raise ValueError(
                f"effective_sample_size must be non-negative: {self.effective_sample_size!r}"
            )

    @property
    def is_fresh(self) -> bool:
        return self.staleness is StalenessReason.FRESH
