"""Add local Kalshi paper GTC orders and full cancellation evidence.

Revision ID: 202608060001
Revises: 202608050002
Create Date: 2026-08-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from alembic_helpers import safe_add_column, safe_create_index, safe_create_table

revision = "202608060001"
down_revision = "202608050002"
branch_labels = None
depends_on = None


def _unique_constraint_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in set(inspector.get_table_names()):
        return set()
    return {
        str(constraint["name"])
        for constraint in inspector.get_unique_constraints(table_name)
        if constraint.get("name")
    }


def _check_constraint_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in set(inspector.get_table_names()):
        return set()
    return {
        str(constraint["name"])
        for constraint in inspector.get_check_constraints(table_name)
        if constraint.get("name")
    }


def upgrade() -> None:
    safe_add_column(
        "kalshi_paper_accounts",
        sa.Column("reserved_cash", sa.Numeric(), nullable=False, server_default=sa.text("0")),
    )
    safe_add_column(
        "kalshi_paper_intents",
        sa.Column("time_in_force", sa.String(), nullable=True),
    )

    if "ck_kalshi_paper_accounts_reserved_cash" not in _check_constraint_names("kalshi_paper_accounts"):
        op.execute(
            "ALTER TABLE kalshi_paper_accounts "
            "ADD CONSTRAINT ck_kalshi_paper_accounts_reserved_cash CHECK ("
            "reserved_cash <> 'NaN'::numeric AND reserved_cash < 'Infinity'::numeric "
            "AND reserved_cash >= 0 AND scale(reserved_cash) <= 18 AND reserved_cash <= cash_balance)"
        )

    op.execute("ALTER TABLE kalshi_paper_intents DROP CONSTRAINT IF EXISTS ck_kalshi_paper_intents_request_shape")
    op.execute(
        "ALTER TABLE kalshi_paper_intents ADD CONSTRAINT ck_kalshi_paper_intents_request_shape CHECK ("
        "(action = 'pass' AND time_in_force IS NULL "
        "AND requested_quantity IS NULL AND limit_price IS NULL) OR "
        "(action = 'execute' AND (time_in_force IS NULL OR "
        "time_in_force IN ('immediate_or_cancel', 'good_till_canceled')) "
        "AND requested_quantity <> 'NaN'::numeric AND requested_quantity < 'Infinity'::numeric "
        "AND requested_quantity > 0 AND scale(requested_quantity) <= 2 "
        "AND limit_price <> 'NaN'::numeric AND limit_price > 0 AND limit_price < 1 "
        "AND scale(limit_price) <= 6))"
    )

    op.execute("ALTER TABLE kalshi_paper_decisions DROP CONSTRAINT IF EXISTS ck_kalshi_paper_decisions_request_shape")
    op.execute(
        "ALTER TABLE kalshi_paper_decisions ADD CONSTRAINT ck_kalshi_paper_decisions_request_shape CHECK ("
        "(action = 'pass' AND order_side IS NULL AND time_in_force IS NULL "
        "AND requested_quantity IS NULL AND limit_price IS NULL) OR "
        "(action = 'execute' AND order_side = 'buy' "
        "AND time_in_force IN ('immediate_or_cancel', 'good_till_canceled') "
        "AND requested_quantity <> 'NaN'::numeric AND requested_quantity < 'Infinity'::numeric "
        "AND requested_quantity > 0 AND scale(requested_quantity) <= 2 "
        "AND limit_price <> 'NaN'::numeric AND limit_price > 0 AND limit_price < 1 "
        "AND scale(limit_price) <= 6))"
    )
    if "uq_kalshi_paper_decisions_order_facts" not in _unique_constraint_names("kalshi_paper_decisions"):
        op.execute(
            "ALTER TABLE kalshi_paper_decisions "
            "ADD CONSTRAINT uq_kalshi_paper_decisions_order_facts UNIQUE ("
            "account_id, decision_id, ticker, outcome, order_side, time_in_force, requested_quantity, "
            "filled_quantity, remaining_quantity, limit_price, status)"
        )

    safe_create_table(
        "kalshi_paper_orders",
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("order_id", sa.String(), nullable=False),
        sa.Column("decision_id", sa.String(), nullable=False),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("time_in_force", sa.String(), nullable=False),
        sa.Column("requested_quantity", sa.Numeric(), nullable=False),
        sa.Column("filled_quantity", sa.Numeric(), nullable=False),
        sa.Column("open_quantity", sa.Numeric(), nullable=False),
        sa.Column("limit_price", sa.Numeric(), nullable=False),
        sa.Column("decision_status", sa.String(), nullable=False),
        sa.Column("reserved_cash", sa.Numeric(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("account_id", "order_id", name="pk_kalshi_paper_orders"),
        sa.ForeignKeyConstraint(
            [
                "account_id", "decision_id", "ticker", "outcome", "side", "time_in_force",
                "requested_quantity", "filled_quantity", "open_quantity", "limit_price", "decision_status",
            ],
            [
                "kalshi_paper_decisions.account_id", "kalshi_paper_decisions.decision_id",
                "kalshi_paper_decisions.ticker", "kalshi_paper_decisions.outcome",
                "kalshi_paper_decisions.order_side", "kalshi_paper_decisions.time_in_force",
                "kalshi_paper_decisions.requested_quantity", "kalshi_paper_decisions.filled_quantity",
                "kalshi_paper_decisions.remaining_quantity", "kalshi_paper_decisions.limit_price",
                "kalshi_paper_decisions.status",
            ],
            ondelete="RESTRICT",
            name="fk_kalshi_paper_orders_decision_facts",
        ),
        sa.UniqueConstraint("account_id", "decision_id", name="uq_kalshi_paper_orders_decision"),
        sa.UniqueConstraint("account_id", "order_id", "reserved_cash", name="uq_kalshi_paper_orders_reservation"),
        sa.CheckConstraint("length(btrim(order_id)) > 0", name="ck_kalshi_paper_orders_id"),
        sa.CheckConstraint("side = 'buy'", name="ck_kalshi_paper_orders_side"),
        sa.CheckConstraint("outcome IN ('yes', 'no')", name="ck_kalshi_paper_orders_outcome"),
        sa.CheckConstraint("time_in_force = 'good_till_canceled'", name="ck_kalshi_paper_orders_tif"),
        sa.CheckConstraint(
            "(decision_status = 'filled' AND open_quantity = 0) OR "
            "(decision_status IN ('partial', 'no_fill') AND open_quantity > 0)",
            name="ck_kalshi_paper_orders_decision_status",
        ),
        sa.CheckConstraint(
            "requested_quantity <> 'NaN'::numeric AND requested_quantity < 'Infinity'::numeric "
            "AND requested_quantity > 0 AND scale(requested_quantity) <= 2 "
            "AND filled_quantity <> 'NaN'::numeric AND filled_quantity < 'Infinity'::numeric "
            "AND filled_quantity >= 0 AND scale(filled_quantity) <= 2 "
            "AND open_quantity <> 'NaN'::numeric AND open_quantity < 'Infinity'::numeric "
            "AND open_quantity >= 0 AND scale(open_quantity) <= 2 "
            "AND requested_quantity = filled_quantity + open_quantity",
            name="ck_kalshi_paper_orders_quantities",
        ),
        sa.CheckConstraint(
            "limit_price <> 'NaN'::numeric AND limit_price > 0 "
            "AND limit_price < 1 AND scale(limit_price) <= 6",
            name="ck_kalshi_paper_orders_price",
        ),
        sa.CheckConstraint(
            "reserved_cash <> 'NaN'::numeric AND reserved_cash < 'Infinity'::numeric "
            "AND reserved_cash >= 0 AND scale(reserved_cash) <= 18 "
            "AND reserved_cash = open_quantity * limit_price",
            name="ck_kalshi_paper_orders_reservation_cash",
        ),
    )
    safe_create_table(
        "kalshi_paper_cancellations",
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("cancellation_id", sa.String(), nullable=False),
        sa.Column("order_id", sa.String(), nullable=False),
        sa.Column("released_cash", sa.Numeric(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("account_id", "cancellation_id", name="pk_kalshi_paper_cancellations"),
        sa.ForeignKeyConstraint(
            ["account_id", "order_id", "released_cash"],
            ["kalshi_paper_orders.account_id", "kalshi_paper_orders.order_id", "kalshi_paper_orders.reserved_cash"],
            ondelete="RESTRICT",
            name="fk_kalshi_paper_cancellations_order_reservation",
        ),
        sa.UniqueConstraint("account_id", "order_id", name="uq_kalshi_paper_cancellations_order"),
        sa.CheckConstraint("length(btrim(cancellation_id)) > 0", name="ck_kalshi_paper_cancellations_id"),
        sa.CheckConstraint("status = 'cancelled'", name="ck_kalshi_paper_cancellations_status"),
        sa.CheckConstraint(
            "released_cash <> 'NaN'::numeric AND released_cash < 'Infinity'::numeric "
            "AND released_cash > 0 AND scale(released_cash) <= 18",
            name="ck_kalshi_paper_cancellations_cash",
        ),
    )
    safe_create_table(
        "kalshi_paper_order_events",
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("order_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("cancellation_id", sa.String(), nullable=True),
        sa.Column("quantity", sa.Numeric(), nullable=False),
        sa.Column("reserved_cash", sa.Numeric(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("account_id", "order_id", "sequence", name="pk_kalshi_paper_order_events"),
        sa.ForeignKeyConstraint(
            ["account_id", "order_id"],
            ["kalshi_paper_orders.account_id", "kalshi_paper_orders.order_id"],
            ondelete="RESTRICT",
            name="fk_kalshi_paper_order_events_order",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "cancellation_id"],
            ["kalshi_paper_cancellations.account_id", "kalshi_paper_cancellations.cancellation_id"],
            ondelete="RESTRICT",
            name="fk_kalshi_paper_order_events_cancellation",
        ),
        sa.CheckConstraint("sequence IN (1, 2)", name="ck_kalshi_paper_order_events_sequence"),
        sa.CheckConstraint("event_type IN ('opened', 'cancelled')", name="ck_kalshi_paper_order_events_type"),
        sa.CheckConstraint(
            "(event_type = 'opened' AND sequence = 1 AND cancellation_id IS NULL) OR "
            "(event_type = 'cancelled' AND sequence = 2 AND cancellation_id IS NOT NULL)",
            name="ck_kalshi_paper_order_events_shape",
        ),
        sa.CheckConstraint(
            "quantity <> 'NaN'::numeric AND quantity < 'Infinity'::numeric "
            "AND quantity >= 0 AND scale(quantity) <= 2",
            name="ck_kalshi_paper_order_events_quantity",
        ),
        sa.CheckConstraint(
            "reserved_cash <> 'NaN'::numeric AND reserved_cash < 'Infinity'::numeric "
            "AND reserved_cash >= 0 AND scale(reserved_cash) <= 18",
            name="ck_kalshi_paper_order_events_cash",
        ),
    )
    safe_create_index("idx_kalshi_paper_orders_account_created", "kalshi_paper_orders", ["account_id", "created_at"])
    safe_create_index(
        "idx_kalshi_paper_order_events_cancel",
        "kalshi_paper_order_events",
        ["account_id", "cancellation_id"],
        unique=True,
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_kalshi_paper_decision_intent()
        RETURNS trigger AS $$
        DECLARE intended kalshi_paper_intents%ROWTYPE;
        BEGIN
            SELECT * INTO intended FROM kalshi_paper_intents
             WHERE account_id = NEW.account_id AND decision_id = NEW.decision_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'Kalshi paper decision intent does not exist'; END IF;
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
               OR NEW.time_in_force IS DISTINCT FROM (
                  CASE WHEN intended.action = 'execute'
                       THEN COALESCE(intended.time_in_force, 'immediate_or_cancel')
                       ELSE NULL END)
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
        """
        CREATE OR REPLACE FUNCTION validate_kalshi_paper_order_lifecycle()
        RETURNS trigger AS $$
        DECLARE
            target_account_id text;
            projected_reserved numeric;
            evidenced_reserved numeric;
            broken_orders bigint;
        BEGIN
            IF TG_TABLE_NAME = 'kalshi_paper_accounts' THEN target_account_id := NEW.id;
            ELSE target_account_id := NEW.account_id; END IF;
            SELECT reserved_cash INTO projected_reserved FROM kalshi_paper_accounts WHERE id = target_account_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'Kalshi paper account does not exist'; END IF;
            SELECT COALESCE(sum(o.reserved_cash), 0) INTO evidenced_reserved
              FROM kalshi_paper_orders o
             WHERE o.account_id = target_account_id
               AND NOT EXISTS (SELECT 1 FROM kalshi_paper_cancellations c
                                WHERE c.account_id = o.account_id AND c.order_id = o.order_id);
            IF projected_reserved IS DISTINCT FROM evidenced_reserved THEN
                RAISE EXCEPTION 'Kalshi paper account reserved cash does not match immutable orders';
            END IF;
            SELECT count(*) INTO broken_orders FROM kalshi_paper_orders o
             WHERE o.account_id = target_account_id AND (
                (SELECT count(*) FROM kalshi_paper_order_events e
                  WHERE e.account_id=o.account_id AND e.order_id=o.order_id AND e.sequence=1
                    AND e.event_type='opened' AND e.cancellation_id IS NULL
                    AND e.quantity=o.open_quantity AND e.reserved_cash=o.reserved_cash) <> 1
                OR (NOT EXISTS (SELECT 1 FROM kalshi_paper_cancellations c
                                 WHERE c.account_id=o.account_id AND c.order_id=o.order_id)
                    AND EXISTS (SELECT 1 FROM kalshi_paper_order_events e
                                 WHERE e.account_id=o.account_id AND e.order_id=o.order_id AND e.sequence=2))
                OR EXISTS (SELECT 1 FROM kalshi_paper_cancellations c
                            WHERE c.account_id=o.account_id AND c.order_id=o.order_id
                              AND (SELECT count(*) FROM kalshi_paper_order_events e
                                    WHERE e.account_id=o.account_id AND e.order_id=o.order_id AND e.sequence=2
                                      AND e.event_type='cancelled' AND e.cancellation_id=c.cancellation_id
                                      AND e.quantity=o.open_quantity AND e.reserved_cash=c.released_cash) <> 1)
             );
            IF broken_orders <> 0 THEN
                RAISE EXCEPTION 'Kalshi paper order events contradict immutable order or cancellation evidence';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )

    for table in ("kalshi_paper_orders", "kalshi_paper_cancellations", "kalshi_paper_order_events"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_truncate_immutable ON {table}")
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_kalshi_paper_evidence_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_truncate_immutable BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION reject_kalshi_paper_evidence_mutation()"
        )

    trigger_specs = (
        ("kalshi_paper_accounts", "trg_kalshi_paper_accounts_validate_order_lifecycle", "AFTER INSERT OR UPDATE"),
        ("kalshi_paper_orders", "trg_kalshi_paper_orders_validate_lifecycle", "AFTER INSERT"),
        ("kalshi_paper_cancellations", "trg_kalshi_paper_cancellations_validate_lifecycle", "AFTER INSERT"),
        ("kalshi_paper_order_events", "trg_kalshi_paper_order_events_validate_lifecycle", "AFTER INSERT"),
    )
    for table, name, timing in trigger_specs:
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
        op.execute(
            f"CREATE CONSTRAINT TRIGGER {name} {timing} ON {table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION validate_kalshi_paper_order_lifecycle()"
        )


def downgrade() -> None:
    # Financial evidence and its validation are forward-only by policy.
    return
