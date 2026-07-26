"""Calibration + bias-fit recomputation loop.

Periodically:

1. Pulls settled records (Signal → OracleEstimate → Contract → Settlement)
   from Neon into an in-memory dataset.
2. Builds a fresh :class:`CalibrationReport` per-band per-domain and spools
   a ``calibration_snapshot`` write. The next trading tick reads it via
   :func:`load_latest_report` — the phase gate lives in the calibration
   store, not a config flag.
3. Runs :func:`fit_feature` for each registered bias feature, spools a
   ``bias_params`` row, and updates the *in-memory* bias model via
   ``load_params`` so subsequent decisions use the fresh coefficients
   without waiting for a process restart.

Time-split: 70% of the observed decision window is in-sample, the
strictly-later 30% is OOS. If we do not yet have enough OOS observations
to clear ``min_oos_sample``, ``evidence_ok`` stays False and the feature
contributes zero at runtime — which is the correct default.

Design note: everything writes through the spool, so Neon transience
cannot stall the refit loop any more than it can stall the trading loop.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Iterable

import structlog

from sqlalchemy import select

from ..bias.features import (
    LongshotCurveFeature,
    RecencyMomentumFeature,
    SessionLiquidityFeature,
)
from ..bias.fit import Record as FitRecord
from ..bias.fit import TimeSplit, fit_feature
from ..bias.model import ComposedBiasModel
from ..calibration.report import (
    OosRecord,
    build_report,
    snapshot_to_json,
)
from ..core.bias import BiasFeature
from ..domains.crypto.bias_features import RoundNumberDistanceFeature
from ..storage.db import session_scope
from ..storage.models import Contract, OracleEstimate, Settlement, Signal
from ..storage.spool import SpooledWriter

log = structlog.get_logger(__name__)


VENUE_FEATURES = ("longshot_curve", "recency_momentum", "session_liquidity")
DOMAIN_FEATURES = ("round_number_distance_crypto",)
ALL_FEATURES = VENUE_FEATURES + DOMAIN_FEATURES


def _outcome_to_y(outcome: str) -> int | None:
    o = outcome.upper()
    if o == "YES":
        return 1
    if o == "NO":
        return 0
    return None  # VOID or unknown — skip


def _mid_price(bid: float | None, ask: float | None) -> float | None:
    if bid is None and ask is None:
        return None
    if bid is None:
        return ask
    if ask is None:
        return bid
    return 0.5 * (bid + ask)


def _load_settled_rows() -> list[dict]:
    """Load (signal, oracle_estimate, contract, settlement) tuples for
    every settled record. Kept intentionally simple; if this outgrows
    process memory we page it, but for a single-domain V1 the volume is
    small."""

    with session_scope() as sess:
        rows = sess.execute(
            select(
                Signal.created_at,
                Signal.adjusted_p,
                Signal.kalshi_bid,
                Signal.kalshi_ask,
                OracleEstimate.p,
                OracleEstimate.provenance,
                Contract.domain,
                Settlement.outcome,
            )
            .join(OracleEstimate,
                  OracleEstimate.id == Signal.oracle_estimate_pk)
            .join(Contract, Contract.id == Signal.contract_pk)
            .join(Settlement, Settlement.contract_pk == Signal.contract_pk)
            .order_by(Signal.created_at.asc())
        ).all()

    out: list[dict] = []
    for r in rows:
        y = _outcome_to_y(r.outcome)
        if y is None:
            continue
        mid = _mid_price(r.kalshi_bid, r.kalshi_ask)
        if mid is None:
            continue
        prov = r.provenance or {}
        out.append({
            "t_decision": r.created_at,
            "domain": r.domain,
            "kalshi_price": mid,
            "raw_p": r.p,
            "adjusted_p": r.adjusted_p,
            "y": y,
            "recent_log_return": prov.get("recent_log_return"),
            "session_bucket": prov.get("session_bucket"),
            "round_number_distance": prov.get("round_number_distance"),
        })
    return out


def _pick_split(rows: list[dict], in_sample_frac: float) -> TimeSplit | None:
    if len(rows) < 2:
        return None
    ts = [r["t_decision"] for r in rows]
    lo, hi = ts[0], ts[-1]
    if hi <= lo:
        return None
    span = hi - lo
    cut = lo + span * in_sample_frac
    return TimeSplit(in_sample_end=cut, oos_start=cut, oos_end=hi)


def _oos_records(rows: Iterable[dict]) -> list[OosRecord]:
    return [
        OosRecord(
            domain=r["domain"],
            kalshi_price=r["kalshi_price"],
            adjusted_p=r["adjusted_p"],
            raw_p=r["raw_p"],
            y=r["y"],
        )
        for r in rows
    ]


def _fit_records(rows: Iterable[dict]) -> list[FitRecord]:
    return [
        FitRecord(
            t_decision=r["t_decision"],
            domain=r["domain"],
            kalshi_price=r["kalshi_price"],
            raw_p=r["raw_p"],
            y=r["y"],
            recent_log_return=r["recent_log_return"],
            session_bucket=r["session_bucket"],
            round_number_distance=r["round_number_distance"],
        )
        for r in rows
    ]


def _apply_params(bias: ComposedBiasModel, feature_name: str, fr) -> None:
    """Write fitted parameters into the running in-memory model.

    ``load_params`` is the feature-side one-way handoff — no runtime code
    is allowed to construct feature params directly, so we can be sure the
    ``evidence_ok`` gate wraps every mutation.
    """
    for f in bias.features():
        if f.name != feature_name:
            continue
        meta = {
            "oos_sample": fr.oos_sample,
            "oos_brier_improvement": fr.oos_brier_improvement,
            "in_sample_end": fr.in_sample_end.isoformat(),
            "oos_end": fr.oos_end.isoformat(),
        }
        if isinstance(f, LongshotCurveFeature):
            f.load_params(
                alpha_pool=fr.params["alpha_pool"],
                alpha_domain=fr.params["alpha_domain"],
                evidence_ok=fr.evidence_ok,
                meta=meta,
            )
        elif isinstance(f, RecencyMomentumFeature):
            f.load_params(beta=fr.params["beta"],
                          evidence_ok=fr.evidence_ok, meta=meta)
        elif isinstance(f, SessionLiquidityFeature):
            f.load_params(shifts={int(k): float(v)
                                  for k, v in fr.params["shifts"].items()},
                          evidence_ok=fr.evidence_ok, meta=meta)
        elif isinstance(f, RoundNumberDistanceFeature):
            f.load_params(beta=fr.params["beta"],
                          evidence_ok=fr.evidence_ok, meta=meta)
        else:
            log.warning("refit.unknown_feature_class",
                        feature=feature_name, cls=type(f).__name__)
        return


async def refit_once(
    writer: SpooledWriter,
    bias: ComposedBiasModel,
    *,
    phase_gate_min_sample: int,
    in_sample_frac: float = 0.7,
    min_oos_sample: int = 200,
    min_brier_improvement: float = 0.005,
) -> None:
    rows = _load_settled_rows()
    if not rows:
        log.info("refit.skip", reason="no_settled_rows")
        return

    now = datetime.now(timezone.utc)
    report = build_report(
        _oos_records(rows),
        phase_gate_min_sample=phase_gate_min_sample,
    )
    await writer.enqueue("calibration_snapshot", {
        "generated_at": now.isoformat(),
        "phase_gate_min_sample": phase_gate_min_sample,
        "bands": snapshot_to_json(report),
    })
    log.info("refit.calibration", bands=len(report.bands),
             total_records=len(rows))

    split = _pick_split(rows, in_sample_frac)
    if split is None:
        log.info("refit.split_skip", reason="insufficient_time_span")
        return

    fit_rows = _fit_records(rows)
    for feature_name in ALL_FEATURES:
        try:
            fr = fit_feature(
                feature_name, fit_rows, split,
                min_oos_sample=min_oos_sample,
                min_brier_improvement=min_brier_improvement,
            )
        except Exception as e:
            log.warning("refit.fit_error", feature=feature_name, error=str(e))
            continue

        # Domain scoping: venue features live under '*'; domain-specific
        # ones under their domain name so the ledger row is queryable
        # per-domain.
        domain = "crypto" if feature_name in DOMAIN_FEATURES else "*"
        await writer.enqueue("bias_params", {
            "feature_name": feature_name,
            "domain": domain,
            "params": fr.params,
            "in_sample_end": fr.in_sample_end.isoformat(),
            "oos_start": fr.oos_start.isoformat(),
            "oos_end": fr.oos_end.isoformat(),
            "oos_brier_improvement": fr.oos_brier_improvement,
            "oos_sample": fr.oos_sample,
            "evidence_ok": fr.evidence_ok,
            "fit_at": now.isoformat(),
        })
        _apply_params(bias, feature_name, fr)
        log.info("refit.feature", feature=feature_name,
                 evidence_ok=fr.evidence_ok, oos=fr.oos_sample,
                 improvement=fr.oos_brier_improvement)


class RefitScheduler:
    def __init__(
        self,
        writer: SpooledWriter,
        bias: ComposedBiasModel,
        *,
        interval_sec: float,
        phase_gate_min_sample: int,
    ) -> None:
        self._writer = writer
        self._bias = bias
        self._interval = interval_sec
        self._min_sample = phase_gate_min_sample
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name="refit-loop")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await refit_once(
                    self._writer, self._bias,
                    phase_gate_min_sample=self._min_sample,
                )
            except Exception as e:
                log.warning("refit.loop_error", error=str(e))
            try:
                await asyncio.wait_for(self._stopping.wait(),
                                       timeout=self._interval)
            except asyncio.TimeoutError:
                pass
