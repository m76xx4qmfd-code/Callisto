"""Alembic migration tests.

Two cases:

1.  ``test_head_migration_downgrade_upgrade_roundtrip`` — stamps a
    throwaway DB at head, downgrades one revision, re-upgrades.
    Catches a new head migration whose ``downgrade()`` raises or
    isn't symmetric with its ``upgrade()``.  Cheap and runs in seconds.

2.  ``test_alembic_replay_base_to_head_on_empty_db`` (Plan 0020) —
    runs the entire migration chain from ``base`` against a true
    empty database and asserts the final revision matches head.
    Catches a new migration that breaks fresh-DB bootstrap (e.g.
    a non-idempotent ``op.add_column`` colliding with the baseline's
    lazy ``Base.metadata.create_all``).  Slower (~5–15 s) and is
    marked ``slow`` accordingly.

Both skip when no writable Postgres is reachable.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def test_runtime_snapshot_lag_type_matches_migration() -> None:
    from sqlalchemy import Numeric

    from models.database import KalshiPortfolioRuntimeSnapshot

    column_type = KalshiPortfolioRuntimeSnapshot.__table__.c.lag_seconds.type
    assert isinstance(column_type.impl, Numeric)
    assert column_type.impl.precision == 24
    assert column_type.impl.scale == 12


def _build_alembic_config(sync_connection) -> Config:
    """Return an Alembic ``Config`` wired to the given sync connection.

    The Alembic env.py honours ``config.attributes['connection']`` and
    skips its own engine creation when present (see
    ``backend/alembic/env.py:run_migrations_online``).  That lets us
    point migrations at the throwaway database without monkey-patching
    settings or shelling out to the alembic CLI.
    """
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    cfg.attributes["connection"] = sync_connection
    return cfg


@pytest.mark.db
@pytest.mark.slow
@pytest.mark.asyncio
async def test_head_migration_downgrade_upgrade_roundtrip() -> None:
    try:
        from models.database import Base
        from models.model_registry import register_all_models
        from tests.postgres_test_db import build_postgres_session_factory
    except Exception as exc:  # pragma: no cover — defensive
        pytest.skip(f"DB harness unavailable: {exc}")

    register_all_models()

    try:
        engine, _factory = await build_postgres_session_factory(
            Base, "alembic_head_roundtrip"
        )
    except Exception as exc:
        pytest.skip(f"Postgres unreachable for alembic round-trip: {exc}")

    try:
        # Compute head + previous revision once, before opening a
        # connection — pure script-graph traversal, no DB side effects.
        script = ScriptDirectory.from_config(
            Config(str(BACKEND_ROOT / "alembic.ini"))
        )
        head_revision = script.get_current_head()
        assert head_revision, "alembic script directory has no head revision"

        head_script = script.get_revision(head_revision)
        previous_revision = head_script.down_revision
        assert isinstance(previous_revision, str) and previous_revision, (
            "head migration has no single down_revision (merge node?); "
            "extend this test to handle the merge case"
        )

        async with engine.connect() as conn:

            def _run_alembic(sync_conn) -> None:
                from alembic.runtime.migration import MigrationContext

                cfg = _build_alembic_config(sync_conn)

                # 1. Stamp the throwaway DB at head.  ``Base.metadata``
                # was already materialised by ``build_postgres_session_factory``
                # so the schema matches the production DB; we just need
                # alembic_version to reflect that.
                command.stamp(cfg, head_revision)
                rev = MigrationContext.configure(sync_conn).get_current_revision()
                assert rev == head_revision, (
                    f"After stamp expected {head_revision!r}, got {rev!r}"
                )

                # 2. Downgrade one revision to the explicit parent of
                # head.  We compute the target ourselves because the
                # python API does not honour the CLI's ``-1`` relative
                # syntax.  The head migration's ``downgrade()`` runs.
                command.downgrade(cfg, previous_revision)
                rev = MigrationContext.configure(sync_conn).get_current_revision()
                assert rev == previous_revision, (
                    f"After downgrade expected {previous_revision!r}, "
                    f"got {rev!r} — head migration's downgrade() ran "
                    f"but moved alembic_version to an unexpected revision"
                )

                # 3. Re-upgrade to head.  The head migration's
                # ``upgrade()`` runs against a schema that was just
                # rolled back by its own ``downgrade()`` — this is the
                # load-bearing assertion: did downgrade actually undo
                # what upgrade did, or just bump the version row?
                command.upgrade(cfg, "head")
                rev = MigrationContext.configure(sync_conn).get_current_revision()
                assert rev == head_revision, (
                    f"After re-upgrade expected {head_revision!r}, "
                    f"got {rev!r}"
                )

            await conn.run_sync(_run_alembic)
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_head_migration_from_previous_revision_creates_paper_schema() -> None:
    import asyncpg
    from sqlalchemy import MetaData, inspect, text

    from tests.postgres_test_db import build_postgres_session_factory

    engine = None
    try:
        engine, _factory = await build_postgres_session_factory(MetaData(), "alembic_head_schema")
    except (OSError, ValueError, asyncpg.PostgresError) as exc:
        pytest.skip(f"Postgres unreachable for head migration schema test: {exc}")

    assert engine is not None
    try:
        script = ScriptDirectory.from_config(Config(str(BACKEND_ROOT / "alembic.ini")))
        head_revision = script.get_current_head()
        assert head_revision is not None
        head_script = script.get_revision(head_revision)
        previous_revision = head_script.down_revision
        assert isinstance(previous_revision, str) and previous_revision

        async with engine.connect() as conn:

            def _upgrade_head(sync_conn):
                cfg = _build_alembic_config(sync_conn)
                # This regression originally exercised the position migration while it
                # was head.  Keep provisioning its real predecessor now that the
                # test-trade migration follows it, then upgrade through both revisions.
                command.upgrade(cfg, "202608060001")
                sync_conn.execute(text(
                    "DROP TRIGGER IF EXISTS trg_kalshi_paper_decisions_validate_positions "
                    "ON kalshi_paper_decisions"
                ))
                sync_conn.execute(text("DROP TABLE IF EXISTS kalshi_paper_positions CASCADE"))
                sync_conn.execute(text(
                    "DROP TRIGGER IF EXISTS trg_kalshi_paper_fills_validate_sell_evidence "
                    "ON kalshi_paper_fills"
                ))
                sync_conn.execute(text(
                    "ALTER TABLE kalshi_paper_intents DROP COLUMN IF EXISTS order_side, "
                    "DROP COLUMN IF EXISTS position_id"
                ))
                sync_conn.execute(text(
                    "ALTER TABLE kalshi_paper_decisions DROP COLUMN IF EXISTS position_id, "
                    "DROP COLUMN IF EXISTS position_cost_basis, DROP COLUMN IF EXISTS realized_pnl"
                ))
                sync_conn.execute(
                    text(
                        "INSERT INTO kalshi_paper_accounts "
                        "(id, name, currency, starting_cash, cash_balance, journal_sequence, created_at, updated_at) "
                        "VALUES ('prior-account', 'Prior account', 'USD', 10, 10, 0, now(), now())"
                    )
                )
                sync_conn.execute(
                    text(
                        "INSERT INTO kalshi_paper_intents "
                        "(account_id, decision_id, request_hash, action, opportunity_id, opportunity_stable_id, "
                        "opportunity_revision, opportunity_snapshot_json, strategy_key, strategy_version, ticker, "
                        "outcome, requested_quantity, limit_price, created_at) VALUES "
                        "('prior-account', 'prior-intent', :hash, 'execute', 'opp', 'stable', :hash, '{}', "
                        "'basic', NULL, 'KXPRIOR', 'yes', 1, 0.5, now())"
                    ),
                    {"hash": "a" * 64},
                )
                sync_conn.execute(
                    text(
                        "INSERT INTO kalshi_paper_decisions "
                        "(account_id, decision_id, account_sequence, request_hash, action, opportunity_id, "
                        "opportunity_stable_id, opportunity_revision, opportunity_snapshot_json, strategy_key, "
                        "strategy_version, ticker, event_ticker, outcome, order_side, time_in_force, "
                        "requested_quantity, limit_price, status, reason, source_origin, market_observed_at, "
                        "market_fetched_at, market_evidence_hash, market_evidence_json, book_observed_at, "
                        "book_fetched_at, book_evidence_hash, book_evidence_json, fill_formula_version, "
                        "fee_rule_version, fee_provenance_json, filled_quantity, remaining_quantity, "
                        "average_fill_price, notional, fee, cash_before, cash_after, created_at) VALUES "
                        "('prior-account', 'prior-intent', 1, :hash, 'execute', 'opp', 'stable', :hash, '{}', "
                        "'basic', NULL, 'KXPRIOR', NULL, 'yes', 'buy', 'immediate_or_cancel', 1, 0.5, "
                        "'filled', 'prior_fill', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, "
                        "'kalshi-complementary-depth-ioc-v1', 'kalshi-market-fee-waiver-v1', :provenance, "
                        "1, 0, 0.5, 0.5, 0, 10, 9.5, now())"
                    ),
                    {
                        "hash": "a" * 64,
                        "provenance": (
                            '{"kind":"market_fee_waiver","market_snapshot_hash":"' + "b" * 64
                            + '","observed_at":"2026-08-06T12:00:00+00:00","openapi_sha256":"'
                            + "c" * 64 + '","waiver_expiration_time":"2026-08-07T12:00:00+00:00"}'
                        ),
                    },
                )
                sync_conn.execute(text(
                    "INSERT INTO kalshi_paper_fills "
                    "(account_id, decision_id, sequence, quantity, price, notional, fee, source_bid_price, "
                    "source_side, evidence_json, created_at) VALUES "
                    "('prior-account', 'prior-intent', 1, 1, 0.5, 0.5, 0, 0.5, 'no', '{}', now())"
                ))
                sync_conn.execute(text(
                    "UPDATE kalshi_paper_accounts SET cash_balance=9.5, journal_sequence=1 "
                    "WHERE id='prior-account'"
                ))
                sync_conn.commit()
                command.upgrade(cfg, head_revision)
                inspector = inspect(sync_conn)
                tables = set(inspector.get_table_names())
                intents = {column["name"] for column in inspector.get_columns("kalshi_paper_intents")}
                decisions = {column["name"] for column in inspector.get_columns("kalshi_paper_decisions")}
                fills = {column["name"] for column in inspector.get_columns("kalshi_paper_fills")}
                accounts = {column["name"] for column in inspector.get_columns("kalshi_paper_accounts")}
                orders = {column["name"] for column in inspector.get_columns("kalshi_paper_orders")}
                cancellations = {column["name"] for column in inspector.get_columns("kalshi_paper_cancellations")}
                events = {column["name"] for column in inspector.get_columns("kalshi_paper_order_events")}
                positions = {column["name"] for column in inspector.get_columns("kalshi_paper_positions")}
                decision_foreign_keys = {
                    foreign_key["name"] for foreign_key in inspector.get_foreign_keys("kalshi_paper_decisions")
                }
                event_foreign_keys = {
                    foreign_key["name"] for foreign_key in inspector.get_foreign_keys("kalshi_paper_order_events")
                }
                order_indexes = {index["name"] for index in inspector.get_indexes("kalshi_paper_orders")}
                event_indexes = {index["name"] for index in inspector.get_indexes("kalshi_paper_order_events")}
                return (
                    tables, intents, decisions, fills, accounts, orders, cancellations, events, positions,
                    decision_foreign_keys, event_foreign_keys, order_indexes, event_indexes,
                )

            (
                tables, intent_columns, decision_columns, fill_columns, account_columns,
                order_columns, cancellation_columns, event_columns, position_columns, decision_foreign_keys,
                event_foreign_keys, order_indexes, event_indexes,
            ) = await conn.run_sync(_upgrade_head)
            trigger_names = set(
                (
                    await conn.execute(
                        text(
                            "SELECT tgname FROM pg_trigger "
                            "WHERE tgrelid IN ('kalshi_paper_accounts'::regclass, "
                            "'kalshi_paper_intents'::regclass, "
                            "'kalshi_paper_decisions'::regclass, "
                            "'kalshi_paper_fills'::regclass, "
                            "'kalshi_paper_positions'::regclass, "
                            "'kalshi_paper_orders'::regclass, "
                            "'kalshi_paper_cancellations'::regclass, "
                            "'kalshi_paper_order_events'::regclass, "
                            "'kalshi_paper_test_runs'::regclass, "
                            "'kalshi_paper_test_events'::regclass) AND NOT tgisinternal"
                        )
                    )
                ).scalars()
            )

            paper_tables = {
                "kalshi_paper_accounts",
                "kalshi_paper_intents",
                "kalshi_paper_decisions",
                "kalshi_paper_fills",
                "kalshi_paper_orders",
                "kalshi_paper_cancellations",
                "kalshi_paper_order_events",
                "kalshi_paper_test_runs",
                "kalshi_paper_test_events",
            }
            assert paper_tables <= tables
            assert {
                "account_id",
                "decision_id",
                "request_hash",
                "opportunity_revision",
                "opportunity_snapshot_json",
                "time_in_force",
                "requested_quantity",
                "limit_price",
                "order_side", "position_id", "created_at",
            } <= intent_columns
            assert {
                "account_id",
                "decision_id",
                "account_sequence",
                "request_hash",
                "opportunity_revision",
                "requested_quantity",
                "limit_price",
                "filled_quantity",
                "remaining_quantity",
                "notional",
                "fee",
                "cash_before",
                "cash_after",
                "position_id", "position_cost_basis", "realized_pnl",
            } <= decision_columns
            assert {
                "account_id",
                "decision_id",
                "sequence",
                "quantity",
                "price",
                "notional",
                "fee",
                "source_bid_price",
                "source_side",
            } <= fill_columns
            assert "fk_kalshi_paper_decisions_intent" in decision_foreign_keys
            assert "reserved_cash" in account_columns
            assert {
                "account_id", "position_id", "entry_decision_id", "ticker", "outcome",
                "entry_quantity", "entry_notional", "entry_fee", "created_at",
            } <= position_columns
            backfilled_position = (
                await conn.execute(text(
                    "SELECT entry_decision_id, ticker, outcome, entry_quantity, entry_notional, entry_fee "
                    "FROM kalshi_paper_positions WHERE account_id='prior-account'"
                ))
            ).one()
            assert tuple(backfilled_position) == (
                "prior-intent", "KXPRIOR", "yes", 1, Decimal("0.5"), 0,
            )

            assert {"order_id", "decision_id", "open_quantity", "decision_status", "reserved_cash"} <= order_columns
            assert {"cancellation_id", "order_id", "released_cash", "status"} <= cancellation_columns
            assert {"order_id", "sequence", "event_type", "cancellation_id"} <= event_columns
            assert {
                "fk_kalshi_paper_order_events_order",
                "fk_kalshi_paper_order_events_cancellation",
            } <= event_foreign_keys
            assert "idx_kalshi_paper_orders_account_created" in order_indexes
            assert "idx_kalshi_paper_order_events_cancel" in event_indexes
            assert {
                "trg_kalshi_paper_intents_immutable",
                "trg_kalshi_paper_intents_truncate_immutable",
                "trg_kalshi_paper_decisions_immutable",
                "trg_kalshi_paper_decisions_truncate_immutable",
                "trg_kalshi_paper_decisions_fill_aggregate",
                "trg_kalshi_paper_decisions_validate_account_journal",
                "trg_kalshi_paper_fills_immutable",
                "trg_kalshi_paper_fills_truncate_immutable",
                "trg_kalshi_paper_fills_fill_aggregate",
                "trg_kalshi_paper_fills_validate_sell_evidence",
                "trg_kalshi_paper_positions_immutable",
                "trg_kalshi_paper_positions_truncate_immutable",
                "trg_kalshi_paper_positions_validate",
                "trg_kalshi_paper_decisions_validate_positions",
                "trg_kalshi_paper_accounts_validate_journal",
                "trg_kalshi_paper_orders_immutable",
                "trg_kalshi_paper_orders_truncate_immutable",
                "trg_kalshi_paper_cancellations_immutable",
                "trg_kalshi_paper_cancellations_truncate_immutable",
                "trg_kalshi_paper_order_events_immutable",
                "trg_kalshi_paper_order_events_truncate_immutable",
                "trg_kalshi_paper_accounts_validate_order_lifecycle",
                "trg_kalshi_paper_orders_validate_lifecycle",
                "trg_kalshi_paper_cancellations_validate_lifecycle",
                "trg_kalshi_paper_order_events_validate_lifecycle",
                "trg_kalshi_paper_test_runs_protect_request",
                "trg_kalshi_paper_test_runs_validate_projection",
                "trg_kalshi_paper_test_events_immutable",
                "trg_kalshi_paper_test_events_truncate_immutable",
                "trg_kalshi_paper_test_events_validate",
            } <= trigger_names
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.slow
@pytest.mark.asyncio
async def test_alembic_replay_base_to_head_on_empty_db() -> None:
    """Run the full migration chain from base against an empty DB.

    Asserts that ``alembic upgrade base→head`` succeeds end-to-end,
    which is the contract a new developer setup or a bare-metal CI
    bootstrap relies on.

    Plan 0020 made every later schema-additive migration idempotent
    via ``alembic_helpers.safe_*`` wrappers, so the lazy baseline
    (``Base.metadata.create_all``) plus the chain replay produces a
    correct schema without colliding ``op.add_column`` /
    ``op.create_table`` / ``op.create_index`` calls.

    Future migrations should use ``safe_add_column``,
    ``safe_create_table``, and ``safe_create_index`` from
    ``alembic_helpers`` to keep this test green.

    The replay runs in a **subprocess** with the throwaway DB URL in
    its env.  The in-process route (``command.upgrade`` against a
    shared async connection) hits an alembic transaction-state
    assertion midway through the ~130-migration chain — alembic
    expects to manage its own per-migration transaction lifecycle
    and gets confused when the connection's outer state is shared
    across many ``begin_transaction`` calls.  A subprocess gives
    alembic the standalone connection lifecycle it expects, matches
    what production's ``init_database`` actually does on cold start,
    and matches the lifespan smoke test's pattern.
    """
    import asyncio
    import os
    import sys

    try:
        from sqlalchemy import MetaData

        from tests.postgres_test_db import build_postgres_session_factory
    except Exception as exc:  # pragma: no cover — defensive
        pytest.skip(f"DB harness unavailable: {exc}")

    engine = None
    try:
        # Pass an empty MetaData so the factory does NOT pre-create
        # the schema — we want a true empty DB for alembic to
        # bootstrap against.
        engine, _factory = await build_postgres_session_factory(
            MetaData(), "alembic_replay"
        )
    except Exception as exc:
        pytest.skip(f"Postgres unreachable for alembic replay: {exc}")

    assert engine is not None
    try:
        script = ScriptDirectory.from_config(
            Config(str(BACKEND_ROOT / "alembic.ini"))
        )
        head_revision = script.get_current_head()
        assert head_revision, "alembic script directory has no head revision"

        # Use render_as_string(hide_password=False) — str(URL) redacts
        # the password in modern SQLAlchemy and would cause the
        # subprocess to fail with InvalidPasswordError.
        test_database_url = engine.url.render_as_string(hide_password=False)

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _REPLAY_DRIVER_SOURCE,
            cwd=str(BACKEND_ROOT),
            env={
                **os.environ,
                "DATABASE_URL": test_database_url,
                "LOG_LEVEL": "WARNING",
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=60.0
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            pytest.fail(
                "alembic replay subprocess exceeded 60 s — a migration "
                "in the chain is hanging or taking too long"
            )

        if proc.returncode != 0:
            pytest.fail(
                "alembic replay subprocess exited with "
                f"{proc.returncode}\nstdout: {stdout!r}\nstderr: {stderr!r}"
            )

        # The subprocess prints the final revision it observed so we
        # can assert it locally.
        marker = b"REPLAY_OK head="
        assert marker in stdout, (
            f"alembic replay subprocess did not emit success marker.\n"
            f"stdout: {stdout!r}\nstderr: {stderr!r}"
        )
        observed = stdout.split(marker, 1)[1].split(b"\n", 1)[0].strip().decode()
        assert observed == head_revision, (
            f"After full replay expected {head_revision!r}, "
            f"subprocess observed {observed!r}"
        )
        async with engine.connect() as conn:
            runtime_columns = await conn.run_sync(
                lambda sync_conn: {
                    column["name"]
                    for column in __import__("sqlalchemy").inspect(sync_conn).get_columns(
                        "kalshi_portfolio_runtime_snapshots"
                    )
                }
            )
        assert runtime_columns == {
            "principal_fingerprint",
            "updated_at",
            "last_run_at",
            "running",
            "enabled",
            "current_activity",
            "interval_seconds",
            "lag_seconds",
            "last_error",
            "stats_json",
        }
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.slow
@pytest.mark.asyncio
async def test_populated_paper_test_run_upgrades_from_202608070001_to_head() -> None:
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import MetaData, text

    from tests.postgres_test_db import build_postgres_session_factory

    engine = None
    try:
        engine, _factory = await build_postgres_session_factory(
            MetaData(), "alembic_populated_paper_test_upgrade"
        )
    except Exception as exc:
        pytest.skip(f"Postgres unreachable for populated migration test: {exc}")

    assert engine is not None
    request = {
        "account_id": "paper-account:migration",
        "entry_limit_price": "0.600000",
        "opportunity_id": "opportunity:migration",
        "opportunity_revision": "a" * 64,
        "quantity": "2.00",
        "run_id": "paper-test-run:migration",
        "stop_loss_minimum_price": "0.300000",
        "stop_loss_price": "0.400000",
        "take_profit_price": "0.700000",
    }
    request_json = json.dumps(request, sort_keys=True, separators=(",", ":"))
    request_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
    created_at = datetime(2026, 8, 7, 12, 0, 0)

    try:
        async with engine.connect() as conn:

            def _seed_and_upgrade(sync_conn) -> None:
                cfg = _build_alembic_config(sync_conn)
                command.upgrade(cfg, "202608070001")
                sync_conn.execute(
                    text(
                        "ALTER TABLE kalshi_paper_test_runs "
                        "ALTER COLUMN request_json DROP NOT NULL"
                    )
                )
                sync_conn.execute(
                    text(
                        "DROP TRIGGER IF EXISTS trg_kalshi_paper_test_runs_validate_insert "
                        "ON kalshi_paper_test_runs"
                    )
                )
                sync_conn.execute(
                    text(
                        "INSERT INTO kalshi_paper_accounts "
                        "(id,name,currency,starting_cash,cash_balance,reserved_cash,journal_sequence,created_at,updated_at) "
                        "VALUES (:id,:name,'USD',100,100,0,0,:created_at,:created_at)"
                    ),
                    {
                        "id": request["account_id"],
                        "name": "Migration paper account",
                        "created_at": created_at,
                    },
                )
                sync_conn.execute(
                    text(
                        "INSERT INTO kalshi_paper_test_runs "
                        "(run_id,request_hash,account_id,opportunity_id,opportunity_revision,ticker,outcome,"
                        "quantity,entry_limit_price,take_profit_price,stop_loss_price,stop_loss_minimum_price,"
                        "entry_decision_id,position_id,status,next_event_sequence,last_reason,last_error,created_at,updated_at) "
                        "VALUES (:run_id,:request_hash,:account_id,:opportunity_id,:opportunity_revision,'KXMIGRATE','yes',"
                        ":quantity,:entry_limit_price,:take_profit_price,:stop_loss_price,:stop_loss_minimum_price,"
                        ":entry_decision_id,NULL,'starting',2,'operator_started',NULL,:created_at,:created_at)"
                    ),
                    {
                        **request,
                        "request_hash": request_hash,
                        "entry_decision_id": f"paper-test-entry:{request['run_id']}",
                        "created_at": created_at,
                    },
                )
                sync_conn.execute(
                    text(
                        "INSERT INTO kalshi_paper_test_events "
                        "(run_id,sequence,account_id,event_type,position_id,best_bid,trigger_price,exit_decision_id,"
                        "market_observed_at,book_observed_at,quote_evidence_hash,quote_evidence_json,remaining_quantity,"
                        "realized_pnl,reason,created_at) "
                        "VALUES (:run_id,1,:account_id,'started',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,"
                        "'operator_started',:created_at)"
                    ),
                    {
                        "run_id": request["run_id"],
                        "account_id": request["account_id"],
                        "created_at": created_at,
                    },
                )
                sync_conn.commit()
                command.upgrade(cfg, "head")
                assert MigrationContext.configure(sync_conn).get_current_revision() == "202608070002"
                migrated = sync_conn.execute(
                    text(
                        "SELECT request_hash,request_json FROM kalshi_paper_test_runs WHERE run_id=:run_id"
                    ),
                    {"run_id": request["run_id"]},
                ).one()
                assert migrated.request_hash == request_hash
                assert migrated.request_json == request_json

            await conn.run_sync(_seed_and_upgrade)
    finally:
        await engine.dispose()


_REPLAY_DRIVER_SOURCE = """
import asyncio
import os
import subprocess
import sys

from alembic.runtime.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine


async def _read_revision(engine) -> str | None:
    async with engine.connect() as conn:
        return await conn.run_sync(
            lambda sync_conn: MigrationContext.configure(
                sync_conn
            ).get_current_revision()
        )


async def _drive() -> int:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        pre = await _read_revision(engine)
        assert pre is None, f"expected fresh DB, got revision {pre!r}"
    finally:
        await engine.dispose()

    # Shell out to the alembic CLI rather than driving ``command.upgrade``
    # in-process.  Several migrations (e.g. 202603120001_db_hot_path_indexes)
    # use ``context.autocommit_block()`` for ``CREATE INDEX CONCURRENTLY``,
    # which requires alembic to own the connection's transaction lifecycle
    # end-to-end.  Wrapping the engine externally (even via ``engine.connect()``
    # alone) breaks ``autocommit_block``'s ``assert self._transaction is not
    # None`` deep in ``MigrationContext``.  The CLI matches what
    # ``init_database`` in production does on cold start.
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini",
         "upgrade", "head"],
        env={**os.environ},
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        sys.exit(proc.returncode)

    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        post = await _read_revision(engine)
    finally:
        await engine.dispose()

    print(f"REPLAY_OK head={post}", flush=True)
    return 0


sys.exit(asyncio.run(_drive()))
"""
