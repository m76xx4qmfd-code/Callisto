from __future__ import annotations

import asyncio
from decimal import Decimal, getcontext

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from models.database import (
    KalshiPaperAccount,
    KalshiPaperCancellation,
    KalshiPaperOrder,
    VenueExecutionEvent,
    VenueOrderIntentRecord,
    VenueProviderAcknowledgementRecord,
)
from services.kalshi_paper_execution import KalshiPaperProtocolError
from services.kalshi_paper_service import (
    KalshiPaperService,
    PaperCancellationConflict,
    PaperOrderNotCancelable,
)
from tests.test_kalshi_paper_ledger import _build


async def _open_gtc(service, *, account_id: str, decision_id: str, quantity: str = "6.00", limit: str = "0.600000"):
    eligibility = await service.get_eligibility("opp-detection")
    return await service.record_decision(
        account_id=account_id,
        decision_id=decision_id,
        opportunity_id="opp-detection",
        opportunity_revision=eligibility["opportunity_revision"],
        action="execute",
        quantity=quantity,
        limit_price=limit,
        time_in_force="good_till_canceled",
    )


@pytest.mark.db
@pytest.mark.asyncio
async def test_gtc_partial_opens_and_reserves_exact_remainder_without_changing_ioc_default() -> None:
    engine, factory, _market_data, service = await _build("paper_gtc_partial")
    try:
        account = await service.create_account(name="GTC partial", starting_cash="10.00")
        opened = await _open_gtc(service, account_id=account["id"], decision_id="gtc-partial")
        assert opened["time_in_force"] == "good_till_canceled"
        assert opened["status"] == "partial"
        assert opened["reason"] == "displayed_depth_partially_filled_gtc_open"
        assert opened["fill_formula_version"] == "kalshi-complementary-depth-gtc-open-v1"
        assert opened["filled_quantity"] == "5.00"
        assert opened["remaining_quantity"] == "1.00"
        assert opened["order_id"]
        assert opened["reserved_cash"] == "0.600000000000000000"
        assert opened["cash_after"] == "7.100000000000000000"

        async with factory() as session:
            stored = await session.get(KalshiPaperAccount, account["id"])
            assert stored is not None
            assert stored.cash_balance == Decimal("7.100000000000000000")
            assert stored.reserved_cash == Decimal("0.600000000000000000")

        listed = await service.list_orders(account_id=account["id"])
        assert listed[0]["order_id"] == opened["order_id"]
        assert listed[0]["cancelable"] is True
        assert listed[0]["later_matching_supported"] is False
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_gtc_no_cross_reserves_limit_notional_and_exact_math_ignores_decimal_context() -> None:
    engine, factory, market_data, service = await _build("paper_gtc_no_cross")
    try:
        quote = market_data.fetch_quote.return_value
        market_data.fetch_quote.return_value = type(quote)(market=quote.market, book=type(quote.book)(
            ticker=quote.book.ticker,
            yes_bids=quote.book.yes_bids,
            no_bids=(),
            source_origin=quote.book.source_origin,
            observed_at=quote.book.observed_at,
            fetched_at=quote.book.fetched_at,
            evidence_hash=quote.book.evidence_hash,
            evidence_json=quote.book.evidence_json,
        ))
        account = await service.create_account(name="GTC exact", starting_cash="100000000000000000000.00")
        original = getcontext().copy()
        try:
            getcontext().prec = 4
            opened = await _open_gtc(
                service,
                account_id=account["id"],
                decision_id="gtc-exact",
                quantity="99999999999999999999.99",
                limit="0.123000",
            )
        finally:
            getcontext().prec = original.prec
            getcontext().rounding = original.rounding
            getcontext().traps = original.traps
        assert opened["status"] == "no_fill"
        assert opened["reason"] == "displayed_depth_empty"
        assert opened["fill_formula_version"] == "kalshi-complementary-depth-gtc-open-v1"
        assert opened["reserved_cash"] == "12299999999999999999.998770000000000000"
        async with factory() as session:
            stored = await session.get(KalshiPaperAccount, account["id"])
            assert stored is not None
            assert stored.reserved_cash == Decimal("12299999999999999999.998770000000000000")
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_cancel_releases_persisted_reservation_once_replays_and_never_reads_market_or_live_ledger() -> None:
    engine, factory, market_data, service = await _build("paper_cancel_replay")
    try:
        account = await service.create_account(name="Cancel", starting_cash="10.00")
        opened = await _open_gtc(service, account_id=account["id"], decision_id="gtc-cancel")
        reads_before = market_data.fetch_quote.await_count
        request = {
            "account_id": account["id"],
            "order_id": opened["order_id"],
            "cancellation_id": "cancel-stable",
        }
        first = await service.cancel_order(**request)
        restarted = KalshiPaperService(
            session_factory=factory,
            database_engine=engine,
            market_data_client=market_data,
            now=service._now,
        )
        second = await restarted.cancel_order(**request)
        assert second == first
        assert first["status"] == "cancelled"
        assert first["released_cash"] == opened["reserved_cash"]
        assert market_data.fetch_quote.await_count == reads_before

        async with factory() as session:
            stored = await session.get(KalshiPaperAccount, account["id"])
            assert stored is not None and stored.reserved_cash == Decimal("0")
            assert await session.scalar(select(func.count()).select_from(KalshiPaperCancellation)) == 1
            assert await session.scalar(select(func.count()).select_from(VenueOrderIntentRecord)) == 0
            assert await session.scalar(select(func.count()).select_from(VenueExecutionEvent)) == 0
            assert await session.scalar(select(func.count()).select_from(VenueProviderAcknowledgementRecord)) == 0

        for statement in (
            "UPDATE kalshi_paper_orders SET open_quantity = 0 WHERE order_id = :order_id",
            "DELETE FROM kalshi_paper_cancellations WHERE cancellation_id = :cancellation_id",
            "TRUNCATE kalshi_paper_order_events",
        ):
            async with factory() as session:
                with pytest.raises(DBAPIError):
                    await session.execute(
                        text(statement),
                        {"order_id": opened["order_id"], "cancellation_id": request["cancellation_id"]},
                    )
                    await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_cancellation_id_target_conflict_and_concurrent_distinct_ids_release_once() -> None:
    engine, factory, _market_data, service = await _build("paper_cancel_race")
    try:
        account = await service.create_account(name="Cancel race", starting_cash="20.00")
        first_order = await _open_gtc(service, account_id=account["id"], decision_id="gtc-race-1")
        second_order = await _open_gtc(service, account_id=account["id"], decision_id="gtc-race-2")
        await service.cancel_order(account_id=account["id"], order_id=first_order["order_id"], cancellation_id="same-id")
        with pytest.raises(PaperCancellationConflict):
            await service.cancel_order(account_id=account["id"], order_id=second_order["order_id"], cancellation_id="same-id")

        results = await asyncio.gather(
            service.cancel_order(account_id=account["id"], order_id=second_order["order_id"], cancellation_id="race-a"),
            service.cancel_order(account_id=account["id"], order_id=second_order["order_id"], cancellation_id="race-b"),
            return_exceptions=True,
        )
        assert sum(isinstance(item, dict) and item.get("status") == "cancelled" for item in results) == 1
        assert sum(isinstance(item, PaperOrderNotCancelable) for item in results) == 1
        async with factory() as session:
            order = await session.get(KalshiPaperOrder, (account["id"], second_order["order_id"]))
            assert order is not None
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_ioc_and_fully_filled_gtc_are_terminal_noncancelable() -> None:
    engine, _factory, _market_data, service = await _build("paper_cancel_terminal")
    try:
        account = await service.create_account(name="Terminal", starting_cash="10.00")
        eligibility = await service.get_eligibility("opp-detection")
        ioc = await service.record_decision(
            account_id=account["id"], decision_id="ioc", opportunity_id="opp-detection",
            opportunity_revision=eligibility["opportunity_revision"], action="execute",
            quantity="1.00", limit_price="0.600000",
        )
        assert ioc["order_id"] is None
        with pytest.raises(PaperOrderNotCancelable):
            await service.cancel_order(account_id=account["id"], order_id="missing", cancellation_id="cancel-ioc")

        filled = await _open_gtc(service, account_id=account["id"], decision_id="gtc-filled", quantity="1.00")
        assert filled["reserved_cash"] == "0.000000000000000000"
        with pytest.raises(PaperOrderNotCancelable):
            await service.cancel_order(account_id=account["id"], order_id=filled["order_id"], cancellation_id="cancel-filled")
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_concurrent_gtc_placements_cannot_overcommit_available_cash() -> None:
    engine, factory, _market_data, service = await _build("paper_gtc_cash_race")
    try:
        account = await service.create_account(name="GTC cash race", starting_cash="3.50")
        account_id = str(account["id"])
        first, second = await asyncio.gather(
            _open_gtc(service, account_id=account_id, decision_id="gtc-cash-1"),
            _open_gtc(service, account_id=account_id, decision_id="gtc-cash-2"),
        )
        assert sorted((first["status"], second["status"])) == ["partial", "rejected"]
        async with factory() as session:
            stored = await session.get(KalshiPaperAccount, account["id"])
            assert stored is not None
            assert stored.cash_balance == Decimal("0.600000000000000000")
            assert stored.reserved_cash == Decimal("0.600000000000000000")
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_database_rejects_order_facts_that_contradict_causal_decision() -> None:
    engine, factory, _market_data, service = await _build("paper_gtc_order_fact_guard")
    try:
        account = await service.create_account(name="Order fact guard", starting_cash="10.00")
        eligibility = await service.get_eligibility("opp-detection")
        decision = await service.record_decision(
            account_id=str(account["id"]),
            decision_id="ioc-cause",
            opportunity_id="opp-detection",
            opportunity_revision=str(eligibility["opportunity_revision"]),
            action="execute",
            quantity="1.00",
            limit_price="0.600000",
        )
        async with factory() as session:
            with pytest.raises(DBAPIError):
                await session.execute(
                    text(
                        "INSERT INTO kalshi_paper_orders "
                        "(account_id, order_id, decision_id, ticker, outcome, side, time_in_force, "
                        "requested_quantity, filled_quantity, open_quantity, limit_price, decision_status, "
                        "reserved_cash, created_at) "
                        "VALUES (:account_id, 'forged-order', 'ioc-cause', :ticker, 'yes', 'buy', "
                        "'good_till_canceled', 1, 1, 0, 0.6, 'filled', 0, now())"
                    ),
                    {"account_id": account["id"], "ticker": decision["ticker"]},
                )
                await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_database_rejects_gtc_decision_forged_from_ioc_intent() -> None:
    engine, factory, _market_data, service = await _build("paper_gtc_intent_tif_guard")
    try:
        account = await service.create_account(name="Intent TIF guard", starting_cash="10.00")
        eligibility = await service.get_eligibility("opp-detection")
        await service.record_decision(
            account_id=str(account["id"]),
            decision_id="ioc-cause",
            opportunity_id="opp-detection",
            opportunity_revision=str(eligibility["opportunity_revision"]),
            action="execute",
            quantity="1.00",
            limit_price="0.600000",
        )
        forged_hash = "e" * 64
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO kalshi_paper_intents "
                    "SELECT account_id, 'forged-tif', :request_hash, action, opportunity_id, "
                    "opportunity_stable_id, opportunity_revision, opportunity_snapshot_json, strategy_key, "
                    "strategy_version, ticker, outcome, 'immediate_or_cancel', requested_quantity, limit_price, "
                    "created_at "
                    "FROM kalshi_paper_intents WHERE decision_id = 'ioc-cause'"
                ),
                {"request_hash": forged_hash},
            )
        async with factory() as session:
            with pytest.raises(DBAPIError, match="contradicts immutable intent"):
                await session.execute(
                    text(
                        "INSERT INTO kalshi_paper_decisions "
                        "SELECT account_id, 'forged-tif', 2, :request_hash, action, opportunity_id, "
                        "opportunity_stable_id, opportunity_revision, opportunity_snapshot_json, strategy_key, "
                        "strategy_version, ticker, event_ticker, outcome, order_side, 'good_till_canceled', "
                        "requested_quantity, limit_price, status, reason, source_origin, market_observed_at, "
                        "market_fetched_at, market_evidence_hash, market_evidence_json, book_observed_at, "
                        "book_fetched_at, book_evidence_hash, book_evidence_json, fill_formula_version, "
                        "fee_rule_version, fee_provenance_json, filled_quantity, remaining_quantity, "
                        "average_fill_price, notional, fee, cash_before, cash_after, created_at "
                        "FROM kalshi_paper_decisions WHERE decision_id = 'ioc-cause'"
                    ),
                    {"request_hash": forged_hash},
                )
                await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_database_rejects_open_order_forged_from_rejected_gtc_decision() -> None:
    engine, factory, market_data, service = await _build("paper_gtc_rejected_order_guard")
    try:
        market_data.fetch_quote.side_effect = KalshiPaperProtocolError("invalid quote")
        account = await service.create_account(name="Rejected GTC guard", starting_cash="10.00")
        decision = await _open_gtc(
            service,
            account_id=str(account["id"]),
            decision_id="rejected-gtc-cause",
            quantity="1.00",
        )
        assert decision["status"] == "rejected"

        async with factory() as session:
            with pytest.raises(DBAPIError):
                await session.execute(
                    text(
                        "INSERT INTO kalshi_paper_orders "
                        "(account_id, order_id, decision_id, ticker, outcome, side, time_in_force, "
                        "requested_quantity, filled_quantity, open_quantity, limit_price, decision_status, "
                        "reserved_cash, created_at) "
                        "VALUES (:account_id, 'forged-rejected-order', 'rejected-gtc-cause', :ticker, 'yes', 'buy', "
                        "'good_till_canceled', 1, 0, 1, 0.6, 'rejected', 0.6, now())"
                    ),
                    {"account_id": account["id"], "ticker": decision["ticker"]},
                )
                await session.execute(
                    text(
                        "INSERT INTO kalshi_paper_order_events "
                        "(account_id, order_id, sequence, event_type, cancellation_id, quantity, reserved_cash, created_at) "
                        "VALUES (:account_id, 'forged-rejected-order', 1, 'opened', NULL, 1, 0.6, now())"
                    ),
                    {"account_id": account["id"]},
                )
                await session.execute(
                    text("UPDATE kalshi_paper_accounts SET reserved_cash = 0.6 WHERE id = :account_id"),
                    {"account_id": account["id"]},
                )
                await session.commit()
    finally:
        await engine.dispose()
