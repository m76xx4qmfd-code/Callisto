"""Bind venue order intents to an optional authenticated principal.

Revision ID: 202606160006
Revises: 202606160005
Create Date: 2026-08-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from alembic_helpers import column_names, safe_add_column, safe_create_index, table_names

revision = "202606160006"
down_revision = "202606160005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "venue_order_intents" not in table_names():
        return
    safe_add_column(
        "venue_order_intents",
        sa.Column("authenticated_principal_fingerprint", sa.String(length=64), nullable=True),
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_venue_order_intents_principal_fingerprint'
                  AND conrelid = 'venue_order_intents'::regclass
            ) THEN
                ALTER TABLE venue_order_intents
                ADD CONSTRAINT ck_venue_order_intents_principal_fingerprint
                CHECK (
                    authenticated_principal_fingerprint IS NULL OR
                    authenticated_principal_fingerprint ~ '^[0-9a-f]{64}$'
                );
            END IF;
        END;
        $$
        """
    )
    safe_create_index(
        "idx_venue_order_intents_principal",
        "venue_order_intents",
        ["venue", "authenticated_principal_fingerprint"],
    )


def downgrade() -> None:
    if "venue_order_intents" not in table_names():
        return
    op.execute("DROP INDEX IF EXISTS idx_venue_order_intents_principal")
    op.execute("ALTER TABLE venue_order_intents DROP CONSTRAINT IF EXISTS ck_venue_order_intents_principal_fingerprint")
    if "authenticated_principal_fingerprint" in column_names("venue_order_intents"):
        op.drop_column("venue_order_intents", "authenticated_principal_fingerprint")
