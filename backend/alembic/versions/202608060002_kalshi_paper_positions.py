"""Add exact Kalshi paper positions and SELL IOC exit accounting.

Revision ID: 202608060002
Revises: 202608060001
Create Date: 2026-08-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from alembic_helpers import safe_add_column, safe_create_index, safe_create_table

revision = "202608060002"
down_revision = "202608060001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    safe_add_column("kalshi_paper_intents", sa.Column("order_side", sa.String(), nullable=True))
    safe_add_column("kalshi_paper_intents", sa.Column("position_id", sa.String(), nullable=True))
    safe_add_column("kalshi_paper_decisions", sa.Column("position_id", sa.String(), nullable=True))
    safe_add_column("kalshi_paper_decisions", sa.Column("position_cost_basis", sa.Numeric(), nullable=True))
    safe_add_column("kalshi_paper_decisions", sa.Column("realized_pnl", sa.Numeric(), nullable=True))

    op.execute("ALTER TABLE kalshi_paper_intents DROP CONSTRAINT IF EXISTS ck_kalshi_paper_intents_request_shape")
    op.execute(
        "ALTER TABLE kalshi_paper_intents ADD CONSTRAINT ck_kalshi_paper_intents_request_shape CHECK ("
        "(action = 'pass' AND order_side IS NULL AND position_id IS NULL AND time_in_force IS NULL "
        "AND requested_quantity IS NULL AND limit_price IS NULL) OR "
        "(action = 'execute' AND ((COALESCE(order_side, 'buy') = 'buy' AND position_id IS NULL "
        "AND (time_in_force IS NULL OR time_in_force IN ('immediate_or_cancel', 'good_till_canceled'))) "
        "OR (order_side = 'sell' AND length(btrim(position_id)) > 0 "
        "AND time_in_force = 'immediate_or_cancel')) "
        "AND requested_quantity <> 'NaN'::numeric AND requested_quantity < 'Infinity'::numeric "
        "AND requested_quantity > 0 AND scale(requested_quantity) <= 2 "
        "AND limit_price <> 'NaN'::numeric AND limit_price > 0 AND limit_price < 1 "
        "AND scale(limit_price) <= 6))"
    )

    op.execute("ALTER TABLE kalshi_paper_decisions DROP CONSTRAINT IF EXISTS ck_kalshi_paper_decisions_request_shape")
    op.execute(
        "ALTER TABLE kalshi_paper_decisions ADD CONSTRAINT ck_kalshi_paper_decisions_request_shape CHECK ("
        "(action = 'pass' AND order_side IS NULL AND position_id IS NULL AND time_in_force IS NULL "
        "AND requested_quantity IS NULL AND limit_price IS NULL) OR "
        "(action = 'execute' AND ((order_side = 'buy' AND position_id IS NULL "
        "AND time_in_force IN ('immediate_or_cancel', 'good_till_canceled')) OR "
        "(order_side = 'sell' AND length(btrim(position_id)) > 0 "
        "AND time_in_force = 'immediate_or_cancel')) "
        "AND requested_quantity <> 'NaN'::numeric AND requested_quantity < 'Infinity'::numeric "
        "AND requested_quantity > 0 AND scale(requested_quantity) <= 2 "
        "AND limit_price <> 'NaN'::numeric AND limit_price > 0 AND limit_price < 1 "
        "AND scale(limit_price) <= 6))"
    )
    op.execute("ALTER TABLE kalshi_paper_decisions DROP CONSTRAINT IF EXISTS ck_kalshi_paper_decisions_average_price")
    op.execute(
        "ALTER TABLE kalshi_paper_decisions ADD CONSTRAINT ck_kalshi_paper_decisions_average_price CHECK ("
        "(average_fill_price IS NULL AND filled_quantity = 0) OR "
        "(average_fill_price <> 'NaN'::numeric AND average_fill_price > 0 AND average_fill_price < 1 "
        "AND scale(average_fill_price) <= 18 AND filled_quantity > 0 "
        "AND ((order_side = 'buy' AND average_fill_price <= limit_price) OR "
        "(order_side = 'sell' AND average_fill_price >= limit_price))))"
    )
    op.execute("ALTER TABLE kalshi_paper_decisions DROP CONSTRAINT IF EXISTS ck_kalshi_paper_decisions_cash_conservation")
    op.execute(
        "ALTER TABLE kalshi_paper_decisions ADD CONSTRAINT ck_kalshi_paper_decisions_cash_conservation CHECK ("
        "(order_side = 'sell' AND cash_after = cash_before + notional - fee) OR "
        "(order_side IS DISTINCT FROM 'sell' AND cash_after = cash_before - notional - fee))"
    )
    op.execute("ALTER TABLE kalshi_paper_decisions DROP CONSTRAINT IF EXISTS ck_kalshi_paper_decisions_realized_pnl")
    op.execute(
        "ALTER TABLE kalshi_paper_decisions ADD CONSTRAINT ck_kalshi_paper_decisions_realized_pnl CHECK ("
        "(order_side IS DISTINCT FROM 'sell' AND position_cost_basis IS NULL AND realized_pnl IS NULL) OR "
        "(order_side = 'sell' AND position_cost_basis <> 'NaN'::numeric "
        "AND position_cost_basis < 'Infinity'::numeric AND position_cost_basis >= 0 "
        "AND scale(position_cost_basis) <= 18 AND realized_pnl <> 'NaN'::numeric "
        "AND realized_pnl < 'Infinity'::numeric AND realized_pnl > '-Infinity'::numeric "
        "AND scale(realized_pnl) <= 18 AND realized_pnl = notional - fee - position_cost_basis "
        "AND ((filled_quantity = 0 AND position_cost_basis = 0 AND realized_pnl = 0) OR "
        "(filled_quantity > 0 AND position_cost_basis > 0))))"
    )

    safe_create_table(
        "kalshi_paper_positions",
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("position_id", sa.String(), nullable=False),
        sa.Column("entry_decision_id", sa.String(), nullable=False),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("entry_quantity", sa.Numeric(), nullable=False),
        sa.Column("entry_notional", sa.Numeric(), nullable=False),
        sa.Column("entry_fee", sa.Numeric(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("account_id", "position_id", name="pk_kalshi_paper_positions"),
        sa.ForeignKeyConstraint(
            ["account_id", "entry_decision_id"],
            ["kalshi_paper_decisions.account_id", "kalshi_paper_decisions.decision_id"],
            ondelete="RESTRICT",
            name="fk_kalshi_paper_positions_entry_decision",
        ),
        sa.UniqueConstraint("account_id", "entry_decision_id", name="uq_kalshi_paper_positions_entry_decision"),
        sa.CheckConstraint("length(btrim(position_id)) > 0", name="ck_kalshi_paper_positions_id"),
        sa.CheckConstraint("outcome IN ('yes', 'no')", name="ck_kalshi_paper_positions_outcome"),
        sa.CheckConstraint(
            "entry_quantity <> 'NaN'::numeric AND entry_quantity < 'Infinity'::numeric "
            "AND entry_quantity > 0 AND scale(entry_quantity) <= 2",
            name="ck_kalshi_paper_positions_quantity",
        ),
        sa.CheckConstraint(
            "entry_notional <> 'NaN'::numeric AND entry_notional < 'Infinity'::numeric "
            "AND entry_notional > 0 AND scale(entry_notional) <= 18 "
            "AND entry_fee <> 'NaN'::numeric AND entry_fee < 'Infinity'::numeric "
            "AND entry_fee >= 0 AND scale(entry_fee) <= 18",
            name="ck_kalshi_paper_positions_money",
        ),
    )
    safe_create_index(
        "idx_kalshi_paper_positions_account_created",
        "kalshi_paper_positions",
        ["account_id", "created_at"],
    )
    op.execute(
        "INSERT INTO kalshi_paper_positions (account_id, position_id, entry_decision_id, ticker, outcome, "
        "entry_quantity, entry_notional, entry_fee, created_at) "
        "SELECT d.account_id, 'paper-position:legacy:' || md5(d.account_id || '|legacy-position|' || d.decision_id), "
        "d.decision_id, d.ticker, d.outcome, d.filled_quantity, d.notional, d.fee, d.created_at "
        "FROM kalshi_paper_decisions d WHERE d.action = 'execute' AND d.order_side = 'buy' "
        "AND d.filled_quantity > 0 ON CONFLICT (account_id, entry_decision_id) DO NOTHING"
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_kalshi_paper_decision_intent()
        RETURNS trigger AS $$
        DECLARE intended kalshi_paper_intents%ROWTYPE;
                fee_provenance jsonb; fee_provenance_key_count integer;
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
               OR NEW.order_side IS DISTINCT FROM (
                  CASE WHEN intended.action = 'execute' THEN COALESCE(intended.order_side, 'buy') ELSE NULL END)
               OR NEW.position_id IS DISTINCT FROM intended.position_id
               OR NEW.requested_quantity IS DISTINCT FROM intended.requested_quantity
               OR NEW.limit_price IS DISTINCT FROM intended.limit_price
               OR NEW.time_in_force IS DISTINCT FROM (
                  CASE WHEN intended.action = 'execute'
                       THEN COALESCE(intended.time_in_force, 'immediate_or_cancel') ELSE NULL END) THEN
                RAISE EXCEPTION 'Kalshi paper decision contradicts immutable intent';
            END IF;
            IF NEW.fee IS DISTINCT FROM 0 THEN
                RAISE EXCEPTION 'Kalshi paper decision fee contradicts the fee-waiver contract';
            END IF;
            fee_provenance := NEW.fee_provenance_json::jsonb;
            IF NEW.fee_rule_version = 'not_evaluated' THEN
                IF fee_provenance IS DISTINCT FROM '{}'::jsonb THEN
                    RAISE EXCEPTION 'Unevaluated Kalshi paper fee provenance must be empty';
                END IF;
            ELSIF NEW.fee_rule_version = 'kalshi-market-fee-waiver-v1' THEN
                SELECT count(*) INTO fee_provenance_key_count FROM jsonb_object_keys(fee_provenance);
                IF jsonb_typeof(fee_provenance) IS DISTINCT FROM 'object'
                   OR fee_provenance_key_count <> 5
                   OR fee_provenance->>'kind' IS DISTINCT FROM 'market_fee_waiver'
                   OR fee_provenance->>'openapi_sha256' IS DISTINCT FROM
                      '41d93050bf3f692cf3a898ba3a1a033f3e857fee56370ddcb18af6a4225f41cb'
                   OR NOT COALESCE(fee_provenance->>'market_snapshot_hash', '') ~ '^[0-9a-f]{64}$'
                   OR fee_provenance->>'market_snapshot_hash' IS DISTINCT FROM NEW.market_evidence_hash
                   OR (fee_provenance->>'waiver_expiration_time')::timestamptz IS DISTINCT FROM
                      (NEW.market_evidence_json::jsonb->>'fee_waiver_expiration_time')::timestamptz
                   OR (fee_provenance->>'observed_at')::timestamptz IS DISTINCT FROM
                      NEW.market_observed_at AT TIME ZONE 'UTC'
                   OR length(btrim(COALESCE(fee_provenance->>'waiver_expiration_time', ''))) = 0
                   OR length(btrim(COALESCE(fee_provenance->>'observed_at', ''))) = 0 THEN
                    RAISE EXCEPTION 'Kalshi paper fee-waiver provenance is invalid';
                END IF;
            ELSE
                RAISE EXCEPTION 'Kalshi paper fee rule is unsupported';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_kalshi_paper_fill_aggregate()
        RETURNS trigger AS $$
        DECLARE target_account_id text; target_decision_id text;
                expected_quantity numeric; expected_notional numeric; expected_fee numeric;
                expected_order_side text; expected_outcome text; expected_limit_price numeric;
                actual_quantity numeric; actual_notional numeric; actual_fee numeric;
                actual_count integer; min_sequence integer; max_sequence integer;
        BEGIN
            target_account_id := NEW.account_id; target_decision_id := NEW.decision_id;
            SELECT filled_quantity, notional, fee, order_side, outcome, limit_price
              INTO expected_quantity, expected_notional, expected_fee,
                   expected_order_side, expected_outcome, expected_limit_price
              FROM kalshi_paper_decisions
             WHERE account_id=target_account_id AND decision_id=target_decision_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'Kalshi paper fill decision does not exist'; END IF;
            SELECT COALESCE(sum(quantity),0), COALESCE(sum(notional),0), COALESCE(sum(fee),0),
                   count(*), min(sequence), max(sequence)
              INTO actual_quantity, actual_notional, actual_fee, actual_count, min_sequence, max_sequence
              FROM kalshi_paper_fills WHERE account_id=target_account_id AND decision_id=target_decision_id;
            IF actual_quantity<>expected_quantity OR actual_notional<>expected_notional
               OR actual_fee<>expected_fee THEN
              RAISE EXCEPTION 'Kalshi paper fill aggregate does not match decision';
            END IF;
            IF (actual_count=0)<>(expected_quantity=0) THEN
              RAISE EXCEPTION 'Kalshi paper fill count does not match decision';
            END IF;
            IF actual_count>0 AND (min_sequence<>1 OR max_sequence<>actual_count) THEN
              RAISE EXCEPTION 'Kalshi paper fill sequence is not contiguous';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_kalshi_paper_sell_fill()
        RETURNS trigger AS $$
        DECLARE decided kalshi_paper_decisions%ROWTYPE;
                book_evidence jsonb; fill_evidence jsonb; fill_evidence_key_count integer;
                source_level_count integer; source_level_quantity numeric; prior_level_quantity numeric;
        BEGIN
            SELECT * INTO decided
              FROM kalshi_paper_decisions
             WHERE account_id=NEW.account_id AND decision_id=NEW.decision_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'Kalshi paper fill decision does not exist'; END IF;
            IF decided.order_side<>'sell' THEN RETURN NEW; END IF;
            IF NEW.source_side IS DISTINCT FROM decided.outcome
               OR NEW.source_bid_price IS DISTINCT FROM NEW.price OR NEW.price<decided.limit_price THEN
              RAISE EXCEPTION 'Kalshi paper SELL fill contradicts decision evidence';
            END IF;
              IF decided.market_evidence_json IS NULL OR decided.market_evidence_hash IS NULL
              OR decided.book_evidence_json IS NULL OR decided.book_evidence_hash IS NULL
              OR encode(sha256(convert_to(decided.market_evidence_json, 'UTF8')), 'hex')
                  IS DISTINCT FROM decided.market_evidence_hash
              OR encode(sha256(convert_to(decided.book_evidence_json, 'UTF8')), 'hex')
                  IS DISTINCT FROM decided.book_evidence_hash
              OR decided.market_evidence_json::jsonb->>'ticker' IS DISTINCT FROM decided.ticker
              OR decided.book_evidence_json::jsonb->>'ticker' IS DISTINCT FROM decided.ticker THEN
              RAISE EXCEPTION 'Kalshi paper SELL quote evidence is missing or hash-invalid';
              END IF;
              book_evidence := decided.book_evidence_json::jsonb;
              fill_evidence := NEW.evidence_json::jsonb;
              SELECT count(*) INTO fill_evidence_key_count FROM jsonb_object_keys(fill_evidence);
              IF jsonb_typeof(fill_evidence) IS DISTINCT FROM 'object'
              OR fill_evidence_key_count<>9
              OR fill_evidence->>'formula_version' IS DISTINCT FROM decided.fill_formula_version
              OR (fill_evidence->>'quantity')::numeric IS DISTINCT FROM NEW.quantity
              OR (fill_evidence->>'price')::numeric IS DISTINCT FROM NEW.price
              OR (fill_evidence->>'notional')::numeric IS DISTINCT FROM NEW.notional
              OR (fill_evidence->>'fee')::numeric IS DISTINCT FROM NEW.fee
              OR (fill_evidence->>'source_bid_price')::numeric IS DISTINCT FROM NEW.source_bid_price
              OR fill_evidence->>'source_side' IS DISTINCT FROM NEW.source_side
              OR fill_evidence->>'book_evidence_hash' IS DISTINCT FROM decided.book_evidence_hash
              OR fill_evidence->>'position_id' IS DISTINCT FROM decided.position_id THEN
              RAISE EXCEPTION 'Kalshi paper SELL fill JSON contradicts immutable fill evidence';
              END IF;
              SELECT count(*), max((level->>1)::numeric)
              INTO source_level_count, source_level_quantity
              FROM jsonb_array_elements(CASE WHEN decided.outcome='yes' THEN book_evidence->'yes_dollars'
                                           ELSE book_evidence->'no_dollars' END) level
              WHERE jsonb_typeof(level)='array' AND jsonb_array_length(level)=2
              AND (level->>0)::numeric=NEW.price;
              SELECT COALESCE(sum(f.quantity),0) INTO prior_level_quantity
              FROM kalshi_paper_fills f WHERE f.account_id=NEW.account_id
              AND f.decision_id=NEW.decision_id AND f.price=NEW.price;
              IF source_level_count<>1 OR prior_level_quantity+NEW.quantity>source_level_quantity THEN
              RAISE EXCEPTION 'Kalshi paper SELL fill exceeds immutable source book depth';
              END IF;
              RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_fills_validate_sell_evidence ON kalshi_paper_fills")
    op.execute(
        "CREATE TRIGGER trg_kalshi_paper_fills_validate_sell_evidence "
        "BEFORE INSERT ON kalshi_paper_fills FOR EACH ROW "
        "EXECUTE FUNCTION validate_kalshi_paper_sell_fill()"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_kalshi_paper_positions()
        RETURNS trigger AS $$
        DECLARE target_account_id text; broken_entries bigint; broken_positions bigint; broken_exits bigint;
        BEGIN
            target_account_id := NEW.account_id;
            SELECT count(*) INTO broken_entries FROM kalshi_paper_decisions d
             WHERE d.account_id=target_account_id AND d.action='execute' AND d.order_side='buy'
               AND d.filled_quantity>0 AND (SELECT count(*) FROM kalshi_paper_positions p
                 WHERE p.account_id=d.account_id AND p.entry_decision_id=d.decision_id
                   AND p.ticker=d.ticker AND p.outcome=d.outcome AND p.entry_quantity=d.filled_quantity
                   AND p.entry_notional=d.notional AND p.entry_fee=d.fee)<>1;
            IF broken_entries<>0 THEN RAISE EXCEPTION 'Kalshi paper filled entry does not have exact position evidence'; END IF;
            SELECT count(*) INTO broken_positions FROM kalshi_paper_positions p
             WHERE p.account_id=target_account_id AND NOT EXISTS (SELECT 1 FROM kalshi_paper_decisions d
               WHERE d.account_id=p.account_id AND d.decision_id=p.entry_decision_id AND d.action='execute'
                 AND d.order_side='buy' AND d.filled_quantity>0 AND d.ticker=p.ticker AND d.outcome=p.outcome
                 AND d.filled_quantity=p.entry_quantity AND d.notional=p.entry_notional AND d.fee=p.entry_fee);
            IF broken_positions<>0 THEN RAISE EXCEPTION 'Kalshi paper position contradicts immutable entry evidence'; END IF;
            SELECT count(*) INTO broken_exits FROM kalshi_paper_positions p LEFT JOIN LATERAL (
              SELECT COALESCE(sum(d.filled_quantity),0) sold_quantity,
                     COALESCE(sum(d.position_cost_basis),0) allocated_cost FROM kalshi_paper_decisions d
               WHERE d.account_id=p.account_id AND d.position_id=p.position_id
                 AND d.action='execute' AND d.order_side='sell') exits ON true
             WHERE p.account_id=target_account_id AND (exits.sold_quantity>p.entry_quantity
               OR exits.allocated_cost IS DISTINCT FROM (CASE WHEN exits.sold_quantity=p.entry_quantity
                    THEN p.entry_notional+p.entry_fee ELSE trunc((p.entry_notional+p.entry_fee)
                    *exits.sold_quantity/p.entry_quantity,18) END)
               OR EXISTS (SELECT 1 FROM kalshi_paper_decisions d WHERE d.account_id=p.account_id
                    AND d.position_id=p.position_id AND d.action='execute' AND d.order_side='sell'
                    AND (d.ticker IS DISTINCT FROM p.ticker OR d.outcome IS DISTINCT FROM p.outcome))
               OR EXISTS (SELECT 1 FROM (SELECT d.position_cost_basis, d.filled_quantity, d.requested_quantity,
                    COALESCE(sum(d.filled_quantity) OVER (ORDER BY d.account_sequence
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),0) prior_sold
                    FROM kalshi_paper_decisions d WHERE d.account_id=p.account_id
                    AND d.position_id=p.position_id AND d.action='execute' AND d.order_side='sell') exit_row
                    WHERE exit_row.filled_quantity > p.entry_quantity - exit_row.prior_sold
                    OR exit_row.position_cost_basis IS DISTINCT FROM
                    ((CASE WHEN exit_row.prior_sold+exit_row.filled_quantity=p.entry_quantity
                    THEN p.entry_notional+p.entry_fee ELSE trunc((p.entry_notional+p.entry_fee)
                    *(exit_row.prior_sold+exit_row.filled_quantity)/p.entry_quantity,18) END)
                    -(CASE WHEN exit_row.prior_sold=p.entry_quantity THEN p.entry_notional+p.entry_fee
                    ELSE trunc((p.entry_notional+p.entry_fee)*exit_row.prior_sold/p.entry_quantity,18) END))));
            IF broken_exits<>0 THEN RAISE EXCEPTION 'Kalshi paper exits contradict position quantity or cost basis'; END IF;
            IF EXISTS (SELECT 1 FROM kalshi_paper_decisions d WHERE d.account_id=target_account_id
              AND d.order_side='sell' AND NOT EXISTS (SELECT 1 FROM kalshi_paper_positions p
                WHERE p.account_id=d.account_id AND p.position_id=d.position_id)) THEN
              RAISE EXCEPTION 'Kalshi paper exit position does not exist'; END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )

    for table in ("kalshi_paper_positions",):
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
    op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_positions_validate ON kalshi_paper_positions")
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_kalshi_paper_positions_validate AFTER INSERT ON kalshi_paper_positions "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_kalshi_paper_positions()"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_decisions_validate_positions ON kalshi_paper_decisions")
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_kalshi_paper_decisions_validate_positions AFTER INSERT ON kalshi_paper_decisions "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_kalshi_paper_positions()"
    )


def downgrade() -> None:
    # Financial evidence and its validation are forward-only by policy.
    return
