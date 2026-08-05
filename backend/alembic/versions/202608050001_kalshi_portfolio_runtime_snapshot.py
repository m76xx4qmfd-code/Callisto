"""Add principal-scoped Kalshi portfolio runtime health.

Revision ID: 202608050001
Revises: 202608040001
Create Date: 2026-08-05
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic_helpers import safe_create_table

revision = "202608050001"
down_revision = "202608040001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    safe_create_table(
        "kalshi_portfolio_runtime_snapshots",
        sa.Column("principal_fingerprint", sa.String(64), primary_key=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("running", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("current_activity", sa.String(), nullable=True),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("lag_seconds", sa.Numeric(precision=24, scale=12), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("stats_json", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "principal_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_kalshi_runtime_snapshot_principal",
        ),
    )


def downgrade() -> None:
    pass
