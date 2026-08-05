"""Add authoritative Kalshi portfolio projection persistence.

Revision ID: 202608040001
Revises: 202606160006
Create Date: 2026-08-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from alembic_helpers import safe_create_index, safe_create_table, table_names

revision = "202608040001"
down_revision = "202606160006"
branch_labels = None
depends_on = None

_IMMUTABLE = (
    "kalshi_portfolio_coverage_order_memberships",
    "kalshi_portfolio_coverage_fill_memberships",
    "kalshi_portfolio_projection_attempts",
)
_TABLES = (
    "kalshi_portfolio_coverage_order_memberships",
    "kalshi_portfolio_coverage_fill_memberships",
    "kalshi_portfolio_projection_attempts",
    "kalshi_portfolio_projection_heads",
    "kalshi_portfolio_projection_leases",
)


def _immutable(table: str) -> None:
    op.execute(
        sa.text(f"""
    DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_{table}_immutable' AND tgrelid='{table}'::regclass) THEN
        CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table}
        FOR EACH ROW EXECUTE FUNCTION reject_venue_execution_ledger_mutation();
      END IF;
      IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_{table}_truncate_immutable' AND tgrelid='{table}'::regclass) THEN
        CREATE TRIGGER trg_{table}_truncate_immutable BEFORE TRUNCATE ON {table}
        FOR EACH STATEMENT EXECUTE FUNCTION reject_venue_execution_ledger_mutation();
      END IF;
    END $$
    """)
    )


def upgrade() -> None:
    safe_create_table(
        "kalshi_portfolio_coverage_order_memberships",
        sa.Column("principal_fingerprint", sa.String(64), primary_key=True),
        sa.Column("coverage_id", sa.String(), primary_key=True),
        sa.Column("order_id", sa.String(), primary_key=True),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["principal_fingerprint", "coverage_id"],
            [
                "kalshi_portfolio_coverage_checkpoints.principal_fingerprint",
                "kalshi_portfolio_coverage_checkpoints.coverage_id",
            ],
            name="fk_kalshi_coverage_order_membership_checkpoint",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["principal_fingerprint", "order_id", "evidence_hash"],
            [
                "kalshi_portfolio_order_observations.principal_fingerprint",
                "kalshi_portfolio_order_observations.order_id",
                "kalshi_portfolio_order_observations.evidence_hash",
            ],
            name="fk_kalshi_coverage_order_membership_observation",
        ),
        sa.CheckConstraint(
            "principal_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_kalshi_coverage_order_member_principal"
        ),
        sa.CheckConstraint("length(btrim(coverage_id)) > 0", name="ck_kalshi_coverage_order_member_coverage"),
        sa.CheckConstraint("length(btrim(order_id)) > 0", name="ck_kalshi_coverage_order_member_order"),
        sa.CheckConstraint("evidence_hash ~ '^[0-9a-f]{64}$'", name="ck_kalshi_coverage_order_member_hash"),
    )
    safe_create_table(
        "kalshi_portfolio_coverage_fill_memberships",
        sa.Column("principal_fingerprint", sa.String(64), primary_key=True),
        sa.Column("coverage_id", sa.String(), primary_key=True),
        sa.Column("fill_id", sa.String(), primary_key=True),
        sa.ForeignKeyConstraint(
            ["principal_fingerprint", "coverage_id"],
            [
                "kalshi_portfolio_coverage_checkpoints.principal_fingerprint",
                "kalshi_portfolio_coverage_checkpoints.coverage_id",
            ],
            name="fk_kalshi_coverage_fill_membership_checkpoint",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["principal_fingerprint", "fill_id"],
            ["kalshi_portfolio_fill_observations.principal_fingerprint", "kalshi_portfolio_fill_observations.fill_id"],
            name="fk_kalshi_coverage_fill_membership_observation",
        ),
        sa.CheckConstraint("principal_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_kalshi_coverage_fill_member_principal"),
        sa.CheckConstraint("length(btrim(coverage_id)) > 0", name="ck_kalshi_coverage_fill_member_coverage"),
        sa.CheckConstraint("length(btrim(fill_id)) > 0", name="ck_kalshi_coverage_fill_member_fill"),
    )
    safe_create_table(
        "kalshi_portfolio_projection_attempts",
        sa.Column("principal_fingerprint", sa.String(64), primary_key=True),
        sa.Column("projection_id", sa.String(), primary_key=True),
        sa.Column("coverage_id", sa.String(), nullable=True),
        sa.Column("subaccount_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=False),
        sa.Column("coverage_observed_at", sa.DateTime(), nullable=True),
        sa.Column("positions_observed_at", sa.DateTime(), nullable=True),
        sa.Column("balance_observed_at", sa.DateTime(), nullable=True),
        sa.Column("settlements_observed_at", sa.DateTime(), nullable=True),
        sa.Column("component_skew_seconds", sa.Numeric(38, 6), nullable=True),
        sa.Column("correctness_freshness_bound_seconds", sa.Numeric(38, 6), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=True),
        sa.Column("balance_json", sa.JSON(), nullable=True),
        sa.Column("positions_json", sa.JSON(), nullable=True),
        sa.Column("settlements_json", sa.JSON(), nullable=True),
        sa.Column("gaps_json", sa.JSON(), nullable=False),
        sa.Column("retry_allowed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.ForeignKeyConstraint(
            ["principal_fingerprint", "coverage_id"],
            [
                "kalshi_portfolio_coverage_checkpoints.principal_fingerprint",
                "kalshi_portfolio_coverage_checkpoints.coverage_id",
            ],
            name="fk_kalshi_projection_attempt_coverage",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint("principal_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_kalshi_projection_attempt_principal"),
        sa.CheckConstraint(
            "status IN ('complete', 'incomplete', 'failed')", name="ck_kalshi_projection_attempt_status"
        ),
        sa.CheckConstraint(
            "(status = 'failed' AND coverage_id IS NULL) OR (status <> 'failed' AND coverage_id IS NOT NULL)",
            name="ck_kalshi_projection_attempt_coverage_status",
        ),
        sa.CheckConstraint("subaccount_number BETWEEN 0 AND 63", name="ck_kalshi_projection_attempt_subaccount"),
        sa.CheckConstraint("retry_allowed = false", name="ck_kalshi_projection_attempt_no_retry"),
        sa.CheckConstraint("correctness_freshness_bound_seconds > 0", name="ck_kalshi_projection_attempt_bound"),
    )
    safe_create_index(
        "idx_kalshi_projection_attempt_completed", "kalshi_portfolio_projection_attempts", ["completed_at"]
    )
    safe_create_table(
        "kalshi_portfolio_projection_heads",
        sa.Column("principal_fingerprint", sa.String(64), primary_key=True),
        sa.Column("latest_projection_id", sa.String(), nullable=False),
        sa.Column("healthy_projection_id", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["principal_fingerprint", "latest_projection_id"],
            [
                "kalshi_portfolio_projection_attempts.principal_fingerprint",
                "kalshi_portfolio_projection_attempts.projection_id",
            ],
            name="fk_kalshi_projection_head_latest",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["principal_fingerprint", "healthy_projection_id"],
            [
                "kalshi_portfolio_projection_attempts.principal_fingerprint",
                "kalshi_portfolio_projection_attempts.projection_id",
            ],
            name="fk_kalshi_projection_head_healthy",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint("principal_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_kalshi_projection_head_principal"),
    )
    safe_create_table(
        "kalshi_portfolio_projection_leases",
        sa.Column("principal_fingerprint", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("fence_token", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("principal_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_kalshi_projection_lease_principal"),
        sa.CheckConstraint("length(btrim(owner_id)) > 0", name="ck_kalshi_projection_lease_owner"),
        sa.CheckConstraint("fence_token > 0", name="ck_kalshi_projection_lease_fence"),
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_kalshi_projection_healthy_head()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.healthy_projection_id IS NOT NULL AND NOT EXISTS (
                SELECT 1
                FROM kalshi_portfolio_projection_attempts
                WHERE principal_fingerprint = NEW.principal_fingerprint
                  AND projection_id = NEW.healthy_projection_id
                  AND status = 'complete'
            ) THEN
                RAISE EXCEPTION 'healthy projection must reference a complete attempt';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_kalshi_projection_healthy_head_validate ON kalshi_portfolio_projection_heads"
    )
    op.execute(
        "CREATE TRIGGER trg_kalshi_projection_healthy_head_validate "
        "BEFORE INSERT OR UPDATE OF healthy_projection_id ON kalshi_portfolio_projection_heads "
        "FOR EACH ROW EXECUTE FUNCTION validate_kalshi_projection_healthy_head()"
    )
    existing = table_names()
    for table in _IMMUTABLE:
        if table in existing:
            _immutable(table)


def downgrade() -> None:
    existing = table_names()
    for table in reversed(_TABLES):
        if table in existing:
            op.drop_table(table)
