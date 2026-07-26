"""Calibration types read by the ExecutionPolicy.

Calibration is CONTINUOUS: the calibration module recomputes per-band
per-domain reliability every cycle and stores snapshots in Neon. The
ExecutionPolicy consumes the latest snapshot; it does not size on raw
theoretical edge.

Aggregate calibration hides band-level errors — the strategy specifically
exploits price-band-shaped bias, so calibration is reported per band per
domain, never pooled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class BandKey:
    """A calibration bucket.

    Buckets are (domain, price_band). Price bands are inclusive-lower,
    exclusive-upper deciles of Kalshi implied probability. Domain lets
    per-domain calibration diverge without contaminating pooled fits.
    """

    domain: str
    price_band_lo: float
    price_band_hi: float

    def contains(self, price: float) -> bool:
        return self.price_band_lo <= price < self.price_band_hi


@dataclass(frozen=True)
class BandStats:
    """Out-of-sample reliability stats for one band."""

    settled_count: int
    empirical_freq: float          # fraction of YES settlements in band
    mean_predicted: float          # mean adjusted probability we predicted
    brier: float
    brier_vs_uncorrected: float    # negative => bias adjustment helped
    updated_at: datetime


@dataclass(frozen=True)
class CalibrationReport:
    """The object the ExecutionPolicy reads.

    ``bands`` is keyed by BandKey. If a (domain, band) has no BandStats or
    ``settled_count`` below the phase-gate minimum, the ExecutionPolicy MUST
    size the position at zero regardless of apparent edge. This is the
    single most important guard in the system: the failure mode of this
    strategy class is confusing model error for market error, and the only
    defense is refusing to size on untested bands.
    """

    generated_at: datetime
    bands: dict[BandKey, BandStats]
    phase_gate_min_sample: int

    def stats_for(self, domain: str, price: float) -> BandStats | None:
        for k, v in self.bands.items():
            if k.domain == domain and k.contains(price):
                return v
        return None

    def band_is_gated(self, domain: str, price: float) -> bool:
        """True if this (domain, price) band lacks sufficient settled sample
        to permit sizing. Fail-closed: unknown => True (gated)."""

        s = self.stats_for(domain, price)
        if s is None:
            return True
        return s.settled_count < self.phase_gate_min_sample
