"""Add durable Kalshi authenticated-principal portfolio coverage evidence.

Revision ID: 202606160005
Revises: 202606160004
Create Date: 2026-08-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from alembic_helpers import safe_create_index, safe_create_table, table_names

revision = "202606160005"
down_revision = "202606160004"
branch_labels = None
depends_on = None

_TABLES = (
    "kalshi_portfolio_order_identities",
    "kalshi_portfolio_order_observations",
    "kalshi_portfolio_fill_observations",
    "kalshi_portfolio_coverage_checkpoints",
)


def _install_immutable_trigger(table_name: str) -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_venue_execution_ledger_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'venue execution ledger rows are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_trigger
                    WHERE tgname = 'trg_{table_name}_immutable'
                      AND tgrelid = '{table_name}'::regclass
                ) THEN
                    CREATE TRIGGER trg_{table_name}_immutable
                    BEFORE UPDATE OR DELETE ON {table_name}
                    FOR EACH ROW EXECUTE FUNCTION reject_venue_execution_ledger_mutation();
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_trigger
                    WHERE tgname = 'trg_{table_name}_truncate_immutable'
                      AND tgrelid = '{table_name}'::regclass
                ) THEN
                    CREATE TRIGGER trg_{table_name}_truncate_immutable
                    BEFORE TRUNCATE ON {table_name}
                    FOR EACH STATEMENT EXECUTE FUNCTION reject_venue_execution_ledger_mutation();
                END IF;
            END;
            $$
            """
        )
    )


def upgrade() -> None:
    safe_create_table(
        "kalshi_portfolio_order_identities",
        sa.Column("principal_fingerprint", sa.String(length=64), primary_key=True),
        sa.Column("order_id", sa.String(), primary_key=True),
        sa.Column("identity_hash", sa.String(length=64), nullable=False),
        sa.Column("identity_json", sa.JSON(), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "principal_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_kalshi_coverage_identity_principal",
        ),
        sa.CheckConstraint("length(btrim(order_id)) > 0", name="ck_kalshi_coverage_identity_order"),
        sa.CheckConstraint("identity_hash ~ '^[0-9a-f]{64}$'", name="ck_kalshi_coverage_identity_hash"),
    )

    safe_create_table(
        "kalshi_portfolio_order_observations",
        sa.Column("principal_fingerprint", sa.String(length=64), primary_key=True),
        sa.Column("order_id", sa.String(), primary_key=True),
        sa.Column("evidence_hash", sa.String(length=64), primary_key=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("provider_updated_at", sa.DateTime(), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "principal_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_kalshi_coverage_order_principal",
        ),
        sa.CheckConstraint("length(btrim(order_id)) > 0", name="ck_kalshi_coverage_order_id"),
        sa.CheckConstraint("evidence_hash ~ '^[0-9a-f]{64}$'", name="ck_kalshi_coverage_order_hash"),
    )
    safe_create_index(
        "idx_kalshi_coverage_orders_observed",
        "kalshi_portfolio_order_observations",
        ["first_observed_at"],
    )

    safe_create_table(
        "kalshi_portfolio_fill_observations",
        sa.Column("principal_fingerprint", sa.String(length=64), primary_key=True),
        sa.Column("fill_id", sa.String(), primary_key=True),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("provider_created_at", sa.DateTime(), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "principal_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_kalshi_coverage_fill_principal",
        ),
        sa.CheckConstraint("length(btrim(fill_id)) > 0", name="ck_kalshi_coverage_fill_id"),
        sa.CheckConstraint("length(btrim(order_id)) > 0", name="ck_kalshi_coverage_fill_order_id"),
        sa.CheckConstraint("evidence_hash ~ '^[0-9a-f]{64}$'", name="ck_kalshi_coverage_fill_hash"),
    )
    safe_create_index(
        "idx_kalshi_coverage_fills_order",
        "kalshi_portfolio_fill_observations",
        ["order_id"],
    )
    safe_create_index(
        "idx_kalshi_coverage_fills_observed",
        "kalshi_portfolio_fill_observations",
        ["first_observed_at"],
    )

    safe_create_table(
        "kalshi_portfolio_coverage_checkpoints",
        sa.Column("principal_fingerprint", sa.String(length=64), primary_key=True),
        sa.Column("coverage_id", sa.String(), primary_key=True),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("orders_cutoff_before", sa.DateTime(), nullable=False),
        sa.Column("orders_cutoff_after", sa.DateTime(), nullable=False),
        sa.Column("fills_cutoff_before", sa.DateTime(), nullable=False),
        sa.Column("fills_cutoff_after", sa.DateTime(), nullable=False),
        sa.Column("current_orders_pages", sa.Integer(), nullable=False),
        sa.Column("current_fills_pages", sa.Integer(), nullable=False),
        sa.Column("historical_orders_pages", sa.Integer(), nullable=False),
        sa.Column("historical_fills_pages", sa.Integer(), nullable=False),
        sa.Column("current_orders_unique", sa.Integer(), nullable=False),
        sa.Column("current_fills_unique", sa.Integer(), nullable=False),
        sa.Column("historical_orders_unique", sa.Integer(), nullable=False),
        sa.Column("historical_fills_unique", sa.Integer(), nullable=False),
        sa.Column("orders_unique", sa.Integer(), nullable=False),
        sa.Column("fills_unique", sa.Integer(), nullable=False),
        sa.Column("observed_evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("unknown_order_ids_json", sa.JSON(), nullable=False),
        sa.Column("unknown_client_order_ids_json", sa.JSON(), nullable=False),
        sa.Column("unknown_fill_ids_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("retry_allowed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "principal_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_kalshi_coverage_checkpoint_principal",
        ),
        sa.CheckConstraint("length(btrim(coverage_id)) > 0", name="ck_kalshi_coverage_checkpoint_id"),
        sa.CheckConstraint("status IN ('complete', 'incomplete')", name="ck_kalshi_coverage_checkpoint_status"),
        sa.CheckConstraint("length(btrim(reason)) > 0", name="ck_kalshi_coverage_checkpoint_reason"),
        sa.CheckConstraint("retry_allowed = false", name="ck_kalshi_coverage_checkpoint_no_retry"),
        sa.CheckConstraint(
            "current_orders_pages > 0 AND current_fills_pages > 0 "
            "AND historical_orders_pages > 0 AND historical_fills_pages > 0",
            name="ck_kalshi_coverage_checkpoint_pages",
        ),
        sa.CheckConstraint(
            "current_orders_unique >= 0 AND current_fills_unique >= 0 "
            "AND historical_orders_unique >= 0 AND historical_fills_unique >= 0 "
            "AND orders_unique >= 0 AND fills_unique >= 0",
            name="ck_kalshi_coverage_checkpoint_counts",
        ),
        sa.CheckConstraint(
            "observed_evidence_hash ~ '^[0-9a-f]{64}$'",
            name="ck_kalshi_coverage_checkpoint_hash",
        ),
    )
    safe_create_index(
        "idx_kalshi_coverage_checkpoints_observed",
        "kalshi_portfolio_coverage_checkpoints",
        ["observed_at"],
    )
    safe_create_index(
        "idx_kalshi_coverage_checkpoints_status",
        "kalshi_portfolio_coverage_checkpoints",
        ["status", "observed_at"],
    )

    existing = table_names()
    for table_name in _TABLES:
        if table_name in existing:
            _install_immutable_trigger(table_name)


def downgrade() -> None:
    existing = table_names()
    for table_name in reversed(_TABLES):
        if table_name in existing:
            op.drop_table(table_name)
    # Shared with the venue ledger tables; migration 004 owns function removal.
