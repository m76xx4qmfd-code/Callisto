"""Add exact paper-only test-trade run and event evidence.

Revision ID: 202608070001
Revises: 202608060002
Create Date: 2026-08-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from alembic_helpers import safe_create_index, safe_create_table

revision = "202608070001"
down_revision = "202608060002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    safe_create_table(
        "kalshi_paper_test_runs",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("opportunity_id", sa.String(), nullable=False),
        sa.Column("opportunity_revision", sa.String(64), nullable=False),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("quantity", sa.Numeric(), nullable=False),
        sa.Column("entry_limit_price", sa.Numeric(), nullable=False),
        sa.Column("take_profit_price", sa.Numeric(), nullable=False),
        sa.Column("stop_loss_price", sa.Numeric(), nullable=False),
        sa.Column("stop_loss_minimum_price", sa.Numeric(), nullable=False),
        sa.Column("entry_decision_id", sa.String(), nullable=False),
        sa.Column("position_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("next_event_sequence", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("last_reason", sa.String(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", name="pk_kalshi_paper_test_runs"),
        sa.ForeignKeyConstraint(
            ["account_id"], ["kalshi_paper_accounts.id"], ondelete="RESTRICT",
            name="fk_kalshi_paper_test_runs_account_id",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "position_id"],
            ["kalshi_paper_positions.account_id", "kalshi_paper_positions.position_id"],
            ondelete="RESTRICT",
            name="fk_kalshi_paper_test_runs_position",
        ),
        sa.UniqueConstraint("account_id", "entry_decision_id", name="uq_kalshi_paper_test_runs_entry"),
        sa.CheckConstraint("length(btrim(run_id)) > 0", name="ck_kalshi_paper_test_runs_id"),
        sa.CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="ck_kalshi_paper_test_runs_hash"),
        sa.CheckConstraint("opportunity_revision ~ '^[0-9a-f]{64}$'", name="ck_kalshi_paper_test_runs_revision"),
        sa.CheckConstraint("entry_decision_id = 'paper-test-entry:' || run_id", name="ck_kalshi_paper_test_runs_entry_id"),
        sa.CheckConstraint("outcome IN ('yes', 'no')", name="ck_kalshi_paper_test_runs_outcome"),
        sa.CheckConstraint(
            "quantity <> 'NaN'::numeric AND quantity < 'Infinity'::numeric "
            "AND quantity > 0 AND scale(quantity) <= 2",
            name="ck_kalshi_paper_test_runs_quantity",
        ),
        sa.CheckConstraint(
            "entry_limit_price <> 'NaN'::numeric AND entry_limit_price > 0 AND entry_limit_price < 1 "
            "AND scale(entry_limit_price) <= 6 AND take_profit_price <> 'NaN'::numeric "
            "AND take_profit_price > 0 AND take_profit_price < 1 AND scale(take_profit_price) <= 6 "
            "AND stop_loss_price <> 'NaN'::numeric AND stop_loss_price > 0 AND stop_loss_price < 1 "
            "AND scale(stop_loss_price) <= 6 AND stop_loss_minimum_price <> 'NaN'::numeric "
            "AND stop_loss_minimum_price > 0 AND stop_loss_minimum_price < 1 "
            "AND scale(stop_loss_minimum_price) <= 6",
            name="ck_kalshi_paper_test_runs_prices",
        ),
        sa.CheckConstraint(
            "stop_loss_minimum_price <= stop_loss_price AND stop_loss_price < take_profit_price",
            name="ck_kalshi_paper_test_runs_thresholds",
        ),
        sa.CheckConstraint(
            "status IN ('starting','monitoring','paused','entry_unfilled','stopped','completed','blocked')",
            name="ck_kalshi_paper_test_runs_status",
        ),
        sa.CheckConstraint(
            "(status IN ('monitoring','paused','stopped','completed') AND position_id IS NOT NULL) OR "
            "(status IN ('starting','entry_unfilled','blocked'))",
            name="ck_kalshi_paper_test_runs_position_status",
        ),
        sa.CheckConstraint("next_event_sequence > 0", name="ck_kalshi_paper_test_runs_sequence"),
    )
    safe_create_index(
        "idx_kalshi_paper_test_runs_account_created",
        "kalshi_paper_test_runs",
        ["account_id", "created_at"],
    )
    safe_create_index(
        "idx_kalshi_paper_test_runs_status",
        "kalshi_paper_test_runs",
        ["status", "updated_at"],
    )
    safe_create_table(
        "kalshi_paper_test_events",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("position_id", sa.String(), nullable=True),
        sa.Column("best_bid", sa.Numeric(), nullable=True),
        sa.Column("trigger_price", sa.Numeric(), nullable=True),
        sa.Column("exit_decision_id", sa.String(), nullable=True),
        sa.Column("market_observed_at", sa.DateTime(), nullable=True),
        sa.Column("book_observed_at", sa.DateTime(), nullable=True),
        sa.Column("quote_evidence_hash", sa.String(64), nullable=True),
        sa.Column("quote_evidence_json", sa.Text(), nullable=True),
        sa.Column("remaining_quantity", sa.Numeric(), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "sequence", name="pk_kalshi_paper_test_events"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["kalshi_paper_test_runs.run_id"], ondelete="RESTRICT",
            name="fk_kalshi_paper_test_events_run",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "position_id"],
            ["kalshi_paper_positions.account_id", "kalshi_paper_positions.position_id"],
            ondelete="RESTRICT",
            name="fk_kalshi_paper_test_events_position",
        ),
        sa.UniqueConstraint(
            "run_id", "event_type", "exit_decision_id",
            name="uq_kalshi_paper_test_events_semantic_exit",
        ),
        sa.CheckConstraint("sequence > 0", name="ck_kalshi_paper_test_events_sequence"),
        sa.CheckConstraint(
            "event_type IN ('started','entry_filled','entry_unfilled','no_bid','hold',"
            "'take_profit_triggered','stop_loss_triggered','exit_filled','exit_partial',"
            "'exit_no_fill','paused','resumed','stopped','completed','blocked')",
            name="ck_kalshi_paper_test_events_type",
        ),
        sa.CheckConstraint(
            "best_bid IS NULL OR (best_bid <> 'NaN'::numeric AND best_bid > 0 AND best_bid < 1 "
            "AND scale(best_bid) <= 6)", name="ck_kalshi_paper_test_events_bid",
        ),
        sa.CheckConstraint(
            "trigger_price IS NULL OR (trigger_price <> 'NaN'::numeric AND trigger_price > 0 "
            "AND trigger_price < 1 AND scale(trigger_price) <= 6)",
            name="ck_kalshi_paper_test_events_trigger",
        ),
        sa.CheckConstraint(
            "remaining_quantity IS NULL OR (remaining_quantity <> 'NaN'::numeric "
            "AND remaining_quantity < 'Infinity'::numeric AND remaining_quantity >= 0 "
            "AND scale(remaining_quantity) <= 2)", name="ck_kalshi_paper_test_events_remaining",
        ),
        sa.CheckConstraint(
            "realized_pnl IS NULL OR (realized_pnl <> 'NaN'::numeric "
            "AND realized_pnl < 'Infinity'::numeric AND realized_pnl > '-Infinity'::numeric "
            "AND scale(realized_pnl) <= 18)", name="ck_kalshi_paper_test_events_pnl",
        ),
        sa.CheckConstraint(
            "quote_evidence_hash IS NULL OR quote_evidence_hash ~ '^[0-9a-f]{64}$'",
            name="ck_kalshi_paper_test_events_hash",
        ),
    )
    safe_create_index(
        "idx_kalshi_paper_test_events_exit",
        "kalshi_paper_test_events",
        ["account_id", "exit_decision_id"],
    )

    op.execute("""
        CREATE OR REPLACE FUNCTION reject_kalshi_paper_evidence_mutation()
        RETURNS trigger AS $$ BEGIN
            RAISE EXCEPTION 'Kalshi paper financial evidence is immutable';
        END; $$ LANGUAGE plpgsql
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_test_events_immutable ON kalshi_paper_test_events")
    op.execute(
        "CREATE TRIGGER trg_kalshi_paper_test_events_immutable BEFORE UPDATE OR DELETE "
        "ON kalshi_paper_test_events FOR EACH ROW EXECUTE FUNCTION reject_kalshi_paper_evidence_mutation()"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_test_events_truncate_immutable ON kalshi_paper_test_events")
    op.execute(
        "CREATE TRIGGER trg_kalshi_paper_test_events_truncate_immutable BEFORE TRUNCATE "
        "ON kalshi_paper_test_events FOR EACH STATEMENT EXECUTE FUNCTION reject_kalshi_paper_evidence_mutation()"
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION protect_kalshi_paper_test_run_request()
        RETURNS trigger AS $$ BEGIN
            IF NEW.run_id IS DISTINCT FROM OLD.run_id OR NEW.request_hash IS DISTINCT FROM OLD.request_hash
               OR NEW.account_id IS DISTINCT FROM OLD.account_id OR NEW.opportunity_id IS DISTINCT FROM OLD.opportunity_id
               OR NEW.opportunity_revision IS DISTINCT FROM OLD.opportunity_revision OR NEW.ticker IS DISTINCT FROM OLD.ticker
               OR NEW.outcome IS DISTINCT FROM OLD.outcome OR NEW.quantity IS DISTINCT FROM OLD.quantity
               OR NEW.entry_limit_price IS DISTINCT FROM OLD.entry_limit_price
               OR NEW.take_profit_price IS DISTINCT FROM OLD.take_profit_price
               OR NEW.stop_loss_price IS DISTINCT FROM OLD.stop_loss_price
               OR NEW.stop_loss_minimum_price IS DISTINCT FROM OLD.stop_loss_minimum_price
               OR NEW.entry_decision_id IS DISTINCT FROM OLD.entry_decision_id
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'Kalshi paper test run immutable request facts cannot change';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_test_runs_protect_request ON kalshi_paper_test_runs")
    op.execute(
        "CREATE TRIGGER trg_kalshi_paper_test_runs_protect_request BEFORE UPDATE ON kalshi_paper_test_runs "
        "FOR EACH ROW EXECUTE FUNCTION protect_kalshi_paper_test_run_request()"
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION validate_kalshi_paper_test_run_projection()
        RETURNS trigger AS $$
        DECLARE appended_count bigint; latest_event_type text;
        BEGIN
            IF NEW.position_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM kalshi_paper_positions p
                 WHERE p.account_id=NEW.account_id AND p.position_id=NEW.position_id
                   AND p.entry_decision_id=NEW.entry_decision_id
                   AND p.ticker=NEW.ticker AND p.outcome=NEW.outcome
            ) THEN
                RAISE EXCEPTION 'Kalshi paper test run position causality is invalid';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
                (OLD.status='starting' AND NEW.status IN ('monitoring','entry_unfilled','blocked')) OR
                (OLD.status='monitoring' AND NEW.status IN ('paused','stopped','completed','blocked')) OR
                (OLD.status='paused' AND NEW.status IN ('monitoring','stopped','blocked'))
            ) THEN
                RAISE EXCEPTION 'Kalshi paper test run status transition is invalid';
            END IF;
            IF NEW.next_event_sequence < OLD.next_event_sequence THEN
                RAISE EXCEPTION 'Kalshi paper test run event sequence cannot regress';
            END IF;
            IF NEW.next_event_sequence = OLD.next_event_sequence THEN
                IF NEW.position_id IS DISTINCT FROM OLD.position_id OR NEW.status IS DISTINCT FROM OLD.status
                   OR NEW.last_reason IS DISTINCT FROM OLD.last_reason
                   OR NEW.last_error IS DISTINCT FROM OLD.last_error THEN
                    RAISE EXCEPTION 'Kalshi paper test run projection changed without lifecycle event';
                END IF;
            ELSE
                SELECT count(*), max(event_type) FILTER (WHERE sequence=NEW.next_event_sequence-1)
                  INTO appended_count, latest_event_type
                  FROM kalshi_paper_test_events
                 WHERE run_id=NEW.run_id
                   AND sequence>=OLD.next_event_sequence
                   AND sequence<NEW.next_event_sequence;
                IF appended_count IS DISTINCT FROM NEW.next_event_sequence-OLD.next_event_sequence
                   OR latest_event_type IS NULL THEN
                    RAISE EXCEPTION 'Kalshi paper test run projection lacks contiguous lifecycle events';
                END IF;
                IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
                    (NEW.status='monitoring' AND latest_event_type IN ('entry_filled','resumed')) OR
                    (NEW.status='entry_unfilled' AND latest_event_type='entry_unfilled') OR
                    (NEW.status='paused' AND latest_event_type='paused') OR
                    (NEW.status='stopped' AND latest_event_type='stopped') OR
                    (NEW.status='completed' AND latest_event_type='completed') OR
                    (NEW.status='blocked' AND latest_event_type='blocked')
                ) THEN
                    RAISE EXCEPTION 'Kalshi paper test run status lacks matching lifecycle event';
                END IF;
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_test_runs_validate_projection ON kalshi_paper_test_runs")
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_kalshi_paper_test_runs_validate_projection AFTER UPDATE "
        "ON kalshi_paper_test_runs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION validate_kalshi_paper_test_run_projection()"
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION validate_kalshi_paper_test_event()
        RETURNS trigger AS $$
        DECLARE controlled kalshi_paper_test_runs%ROWTYPE; decided kalshi_paper_decisions%ROWTYPE;
                observed_best numeric; authoritative_remaining numeric; authoritative_pnl numeric;
        BEGIN
            SELECT * INTO controlled FROM kalshi_paper_test_runs WHERE run_id=NEW.run_id;
            IF NOT FOUND OR NEW.account_id IS DISTINCT FROM controlled.account_id
               OR NEW.sequence >= controlled.next_event_sequence THEN
                RAISE EXCEPTION 'Kalshi paper test event contradicts run sequence or account';
            END IF;
            IF NEW.position_id IS NOT NULL AND NEW.position_id IS DISTINCT FROM controlled.position_id THEN
                RAISE EXCEPTION 'Kalshi paper test event contradicts run position';
            END IF;
            IF (NEW.quote_evidence_json IS NULL) <> (NEW.quote_evidence_hash IS NULL)
               OR (NEW.quote_evidence_json IS NOT NULL AND encode(sha256(convert_to(NEW.quote_evidence_json,'UTF8')),'hex')
                   IS DISTINCT FROM NEW.quote_evidence_hash) THEN
                RAISE EXCEPTION 'Kalshi paper test event quote hash is invalid';
            END IF;
            IF NEW.quote_evidence_json IS NOT NULL AND (
               NEW.quote_evidence_json ~ '[[:space:]]'
               OR NEW.quote_evidence_json !~ '^[{]"no_dollars":.*,"observed_at":"[^"]+","source_origin":"[^"]+","ticker":"[^"]+","yes_dollars":.*[}]$'
            ) THEN
                RAISE EXCEPTION 'Kalshi paper test event quote evidence is not canonical';
            END IF;
            IF NEW.quote_evidence_json IS NOT NULL
               AND NEW.quote_evidence_json::jsonb->>'ticker' IS DISTINCT FROM controlled.ticker THEN
                RAISE EXCEPTION 'Kalshi paper test event quote ticker is invalid';
            END IF;
            IF NEW.event_type IN ('no_bid','hold','take_profit_triggered','stop_loss_triggered') THEN
                IF NEW.quote_evidence_json IS NULL OR NEW.market_observed_at IS NULL
                   OR NEW.book_observed_at IS NULL THEN
                    RAISE EXCEPTION 'Kalshi paper test observation evidence is incomplete';
                END IF;
                SELECT max((level->>0)::numeric) INTO observed_best
                  FROM jsonb_array_elements(CASE WHEN controlled.outcome='yes'
                    THEN NEW.quote_evidence_json::jsonb->'yes_dollars'
                    ELSE NEW.quote_evidence_json::jsonb->'no_dollars' END) level;
                IF NEW.best_bid IS DISTINCT FROM observed_best THEN
                    RAISE EXCEPTION 'Kalshi paper test event best bid contradicts quote evidence';
                END IF;
                IF NEW.event_type='no_bid' AND NEW.best_bid IS NOT NULL THEN
                    RAISE EXCEPTION 'Kalshi paper test no-bid event contradicts quote evidence';
                END IF;
                IF NEW.event_type='hold' AND
                   (NEW.best_bid IS NULL OR NEW.best_bid<=controlled.stop_loss_price
                    OR NEW.best_bid>=controlled.take_profit_price) THEN
                    RAISE EXCEPTION 'Kalshi paper test hold arithmetic is invalid';
                END IF;
            END IF;
            IF NEW.event_type IN ('take_profit_triggered','stop_loss_triggered') THEN
                IF NEW.exit_decision_id IS NULL OR NEW.exit_decision_id IS DISTINCT FROM
                   'paper-test-exit:'||NEW.run_id||':'||NEW.sequence::text
                   OR NEW.best_bid IS NULL OR NEW.trigger_price IS NULL THEN
                    RAISE EXCEPTION 'Kalshi paper test trigger event shape is invalid';
                END IF;
                IF NEW.event_type='take_profit_triggered' AND
                   (NEW.trigger_price IS DISTINCT FROM controlled.take_profit_price OR NEW.best_bid<NEW.trigger_price) THEN
                    RAISE EXCEPTION 'Kalshi paper test take-profit trigger arithmetic is invalid';
                END IF;
                IF NEW.event_type='stop_loss_triggered' AND
                   (NEW.trigger_price IS DISTINCT FROM controlled.stop_loss_price OR NEW.best_bid>NEW.trigger_price) THEN
                    RAISE EXCEPTION 'Kalshi paper test stop-loss trigger arithmetic is invalid';
                END IF;
            ELSIF NEW.event_type IN ('no_bid','hold') AND
                  (NEW.exit_decision_id IS NOT NULL OR NEW.trigger_price IS NOT NULL) THEN
                RAISE EXCEPTION 'Kalshi paper test observation event shape is invalid';
            END IF;
            IF NEW.event_type IN ('exit_filled','exit_partial','exit_no_fill') THEN
                IF NOT EXISTS (
                    SELECT 1 FROM kalshi_paper_test_events trigger_event
                     WHERE trigger_event.run_id=NEW.run_id
                       AND trigger_event.exit_decision_id=NEW.exit_decision_id
                       AND trigger_event.event_type IN ('take_profit_triggered','stop_loss_triggered')
                ) THEN
                    RAISE EXCEPTION 'Kalshi paper test exit event has no trigger evidence';
                END IF;
                IF NEW.reason='position_changed_before_exit' THEN
                    IF NEW.event_type IS DISTINCT FROM 'exit_no_fill' THEN
                        RAISE EXCEPTION 'Kalshi paper test exceptional exit event shape is invalid';
                    END IF;
                ELSE
                    SELECT * INTO decided FROM kalshi_paper_decisions
                     WHERE account_id=NEW.account_id AND decision_id=NEW.exit_decision_id;
                    IF NOT FOUND OR decided.order_side IS DISTINCT FROM 'sell'
                       OR decided.position_id IS DISTINCT FROM controlled.position_id
                       OR decided.ticker IS DISTINCT FROM controlled.ticker
                       OR decided.outcome IS DISTINCT FROM controlled.outcome THEN
                        RAISE EXCEPTION 'Kalshi paper test exit event contradicts decision causality';
                    END IF;
                END IF;
                SELECT p.entry_quantity-COALESCE(sum(d.filled_quantity) FILTER (WHERE d.order_side='sell'),0),
                       COALESCE(sum(d.realized_pnl) FILTER (WHERE d.order_side='sell'),0)
                  INTO authoritative_remaining, authoritative_pnl
                  FROM kalshi_paper_positions p
                  LEFT JOIN kalshi_paper_decisions d
                    ON d.account_id=p.account_id AND d.position_id=p.position_id
                 WHERE p.account_id=controlled.account_id AND p.position_id=controlled.position_id
                 GROUP BY p.entry_quantity;
                IF NEW.position_id IS DISTINCT FROM controlled.position_id
                   OR NEW.remaining_quantity IS DISTINCT FROM authoritative_remaining
                   OR NEW.realized_pnl IS DISTINCT FROM authoritative_pnl THEN
                    RAISE EXCEPTION 'Kalshi paper test exit event contradicts authoritative position projection';
                END IF;
            END IF;
            IF NEW.event_type='completed' THEN
                SELECT p.entry_quantity-COALESCE(sum(d.filled_quantity) FILTER (WHERE d.order_side='sell'),0),
                       COALESCE(sum(d.realized_pnl) FILTER (WHERE d.order_side='sell'),0)
                  INTO authoritative_remaining, authoritative_pnl
                  FROM kalshi_paper_positions p
                  LEFT JOIN kalshi_paper_decisions d
                    ON d.account_id=p.account_id AND d.position_id=p.position_id
                 WHERE p.account_id=controlled.account_id AND p.position_id=controlled.position_id
                 GROUP BY p.entry_quantity;
                IF NEW.position_id IS DISTINCT FROM controlled.position_id
                   OR authoritative_remaining IS DISTINCT FROM 0
                   OR NEW.remaining_quantity IS DISTINCT FROM authoritative_remaining
                   OR NEW.realized_pnl IS DISTINCT FROM authoritative_pnl THEN
                    RAISE EXCEPTION 'Kalshi paper test completed event contradicts authoritative position';
                END IF;
            END IF;
            IF NEW.event_type='entry_filled' AND NOT EXISTS (
                SELECT 1 FROM kalshi_paper_positions p
                 WHERE p.account_id=controlled.account_id AND p.position_id=NEW.position_id
                   AND p.entry_decision_id=controlled.entry_decision_id
                   AND p.ticker=controlled.ticker AND p.outcome=controlled.outcome
            ) THEN
                RAISE EXCEPTION 'Kalshi paper test entry event contradicts position causality';
            END IF;
            IF NEW.event_type='entry_unfilled' AND
               (controlled.position_id IS NOT NULL OR NOT EXISTS (
                   SELECT 1 FROM kalshi_paper_decisions entry_decision
                    WHERE entry_decision.account_id=controlled.account_id
                      AND entry_decision.decision_id=controlled.entry_decision_id
                      AND entry_decision.order_side='buy' AND entry_decision.filled_quantity=0
               )) THEN
                RAISE EXCEPTION 'Kalshi paper test unfilled event contradicts entry decision';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_test_events_validate ON kalshi_paper_test_events")
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_kalshi_paper_test_events_validate AFTER INSERT "
        "ON kalshi_paper_test_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION validate_kalshi_paper_test_event()"
    )


def downgrade() -> None:
    # Financial evidence and its validation are forward-only by policy.
    return
