from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext
from types import MappingProxyType
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from models.database import (
    Base,
    KalshiPaperAccount,
    KalshiPaperDecision,
    KalshiPaperFill,
    KalshiPaperIntent,
    OpportunityState,
    VenueExecutionEvent,
    VenueOrderIntentRecord,
    VenueProviderAcknowledgementRecord,
)
from services.kalshi_paper_execution import (
    KalshiPaperProtocolError,
    PaperBook,
    PaperBookLevel,
    PaperMarket,
    PaperPriceRange,
    PaperQuote,
)
from services.kalshi_paper_service import (
    KalshiPaperService,
    PaperDecisionConflict,
    _finish_cleanup_before_cancellation,
)
from tests.postgres_test_db import build_postgres_session_factory


NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_owned_cleanup_task() -> None:
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def cleanup() -> None:
        cleanup_started.set()
        await release_cleanup.wait()

    cleanup_task = asyncio.create_task(cleanup())
    wrapper = asyncio.create_task(_finish_cleanup_before_cancellation(cleanup_task))
    await asyncio.wait_for(cleanup_started.wait(), timeout=5)
    wrapper.cancel()
    await asyncio.sleep(0)
    wrapper.cancel()
    await asyncio.sleep(0)
    assert not wrapper.done()
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await wrapper
    assert cleanup_task.done()


def _quote() -> PaperQuote:
    return PaperQuote(
        market=PaperMarket(
            ticker="KXTEST-26",
            event_ticker="KXTEST",
            notional_value=Decimal("1.000000"),
            price_level_structure="deci_cent",
            price_ranges=(PaperPriceRange(start=Decimal("0"), end=Decimal("1"), step=Decimal("0.001")),),
            fee=Decimal("0"),
            fee_rule_version="kalshi-market-fee-waiver-v1",
            fee_provenance=MappingProxyType(
                {
                    "kind": "market_fee_waiver",
                    "waiver_expiration_time": "2026-08-05T12:10:00+00:00",
                    "openapi_sha256": "4" * 64,
                    "market_snapshot_hash": "b" * 64,
                    "observed_at": NOW.isoformat(),
                }
            ),
            fee_waiver_expiration=datetime(2026, 8, 5, 12, 10, tzinfo=timezone.utc),
            observed_at=NOW,
            fetched_at=NOW,
            evidence_hash="b" * 64,
            evidence_json='{"ticker":"KXTEST-26"}',
        ),
        book=PaperBook(
            ticker="KXTEST-26",
            yes_bids=(PaperBookLevel(price=Decimal("0.400000"), quantity=Decimal("3.00")),),
            no_bids=(
                PaperBookLevel(price=Decimal("0.400000"), quantity=Decimal("3.00")),
                PaperBookLevel(price=Decimal("0.450000"), quantity=Decimal("2.00")),
            ),
            source_origin="https://external-api.kalshi.com",
            observed_at=NOW,
            fetched_at=NOW,
            evidence_hash="c" * 64,
            evidence_json='{"ticker":"KXTEST-26"}',
        ),
    )


async def _build(name: str):
    engine, factory = await build_postgres_session_factory(Base, name)
    market_data = AsyncMock()
    market_data.fetch_quote.return_value = _quote()
    service = KalshiPaperService(
        session_factory=factory,
        database_engine=engine,
        market_data_client=market_data,
        now=lambda: NOW,
    )
    async with factory() as session, session.begin():
        session.add(
            OpportunityState(
                stable_id="opp-stable",
                opportunity_json={
                    "id": "opp-detection",
                    "stable_id": "opp-stable",
                    "strategy": "news_edge",
                    "revision": 7,
                    "title": "Test Kalshi opportunity",
                    "description": "One exact Kalshi side",
                    "positions_to_take": [
                        {
                            "platform": "kalshi",
                            "action": "buy",
                            "outcome": "YES",
                            "ticker": "KXTEST-26",
                            "market_id": "KXTEST-26",
                        }
                    ],
                    "markets": [
                        {
                            "platform": "kalshi",
                            "id": "KXTEST-26",
                            "ticker": "KXTEST-26",
                        }
                    ],
                },
                first_seen_at=NOW,
                last_seen_at=NOW,
                last_updated_at=NOW,
                is_active=True,
            )
        )
    return engine, factory, market_data, service


@pytest.mark.db
@pytest.mark.asyncio
async def test_full_fill_commits_exact_immutable_evidence_and_cash_atomically() -> None:
    engine, factory, market_data, service = await _build("kalshi_paper_full_fill")
    try:
        account = await service.create_account(name="Paper One", starting_cash="100.00")
        eligibility = await service.get_eligibility("opp-detection")
        result = await service.record_decision(
            account_id=account["id"],
            decision_id="decision-1",
            opportunity_id="opp-detection",
            opportunity_revision=eligibility["opportunity_revision"],
            action="execute",
            quantity="4.00",
            limit_price="0.600000",
        )

        assert result["status"] == "filled"
        assert result["filled_quantity"] == "4.00"
        assert result["remaining_quantity"] == "0.00"
        assert result["notional"] == "2.30000000"
        assert result["fee"] == "0.000000000000000000"
        assert result["cash_before"] == "100.000000000000000000"
        assert result["cash_after"] == "97.700000000000000000"
        assert result["fills"] == [
            {
                "sequence": 1,
                "quantity": "2.00",
                "price": "0.550000",
                "notional": "1.10000000",
                "fee": "0.000000000000000000",
                "source_bid_price": "0.450000",
                "source_side": "no",
            },
            {
                "sequence": 2,
                "quantity": "2.00",
                "price": "0.600000",
                "notional": "1.20000000",
                "fee": "0.000000000000000000",
                "source_bid_price": "0.400000",
                "source_side": "no",
            },
        ]
        assert market_data.fetch_quote.await_count == 1

        async with factory() as session:
            stored_account = await session.get(KalshiPaperAccount, account["id"])
            assert stored_account is not None
            assert stored_account.cash_balance == Decimal("97.700000000000000000")
            assert stored_account.journal_sequence == 1
            assert await session.scalar(select(func.count()).select_from(KalshiPaperDecision)) == 1
            assert await session.scalar(select(func.count()).select_from(KalshiPaperFill)) == 2
            assert await session.scalar(select(func.count()).select_from(VenueOrderIntentRecord)) == 0
            assert await session.scalar(select(func.count()).select_from(VenueExecutionEvent)) == 0
            assert await session.scalar(select(func.count()).select_from(VenueProviderAcknowledgementRecord)) == 0
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_same_decision_replays_without_second_market_read_and_conflict_is_immutable() -> None:
    engine, _factory, market_data, service = await _build("kalshi_paper_replay")
    try:
        account = await service.create_account(name="Replay", starting_cash="10.00")
        eligibility = await service.get_eligibility("opp-detection")
        request = {
            "account_id": account["id"],
            "decision_id": "stable-decision",
            "opportunity_id": "opp-detection",
            "opportunity_revision": eligibility["opportunity_revision"],
            "action": "execute",
            "quantity": "4.00",
            "limit_price": "0.600000",
        }
        first = await service.record_decision(**request)
        second = await service.record_decision(**request, time_in_force="immediate_or_cancel")
        assert second == first
        assert market_data.fetch_quote.await_count == 1

        with pytest.raises(PaperDecisionConflict):
            await service.record_decision(**{**request, "quantity": "5.00"})
        assert market_data.fetch_quote.await_count == 1
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_concurrent_identical_first_requests_have_one_sequence_and_one_cash_debit() -> None:
    engine, factory, market_data, service = await _build("kalshi_paper_concurrent_same")
    try:
        account = await service.create_account(name="Concurrent", starting_cash="10.00")
        eligibility = await service.get_eligibility("opp-detection")
        request = {
            "account_id": account["id"],
            "decision_id": "same-first",
            "opportunity_id": "opp-detection",
            "opportunity_revision": eligibility["opportunity_revision"],
            "action": "execute",
            "quantity": "4.00",
            "limit_price": "0.600000",
        }
        first, second = await asyncio.gather(
            service.record_decision(**request),
            service.record_decision(**request),
        )
        assert first == second
        assert market_data.fetch_quote.await_count == 1
        async with factory() as session:
            stored_account = await session.get(KalshiPaperAccount, account["id"])
            assert stored_account is not None
            assert stored_account.cash_balance == Decimal("7.700000000000000000")
            assert stored_account.journal_sequence == 1
            assert await session.scalar(select(func.count()).select_from(KalshiPaperDecision)) == 1
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_concurrent_different_decisions_serialize_account_cash_without_overdraft() -> None:
    engine, factory, _market_data, service = await _build("kalshi_paper_concurrent_cash")
    try:
        account = await service.create_account(name="Cash race", starting_cash="3.00")
        eligibility = await service.get_eligibility("opp-detection")
        request = {
            "account_id": account["id"],
            "opportunity_id": "opp-detection",
            "opportunity_revision": eligibility["opportunity_revision"],
            "action": "execute",
            "quantity": "4.00",
            "limit_price": "0.600000",
        }
        first, second = await asyncio.gather(
            service.record_decision(decision_id="cash-race-1", **request),
            service.record_decision(decision_id="cash-race-2", **request),
        )
        assert sorted((first["status"], second["status"])) == ["filled", "rejected"]
        assert {first["reason"], second["reason"]} == {
            "displayed_depth_filled_ioc",
            "insufficient_paper_cash",
        }
        async with factory() as session:
            stored_account = await session.get(KalshiPaperAccount, account["id"])
            assert stored_account is not None
            assert stored_account.cash_balance == Decimal("0.700000000000000000")
            assert stored_account.journal_sequence == 2
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_cancelled_first_attempt_leaves_intent_and_retry_expires_without_market_read() -> None:
    engine, factory, market_data, service = await _build("kalshi_paper_restart_expiry")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_quote(_ticker: str) -> PaperQuote:
        entered.set()
        await release.wait()
        return _quote()

    market_data.fetch_quote.side_effect = blocked_quote
    try:
        account = await service.create_account(name="Restart", starting_cash="10.00")
        eligibility = await service.get_eligibility("opp-detection")
        request = {
            "account_id": account["id"],
            "decision_id": "crashed-first",
            "opportunity_id": "opp-detection",
            "opportunity_revision": eligibility["opportunity_revision"],
            "action": "execute",
            "quantity": "1.00",
            "limit_price": "0.600000",
        }
        task = asyncio.create_task(service.record_decision(**request))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(KalshiPaperIntent)) == 1
            assert await session.scalar(select(func.count()).select_from(KalshiPaperDecision)) == 0

        market_data.fetch_quote.side_effect = None
        market_data.fetch_quote.return_value = _quote()
        result = await service.record_decision(**request)
        assert result["status"] == "rejected"
        assert result["reason"] == "expired_after_restart"
        assert result["cash_before"] == "10.000000000000000000"
        assert result["cash_after"] == "10.000000000000000000"
        assert market_data.fetch_quote.await_count == 1
    finally:
        release.set()
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_cash_debit_is_exact_at_envelope_under_tiny_decimal_precision() -> None:
    engine, _factory, _market_data, service = await _build("kalshi_paper_cash_precision")
    original = getcontext().copy()
    try:
        account = await service.create_account(
            name="Exact cash",
            starting_cash="9999999999999999999999999999999999999999.999999999999999999",
        )
        eligibility = await service.get_eligibility("opp-detection")
        getcontext().prec = 4
        result = await service.record_decision(
            account_id=account["id"],
            decision_id="exact-cash",
            opportunity_id="opp-detection",
            opportunity_revision=eligibility["opportunity_revision"],
            action="execute",
            quantity="0.01",
            limit_price="0.600000",
        )
        assert result["notional"] == "0.00550000"
        assert result["cash_after"] == "9999999999999999999999999999999999999999.994499999999999999"
    finally:
        getcontext().prec = original.prec
        getcontext().rounding = original.rounding
        getcontext().traps = original.traps
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_fee_waiver_expired_before_locked_commit_is_durable_rejection() -> None:
    engine, _factory, market_data, service = await _build("kalshi_paper_commit_fee_expiry")
    commit_time = datetime(2026, 8, 5, 12, 10, tzinfo=timezone.utc)
    quote = _quote()
    market_data.fetch_quote.return_value = PaperQuote(
        market=replace(quote.market, observed_at=commit_time, fetched_at=commit_time),
        book=replace(quote.book, observed_at=commit_time, fetched_at=commit_time),
    )
    service._now = lambda: commit_time
    try:
        account = await service.create_account(name="Fee expiry", starting_cash="10.00")
        eligibility = await service.get_eligibility("opp-detection")
        result = await service.record_decision(
            account_id=account["id"],
            decision_id="fee-expired",
            opportunity_id="opp-detection",
            opportunity_revision=eligibility["opportunity_revision"],
            action="execute",
            quantity="1.00",
            limit_price="0.600000",
        )
        assert result["status"] == "rejected"
        assert result["reason"] == "fee_waiver_expired_before_commit"
        assert result["filled_quantity"] == "0.00"
        assert result["cash_after"] == "10.000000000000000000"
        assert market_data.fetch_quote.await_count == 1
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_stale_quote_before_locked_commit_is_durable_rejection() -> None:
    engine, _factory, market_data, service = await _build("kalshi_paper_commit_quote_stale")
    service._now = lambda: NOW + timedelta(seconds=6)
    try:
        account = await service.create_account(name="Stale quote", starting_cash="10.00")
        eligibility = await service.get_eligibility("opp-detection")
        result = await service.record_decision(
            account_id=account["id"],
            decision_id="quote-stale",
            opportunity_id="opp-detection",
            opportunity_revision=eligibility["opportunity_revision"],
            action="execute",
            quantity="1.00",
            limit_price="0.600000",
        )
        assert result["status"] == "rejected"
        assert result["reason"] == "market_data_stale_before_commit"
        assert result["filled_quantity"] == "0.00"
        assert result["cash_after"] == "10.000000000000000000"
        assert market_data.fetch_quote.await_count == 1
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_pass_and_fail_closed_market_rejection_are_durable_without_cash_mutation() -> None:
    engine, factory, market_data, service = await _build("kalshi_paper_pass_reject")
    try:
        account = await service.create_account(name="Passes", starting_cash="10.00")
        eligibility = await service.get_eligibility("opp-detection")
        passed = await service.record_decision(
            account_id=account["id"],
            decision_id="pass-1",
            opportunity_id="opp-detection",
            opportunity_revision=eligibility["opportunity_revision"],
            action="pass",
        )
        assert passed["status"] == "passed"
        assert passed["cash_after"] == "10.000000000000000000"
        assert market_data.fetch_quote.await_count == 0

        market_data.fetch_quote.side_effect = KalshiPaperProtocolError("Kalshi market fee waiver is not active")
        rejected = await service.record_decision(
            account_id=account["id"],
            decision_id="reject-1",
            opportunity_id="opp-detection",
            opportunity_revision=eligibility["opportunity_revision"],
            action="execute",
            quantity="1.00",
            limit_price="0.600000",
        )
        assert rejected["status"] == "rejected"
        assert rejected["reason"] == "market_data_rejected"
        assert rejected["cash_after"] == "10.000000000000000000"

        replayed = await service.record_decision(
            account_id=account["id"],
            decision_id="reject-1",
            opportunity_id="opp-detection",
            opportunity_revision=eligibility["opportunity_revision"],
            action="execute",
            quantity="1.00",
            limit_price="0.600000",
        )
        assert replayed == rejected
        assert market_data.fetch_quote.await_count == 1

        async with factory() as session:
            stored_account = await session.get(KalshiPaperAccount, account["id"])
            assert stored_account is not None
            assert stored_account.cash_balance == Decimal("10.000000000000000000")
            assert stored_account.journal_sequence == 2
            assert await session.scalar(select(func.count()).select_from(KalshiPaperDecision)) == 2
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_database_rejects_mutation_truncation_and_excess_scale() -> None:
    engine, factory, _market_data, service = await _build("kalshi_paper_db_guards")
    try:
        account = await service.create_account(name="Guards", starting_cash="10.00")
        eligibility = await service.get_eligibility("opp-detection")
        await service.record_decision(
            account_id=account["id"],
            decision_id="guarded",
            opportunity_id="opp-detection",
            opportunity_revision=eligibility["opportunity_revision"],
            action="execute",
            quantity="1.00",
            limit_price="0.600000",
        )

        for statement in (
            "UPDATE kalshi_paper_intents SET action = 'pass' WHERE decision_id = 'guarded'",
            "TRUNCATE kalshi_paper_intents CASCADE",
            "UPDATE kalshi_paper_decisions SET reason = 'changed' WHERE decision_id = 'guarded'",
            "DELETE FROM kalshi_paper_fills WHERE decision_id = 'guarded'",
            "TRUNCATE kalshi_paper_fills",
        ):
            async with factory() as session:
                with pytest.raises(DBAPIError):
                    await session.execute(text(statement))
                    await session.commit()

        async with factory() as session:
            await session.execute(
                text(
                    "UPDATE kalshi_paper_accounts SET cash_balance = cash_balance + 1 "
                    "WHERE id = :account_id"
                ),
                {"account_id": account["id"]},
            )
            with pytest.raises(DBAPIError, match="cash does not match immutable journal"):
                await session.commit()

        async with factory() as session:
            await session.execute(
                text(
                    "INSERT INTO kalshi_paper_intents "
                    "SELECT account_id, 'forged-journal', request_hash, action, opportunity_id, "
                    "opportunity_stable_id, opportunity_revision, opportunity_snapshot_json, strategy_key, "
                    "strategy_version, ticker, outcome, time_in_force, requested_quantity, limit_price, created_at "
                    "FROM kalshi_paper_intents WHERE decision_id = 'guarded'"
                )
            )
            await session.execute(
                text(
                    "INSERT INTO kalshi_paper_decisions "
                    "SELECT account_id, 'forged-journal', 2, request_hash, action, opportunity_id, "
                    "opportunity_stable_id, opportunity_revision, opportunity_snapshot_json, strategy_key, "
                    "strategy_version, ticker, event_ticker, outcome, order_side, time_in_force, "
                    "requested_quantity, limit_price, 'rejected', 'forged', source_origin, market_observed_at, "
                    "market_fetched_at, market_evidence_hash, market_evidence_json, book_observed_at, "
                    "book_fetched_at, book_evidence_hash, book_evidence_json, fill_formula_version, "
                    "fee_rule_version, fee_provenance_json, 0, requested_quantity, NULL, 0, 0, 999, 999, created_at "
                    "FROM kalshi_paper_decisions WHERE decision_id = 'guarded'"
                )
            )
            with pytest.raises(DBAPIError, match="journal sequence is not contiguous"):
                await session.commit()

        async with factory() as session:
            await session.execute(
                text(
                    "INSERT INTO kalshi_paper_intents "
                    "SELECT account_id, 'contradiction', request_hash, action, opportunity_id, "
                    "opportunity_stable_id, opportunity_revision, opportunity_snapshot_json, strategy_key, "
                    "strategy_version, ticker, outcome, time_in_force, requested_quantity, limit_price, created_at "
                    "FROM kalshi_paper_intents WHERE decision_id = 'guarded'"
                )
            )
            with pytest.raises(DBAPIError, match="contradicts immutable intent"):
                await session.execute(
                    text(
                        "INSERT INTO kalshi_paper_decisions "
                        "SELECT account_id, 'contradiction', 2, request_hash, action, opportunity_id, "
                        "opportunity_stable_id, opportunity_revision, opportunity_snapshot_json, strategy_key, "
                        "strategy_version, ticker || '-DIFFERENT', event_ticker, outcome, order_side, time_in_force, "
                        "requested_quantity, limit_price, status, reason, source_origin, market_observed_at, "
                        "market_fetched_at, market_evidence_hash, market_evidence_json, book_observed_at, "
                        "book_fetched_at, book_evidence_hash, book_evidence_json, fill_formula_version, "
                        "fee_rule_version, fee_provenance_json, filled_quantity, remaining_quantity, "
                        "average_fill_price, notional, fee, cash_before, cash_after, created_at "
                        "FROM kalshi_paper_decisions WHERE decision_id = 'guarded'"
                    )
                )

        async with factory() as session:
            with pytest.raises(DBAPIError):
                await session.execute(
                    text(
                        "INSERT INTO kalshi_paper_accounts "
                        "(id, name, currency, starting_cash, cash_balance, reserved_cash, journal_sequence, created_at, updated_at) "
                        "VALUES ('scale', 'Scale', 'USD', 1.0000000000000000001, 1, 0, 0, now(), now())"
                    )
                )
                await session.commit()
    finally:
        await engine.dispose()
