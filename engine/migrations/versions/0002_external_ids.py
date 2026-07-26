"""external_id on oracle_estimates and signals

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-26
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add nullable, backfill (empty tables in practice — this migration
    # runs before the engine has been pointed at Neon), then enforce NOT NULL.
    op.add_column(
        "oracle_estimates",
        sa.Column("external_id", sa.String(length=64), nullable=True),
    )
    op.execute(
        "UPDATE oracle_estimates SET external_id = gen_random_uuid()::text "
        "WHERE external_id IS NULL"
    )
    op.alter_column("oracle_estimates", "external_id", nullable=False)
    op.create_unique_constraint(
        "uq_oracle_estimates_external_id", "oracle_estimates", ["external_id"]
    )
    op.create_index(
        "ix_oracle_estimates_external_id", "oracle_estimates", ["external_id"]
    )

    op.add_column(
        "signals",
        sa.Column("external_id", sa.String(length=64), nullable=True),
    )
    op.execute(
        "UPDATE signals SET external_id = gen_random_uuid()::text "
        "WHERE external_id IS NULL"
    )
    op.alter_column("signals", "external_id", nullable=False)
    op.create_unique_constraint(
        "uq_signals_external_id", "signals", ["external_id"]
    )
    op.create_index("ix_signals_external_id", "signals", ["external_id"])


def downgrade() -> None:
    op.drop_index("ix_signals_external_id", table_name="signals")
    op.drop_constraint("uq_signals_external_id", "signals", type_="unique")
    op.drop_column("signals", "external_id")

    op.drop_index("ix_oracle_estimates_external_id", table_name="oracle_estimates")
    op.drop_constraint(
        "uq_oracle_estimates_external_id", "oracle_estimates", type_="unique"
    )
    op.drop_column("oracle_estimates", "external_id")
