"""High-level Neon writer used by the engine.

Every write goes through the SpooledWriter: the trading loop only ever
awaits the local SQLite append. A Neon outage cannot stall the loop.

Kinds map to handler methods on ``NeonSink`` that translate the dict
payload into INSERTs via SQLAlchemy. Handlers must be idempotent per
payload — the drainer retries on failure, and any exception rolls back
the transaction so partial writes never leak.

FK resolution
-------------
Trading-loop payloads carry *business keys* (``contract_id`` ticker,
``external_id`` UUID for oracle estimates / signals), not database
primary keys — pks only exist after the drain sink commits. Handlers
resolve business keys to pks through :class:`PkResolver` inside the
same session as the write. ``contract_upsert`` must be enqueued before
any payload that references the contract, and ``oracle_estimate``
before the ``signal`` that depends on it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog

from sqlalchemy.dialects.postgresql import insert as pg_insert

from .db import session_scope
from .models import (
    BiasParams,
    CalibrationSnapshot,
    Contract,
    Heartbeat,
    OracleEstimate,
    PaperFill,
    PaperOrder,
    Quote,
    RawPull,
    RealizedVol,
    Settlement,
    SettlementBasis,
    Signal,
)
from .resolver import PkResolver
from .spool import SpooledWriter

log = structlog.get_logger(__name__)


def _parse_ts(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(v)


class NeonSink:
    """Drain-side handler. One instance per engine process.

    Exposes ``__call__`` so it can be passed as the ``sink`` callable to
    :class:`SpooledWriter` without a lambda.
    """

    def __init__(self, resolver: PkResolver | None = None) -> None:
        self._resolver = resolver or PkResolver()

    def __call__(self, kind: str, payload: dict[str, Any]) -> None:
        with session_scope() as sess:
            handler = self._HANDLERS.get(kind)
            if handler is None:
                raise ValueError(f"unknown spool kind: {kind}")
            handler(self, sess, payload)

    # -- handlers -----------------------------------------------------------

    def _raw_pull(self, sess, payload: dict[str, Any]) -> None:
        sess.add(RawPull(
            source=payload["source"],
            endpoint=payload["endpoint"],
            request_params=payload.get("request_params"),
            response=payload["response"],
            http_status=payload.get("http_status"),
            ingest_ts=_parse_ts(payload["ingest_ts"]),
        ))

    def _contract_upsert(self, sess, payload: dict[str, Any]) -> None:
        stmt = pg_insert(Contract).values(
            contract_id=payload["contract_id"],
            domain=payload["domain"],
            underlying=payload["underlying"],
            side=payload["side"],
            open_time=_parse_ts(payload.get("open_time")),
            close_time=_parse_ts(payload.get("close_time")),
            settlement_time=_parse_ts(payload.get("settlement_time")),
            settlement_source=payload["settlement_source"],
            features=payload.get("features", {}),
        )
        # Update the mutable metadata (features, close/settlement times) on
        # conflict — Kalshi occasionally revises close times, and features
        # like ``strike``/``horizon_seconds`` can be re-derived. Identity
        # fields (contract_id, domain, underlying, side) stay put.
        stmt = stmt.on_conflict_do_update(
            index_elements=["contract_id"],
            set_={
                "open_time": stmt.excluded.open_time,
                "close_time": stmt.excluded.close_time,
                "settlement_time": stmt.excluded.settlement_time,
                "settlement_source": stmt.excluded.settlement_source,
                "features": stmt.excluded.features,
            },
        )
        sess.execute(stmt)

    def _quote(self, sess, payload: dict[str, Any]) -> None:
        contract_pk = self._resolver.contract_pk(sess, payload["contract_id"])
        sess.add(Quote(
            contract_pk=contract_pk,
            bid=payload.get("bid"),
            ask=payload.get("ask"),
            bid_size=payload.get("bid_size"),
            ask_size=payload.get("ask_size"),
            last_trade_price=payload.get("last_trade_price"),
            data_ts=_parse_ts(payload["data_ts"]),
            ingest_ts=_parse_ts(payload["ingest_ts"]),
        ))

    def _oracle_estimate(self, sess, payload: dict[str, Any]) -> None:
        contract_pk = self._resolver.contract_pk(sess, payload["contract_id"])
        sess.add(OracleEstimate(
            external_id=payload["external_id"],
            contract_pk=contract_pk,
            oracle_name=payload["oracle_name"],
            oracle_version=payload["oracle_version"],
            p=payload["p"],
            variance=payload["variance"],
            effective_sample_size=payload["effective_sample_size"],
            staleness=payload["staleness"],
            data_ts=_parse_ts(payload["data_ts"]),
            provenance=payload.get("provenance", {}),
            ingest_ts=_parse_ts(payload["ingest_ts"]),
        ))

    def _signal(self, sess, payload: dict[str, Any]) -> None:
        contract_pk = self._resolver.contract_pk(sess, payload["contract_id"])
        oracle_pk = self._resolver.oracle_estimate_pk(
            sess, payload["oracle_estimate_external_id"]
        )
        sess.add(Signal(
            external_id=payload["external_id"],
            contract_pk=contract_pk,
            oracle_estimate_pk=oracle_pk,
            adjusted_p=payload["adjusted_p"],
            kalshi_bid=payload.get("kalshi_bid"),
            kalshi_ask=payload.get("kalshi_ask"),
            fee_bps=payload["fee_bps"],
            edge_net=payload["edge_net"],
            calibration_confidence=payload["calibration_confidence"],
            bias_adjustments=payload.get("bias_adjustments", []),
            rank=payload.get("rank"),
            created_at=_parse_ts(payload["created_at"]),
        ))

    def _paper_order(self, sess, payload: dict[str, Any]) -> None:
        contract_pk = self._resolver.contract_pk(sess, payload["contract_id"])
        signal_pk: int | None = None
        sig_ext = payload.get("signal_external_id")
        if sig_ext:
            signal_pk = self._resolver.signal_pk(sess, sig_ext)
        order = PaperOrder(
            signal_pk=signal_pk,
            contract_pk=contract_pk,
            side=payload["side"],
            action=payload["action"],
            size_contracts=payload["size_contracts"],
            limit_price=payload.get("limit_price"),
            hypothetical_fill_price=payload.get("hypothetical_fill_price"),
            hypothetical_fill_size=payload.get("hypothetical_fill_size"),
            status=payload["status"],
            reason=payload.get("reason"),
            created_at=_parse_ts(payload["created_at"]),
        )
        sess.add(order)
        # ``attached_fill`` piggybacks on the same transaction so we never
        # have an order marked FILLED without a matching fill row (or vice
        # versa). Emitting them as two separate spool entries would let a
        # crash between them create exactly that inconsistency.
        fill = payload.get("attached_fill")
        if fill is not None:
            sess.flush()  # populate order.id for the FK
            sess.add(PaperFill(
                paper_order_pk=order.id,
                price=fill["price"],
                size_contracts=fill["size_contracts"],
                fee=fill["fee"],
                fill_ts=_parse_ts(fill["fill_ts"]),
            ))

    def _paper_fill(self, sess, payload: dict[str, Any]) -> None:
        sess.add(PaperFill(
            paper_order_pk=payload["paper_order_pk"],
            price=payload["price"],
            size_contracts=payload["size_contracts"],
            fee=payload["fee"],
            fill_ts=_parse_ts(payload["fill_ts"]),
        ))

    def _settlement(self, sess, payload: dict[str, Any]) -> None:
        contract_pk = self._resolver.contract_pk(sess, payload["contract_id"])
        stmt = pg_insert(Settlement).values(
            contract_pk=contract_pk,
            outcome=payload["outcome"],
            settlement_value=payload.get("settlement_value"),
            settled_at=_parse_ts(payload["settled_at"]),
            ingest_ts=_parse_ts(payload["ingest_ts"]),
        ).on_conflict_do_nothing(index_elements=["contract_pk"])
        sess.execute(stmt)

    def _settlement_basis(self, sess, payload: dict[str, Any]) -> None:
        sess.add(SettlementBasis(
            underlying=payload["underlying"],
            settlement_value=payload["settlement_value"],
            kraken_spot_at_settle=payload["kraken_spot_at_settle"],
            basis_bps=payload["basis_bps"],
            settled_at=_parse_ts(payload["settled_at"]),
            ingest_ts=_parse_ts(payload["ingest_ts"]),
        ))

    def _realized_vol(self, sess, payload: dict[str, Any]) -> None:
        sess.add(RealizedVol(
            underlying=payload["underlying"],
            horizon_seconds=payload["horizon_seconds"],
            sigma_annualized=payload["sigma_annualized"],
            tick_count=payload["tick_count"],
            data_ts=_parse_ts(payload["data_ts"]),
            ingest_ts=_parse_ts(payload["ingest_ts"]),
        ))

    def _bias_params(self, sess, payload: dict[str, Any]) -> None:
        sess.add(BiasParams(
            feature_name=payload["feature_name"],
            domain=payload["domain"],
            params=payload["params"],
            in_sample_end=_parse_ts(payload["in_sample_end"]),
            oos_start=_parse_ts(payload["oos_start"]),
            oos_end=_parse_ts(payload["oos_end"]),
            oos_brier_improvement=payload.get("oos_brier_improvement"),
            oos_sample=payload["oos_sample"],
            evidence_ok=payload["evidence_ok"],
            fit_at=_parse_ts(payload["fit_at"]),
        ))

    def _calibration_snapshot(self, sess, payload: dict[str, Any]) -> None:
        sess.add(CalibrationSnapshot(
            generated_at=_parse_ts(payload["generated_at"]),
            phase_gate_min_sample=payload["phase_gate_min_sample"],
            bands=payload["bands"],
        ))

    def _heartbeat(self, sess, payload: dict[str, Any]) -> None:
        stmt = pg_insert(Heartbeat).values(
            engine_id=payload["engine_id"],
            last_beat=_parse_ts(payload["last_beat"]),
            phase=payload["phase"],
            version=payload["version"],
            notes=payload.get("notes"),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["engine_id"],
            set_={
                "last_beat": stmt.excluded.last_beat,
                "phase": stmt.excluded.phase,
                "version": stmt.excluded.version,
                "notes": stmt.excluded.notes,
            },
        )
        sess.execute(stmt)

    _HANDLERS = {
        "raw_pull": _raw_pull,
        "contract_upsert": _contract_upsert,
        "quote": _quote,
        "oracle_estimate": _oracle_estimate,
        "signal": _signal,
        "paper_order": _paper_order,
        "paper_fill": _paper_fill,
        "settlement": _settlement,
        "settlement_basis": _settlement_basis,
        "realized_vol": _realized_vol,
        "bias_params": _bias_params,
        "calibration_snapshot": _calibration_snapshot,
        "heartbeat": _heartbeat,
    }


def build_writer(spool_path: str) -> SpooledWriter:
    return SpooledWriter(spool_path=spool_path, sink=NeonSink())
