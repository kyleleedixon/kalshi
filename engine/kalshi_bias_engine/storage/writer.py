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


class _BatchCache:
    """Session-scoped pk cache with lazy-flush ORM handles.

    Signals in a batch reference oracle_estimates in the same batch, which
    reference contracts upserted in the same batch. Round-tripping SELECT id
    ... for every reference caps drain throughput at ~3 rows/sec (each
    round-trip to Neon ~100ms).

    Values are ``int`` (pk already known — e.g. contract upserted via
    RETURNING) or an ORM instance (pk not yet assigned — sess.add()'d but
    not flushed). Looking up an ORM value triggers a single ``sess.flush()``
    that sends every pending insert as one insertmanyvalues round-trip, then
    replaces the entries with their assigned pks.
    """

    def __init__(self) -> None:
        self.contract_pk: dict[str, int] = {}
        self.oracle_estimate_pk: dict[str, Any] = {}
        self.signal_pk: dict[str, Any] = {}


class NeonSink:
    """Drain-side handler. One instance per engine process.

    Exposes ``__call__`` so it can be passed as the ``sink`` callable to
    :class:`SpooledWriter` without a lambda.
    """

    def __init__(self, resolver: PkResolver | None = None) -> None:
        self._resolver = resolver or PkResolver()
        self._cache: _BatchCache | None = None
        # Persistent contract_id -> pk cache. Contract pks are immutable
        # once assigned, so a hit avoids the ~100ms Neon SELECT round-trip.
        # This is the difference between the drain keeping up with the
        # discovery loop and falling behind: without it, oracle_estimates
        # in a later batch each SELECT the pk of their contract, and each
        # SELECT is a Neon round-trip. ~1k crypto contracts total → cache
        # size is negligible.
        self._contract_pk_cache: dict[str, int] = {}

    def __call__(self, kind: str, payload: dict[str, Any]) -> None:
        self._cache = _BatchCache()
        try:
            with session_scope() as sess:
                handler = self._HANDLERS.get(kind)
                if handler is None:
                    raise ValueError(f"unknown spool kind: {kind}")
                handler(self, sess, payload)
        finally:
            self._cache = None

    def apply_batch(self, items: list[tuple[str, dict[str, Any]]]) -> int:
        """Apply a list of ``(kind, payload)`` writes in a single session.

        Amortizes the Neon connect + commit round-trip across the batch —
        per-row session_scope() at Neon's typical 100-300ms round-trip
        latency caps throughput at ~5-10 writes/sec, which is not enough
        for our discovery cadence. On any handler failure the whole batch
        rolls back and the caller retries item-by-item to isolate the bad
        row.
        """
        applied = 0
        self._cache = _BatchCache()
        try:
            with session_scope() as sess:
                # Dependency order: contract -> quote -> oracle_estimate ->
                # signal -> paper_order. Processing kind-by-kind (rather than
                # spool order) lets a single sess.flush() between kinds bulk
                # push all pending inserts of that kind in one round-trip.
                # Spool interleaves O/S, which without regrouping caused
                # sess.flush() to fire per signal (~30ms each × 250 = 7s+).
                by_kind: dict[str, list[dict[str, Any]]] = {}
                for kind, payload in items:
                    by_kind.setdefault(kind, []).append(payload)

                self._bulk_contract_upsert(
                    sess, by_kind.pop("contract_upsert", [])
                )
                self._warm_contract_pks(sess, items)

                for kind in ("quote", "oracle_estimate", "signal",
                             "paper_order", "paper_fill", "settlement",
                             "settlement_basis", "realized_vol",
                             "bias_params", "calibration_snapshot",
                             "heartbeat", "raw_pull"):
                    payloads = by_kind.pop(kind, None)
                    if not payloads:
                        continue
                    handler = self._HANDLERS[kind]
                    for p in payloads:
                        handler(self, sess, p)
                        applied += 1
                    # Flush all just-added rows in one insertmanyvalues
                    # round-trip so the NEXT kind sees assigned pks and
                    # doesn't trigger a per-row flush from the cache lookup.
                    sess.flush()
                applied += len(items) - applied - len(by_kind.get("contract_upsert", []))
                # Any remaining unknown kinds (shouldn't happen — defensive).
                for kind, payloads in by_kind.items():
                    if kind == "contract_upsert":
                        applied += len(payloads)
                        continue
                    raise ValueError(f"unknown spool kind: {kind}")
        finally:
            self._cache = None
        return applied

    def _warm_contract_pks(
        self, sess, items: list[tuple[str, dict[str, Any]]]
    ) -> None:
        """One SELECT to fetch pks for any referenced contracts not yet
        cached. Without this the very first batch after a restart does one
        SELECT per quote/oracle_estimate."""
        needed: set[str] = set()
        for _kind, payload in items:
            cid = payload.get("contract_id")
            if cid and cid not in self._contract_pk_cache:
                needed.add(cid)
        if not needed:
            return
        from sqlalchemy import select
        rows = sess.execute(
            select(Contract.contract_id, Contract.id).where(
                Contract.contract_id.in_(needed)
            )
        ).all()
        for cid, pk in rows:
            self._contract_pk_cache[cid] = pk

    def _bulk_contract_upsert(
        self, sess, payloads: list[dict[str, Any]]
    ) -> None:
        """Upsert all contract rows in a batch in a single round-trip.

        Per-row ``pg_insert().returning()`` was 1 round-trip per contract,
        which dominated drain latency (100 contracts × 100ms round-trip =
        10s per batch). Batching via ``pg_insert(...).values(list)`` sends
        them all as one INSERT ... ON CONFLICT and returns every pk in one
        round-trip.
        """
        if not payloads:
            return
        # De-dup within the batch: repeated contract_ids in one INSERT would
        # trigger the "ON CONFLICT cannot target row a second time" error.
        # Keep the last occurrence — it's the freshest metadata.
        by_id: dict[str, dict[str, Any]] = {}
        for p in payloads:
            by_id[p["contract_id"]] = p
        rows = [
            {
                "contract_id": p["contract_id"],
                "domain": p["domain"],
                "underlying": p["underlying"],
                "side": p["side"],
                "open_time": _parse_ts(p.get("open_time")),
                "close_time": _parse_ts(p.get("close_time")),
                "settlement_time": _parse_ts(p.get("settlement_time")),
                "settlement_source": p["settlement_source"],
                "features": p.get("features", {}),
            }
            for p in by_id.values()
        ]
        stmt = pg_insert(Contract).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["contract_id"],
            set_={
                "open_time": stmt.excluded.open_time,
                "close_time": stmt.excluded.close_time,
                "settlement_time": stmt.excluded.settlement_time,
                "settlement_source": stmt.excluded.settlement_source,
                "features": stmt.excluded.features,
            },
        ).returning(Contract.id, Contract.contract_id)
        result = sess.execute(stmt)
        for pk, cid in result:
            self._contract_pk_cache[cid] = pk
            if self._cache is not None:
                self._cache.contract_pk[cid] = pk

    def _contract_pk(self, sess, contract_id: str) -> int:
        pk = self._contract_pk_cache.get(contract_id)
        if pk is not None:
            return pk
        if self._cache is not None:
            pk = self._cache.contract_pk.get(contract_id)
            if pk is not None:
                self._contract_pk_cache[contract_id] = pk
                return pk
        pk = self._resolver.contract_pk(sess, contract_id)
        self._contract_pk_cache[contract_id] = pk
        if self._cache is not None:
            self._cache.contract_pk[contract_id] = pk
        return pk

    def _oracle_estimate_pk(self, sess, external_id: str) -> int:
        if self._cache is not None:
            v = self._cache.oracle_estimate_pk.get(external_id)
            if v is not None:
                if isinstance(v, int):
                    return v
                if v.id is None:
                    sess.flush()
                self._cache.oracle_estimate_pk[external_id] = v.id
                return v.id
        pk = self._resolver.oracle_estimate_pk(sess, external_id)
        if self._cache is not None:
            self._cache.oracle_estimate_pk[external_id] = pk
        return pk

    def _signal_pk(self, sess, external_id: str) -> int:
        if self._cache is not None:
            v = self._cache.signal_pk.get(external_id)
            if v is not None:
                if isinstance(v, int):
                    return v
                if v.id is None:
                    sess.flush()
                self._cache.signal_pk[external_id] = v.id
                return v.id
        pk = self._resolver.signal_pk(sess, external_id)
        if self._cache is not None:
            self._cache.signal_pk[external_id] = pk
        return pk

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
        contract_id = payload["contract_id"]
        stmt = pg_insert(Contract).values(
            contract_id=contract_id,
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
        ).returning(Contract.id)
        pk = sess.execute(stmt).scalar_one()
        if self._cache is not None:
            self._cache.contract_pk[contract_id] = pk

    def _quote(self, sess, payload: dict[str, Any]) -> None:
        contract_pk = self._contract_pk(sess, payload["contract_id"])
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
        contract_pk = self._contract_pk(sess, payload["contract_id"])
        obj = OracleEstimate(
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
        )
        sess.add(obj)
        if self._cache is not None:
            # Cache the ORM handle; flush is deferred until a signal in the
            # same batch actually needs the pk — that flush sends every
            # pending oracle_estimate in one insertmanyvalues round-trip.
            self._cache.oracle_estimate_pk[payload["external_id"]] = obj

    def _signal(self, sess, payload: dict[str, Any]) -> None:
        contract_pk = self._contract_pk(sess, payload["contract_id"])
        oracle_pk = self._oracle_estimate_pk(
            sess, payload["oracle_estimate_external_id"]
        )
        obj = Signal(
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
        )
        sess.add(obj)
        if self._cache is not None:
            self._cache.signal_pk[payload["external_id"]] = obj

    def _paper_order(self, sess, payload: dict[str, Any]) -> None:
        contract_pk = self._contract_pk(sess, payload["contract_id"])
        signal_pk: int | None = None
        sig_ext = payload.get("signal_external_id")
        if sig_ext:
            signal_pk = self._signal_pk(sess, sig_ext)
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
        contract_pk = self._contract_pk(sess, payload["contract_id"])
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
    return SpooledWriter(
        spool_path=spool_path,
        sink=NeonSink(),
        drain_interval_sec=0.2,
        max_batch=500,
    )
