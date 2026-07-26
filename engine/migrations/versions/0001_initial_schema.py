"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-07-26
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "raw_pulls",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("endpoint", sa.String(length=256), nullable=False),
        sa.Column("request_params", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("ingest_ts", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_raw_pulls_source", "raw_pulls", ["source"])
    op.create_index("ix_raw_pulls_endpoint", "raw_pulls", ["endpoint"])
    op.create_index("ix_raw_pulls_ingest_ts", "raw_pulls", ["ingest_ts"])

    op.create_table(
        "contracts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("contract_id", sa.String(length=128), nullable=False),
        sa.Column("domain", sa.String(length=32), nullable=False),
        sa.Column("underlying", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settlement_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settlement_source", sa.String(length=64), nullable=False),
        sa.Column("features", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("contract_id"),
    )
    op.create_index("ix_contracts_domain", "contracts", ["domain"])
    op.create_index("ix_contracts_underlying", "contracts", ["underlying"])
    op.create_index("ix_contracts_settlement_time", "contracts", ["settlement_time"])

    op.create_table(
        "quotes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("contract_pk", sa.BigInteger(),
                  sa.ForeignKey("contracts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("bid", sa.Float(), nullable=True),
        sa.Column("ask", sa.Float(), nullable=True),
        sa.Column("bid_size", sa.Integer(), nullable=True),
        sa.Column("ask_size", sa.Integer(), nullable=True),
        sa.Column("last_trade_price", sa.Float(), nullable=True),
        sa.Column("data_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingest_ts", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_quotes_contract_pk", "quotes", ["contract_pk"])
    op.create_index("ix_quotes_data_ts", "quotes", ["data_ts"])

    op.create_table(
        "oracle_estimates",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("contract_pk", sa.BigInteger(),
                  sa.ForeignKey("contracts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("oracle_name", sa.String(length=64), nullable=False),
        sa.Column("oracle_version", sa.String(length=32), nullable=False),
        sa.Column("p", sa.Float(), nullable=False),
        sa.Column("variance", sa.Float(), nullable=False),
        sa.Column("effective_sample_size", sa.Integer(), nullable=False),
        sa.Column("staleness", sa.String(length=32), nullable=False),
        sa.Column("data_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("ingest_ts", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("p >= 0 AND p <= 1", name="ck_oracle_p_range"),
        sa.CheckConstraint("variance >= 0", name="ck_oracle_var_nonneg"),
    )
    op.create_index("ix_oracle_estimates_contract_pk", "oracle_estimates", ["contract_pk"])
    op.create_index("ix_oracle_estimates_oracle_name", "oracle_estimates", ["oracle_name"])
    op.create_index("ix_oracle_estimates_staleness", "oracle_estimates", ["staleness"])
    op.create_index("ix_oracle_estimates_ingest_ts", "oracle_estimates", ["ingest_ts"])

    op.create_table(
        "bias_params",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("feature_name", sa.String(length=64), nullable=False),
        sa.Column("domain", sa.String(length=32), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("in_sample_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("oos_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("oos_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("oos_brier_improvement", sa.Float(), nullable=True),
        sa.Column("oos_sample", sa.Integer(), nullable=False),
        sa.Column("evidence_ok", sa.Boolean(), nullable=False),
        sa.Column("fit_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_bias_params_feature_name", "bias_params", ["feature_name"])
    op.create_index("ix_bias_params_domain", "bias_params", ["domain"])

    op.create_table(
        "signals",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("contract_pk", sa.BigInteger(),
                  sa.ForeignKey("contracts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("oracle_estimate_pk", sa.BigInteger(),
                  sa.ForeignKey("oracle_estimates.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("adjusted_p", sa.Float(), nullable=False),
        sa.Column("kalshi_bid", sa.Float(), nullable=True),
        sa.Column("kalshi_ask", sa.Float(), nullable=True),
        sa.Column("fee_bps", sa.Float(), nullable=False),
        sa.Column("edge_net", sa.Float(), nullable=False),
        sa.Column("calibration_confidence", sa.Float(), nullable=False),
        sa.Column("bias_adjustments", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_signals_contract_pk", "signals", ["contract_pk"])
    op.create_index("ix_signals_created_at", "signals", ["created_at"])

    op.create_table(
        "paper_orders",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("signal_pk", sa.BigInteger(),
                  sa.ForeignKey("signals.id", ondelete="SET NULL"), nullable=True),
        sa.Column("contract_pk", sa.BigInteger(),
                  sa.ForeignKey("contracts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("action", sa.String(length=8), nullable=False),
        sa.Column("size_contracts", sa.Integer(), nullable=False),
        sa.Column("limit_price", sa.Float(), nullable=True),
        sa.Column("hypothetical_fill_price", sa.Float(), nullable=True),
        sa.Column("hypothetical_fill_size", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_paper_orders_contract_pk", "paper_orders", ["contract_pk"])
    op.create_index("ix_paper_orders_created_at", "paper_orders", ["created_at"])

    op.create_table(
        "paper_fills",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("paper_order_pk", sa.BigInteger(),
                  sa.ForeignKey("paper_orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("size_contracts", sa.Integer(), nullable=False),
        sa.Column("fee", sa.Float(), nullable=False),
        sa.Column("fill_ts", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_paper_fills_paper_order_pk", "paper_fills", ["paper_order_pk"])

    op.create_table(
        "settlements",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("contract_pk", sa.BigInteger(),
                  sa.ForeignKey("contracts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("outcome", sa.String(length=8), nullable=False),
        sa.Column("settlement_value", sa.Float(), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingest_ts", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("contract_pk"),
    )
    op.create_index("ix_settlements_settled_at", "settlements", ["settled_at"])

    op.create_table(
        "calibration_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("phase_gate_min_sample", sa.Integer(), nullable=False),
        sa.Column("bands", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )
    op.create_index("ix_calibration_snapshots_generated_at",
                    "calibration_snapshots", ["generated_at"])

    op.create_table(
        "control",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
    )

    op.create_table(
        "heartbeat",
        sa.Column("engine_id", sa.String(length=64), primary_key=True),
        sa.Column("last_beat", sa.DateTime(timezone=True), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
    )

    op.create_table(
        "realized_vol",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("underlying", sa.String(length=32), nullable=False),
        sa.Column("horizon_seconds", sa.Integer(), nullable=False),
        sa.Column("sigma_annualized", sa.Float(), nullable=False),
        sa.Column("tick_count", sa.Integer(), nullable=False),
        sa.Column("data_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingest_ts", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_realized_vol_underlying", "realized_vol", ["underlying"])
    op.create_index("ix_realized_vol_horizon", "realized_vol", ["horizon_seconds"])
    op.create_index("ix_realized_vol_data_ts", "realized_vol", ["data_ts"])
    op.create_index("ix_realized_vol_lookup", "realized_vol",
                    ["underlying", "horizon_seconds", "data_ts"])

    op.create_table(
        "settlement_basis",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("underlying", sa.String(length=32), nullable=False),
        sa.Column("settlement_value", sa.Float(), nullable=False),
        sa.Column("kraken_spot_at_settle", sa.Float(), nullable=False),
        sa.Column("basis_bps", sa.Float(), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingest_ts", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_settlement_basis_underlying", "settlement_basis", ["underlying"])
    op.create_index("ix_settlement_basis_settled_at", "settlement_basis", ["settled_at"])

    # Seed control keys so the dashboard can toggle them without INSERT.
    op.execute("""
        INSERT INTO control (key, value, updated_at, updated_by) VALUES
          ('kill_switch', '{"active": false}'::jsonb, NOW(), 'migration'),
          ('phase_gates',
            '{"crypto": {"unlocked": false, "min_settled_sample": 200,
                          "min_brier_improvement": 0.005}}'::jsonb,
            NOW(), 'migration')
        ON CONFLICT (key) DO NOTHING;
    """)


def downgrade() -> None:
    for t in [
        "settlement_basis", "realized_vol", "heartbeat", "control",
        "calibration_snapshots", "settlements", "paper_fills", "paper_orders",
        "signals", "bias_params", "oracle_estimates", "quotes", "contracts",
        "raw_pulls",
    ]:
        op.drop_table(t)
