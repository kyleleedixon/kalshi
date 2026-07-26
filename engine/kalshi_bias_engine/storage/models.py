"""SQLAlchemy ORM models.

Every model carries a ``created_at`` in UTC and, where relevant, an
``ingest_ts`` distinct from any upstream ``data_ts`` — so we can tell
"when did we learn this" from "when did it happen upstream." Post-mortems
depend on that distinction.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# -----------------------------------------------------------------------------
# Ingestion / raw
# -----------------------------------------------------------------------------


class RawPull(Base):
    """Raw upstream responses preserved verbatim. Recalibration and
    post-mortems need originals, not just parsed features."""

    __tablename__ = "raw_pulls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), index=True)  # 'kalshi_rest' etc.
    endpoint: Mapped[str] = mapped_column(String(256), index=True)
    request_params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    response: Mapped[dict] = mapped_column(JSONB)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ingest_ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


# -----------------------------------------------------------------------------
# Contracts & quotes
# -----------------------------------------------------------------------------


class Contract(Base):
    __tablename__ = "contracts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    contract_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    domain: Mapped[str] = mapped_column(String(32), index=True)
    underlying: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(8))
    open_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settlement_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    settlement_source: Mapped[str] = mapped_column(String(64))
    features: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    quotes: Mapped[list["Quote"]] = relationship(back_populates="contract")


class Quote(Base):
    """Snapshot of Kalshi top-of-book for a contract."""

    __tablename__ = "quotes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    contract_pk: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("contracts.id", ondelete="CASCADE"), index=True
    )
    bid: Mapped[float | None] = mapped_column(Float)
    ask: Mapped[float | None] = mapped_column(Float)
    bid_size: Mapped[int | None] = mapped_column(Integer)
    ask_size: Mapped[int | None] = mapped_column(Integer)
    last_trade_price: Mapped[float | None] = mapped_column(Float)
    data_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingest_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    contract: Mapped[Contract] = relationship(back_populates="quotes")


# -----------------------------------------------------------------------------
# Oracle / bias / signals
# -----------------------------------------------------------------------------


class OracleEstimate(Base):
    __tablename__ = "oracle_estimates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    contract_pk: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("contracts.id", ondelete="CASCADE"), index=True
    )
    oracle_name: Mapped[str] = mapped_column(String(64), index=True)
    oracle_version: Mapped[str] = mapped_column(String(32))
    p: Mapped[float] = mapped_column(Float)
    variance: Mapped[float] = mapped_column(Float)
    effective_sample_size: Mapped[int] = mapped_column(Integer)
    staleness: Mapped[str] = mapped_column(String(32), index=True)
    data_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)
    ingest_ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    __table_args__ = (
        CheckConstraint("p >= 0 AND p <= 1", name="ck_oracle_p_range"),
        CheckConstraint("variance >= 0", name="ck_oracle_var_nonneg"),
    )


class BiasParams(Base):
    """Fitted parameter snapshots for a bias feature. Insert-only history."""

    __tablename__ = "bias_params"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    feature_name: Mapped[str] = mapped_column(String(64), index=True)
    domain: Mapped[str] = mapped_column(String(32), index=True)  # or '*' for pooled
    params: Mapped[dict] = mapped_column(JSONB)
    in_sample_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    oos_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    oos_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    oos_brier_improvement: Mapped[float | None] = mapped_column(Float)
    oos_sample: Mapped[int] = mapped_column(Integer)
    evidence_ok: Mapped[bool] = mapped_column(Boolean)
    fit_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Signal(Base):
    """A ranked candidate emitted this cycle. Full provenance so any live
    decision is reconstructable."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    contract_pk: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("contracts.id", ondelete="CASCADE"), index=True
    )
    oracle_estimate_pk: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("oracle_estimates.id", ondelete="RESTRICT")
    )
    adjusted_p: Mapped[float] = mapped_column(Float)
    kalshi_bid: Mapped[float | None] = mapped_column(Float)
    kalshi_ask: Mapped[float | None] = mapped_column(Float)
    fee_bps: Mapped[float] = mapped_column(Float)
    edge_net: Mapped[float] = mapped_column(Float)  # after fees + crossing cost
    calibration_confidence: Mapped[float] = mapped_column(Float)
    bias_adjustments: Mapped[list] = mapped_column(JSONB, default=list)
    rank: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


# -----------------------------------------------------------------------------
# Paper ledger (Phase 1)
# -----------------------------------------------------------------------------


class PaperOrder(Base):
    __tablename__ = "paper_orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_pk: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("signals.id", ondelete="SET NULL")
    )
    contract_pk: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("contracts.id", ondelete="CASCADE"), index=True
    )
    side: Mapped[str] = mapped_column(String(8))  # YES/NO
    action: Mapped[str] = mapped_column(String(8))  # OPEN/CLOSE
    size_contracts: Mapped[int] = mapped_column(Integer)
    limit_price: Mapped[float | None] = mapped_column(Float)
    hypothetical_fill_price: Mapped[float | None] = mapped_column(Float)
    hypothetical_fill_size: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))  # NEW/FILLED/PARTIAL/REJECTED
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class PaperFill(Base):
    __tablename__ = "paper_fills"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    paper_order_pk: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("paper_orders.id", ondelete="CASCADE"), index=True
    )
    price: Mapped[float] = mapped_column(Float)
    size_contracts: Mapped[int] = mapped_column(Integer)
    fee: Mapped[float] = mapped_column(Float)
    fill_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Settlement(Base):
    """Settled outcome per contract. Feeds calibration."""

    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    contract_pk: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("contracts.id", ondelete="CASCADE"), unique=True
    )
    outcome: Mapped[str] = mapped_column(String(8))  # YES/NO/VOID
    settlement_value: Mapped[float | None] = mapped_column(Float)  # index composite
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingest_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CalibrationSnapshot(Base):
    """A full per-band per-domain snapshot. First-class object read by
    ExecutionPolicy — do not compute on-demand."""

    __tablename__ = "calibration_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    phase_gate_min_sample: Mapped[int] = mapped_column(Integer)
    # payload shape: [{"domain": "...", "band_lo": 0.0, "band_hi": 0.1,
    #                   "settled_count": ..., "empirical_freq": ...,
    #                   "mean_predicted": ..., "brier": ...,
    #                   "brier_vs_uncorrected": ...}]
    bands: Mapped[list] = mapped_column(JSONB)


# -----------------------------------------------------------------------------
# Control plane
# -----------------------------------------------------------------------------


class Control(Base):
    """Single-row (per key) control settings the dashboard writes.

    Kill switch, per-domain phase, config knobs. Reads fail closed — if the
    engine cannot read this table freshly, it must not place new orders.
    """

    __tablename__ = "control"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    updated_by: Mapped[str | None] = mapped_column(String(64))


class Heartbeat(Base):
    """Engine heartbeat. Dashboard reads staleness to power dead-man's-switch."""

    __tablename__ = "heartbeat"

    engine_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_beat: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    phase: Mapped[str] = mapped_column(String(16))  # PAPER/LIVE/HALTED
    version: Mapped[str] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)


# -----------------------------------------------------------------------------
# Realized-vol side table (Kraken)
# -----------------------------------------------------------------------------


class RealizedVol(Base):
    __tablename__ = "realized_vol"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    underlying: Mapped[str] = mapped_column(String(32), index=True)
    horizon_seconds: Mapped[int] = mapped_column(Integer, index=True)
    sigma_annualized: Mapped[float] = mapped_column(Float)
    tick_count: Mapped[int] = mapped_column(Integer)
    data_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingest_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_realized_vol_lookup", "underlying", "horizon_seconds", "data_ts"),
    )


class SettlementBasis(Base):
    """Measured basis between Kraken spot and CF Benchmarks settlement value.
    Feeds oracle confidence — Kraken is a PROXY for the composite index."""

    __tablename__ = "settlement_basis"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    underlying: Mapped[str] = mapped_column(String(32), index=True)
    settlement_value: Mapped[float] = mapped_column(Float)
    kraken_spot_at_settle: Mapped[float] = mapped_column(Float)
    basis_bps: Mapped[float] = mapped_column(Float)
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingest_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
