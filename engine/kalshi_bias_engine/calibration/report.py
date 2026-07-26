"""Calibration report builder + loader.

Aggregate calibration hides band-level errors. We bucket by (domain, price
decile) and report Brier per bucket, plus Brier improvement vs. the
uncorrected oracle. The report is a first-class object the ExecutionPolicy
reads — do NOT compute this on-demand at decision time.

Time-based split is enforced by the fitting pipeline (see ``bias/fit.py``).
This module operates on OOS records only: reliability of decisions made
against parameters fit strictly earlier than the decision timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select

from ..core.calibration import BandKey, BandStats, CalibrationReport
from ..storage.db import session_scope
from ..storage.models import CalibrationSnapshot


DEFAULT_BANDS = (
    (0.00, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.35),
    (0.35, 0.50), (0.50, 0.65), (0.65, 0.80), (0.80, 0.90),
    (0.90, 0.95), (0.95, 1.00 + 1e-9),
)


@dataclass
class PriceBands:
    edges: tuple[tuple[float, float], ...] = DEFAULT_BANDS


@dataclass
class OosRecord:
    domain: str
    kalshi_price: float
    adjusted_p: float
    raw_p: float
    y: int


def build_report(
    records: Sequence[OosRecord],
    *,
    bands: PriceBands = PriceBands(),
    phase_gate_min_sample: int,
) -> CalibrationReport:
    now = datetime.now(timezone.utc)
    out: dict[BandKey, BandStats] = {}
    by_bucket: dict[BandKey, list[OosRecord]] = {}

    for r in records:
        for lo, hi in bands.edges:
            if lo <= r.kalshi_price < hi:
                key = BandKey(r.domain, lo, hi)
                by_bucket.setdefault(key, []).append(r)
                break

    for key, rs in by_bucket.items():
        n = len(rs)
        if n == 0:
            continue
        y = [x.y for x in rs]
        pred = [x.adjusted_p for x in rs]
        raw = [x.raw_p for x in rs]
        brier_adj = sum((p - yi) ** 2 for p, yi in zip(pred, y)) / n
        brier_raw = sum((p - yi) ** 2 for p, yi in zip(raw, y)) / n
        out[key] = BandStats(
            settled_count=n,
            empirical_freq=sum(y) / n,
            mean_predicted=sum(pred) / n,
            brier=brier_adj,
            brier_vs_uncorrected=brier_adj - brier_raw,
            updated_at=now,
        )

    return CalibrationReport(
        generated_at=now,
        bands=out,
        phase_gate_min_sample=phase_gate_min_sample,
    )


def snapshot_to_json(report: CalibrationReport) -> list[dict]:
    return [
        {
            "domain": k.domain,
            "band_lo": k.price_band_lo,
            "band_hi": k.price_band_hi,
            "settled_count": v.settled_count,
            "empirical_freq": v.empirical_freq,
            "mean_predicted": v.mean_predicted,
            "brier": v.brier,
            "brier_vs_uncorrected": v.brier_vs_uncorrected,
        }
        for k, v in report.bands.items()
    ]


def snapshot_from_json(
    payload: list[dict], generated_at: datetime, phase_gate_min_sample: int
) -> CalibrationReport:
    bands: dict[BandKey, BandStats] = {}
    for row in payload:
        key = BandKey(row["domain"], row["band_lo"], row["band_hi"])
        bands[key] = BandStats(
            settled_count=int(row["settled_count"]),
            empirical_freq=float(row["empirical_freq"]),
            mean_predicted=float(row["mean_predicted"]),
            brier=float(row["brier"]),
            brier_vs_uncorrected=float(row["brier_vs_uncorrected"]),
            updated_at=generated_at,
        )
    return CalibrationReport(
        generated_at=generated_at,
        bands=bands,
        phase_gate_min_sample=phase_gate_min_sample,
    )


def load_latest_report(default_min_sample: int) -> CalibrationReport:
    """Read the most recent snapshot from Neon. Falls back to an empty
    report (all bands gated) if none exists — that's the correct starting
    state: nothing is calibrated yet, so nothing is tradable."""

    try:
        with session_scope() as sess:
            row = sess.execute(
                select(CalibrationSnapshot).order_by(
                    CalibrationSnapshot.generated_at.desc()
                ).limit(1)
            ).scalar_one_or_none()
    except Exception:
        row = None

    if row is None:
        return CalibrationReport(
            generated_at=datetime.now(timezone.utc),
            bands={},
            phase_gate_min_sample=default_min_sample,
        )
    return snapshot_from_json(row.bands, row.generated_at, row.phase_gate_min_sample)
