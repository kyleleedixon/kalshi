"""Bias-feature fitting pipeline.

Design invariants:

* Time-based in-sample / out-of-sample split enforced in code. The bias
  curve cannot grade its own homework — the OOS window is a strict-later
  slice of the same feature and never overlaps in-sample. If a caller
  passes a split with any overlap, we raise.

* Every fit emits a ``bias_params`` row containing in-sample end, OOS
  window, OOS sample size, Brier improvement vs. uncorrected oracle, and
  an ``evidence_ok`` flag. That flag is what makes the feature actually
  contribute at runtime — a feature with no measured improvement gets
  toggled off automatically, not left on because the literature says it
  exists.

* Hierarchical partial pooling for the longshot curve is implemented as a
  simple penalized regression: pooled slope shared across domains, plus
  per-domain offsets shrunk toward zero with a Gaussian prior whose
  variance controls the pooling strength. Collapses to a single OLS in the
  one-domain case.

The dataset shape passed in is a list of records::

    Record(t_decision, domain, kalshi_price, raw_p, adjusted_p, y)

where ``y`` in {0,1} is the settled outcome. Callers assemble this from
Neon (oracle_estimates JOIN quotes JOIN settlements).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

import numpy as np

from .features import (
    LongshotCurveFeature,
    RecencyMomentumFeature,
    SessionLiquidityFeature,
)


@dataclass(frozen=True)
class TimeSplit:
    in_sample_end: datetime
    oos_start: datetime
    oos_end: datetime

    def __post_init__(self) -> None:
        if self.oos_start < self.in_sample_end:
            raise ValueError(
                "OOS window must start no earlier than in-sample end "
                f"(in_sample_end={self.in_sample_end}, oos_start={self.oos_start})"
            )
        if self.oos_end <= self.oos_start:
            raise ValueError("oos_end must be after oos_start")


@dataclass
class Record:
    t_decision: datetime
    domain: str
    kalshi_price: float
    raw_p: float
    y: int
    recent_log_return: float | None = None
    session_bucket: int | None = None
    round_number_distance: float | None = None


@dataclass
class FitResult:
    params: dict
    oos_sample: int
    oos_brier_improvement: float | None
    evidence_ok: bool
    in_sample_end: datetime
    oos_start: datetime
    oos_end: datetime


def _brier(pred: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((pred - y) ** 2))


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-9, 1.0 - 1e-9)
    return np.log(p / (1.0 - p))


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-z))


def _split(records: Sequence[Record], split: TimeSplit
           ) -> tuple[list[Record], list[Record]]:
    in_s = [r for r in records if r.t_decision <= split.in_sample_end]
    oos = [r for r in records
           if split.oos_start <= r.t_decision <= split.oos_end]
    return in_s, oos


# ---------------------------------------------------------------------------
# Longshot curve — hierarchical partial pooling
# ---------------------------------------------------------------------------


def _fit_longshot_hierarchical(
    records: Sequence[Record],
    prior_domain_std: float,
) -> tuple[float, dict[str, float]]:
    """Penalized least-squares in log-odds space.

    Minimize:
        sum_i ( residual_i - (alpha_pool + alpha_domain[d_i]) * x_i )^2
      + (1 / prior_domain_std^2) * sum_d alpha_domain[d]^2

    where ``residual = logit(y_smoothed) - logit(raw_p)`` (Laplace-smoothed
    outcome so logit is defined) and ``x = logit(kalshi_price)``.

    Closed form via a small linear system: N = 1 + D unknowns for D domains.
    """

    if not records:
        return 0.0, {}

    domains = sorted({r.domain for r in records})
    d_index = {d: i for i, d in enumerate(domains)}
    D = len(domains)

    # Laplace smoothing so logit(y) is defined for singleton observations.
    # We're fitting mean behavior; the per-record y is 0/1 but the fit is
    # over their empirical relationship to kalshi_price.
    ys = np.array([r.y for r in records], dtype=float)
    y_smoothed = (ys + 0.5) / 2.0  # 0 -> 0.25, 1 -> 0.75
    logit_y = _logit(y_smoothed)
    raw_ps = np.array([r.raw_p for r in records], dtype=float)
    logit_raw = _logit(raw_ps)
    resid = logit_y - logit_raw
    x = _logit(np.array([r.kalshi_price for r in records], dtype=float))

    # Design matrix: [x | x * one_hot(domain)]
    A = np.zeros((len(records), 1 + D))
    A[:, 0] = x
    for i, r in enumerate(records):
        A[i, 1 + d_index[r.domain]] = x[i]

    lam = 1.0 / max(prior_domain_std ** 2, 1e-6)
    # Regularization only on domain offsets (columns 1..D).
    reg = np.zeros((1 + D, 1 + D))
    for j in range(1, 1 + D):
        reg[j, j] = lam

    theta, *_ = np.linalg.lstsq(A.T @ A + reg, A.T @ resid, rcond=None)
    alpha_pool = float(theta[0])
    alpha_domain = {d: float(theta[1 + d_index[d]]) for d in domains}
    return alpha_pool, alpha_domain


def _predict_longshot(
    records: Sequence[Record], alpha_pool: float, alpha_domain: dict[str, float]
) -> np.ndarray:
    logit_raw = _logit(np.array([r.raw_p for r in records], dtype=float))
    x = _logit(np.array([r.kalshi_price for r in records], dtype=float))
    slopes = np.array([alpha_pool + alpha_domain.get(r.domain, 0.0) for r in records])
    return _sigmoid(logit_raw + slopes * x)


# ---------------------------------------------------------------------------
# Simple univariate features (round-number, recency, session)
# ---------------------------------------------------------------------------


def _fit_univariate_logit(
    residuals: np.ndarray, x: np.ndarray
) -> float:
    """Closed-form OLS slope of ``residuals ~ beta * x`` (no intercept)."""
    denom = float(np.sum(x * x))
    if denom <= 0:
        return 0.0
    return float(np.sum(x * residuals) / denom)


def _fit_bucket_shifts(
    residuals: np.ndarray, buckets: np.ndarray
) -> dict[int, float]:
    shifts: dict[int, float] = {}
    for b in np.unique(buckets):
        mask = buckets == b
        if mask.sum() == 0:
            continue
        shifts[int(b)] = float(residuals[mask].mean())
    return shifts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fit_feature(
    feature_name: str,
    records: Sequence[Record],
    split: TimeSplit,
    *,
    min_oos_sample: int = 200,
    min_brier_improvement: float = 0.005,
    prior_domain_std: float = 1.0,
) -> FitResult:
    """Fit ``feature_name`` on the in-sample slice, evaluate on OOS.

    Returns FitResult with ``evidence_ok`` set only if OOS sample >=
    ``min_oos_sample`` AND Brier improvement over uncorrected oracle >=
    ``min_brier_improvement``. That gate is what actually toggles the
    feature on at runtime.
    """

    in_s, oos = _split(records, split)
    y_oos = np.array([r.y for r in oos], dtype=float)
    raw_oos = np.array([r.raw_p for r in oos], dtype=float)

    if feature_name == "longshot_curve":
        alpha_pool, alpha_domain = _fit_longshot_hierarchical(in_s, prior_domain_std)
        if oos:
            pred_oos = _predict_longshot(oos, alpha_pool, alpha_domain)
            brier_adj = _brier(pred_oos, y_oos)
            brier_raw = _brier(raw_oos, y_oos)
            improvement = brier_raw - brier_adj
        else:
            improvement = None
        params = {"alpha_pool": alpha_pool, "alpha_domain": alpha_domain}

    elif feature_name in ("round_number_distance_crypto", "recency_momentum"):
        residuals_in = _logit(
            (np.array([r.y for r in in_s], dtype=float) + 0.5) / 2.0
        ) - _logit(np.array([r.raw_p for r in in_s], dtype=float))

        if feature_name == "round_number_distance_crypto":
            x_in = np.array(
                [r.round_number_distance or 0.0 for r in in_s], dtype=float
            )
            x_oos = np.array(
                [r.round_number_distance or 0.0 for r in oos], dtype=float
            )
        else:
            x_in = np.array(
                [r.recent_log_return or 0.0 for r in in_s], dtype=float
            )
            x_oos = np.array(
                [r.recent_log_return or 0.0 for r in oos], dtype=float
            )

        beta = _fit_univariate_logit(residuals_in, x_in)
        if oos:
            pred_oos = _sigmoid(_logit(raw_oos) + beta * x_oos)
            brier_adj = _brier(pred_oos, y_oos)
            brier_raw = _brier(raw_oos, y_oos)
            improvement = brier_raw - brier_adj
        else:
            improvement = None
        params = {"beta": beta}

    elif feature_name == "session_liquidity":
        residuals_in = _logit(
            (np.array([r.y for r in in_s], dtype=float) + 0.5) / 2.0
        ) - _logit(np.array([r.raw_p for r in in_s], dtype=float))
        buckets_in = np.array(
            [r.session_bucket if r.session_bucket is not None else -1 for r in in_s],
            dtype=int,
        )
        shifts = _fit_bucket_shifts(residuals_in, buckets_in)

        if oos:
            buckets_oos = np.array(
                [r.session_bucket if r.session_bucket is not None else -1 for r in oos],
                dtype=int,
            )
            pred_oos = _sigmoid(
                _logit(raw_oos)
                + np.array([shifts.get(int(b), 0.0) for b in buckets_oos])
            )
            brier_adj = _brier(pred_oos, y_oos)
            brier_raw = _brier(raw_oos, y_oos)
            improvement = brier_raw - brier_adj
        else:
            improvement = None
        params = {"shifts": {str(k): v for k, v in shifts.items()}}

    else:
        raise ValueError(f"unknown feature: {feature_name}")

    oos_sample = len(oos)
    evidence_ok = (
        oos_sample >= min_oos_sample
        and improvement is not None
        and improvement >= min_brier_improvement
    )
    return FitResult(
        params=params,
        oos_sample=oos_sample,
        oos_brier_improvement=(None if improvement is None else float(improvement)),
        evidence_ok=bool(evidence_ok),
        in_sample_end=split.in_sample_end,
        oos_start=split.oos_start,
        oos_end=split.oos_end,
    )
