"""Add the logged venue-neutral execution ledger.

Revision ID: 202606160004
Revises: 202606160003
Create Date: 2026-08-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from alembic_helpers import safe_create_index, safe_create_table, table_names

revision = "202606160004"
down_revision = "202606160003"
branch_labels = None
depends_on = None


_TABLES = (
    "venue_order_intents",
    "venue_execution_events",
    "venue_provider_acknowledgements",
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
                    SELECT 1
                    FROM pg_trigger
                    WHERE tgname = 'trg_{table_name}_immutable'
                      AND tgrelid = '{table_name}'::regclass
                ) THEN
                    CREATE TRIGGER trg_{table_name}_immutable
                    BEFORE UPDATE OR DELETE ON {table_name}
                    FOR EACH ROW EXECUTE FUNCTION reject_venue_execution_ledger_mutation();
                END IF;
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_trigger
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


def _install_acknowledgement_validation() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_venue_provider_acknowledgement()
        RETURNS trigger AS $$
        DECLARE intended_quantity numeric;
        BEGIN
            SELECT quantity INTO intended_quantity
            FROM venue_order_intents
            WHERE id = NEW.intent_id
              AND venue = NEW.venue
              AND client_order_id = NEW.client_order_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'acknowledgement identity does not match order intent';
            END IF;
            IF NEW.filled_quantity + NEW.remaining_quantity <> intended_quantity THEN
                RAISE EXCEPTION 'acknowledgement quantity does not match order intent';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_trigger
                WHERE tgname = 'trg_venue_provider_ack_validate'
                  AND tgrelid = 'venue_provider_acknowledgements'::regclass
            ) THEN
                CREATE TRIGGER trg_venue_provider_ack_validate
                BEFORE INSERT ON venue_provider_acknowledgements
                FOR EACH ROW EXECUTE FUNCTION validate_venue_provider_acknowledgement();
            END IF;
        END;
        $$
        """
    )


def _install_execution_event_validation() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_venue_execution_event_provider_identity()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.provider_order_id IS NOT NULL AND NOT EXISTS (
                SELECT 1
                FROM venue_provider_acknowledgements
                WHERE intent_id = NEW.intent_id
                  AND venue = NEW.venue
                  AND provider_order_id = NEW.provider_order_id
            ) THEN
                RAISE EXCEPTION 'execution event provider order identity does not match acknowledgement';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_trigger
                WHERE tgname = 'trg_venue_execution_event_provider_validate'
                  AND tgrelid = 'venue_execution_events'::regclass
            ) THEN
                CREATE TRIGGER trg_venue_execution_event_provider_validate
                BEFORE INSERT ON venue_execution_events
                FOR EACH ROW EXECUTE FUNCTION validate_venue_execution_event_provider_identity();
            END IF;
        END;
        $$
        """
    )


def upgrade() -> None:
    safe_create_table(
        "venue_order_intents",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("venue", sa.String(), nullable=False),
        sa.Column("client_order_id", sa.String(), nullable=False),
        sa.Column("instrument_id", sa.String(), nullable=False),
        sa.Column("book_side", sa.String(), nullable=False),
        sa.Column("quantity", sa.Numeric(38, 18, asdecimal=True), nullable=False),
        sa.Column("limit_price", sa.Numeric(38, 18, asdecimal=True), nullable=False),
        sa.Column("time_in_force", sa.String(), nullable=False),
        sa.Column("post_only", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=True),
        sa.Column("decision_id", sa.String(), nullable=True),
        sa.Column("strategy_key", sa.String(), nullable=True),
        sa.Column("strategy_version", sa.Integer(), nullable=True),
        sa.Column("trace_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("venue", "client_order_id", name="uq_venue_order_intents_client"),
        sa.UniqueConstraint(
            "id",
            "venue",
            "client_order_id",
            name="uq_venue_order_intents_identity",
        ),
        sa.UniqueConstraint("id", "venue", name="uq_venue_order_intents_venue_identity"),
        sa.CheckConstraint("venue IN ('kalshi', 'polymarket')", name="ck_venue_order_intents_venue"),
        sa.CheckConstraint("book_side IN ('bid', 'ask')", name="ck_venue_order_intents_side"),
        sa.CheckConstraint(
            "quantity <> 'NaN'::numeric AND quantity > 0",
            name="ck_venue_order_intents_quantity",
        ),
        sa.CheckConstraint("limit_price > 0 AND limit_price < 1", name="ck_venue_order_intents_price"),
        sa.CheckConstraint(
            "time_in_force IN ('good_till_canceled', 'immediate_or_cancel', 'fill_or_kill')",
            name="ck_venue_order_intents_tif",
        ),
        sa.CheckConstraint("length(btrim(source)) > 0", name="ck_venue_order_intents_source"),
    )
    safe_create_index(
        "idx_venue_order_intents_created",
        "venue_order_intents",
        ["created_at"],
    )

    safe_create_table(
        "venue_execution_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "intent_id",
            sa.String(),
            nullable=False,
        ),
        sa.Column("venue", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("dedupe_key", sa.String(), nullable=False),
        sa.Column("provider_order_id", sa.String(), nullable=True),
        sa.Column("provider_event_id", sa.String(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["intent_id", "venue"],
            ["venue_order_intents.id", "venue_order_intents.venue"],
            name="fk_venue_execution_events_intent_venue",
        ),
        sa.UniqueConstraint("intent_id", "sequence", name="uq_venue_execution_events_sequence"),
        sa.UniqueConstraint("intent_id", "dedupe_key", name="uq_venue_execution_events_dedupe"),
        sa.UniqueConstraint("venue", "provider_event_id", name="uq_venue_execution_events_provider_event"),
        sa.CheckConstraint("sequence > 0", name="ck_venue_execution_events_sequence"),
        sa.CheckConstraint("length(btrim(event_type)) > 0", name="ck_venue_execution_events_type"),
        sa.CheckConstraint("length(btrim(source)) > 0", name="ck_venue_execution_events_source"),
        sa.CheckConstraint("length(btrim(dedupe_key)) > 0", name="ck_venue_execution_events_dedupe"),
        sa.CheckConstraint(
            "provider_event_id IS NULL OR provider_order_id IS NOT NULL",
            name="ck_venue_execution_events_provider_identity",
        ),
    )
    safe_create_index(
        "idx_venue_execution_events_intent_created",
        "venue_execution_events",
        ["intent_id", "created_at"],
    )
    safe_create_index(
        "idx_venue_execution_events_provider",
        "venue_execution_events",
        ["provider_order_id"],
    )

    safe_create_table(
        "venue_provider_acknowledgements",
        sa.Column("intent_id", sa.String(), primary_key=True),
        sa.Column("venue", sa.String(), nullable=False),
        sa.Column("client_order_id", sa.String(), nullable=False),
        sa.Column("provider_order_id", sa.String(), nullable=False),
        sa.Column("provider_status", sa.String(), nullable=False),
        sa.Column("filled_quantity", sa.Numeric(38, 18, asdecimal=True), nullable=False),
        sa.Column("remaining_quantity", sa.Numeric(38, 18, asdecimal=True), nullable=False),
        sa.Column("provider_timestamp", sa.DateTime(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["intent_id", "venue", "client_order_id"],
            [
                "venue_order_intents.id",
                "venue_order_intents.venue",
                "venue_order_intents.client_order_id",
            ],
            name="fk_venue_provider_ack_intent_identity",
        ),
        sa.UniqueConstraint("venue", "provider_order_id", name="uq_venue_provider_ack_provider"),
        sa.CheckConstraint("filled_quantity >= 0", name="ck_venue_provider_ack_filled"),
        sa.CheckConstraint("remaining_quantity >= 0", name="ck_venue_provider_ack_remaining"),
        sa.CheckConstraint("length(btrim(client_order_id)) > 0", name="ck_venue_provider_ack_client"),
        sa.CheckConstraint("length(btrim(provider_order_id)) > 0", name="ck_venue_provider_ack_provider"),
        sa.CheckConstraint("length(btrim(provider_status)) > 0", name="ck_venue_provider_ack_status"),
    )
    safe_create_index(
        "idx_venue_provider_ack_created",
        "venue_provider_acknowledgements",
        ["created_at"],
    )

    existing = table_names()
    for table_name in _TABLES:
        if table_name in existing:
            _install_immutable_trigger(table_name)
    if "venue_provider_acknowledgements" in existing:
        _install_acknowledgement_validation()
    if "venue_execution_events" in existing and "venue_provider_acknowledgements" in existing:
        _install_execution_event_validation()


def downgrade() -> None:
    existing = table_names()
    for table_name in reversed(_TABLES):
        if table_name in existing:
            op.drop_table(table_name)
    op.execute("DROP FUNCTION IF EXISTS validate_venue_provider_acknowledgement()")
    op.execute("DROP FUNCTION IF EXISTS validate_venue_execution_event_provider_identity()")
    op.execute("DROP FUNCTION IF EXISTS reject_venue_execution_ledger_mutation()")
