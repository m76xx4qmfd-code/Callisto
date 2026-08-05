"""Add exact restart-safe Kalshi paper execution evidence.

Revision ID: 202608050002
Revises: 202608050001
Create Date: 2026-08-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from alembic_helpers import safe_create_index, safe_create_table, table_names

revision = "202608050002"
down_revision = "202608050001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    safe_create_table(
        "kalshi_paper_accounts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False, server_default="USD"),
        sa.Column("starting_cash", sa.Numeric(), nullable=False),
        sa.Column("cash_balance", sa.Numeric(), nullable=False),
        sa.Column("journal_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("name", name="uq_kalshi_paper_accounts_name"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_kalshi_paper_accounts_name"),
        sa.CheckConstraint("currency = 'USD'", name="ck_kalshi_paper_accounts_currency"),
        sa.CheckConstraint(
            "starting_cash <> 'NaN'::numeric AND starting_cash < 'Infinity'::numeric "
            "AND starting_cash >= 0 AND scale(starting_cash) <= 18",
            name="ck_kalshi_paper_accounts_starting_cash",
        ),
        sa.CheckConstraint(
            "cash_balance <> 'NaN'::numeric AND cash_balance < 'Infinity'::numeric "
            "AND cash_balance >= 0 AND scale(cash_balance) <= 18",
            name="ck_kalshi_paper_accounts_cash_balance",
        ),
        sa.CheckConstraint("journal_sequence >= 0", name="ck_kalshi_paper_accounts_sequence"),
    )
    safe_create_table(
        "kalshi_paper_intents",
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("decision_id", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("opportunity_id", sa.String(), nullable=False),
        sa.Column("opportunity_stable_id", sa.String(), nullable=False),
        sa.Column("opportunity_revision", sa.String(64), nullable=False),
        sa.Column("opportunity_snapshot_json", sa.Text(), nullable=False),
        sa.Column("strategy_key", sa.String(), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=True),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("requested_quantity", sa.Numeric(), nullable=True),
        sa.Column("limit_price", sa.Numeric(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("account_id", "decision_id", name="pk_kalshi_paper_intents"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["kalshi_paper_accounts.id"],
            ondelete="RESTRICT",
            name="fk_kalshi_paper_intents_account",
        ),
        sa.UniqueConstraint(
            "account_id",
            "decision_id",
            "request_hash",
            name="uq_kalshi_paper_intents_request_identity",
        ),
        sa.CheckConstraint("length(btrim(decision_id)) > 0", name="ck_kalshi_paper_intents_id"),
        sa.CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="ck_kalshi_paper_intents_request_hash"),
        sa.CheckConstraint(
            "opportunity_revision ~ '^[0-9a-f]{64}$'",
            name="ck_kalshi_paper_intents_opportunity_revision",
        ),
        sa.CheckConstraint("action IN ('execute', 'pass')", name="ck_kalshi_paper_intents_action"),
        sa.CheckConstraint("outcome IN ('yes', 'no')", name="ck_kalshi_paper_intents_outcome"),
        sa.CheckConstraint(
            "(action = 'pass' AND requested_quantity IS NULL AND limit_price IS NULL) OR "
            "(action = 'execute' AND requested_quantity <> 'NaN'::numeric "
            "AND requested_quantity < 'Infinity'::numeric AND requested_quantity > 0 "
            "AND scale(requested_quantity) <= 2 "
            "AND limit_price <> 'NaN'::numeric AND limit_price > 0 AND limit_price < 1 "
            "AND scale(limit_price) <= 6)",
            name="ck_kalshi_paper_intents_request_shape",
        ),
    )
    safe_create_table(
        "kalshi_paper_decisions",
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("decision_id", sa.String(), nullable=False),
        sa.Column("account_sequence", sa.BigInteger(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("opportunity_id", sa.String(), nullable=False),
        sa.Column("opportunity_stable_id", sa.String(), nullable=False),
        sa.Column("opportunity_revision", sa.String(64), nullable=False),
        sa.Column("opportunity_snapshot_json", sa.Text(), nullable=False),
        sa.Column("strategy_key", sa.String(), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=True),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("event_ticker", sa.String(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("order_side", sa.String(), nullable=True),
        sa.Column("time_in_force", sa.String(), nullable=True),
        sa.Column("requested_quantity", sa.Numeric(), nullable=True),
        sa.Column("limit_price", sa.Numeric(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("source_origin", sa.String(), nullable=True),
        sa.Column("market_observed_at", sa.DateTime(), nullable=True),
        sa.Column("market_fetched_at", sa.DateTime(), nullable=True),
        sa.Column("market_evidence_hash", sa.String(64), nullable=True),
        sa.Column("market_evidence_json", sa.Text(), nullable=True),
        sa.Column("book_observed_at", sa.DateTime(), nullable=True),
        sa.Column("book_fetched_at", sa.DateTime(), nullable=True),
        sa.Column("book_evidence_hash", sa.String(64), nullable=True),
        sa.Column("book_evidence_json", sa.Text(), nullable=True),
        sa.Column("fill_formula_version", sa.String(), nullable=False),
        sa.Column("fee_rule_version", sa.String(), nullable=False),
        sa.Column("fee_provenance_json", sa.Text(), nullable=False),
        sa.Column("filled_quantity", sa.Numeric(), nullable=False),
        sa.Column("remaining_quantity", sa.Numeric(), nullable=False),
        sa.Column("average_fill_price", sa.Numeric(), nullable=True),
        sa.Column("notional", sa.Numeric(), nullable=False),
        sa.Column("fee", sa.Numeric(), nullable=False),
        sa.Column("cash_before", sa.Numeric(), nullable=False),
        sa.Column("cash_after", sa.Numeric(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("account_id", "decision_id", name="pk_kalshi_paper_decisions"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["kalshi_paper_accounts.id"],
            ondelete="RESTRICT",
            name="fk_kalshi_paper_decisions_account",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "decision_id", "request_hash"],
            [
                "kalshi_paper_intents.account_id",
                "kalshi_paper_intents.decision_id",
                "kalshi_paper_intents.request_hash",
            ],
            ondelete="RESTRICT",
            name="fk_kalshi_paper_decisions_intent",
        ),
        sa.UniqueConstraint("account_id", "account_sequence", name="uq_kalshi_paper_decisions_sequence"),
        sa.CheckConstraint("length(btrim(decision_id)) > 0", name="ck_kalshi_paper_decisions_id"),
        sa.CheckConstraint("account_sequence > 0", name="ck_kalshi_paper_decisions_sequence"),
        sa.CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="ck_kalshi_paper_decisions_request_hash"),
        sa.CheckConstraint(
            "opportunity_revision ~ '^[0-9a-f]{64}$'",
            name="ck_kalshi_paper_decisions_opportunity_revision",
        ),
        sa.CheckConstraint("action IN ('execute', 'pass')", name="ck_kalshi_paper_decisions_action"),
        sa.CheckConstraint("outcome IN ('yes', 'no')", name="ck_kalshi_paper_decisions_outcome"),
        sa.CheckConstraint(
            "status IN ('filled', 'partial', 'no_fill', 'passed', 'rejected')",
            name="ck_kalshi_paper_decisions_status",
        ),
        sa.CheckConstraint("length(btrim(reason)) > 0", name="ck_kalshi_paper_decisions_reason"),
        sa.CheckConstraint(
            "market_evidence_hash IS NULL OR market_evidence_hash ~ '^[0-9a-f]{64}$'",
            name="ck_kalshi_paper_decisions_market_hash",
        ),
        sa.CheckConstraint(
            "book_evidence_hash IS NULL OR book_evidence_hash ~ '^[0-9a-f]{64}$'",
            name="ck_kalshi_paper_decisions_book_hash",
        ),
        sa.CheckConstraint(
            "(action = 'pass' AND order_side IS NULL AND time_in_force IS NULL "
            "AND requested_quantity IS NULL AND limit_price IS NULL) OR "
            "(action = 'execute' AND order_side = 'buy' AND time_in_force = 'immediate_or_cancel' "
            "AND requested_quantity <> 'NaN'::numeric AND requested_quantity < 'Infinity'::numeric "
            "AND requested_quantity > 0 AND scale(requested_quantity) <= 2 "
            "AND limit_price <> 'NaN'::numeric AND limit_price > 0 AND limit_price < 1 "
            "AND scale(limit_price) <= 6)",
            name="ck_kalshi_paper_decisions_request_shape",
        ),
        sa.CheckConstraint(
            "filled_quantity <> 'NaN'::numeric AND filled_quantity < 'Infinity'::numeric "
            "AND filled_quantity >= 0 AND scale(filled_quantity) <= 2 "
            "AND remaining_quantity <> 'NaN'::numeric AND remaining_quantity < 'Infinity'::numeric "
            "AND remaining_quantity >= 0 AND scale(remaining_quantity) <= 2",
            name="ck_kalshi_paper_decisions_quantities",
        ),
        sa.CheckConstraint(
            "notional <> 'NaN'::numeric AND notional < 'Infinity'::numeric AND notional >= 0 "
            "AND scale(notional) <= 18 "
            "AND fee <> 'NaN'::numeric AND fee < 'Infinity'::numeric AND fee >= 0 "
            "AND scale(fee) <= 18 "
            "AND cash_before <> 'NaN'::numeric AND cash_before < 'Infinity'::numeric AND cash_before >= 0 "
            "AND scale(cash_before) <= 18 "
            "AND cash_after <> 'NaN'::numeric AND cash_after < 'Infinity'::numeric AND cash_after >= 0 "
            "AND scale(cash_after) <= 18",
            name="ck_kalshi_paper_decisions_money",
        ),
        sa.CheckConstraint(
            "(average_fill_price IS NULL AND filled_quantity = 0) OR "
            "(average_fill_price <> 'NaN'::numeric AND average_fill_price > 0 AND average_fill_price < 1 "
            "AND scale(average_fill_price) <= 18 AND filled_quantity > 0 "
            "AND average_fill_price <= limit_price)",
            name="ck_kalshi_paper_decisions_average_price",
        ),
        sa.CheckConstraint(
            "(action = 'pass' AND filled_quantity = 0 AND remaining_quantity = 0) OR "
            "(action = 'execute' AND requested_quantity = filled_quantity + remaining_quantity)",
            name="ck_kalshi_paper_decisions_quantity_conservation",
        ),
        sa.CheckConstraint(
            "cash_after = cash_before - notional - fee",
            name="ck_kalshi_paper_decisions_cash_conservation",
        ),
        sa.CheckConstraint(
            "(status = 'filled' AND action = 'execute' AND filled_quantity = requested_quantity "
            "AND remaining_quantity = 0) OR "
            "(status = 'partial' AND action = 'execute' AND filled_quantity > 0 "
            "AND filled_quantity < requested_quantity AND remaining_quantity > 0) OR "
            "(status = 'no_fill' AND action = 'execute' AND filled_quantity = 0 "
            "AND remaining_quantity = requested_quantity AND notional = 0 AND fee = 0) OR "
            "(status = 'passed' AND action = 'pass' AND filled_quantity = 0 "
            "AND remaining_quantity = 0 AND notional = 0 AND fee = 0) OR "
            "(status = 'rejected' AND filled_quantity = 0 AND notional = 0 AND fee = 0 "
            "AND ((action = 'pass' AND remaining_quantity = 0) OR "
            "(action = 'execute' AND remaining_quantity = requested_quantity)))",
            name="ck_kalshi_paper_decisions_status_shape",
        ),
    )
    safe_create_table(
        "kalshi_paper_fills",
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("decision_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Numeric(), nullable=False),
        sa.Column("price", sa.Numeric(), nullable=False),
        sa.Column("notional", sa.Numeric(), nullable=False),
        sa.Column("fee", sa.Numeric(), nullable=False),
        sa.Column("source_bid_price", sa.Numeric(), nullable=False),
        sa.Column("source_side", sa.String(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("account_id", "decision_id", "sequence", name="pk_kalshi_paper_fills"),
        sa.ForeignKeyConstraint(
            ["account_id", "decision_id"],
            ["kalshi_paper_decisions.account_id", "kalshi_paper_decisions.decision_id"],
            ondelete="RESTRICT",
            name="fk_kalshi_paper_fills_decision",
        ),
        sa.CheckConstraint("sequence > 0", name="ck_kalshi_paper_fills_sequence"),
        sa.CheckConstraint(
            "quantity <> 'NaN'::numeric AND quantity < 'Infinity'::numeric "
            "AND quantity > 0 AND scale(quantity) <= 2",
            name="ck_kalshi_paper_fills_quantity",
        ),
        sa.CheckConstraint(
            "price <> 'NaN'::numeric AND price > 0 AND price < 1 AND scale(price) <= 6",
            name="ck_kalshi_paper_fills_price",
        ),
        sa.CheckConstraint(
            "source_bid_price <> 'NaN'::numeric AND source_bid_price > 0 "
            "AND source_bid_price < 1 AND scale(source_bid_price) <= 6",
            name="ck_kalshi_paper_fills_source_price",
        ),
        sa.CheckConstraint("source_side IN ('yes', 'no')", name="ck_kalshi_paper_fills_source_side"),
        sa.CheckConstraint(
            "notional <> 'NaN'::numeric AND notional < 'Infinity'::numeric "
            "AND notional > 0 AND scale(notional) <= 18 "
            "AND notional = quantity * price",
            name="ck_kalshi_paper_fills_notional",
        ),
        sa.CheckConstraint(
            "fee <> 'NaN'::numeric AND fee < 'Infinity'::numeric AND fee >= 0 AND scale(fee) <= 18",
            name="ck_kalshi_paper_fills_fee",
        ),
    )
    safe_create_index(
        "idx_kalshi_paper_decisions_created",
        "kalshi_paper_decisions",
        ["account_id", "created_at"],
    )
    safe_create_index(
        "idx_kalshi_paper_decisions_market",
        "kalshi_paper_decisions",
        ["account_id", "ticker", "created_at"],
    )
    safe_create_index(
        "idx_kalshi_paper_decisions_opportunity",
        "kalshi_paper_decisions",
        ["opportunity_id", "opportunity_revision"],
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_kalshi_paper_evidence_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'Kalshi paper financial evidence is immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_intents_immutable ON kalshi_paper_intents")
    op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_intents_truncate_immutable ON kalshi_paper_intents")
    op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_decisions_immutable ON kalshi_paper_decisions")
    op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_decisions_truncate_immutable ON kalshi_paper_decisions")
    op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_fills_immutable ON kalshi_paper_fills")
    op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_fills_truncate_immutable ON kalshi_paper_fills")
    op.execute(
        "CREATE TRIGGER trg_kalshi_paper_intents_immutable "
        "BEFORE UPDATE OR DELETE ON kalshi_paper_intents "
        "FOR EACH ROW EXECUTE FUNCTION reject_kalshi_paper_evidence_mutation()"
    )
    op.execute(
        "CREATE TRIGGER trg_kalshi_paper_intents_truncate_immutable "
        "BEFORE TRUNCATE ON kalshi_paper_intents "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_kalshi_paper_evidence_mutation()"
    )
    op.execute(
        "CREATE TRIGGER trg_kalshi_paper_decisions_immutable "
        "BEFORE UPDATE OR DELETE ON kalshi_paper_decisions "
        "FOR EACH ROW EXECUTE FUNCTION reject_kalshi_paper_evidence_mutation()"
    )
    op.execute(
        "CREATE TRIGGER trg_kalshi_paper_decisions_truncate_immutable "
        "BEFORE TRUNCATE ON kalshi_paper_decisions "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_kalshi_paper_evidence_mutation()"
    )
    op.execute(
        "CREATE TRIGGER trg_kalshi_paper_fills_immutable "
        "BEFORE UPDATE OR DELETE ON kalshi_paper_fills "
        "FOR EACH ROW EXECUTE FUNCTION reject_kalshi_paper_evidence_mutation()"
    )
    op.execute(
        "CREATE TRIGGER trg_kalshi_paper_fills_truncate_immutable "
        "BEFORE TRUNCATE ON kalshi_paper_fills "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_kalshi_paper_evidence_mutation()"
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_kalshi_paper_decision_intent()
        RETURNS trigger AS $$
        DECLARE intended kalshi_paper_intents%ROWTYPE;
        BEGIN
            SELECT * INTO intended
              FROM kalshi_paper_intents
             WHERE account_id = NEW.account_id
               AND decision_id = NEW.decision_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Kalshi paper decision intent does not exist';
            END IF;
            IF NEW.request_hash IS DISTINCT FROM intended.request_hash
               OR NEW.action IS DISTINCT FROM intended.action
               OR NEW.opportunity_id IS DISTINCT FROM intended.opportunity_id
               OR NEW.opportunity_stable_id IS DISTINCT FROM intended.opportunity_stable_id
               OR NEW.opportunity_revision IS DISTINCT FROM intended.opportunity_revision
               OR NEW.opportunity_snapshot_json IS DISTINCT FROM intended.opportunity_snapshot_json
               OR NEW.strategy_key IS DISTINCT FROM intended.strategy_key
               OR NEW.strategy_version IS DISTINCT FROM intended.strategy_version
               OR NEW.ticker IS DISTINCT FROM intended.ticker
               OR NEW.outcome IS DISTINCT FROM intended.outcome
               OR NEW.requested_quantity IS DISTINCT FROM intended.requested_quantity
               OR NEW.limit_price IS DISTINCT FROM intended.limit_price THEN
                RAISE EXCEPTION 'Kalshi paper decision contradicts immutable intent';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_kalshi_paper_decisions_validate_intent ON kalshi_paper_decisions"
    )
    op.execute(
        "CREATE TRIGGER trg_kalshi_paper_decisions_validate_intent "
        "BEFORE INSERT ON kalshi_paper_decisions "
        "FOR EACH ROW EXECUTE FUNCTION validate_kalshi_paper_decision_intent()"
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_kalshi_paper_fill_aggregate()
        RETURNS trigger AS $$
        DECLARE
            expected_quantity numeric;
            expected_notional numeric;
            expected_fee numeric;
            actual_quantity numeric;
            actual_notional numeric;
            actual_fee numeric;
            actual_count integer;
            min_sequence integer;
            max_sequence integer;
        BEGIN
            SELECT filled_quantity, notional, fee
              INTO expected_quantity, expected_notional, expected_fee
              FROM kalshi_paper_decisions
             WHERE account_id = NEW.account_id AND decision_id = NEW.decision_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Kalshi paper fill decision does not exist';
            END IF;
            SELECT COALESCE(sum(quantity), 0), COALESCE(sum(notional), 0),
                   COALESCE(sum(fee), 0), count(*), min(sequence), max(sequence)
              INTO actual_quantity, actual_notional, actual_fee,
                   actual_count, min_sequence, max_sequence
              FROM kalshi_paper_fills
             WHERE account_id = NEW.account_id AND decision_id = NEW.decision_id;
            IF actual_quantity <> expected_quantity
               OR actual_notional <> expected_notional
               OR actual_fee <> expected_fee THEN
                RAISE EXCEPTION 'Kalshi paper fill aggregate does not match decision';
            END IF;
            IF (actual_count = 0) <> (expected_quantity = 0) THEN
                RAISE EXCEPTION 'Kalshi paper fill count does not match decision';
            END IF;
            IF actual_count > 0 AND (min_sequence <> 1 OR max_sequence <> actual_count) THEN
                RAISE EXCEPTION 'Kalshi paper fill sequence is not contiguous';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_kalshi_paper_decisions_fill_aggregate ON kalshi_paper_decisions"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_fills_fill_aggregate ON kalshi_paper_fills")
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_kalshi_paper_decisions_fill_aggregate "
        "AFTER INSERT ON kalshi_paper_decisions DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION validate_kalshi_paper_fill_aggregate()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_kalshi_paper_fills_fill_aggregate "
        "AFTER INSERT ON kalshi_paper_fills DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION validate_kalshi_paper_fill_aggregate()"
    )


def downgrade() -> None:
    existing = table_names()
    if "kalshi_paper_fills" in existing:
        op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_fills_fill_aggregate ON kalshi_paper_fills")
        op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_fills_immutable ON kalshi_paper_fills")
        op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_fills_truncate_immutable ON kalshi_paper_fills")
        op.drop_table("kalshi_paper_fills")
    if "kalshi_paper_decisions" in existing:
        op.execute(
            "DROP TRIGGER IF EXISTS trg_kalshi_paper_decisions_fill_aggregate ON kalshi_paper_decisions"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_kalshi_paper_decisions_validate_intent ON kalshi_paper_decisions"
        )
        op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_decisions_immutable ON kalshi_paper_decisions")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_kalshi_paper_decisions_truncate_immutable ON kalshi_paper_decisions"
        )
        op.drop_table("kalshi_paper_decisions")
    if "kalshi_paper_intents" in existing:
        op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_intents_immutable ON kalshi_paper_intents")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_kalshi_paper_intents_truncate_immutable ON kalshi_paper_intents"
        )
        op.drop_table("kalshi_paper_intents")
    if "kalshi_paper_accounts" in existing:
        op.drop_table("kalshi_paper_accounts")
    op.execute("DROP FUNCTION IF EXISTS validate_kalshi_paper_fill_aggregate()")
    op.execute("DROP FUNCTION IF EXISTS validate_kalshi_paper_decision_intent()")
    op.execute("DROP FUNCTION IF EXISTS reject_kalshi_paper_evidence_mutation()")
