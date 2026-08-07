"""Bind paper test-run requests and canonical quote evidence.

Revision ID: 202608070002
Revises: 202608070001
Create Date: 2026-08-07
"""

from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from alembic import op

from alembic_helpers import safe_add_column

revision = "202608070002"
down_revision = "202608070001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    safe_add_column(
        "kalshi_paper_test_runs", sa.Column("request_json", sa.Text(), nullable=True)
    )
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT run_id,request_hash,account_id,opportunity_id,opportunity_revision,quantity,"
        "entry_limit_price,take_profit_price,stop_loss_price,stop_loss_minimum_price "
        "FROM kalshi_paper_test_runs"
    )).mappings()
    for row in rows:
        canonical = json.dumps(
            {
                "account_id": row["account_id"],
                "entry_limit_price": format(row["entry_limit_price"], ".6f"),
                "opportunity_id": row["opportunity_id"],
                "opportunity_revision": row["opportunity_revision"],
                "quantity": format(row["quantity"], ".2f"),
                "run_id": row["run_id"],
                "stop_loss_minimum_price": format(row["stop_loss_minimum_price"], ".6f"),
                "stop_loss_price": format(row["stop_loss_price"], ".6f"),
                "take_profit_price": format(row["take_profit_price"], ".6f"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != row["request_hash"]:
            raise RuntimeError("existing Kalshi paper test request hash cannot be reconstructed")
        bind.execute(
            sa.text("UPDATE kalshi_paper_test_runs SET request_json=:request_json WHERE run_id=:run_id"),
            {"run_id": row["run_id"], "request_json": canonical},
        )
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM kalshi_paper_test_runs r
                 WHERE r.request_json IS NULL
                    OR encode(sha256(convert_to(r.request_json,'UTF8')),'hex') IS DISTINCT FROM r.request_hash
                    OR (SELECT count(*) FROM kalshi_paper_test_events e
                         WHERE e.run_id=r.run_id AND e.sequence=1 AND e.account_id=r.account_id
                           AND e.event_type='started' AND e.position_id IS NULL AND e.best_bid IS NULL
                           AND e.trigger_price IS NULL AND e.exit_decision_id IS NULL
                           AND e.market_observed_at IS NULL AND e.book_observed_at IS NULL
                           AND e.quote_evidence_hash IS NULL AND e.quote_evidence_json IS NULL
                           AND e.remaining_quantity IS NULL AND e.realized_pnl IS NULL
                           AND e.reason='operator_started' AND e.created_at=r.created_at) <> 1
            ) THEN
                RAISE EXCEPTION 'existing Kalshi paper test evidence cannot be canonically reconstructed';
            END IF;
        END $$
        """
    )
    op.alter_column("kalshi_paper_test_runs", "request_json", nullable=False)
    op.execute(
        "ALTER TABLE kalshi_paper_test_runs "
        "DROP CONSTRAINT IF EXISTS ck_kalshi_paper_test_runs_thresholds"
    )
    op.execute(
        "ALTER TABLE kalshi_paper_test_runs ADD CONSTRAINT ck_kalshi_paper_test_runs_thresholds "
        "CHECK (stop_loss_minimum_price <= stop_loss_price "
        "AND stop_loss_price < entry_limit_price "
        "AND entry_limit_price < take_profit_price)"
    )

    op.execute("""
        CREATE OR REPLACE FUNCTION protect_kalshi_paper_test_run_request()
        RETURNS trigger AS $$ BEGIN
            IF NEW.run_id IS DISTINCT FROM OLD.run_id OR NEW.request_hash IS DISTINCT FROM OLD.request_hash
               OR NEW.request_json IS DISTINCT FROM OLD.request_json
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
    op.execute("""
        CREATE OR REPLACE FUNCTION validate_kalshi_paper_test_run_insert()
        RETURNS trigger AS $$
        DECLARE canonical_request text; matching_started bigint; total_events bigint;
        BEGIN
            SELECT
                '{"account_id":' || to_json(NEW.account_id)::text ||
                ',"entry_limit_price":' || to_json('0.' || lpad(trunc(NEW.entry_limit_price*1000000)::text,6,'0'))::text ||
                ',"opportunity_id":' || to_json(NEW.opportunity_id)::text ||
                ',"opportunity_revision":' || to_json(NEW.opportunity_revision)::text ||
                ',"quantity":' || to_json(trunc(NEW.quantity)::text || '.' ||
                    lpad(trunc((NEW.quantity-trunc(NEW.quantity))*100)::text,2,'0'))::text ||
                ',"run_id":' || to_json(NEW.run_id)::text ||
                ',"stop_loss_minimum_price":' || to_json('0.' || lpad(trunc(NEW.stop_loss_minimum_price*1000000)::text,6,'0'))::text ||
                ',"stop_loss_price":' || to_json('0.' || lpad(trunc(NEW.stop_loss_price*1000000)::text,6,'0'))::text ||
                ',"take_profit_price":' || to_json('0.' || lpad(trunc(NEW.take_profit_price*1000000)::text,6,'0'))::text || '}'
              INTO canonical_request;
            IF NEW.request_json IS DISTINCT FROM canonical_request
               OR encode(sha256(convert_to(NEW.request_json,'UTF8')),'hex') IS DISTINCT FROM NEW.request_hash THEN
                RAISE EXCEPTION 'Kalshi paper test run canonical request identity is invalid';
            END IF;
            IF NEW.status IS DISTINCT FROM 'starting' OR NEW.position_id IS NOT NULL
               OR NEW.next_event_sequence IS DISTINCT FROM 2
               OR NEW.last_reason IS DISTINCT FROM 'operator_started' OR NEW.last_error IS NOT NULL THEN
                RAISE EXCEPTION 'Kalshi paper test run initial projection is invalid';
            END IF;
            SELECT count(*), count(*) FILTER (WHERE sequence=1 AND account_id=NEW.account_id
                AND event_type='started' AND position_id IS NULL AND best_bid IS NULL
                AND trigger_price IS NULL AND exit_decision_id IS NULL AND market_observed_at IS NULL
                AND book_observed_at IS NULL AND quote_evidence_hash IS NULL AND quote_evidence_json IS NULL
                AND remaining_quantity IS NULL AND realized_pnl IS NULL
                AND reason='operator_started' AND created_at=NEW.created_at)
              INTO total_events, matching_started FROM kalshi_paper_test_events WHERE run_id=NEW.run_id;
            IF total_events IS DISTINCT FROM 1 OR matching_started IS DISTINCT FROM 1 THEN
                RAISE EXCEPTION 'Kalshi paper test run requires exactly one canonical started event';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_test_runs_validate_insert ON kalshi_paper_test_runs")
    op.execute("""
        CREATE CONSTRAINT TRIGGER trg_kalshi_paper_test_runs_validate_insert AFTER INSERT
        ON kalshi_paper_test_runs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION validate_kalshi_paper_test_run_insert()
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION reject_kalshi_paper_test_run_erasure()
        RETURNS trigger AS $$ BEGIN
            RAISE EXCEPTION 'Kalshi paper test run projection cannot be erased';
        END; $$ LANGUAGE plpgsql
    """)
    for operation, suffix, scope in (
        ("DELETE", "delete", "FOR EACH ROW"),
        ("TRUNCATE", "truncate", "FOR EACH STATEMENT"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_kalshi_paper_test_runs_reject_{suffix} ON kalshi_paper_test_runs")
        op.execute(
            f"CREATE TRIGGER trg_kalshi_paper_test_runs_reject_{suffix} BEFORE {operation} "
            f"ON kalshi_paper_test_runs {scope} EXECUTE FUNCTION reject_kalshi_paper_test_run_erasure()"
        )
    op.execute("""
        CREATE OR REPLACE FUNCTION validate_kalshi_paper_test_entry_transition()
        RETURNS trigger AS $$ BEGIN
            IF OLD.status='starting' AND NEW.status IN ('monitoring','entry_unfilled') THEN
                IF NOT EXISTS (
                    SELECT 1 FROM kalshi_paper_intents i
                     WHERE i.account_id=NEW.account_id AND i.decision_id=NEW.entry_decision_id
                       AND i.action='execute' AND COALESCE(i.order_side,'buy')='buy'
                       AND COALESCE(i.time_in_force,'immediate_or_cancel')='immediate_or_cancel'
                       AND i.position_id IS NULL AND i.opportunity_id=NEW.opportunity_id
                       AND i.opportunity_revision=NEW.opportunity_revision AND i.ticker=NEW.ticker
                       AND i.outcome=NEW.outcome AND i.requested_quantity=NEW.quantity
                       AND i.limit_price=NEW.entry_limit_price
                ) OR NOT EXISTS (
                    SELECT 1 FROM kalshi_paper_decisions d
                     WHERE d.account_id=NEW.account_id AND d.decision_id=NEW.entry_decision_id
                       AND d.action='execute' AND d.order_side='buy'
                       AND d.time_in_force='immediate_or_cancel' AND d.position_id IS NULL
                       AND d.opportunity_id=NEW.opportunity_id AND d.opportunity_revision=NEW.opportunity_revision
                       AND d.ticker=NEW.ticker AND d.outcome=NEW.outcome
                       AND d.requested_quantity=NEW.quantity AND d.limit_price=NEW.entry_limit_price
                       AND ((NEW.status='monitoring' AND d.filled_quantity>0)
                         OR (NEW.status='entry_unfilled' AND d.filled_quantity=0))
                ) THEN
                    RAISE EXCEPTION 'Kalshi paper test entry transition contradicts intent or decision causality';
                END IF;
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_test_runs_validate_entry_transition ON kalshi_paper_test_runs")
    op.execute("""
        CREATE CONSTRAINT TRIGGER trg_kalshi_paper_test_runs_validate_entry_transition AFTER UPDATE
        ON kalshi_paper_test_runs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION validate_kalshi_paper_test_entry_transition()
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION validate_kalshi_paper_test_event_highs()
        RETURNS trigger AS $$
        DECLARE controlled kalshi_paper_test_runs%ROWTYPE; evidence jsonb; observed_best numeric;
        BEGIN
            SELECT * INTO controlled FROM kalshi_paper_test_runs WHERE run_id=NEW.run_id;
            IF NEW.event_type='started' AND (
                NEW.sequence IS DISTINCT FROM 1 OR NEW.account_id IS DISTINCT FROM controlled.account_id
                OR NEW.position_id IS NOT NULL OR NEW.best_bid IS NOT NULL OR NEW.trigger_price IS NOT NULL
                OR NEW.exit_decision_id IS NOT NULL OR NEW.market_observed_at IS NOT NULL
                OR NEW.book_observed_at IS NOT NULL OR NEW.quote_evidence_hash IS NOT NULL
                OR NEW.quote_evidence_json IS NOT NULL OR NEW.remaining_quantity IS NOT NULL
                OR NEW.realized_pnl IS NOT NULL OR NEW.reason IS DISTINCT FROM 'operator_started'
                OR NEW.created_at IS DISTINCT FROM controlled.created_at
            ) THEN RAISE EXCEPTION 'Kalshi paper test started event shape is invalid'; END IF;
            IF NEW.quote_evidence_json IS NOT NULL THEN
                evidence := NEW.quote_evidence_json::jsonb;
                IF jsonb_typeof(evidence) IS DISTINCT FROM 'object'
                   OR (SELECT count(*) FROM jsonb_object_keys(evidence)) IS DISTINCT FROM 5
                   OR EXISTS (SELECT 1 FROM jsonb_object_keys(evidence) key
                              WHERE key NOT IN ('no_dollars','observed_at','source_origin','ticker','yes_dollars'))
                   OR jsonb_typeof(evidence->'no_dollars') IS DISTINCT FROM 'array'
                   OR jsonb_typeof(evidence->'yes_dollars') IS DISTINCT FROM 'array'
                   OR jsonb_typeof(evidence->'observed_at') IS DISTINCT FROM 'string'
                   OR jsonb_typeof(evidence->'source_origin') IS DISTINCT FROM 'string'
                   OR jsonb_typeof(evidence->'ticker') IS DISTINCT FROM 'string'
                   OR NEW.quote_evidence_json IS DISTINCT FROM
                      '{"no_dollars":' || regexp_replace((evidence->'no_dollars')::text,'[[:space:]]','','g') ||
                      ',"observed_at":' || to_json(evidence->>'observed_at')::text ||
                      ',"source_origin":' || to_json(evidence->>'source_origin')::text ||
                      ',"ticker":' || to_json(evidence->>'ticker')::text ||
                      ',"yes_dollars":' || regexp_replace((evidence->'yes_dollars')::text,'[[:space:]]','','g') || '}'
                   OR evidence->>'ticker' IS DISTINCT FROM controlled.ticker
                   OR evidence->>'source_origin' IS DISTINCT FROM 'https://external-api.kalshi.com'
                   OR evidence->>'observed_at' IS DISTINCT FROM (
                      to_char(NEW.book_observed_at,'YYYY-MM-DD"T"HH24:MI:SS') ||
                      CASE WHEN to_char(NEW.book_observed_at,'US')='000000' THEN ''
                           ELSE '.' || to_char(NEW.book_observed_at,'US') END || '+00:00')
                   OR EXISTS (
                       SELECT 1 FROM jsonb_array_elements((evidence->'no_dollars') || (evidence->'yes_dollars')) level
                        WHERE jsonb_typeof(level) IS DISTINCT FROM 'array' OR jsonb_array_length(level)<>2
                           OR jsonb_typeof(level->0) IS DISTINCT FROM 'string'
                           OR jsonb_typeof(level->1) IS DISTINCT FROM 'string'
                           OR level->>0 !~ '^0[.][0-9]{6}$' OR (level->>0)::numeric<=0 OR (level->>0)::numeric>=1
                           OR level->>1 !~ '^(0|[1-9][0-9]*)[.][0-9]{2}$' OR (level->>1)::numeric<=0
                   ) THEN RAISE EXCEPTION 'Kalshi paper test event quote evidence fact domain is invalid'; END IF;
                SELECT max((level->>0)::numeric) INTO observed_best
                  FROM jsonb_array_elements(CASE WHEN controlled.outcome='yes'
                    THEN evidence->'yes_dollars' ELSE evidence->'no_dollars' END) level;
                IF NEW.best_bid IS DISTINCT FROM observed_best THEN
                    RAISE EXCEPTION 'Kalshi paper test event controlled bid contradicts quote evidence';
                END IF;
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_kalshi_paper_test_events_validate_highs ON kalshi_paper_test_events")
    op.execute("""
        CREATE CONSTRAINT TRIGGER trg_kalshi_paper_test_events_validate_highs AFTER INSERT
        ON kalshi_paper_test_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION validate_kalshi_paper_test_event_highs()
    """)


def downgrade() -> None:
    return
