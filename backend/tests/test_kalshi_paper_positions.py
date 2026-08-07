from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, localcontext

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from models.database import KalshiPaperDecision, KalshiPaperIntent, KalshiPaperPosition
from services.kalshi_paper_execution import (
    KalshiPaperProtocolError,
    PaperBook,
    PaperBookLevel,
    PaperPriceRange,
    simulate_sell_ioc,
)
from services.kalshi_paper_service import (
    PaperPositionNotClosable,
    _canonical_json,
    _sha256,
)
from tests.test_kalshi_paper_ledger import _build, _quote


NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
RANGES = (PaperPriceRange(start=Decimal("0"), end=Decimal("1"), step=Decimal("0.001")),)


def _book() -> PaperBook:
    return PaperBook(
        ticker="KXPOSITION-26",
        yes_bids=(
            PaperBookLevel(price=Decimal("0.600000"), quantity=Decimal("3.00")),
            PaperBookLevel(price=Decimal("0.700000"), quantity=Decimal("2.00")),
        ),
        no_bids=(
            PaperBookLevel(price=Decimal("0.350000"), quantity=Decimal("4.00")),
            PaperBookLevel(price=Decimal("0.450000"), quantity=Decimal("1.00")),
        ),
        source_origin="https://external-api.kalshi.com",
        observed_at=NOW,
        fetched_at=NOW,
        evidence_hash="d" * 64,
        evidence_json='{"ticker":"KXPOSITION-26"}',
    )


def test_sell_yes_ioc_consumes_highest_owned_outcome_bids_at_or_above_minimum() -> None:
    result = simulate_sell_ioc(
        book=_book(),
        outcome="yes",
        quantity=Decimal("4.00"),
        minimum_price=Decimal("0.600000"),
        price_ranges=RANGES,
    )

    assert result.status == "filled"
    assert result.reason == "displayed_owned_depth_filled_ioc"
    assert result.filled_quantity == Decimal("4.00")
    assert result.remaining_quantity == Decimal("0.00")
    assert result.notional == Decimal("2.60000000")
    assert result.average_fill_price == Decimal("0.650000000000000000")
    assert [(fill.quantity, fill.price, fill.source_side) for fill in result.fills] == [
        (Decimal("2.00"), Decimal("0.700000"), "yes"),
        (Decimal("2.00"), Decimal("0.600000"), "yes"),
    ]
    assert result.formula_version == "kalshi-owned-depth-sell-ioc-v1"


def test_sell_no_ioc_is_symmetric_and_preserves_unfilled_quantity() -> None:
    result = simulate_sell_ioc(
        book=_book(),
        outcome="no",
        quantity=Decimal("3.00"),
        minimum_price=Decimal("0.400000"),
        price_ranges=RANGES,
    )

    assert result.status == "partial"
    assert result.filled_quantity == Decimal("1.00")
    assert result.remaining_quantity == Decimal("2.00")
    assert result.notional == Decimal("0.45000000")
    assert result.fills[0].source_side == "no"
    assert result.fills[0].price == Decimal("0.450000")


def test_sell_ioc_rejects_off_tick_minimum_and_does_not_round() -> None:
    with pytest.raises(KalshiPaperProtocolError, match="minimum_price is not valid"):
        simulate_sell_ioc(
            book=_book(),
            outcome="yes",
            quantity=Decimal("1.00"),
            minimum_price=Decimal("0.600500"),
            price_ranges=RANGES,
        )


def test_sell_ioc_no_fill_keeps_the_entire_requested_quantity() -> None:
    result = simulate_sell_ioc(
        book=_book(),
        outcome="yes",
        quantity=Decimal("1.00"),
        minimum_price=Decimal("0.800000"),
        price_ranges=RANGES,
    )

    assert result.status == "no_fill"
    assert result.filled_quantity == Decimal("0.00")
    assert result.remaining_quantity == Decimal("1.00")
    assert result.notional == Decimal("0.00000000")
    assert result.fills == ()


@pytest.mark.db
@pytest.mark.asyncio
async def test_position_partial_and_full_sell_ioc_conserve_quantity_cash_and_realized_pnl() -> None:
    engine, _factory, market_data, service = await _build("kalshi_paper_position_partial_close")
    try:
        account = await service.create_account(name="Positions", starting_cash="100.00")
        account_id = str(account["id"])
        eligibility = await service.get_eligibility("opp-detection")
        entry = await service.record_decision(
            account_id=account_id,
            decision_id="entry-1",
            action="execute",
            opportunity_id="opp-detection",
            opportunity_revision=str(eligibility["opportunity_revision"]),
            quantity="4.00",
            limit_price="0.600000",
        )
        assert entry["status"] == "filled"
        assert entry["notional"] == "2.30000000"

        positions = await service.list_positions(account_id=account_id)
        assert len(positions) == 1
        position = positions[0]
        assert position["entry_quantity"] == "4.00"
        assert position["remaining_quantity"] == "4.00"
        position_id = str(position["position_id"])

        partial = await service.record_exit(
            account_id=account_id,
            decision_id="exit-1",
            position_id=position_id,
            quantity="2.00",
            minimum_price="0.400000",
        )
        assert partial["order_side"] == "sell"
        assert partial["status"] == "filled"
        assert partial["notional"] == "0.80000000"
        assert partial["position_cost_basis"] == "1.150000000000000000"
        assert partial["realized_pnl"] == "-0.350000000000000000"
        assert partial["cash_before"] == "97.700000000000000000"
        assert partial["cash_after"] == "98.500000000000000000"

        replay = await service.record_exit(
            account_id=account_id,
            decision_id="exit-1",
            position_id=position_id,
            quantity="2.00",
            minimum_price="0.400000",
        )
        assert replay == partial
        assert market_data.fetch_quote.await_count == 2

        after_partial = (await service.list_positions(account_id=account_id))[0]
        assert after_partial["sold_quantity"] == "2.00"
        assert after_partial["remaining_quantity"] == "2.00"
        assert after_partial["realized_pnl"] == "-0.350000000000000000"
        assert after_partial["status"] == "open"

        closed = await service.record_exit(
            account_id=account_id,
            decision_id="exit-2",
            position_id=position_id,
            quantity="2.00",
            minimum_price="0.400000",
        )
        assert closed["position_cost_basis"] == "1.150000000000000000"
        assert closed["realized_pnl"] == "-0.350000000000000000"
        final_position = (await service.list_positions(account_id=account_id))[0]
        assert final_position["remaining_quantity"] == "0.00"
        assert final_position["allocated_entry_cost"] == "2.300000000000000000"
        assert final_position["realized_pnl"] == "-0.700000000000000000"
        assert final_position["status"] == "closed"
        accounts = await service.list_accounts()
        assert accounts[0]["cash_balance"] == "99.300000000000000000"
        assert accounts[0]["journal_sequence"] == 3
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_concurrent_full_exits_have_exactly_one_winner_and_never_oversell() -> None:
    engine, _factory, market_data, service = await _build("kalshi_paper_position_concurrent_exit")
    try:
        account = await service.create_account(name="Concurrent exits", starting_cash="100.00")
        account_id = str(account["id"])
        eligibility = await service.get_eligibility("opp-detection")
        await service.record_decision(
            account_id=account_id,
            decision_id="entry-concurrent",
            action="execute",
            opportunity_id="opp-detection",
            opportunity_revision=str(eligibility["opportunity_revision"]),
            quantity="4.00",
            limit_price="0.600000",
        )
        position_id = str((await service.list_positions(account_id=account_id))[0]["position_id"])

        results = await asyncio.gather(
            service.record_exit(
                account_id=account_id,
                decision_id="exit-a",
                position_id=position_id,
                quantity="4.00",
                minimum_price="0.400000",
            ),
            service.record_exit(
                account_id=account_id,
                decision_id="exit-b",
                position_id=position_id,
                quantity="4.00",
                minimum_price="0.400000",
            ),
            return_exceptions=True,
        )
        successful = [result for result in results if isinstance(result, dict)]
        rejected = [result for result in results if isinstance(result, PaperPositionNotClosable)]
        assert len(successful) == 1
        assert len(rejected) == 1
        assert successful[0]["filled_quantity"] == "3.00"
        assert successful[0]["remaining_quantity"] == "1.00"
        position = (await service.list_positions(account_id=account_id))[0]
        assert position["sold_quantity"] == "3.00"
        assert position["remaining_quantity"] == "1.00"
        assert market_data.fetch_quote.await_count == 2
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_oversize_exit_is_rejected_before_market_data_and_journal_mutation() -> None:
    engine, _factory, market_data, service = await _build("kalshi_paper_position_oversize_exit")
    try:
        account = await service.create_account(name="Oversize exit", starting_cash="100.00")
        account_id = str(account["id"])
        eligibility = await service.get_eligibility("opp-detection")
        await service.record_decision(
            account_id=account_id,
            decision_id="entry-oversize",
            action="execute",
            opportunity_id="opp-detection",
            opportunity_revision=str(eligibility["opportunity_revision"]),
            quantity="4.00",
            limit_price="0.600000",
        )
        position_id = str((await service.list_positions(account_id=account_id))[0]["position_id"])
        with pytest.raises(PaperPositionNotClosable, match="exceeds"):
            await service.record_exit(
                account_id=account_id,
                decision_id="exit-oversize",
                position_id=position_id,
                quantity="4.01",
                minimum_price="0.400000",
            )
        assert market_data.fetch_quote.await_count == 1
        accounts = await service.list_accounts()
        assert accounts[0]["journal_sequence"] == 1
        assert (await service.list_positions(account_id=account_id))[0]["remaining_quantity"] == "4.00"
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_incomplete_sell_intent_restarts_to_rejection_without_a_second_quote() -> None:
    engine, factory, market_data, service = await _build("kalshi_paper_position_restart_exit")
    try:
        account = await service.create_account(name="Restart exit", starting_cash="100.00")
        account_id = str(account["id"])
        eligibility = await service.get_eligibility("opp-detection")
        await service.record_decision(
            account_id=account_id,
            decision_id="entry-restart",
            action="execute",
            opportunity_id="opp-detection",
            opportunity_revision=str(eligibility["opportunity_revision"]),
            quantity="4.00",
            limit_price="0.600000",
        )
        async with factory() as session:
            position = (await session.execute(
                select(KalshiPaperPosition).where(KalshiPaperPosition.account_id == account_id)
            )).scalar_one()
            entry = await session.get(KalshiPaperDecision, (account_id, position.entry_decision_id))
            assert entry is not None
            decision_id = "exit-restart"
            canonical = _canonical_json({
                "account_id": account_id,
                "decision_id": decision_id,
                "position_id": position.position_id,
                "action": "execute",
                "order_side": "sell",
                "time_in_force": "immediate_or_cancel",
                "quantity": "1.00",
                "minimum_price": "0.400000",
            })
            session.add(KalshiPaperIntent(
                account_id=account_id,
                decision_id=decision_id,
                request_hash=_sha256(canonical),
                action="execute",
                opportunity_id=entry.opportunity_id,
                opportunity_stable_id=entry.opportunity_stable_id,
                opportunity_revision=entry.opportunity_revision,
                opportunity_snapshot_json=entry.opportunity_snapshot_json,
                strategy_key=entry.strategy_key,
                strategy_version=entry.strategy_version,
                ticker=position.ticker,
                outcome=position.outcome,
                order_side="sell",
                position_id=position.position_id,
                time_in_force="immediate_or_cancel",
                requested_quantity=Decimal("1.00"),
                limit_price=Decimal("0.400000"),
                created_at=NOW,
            ))
            await session.commit()

        with pytest.raises(PaperPositionNotClosable, match="unresolved exit intent exit-restart"):
            await service.record_exit(
                account_id=account_id,
                decision_id="exit-intervening",
                position_id=position.position_id,
                quantity="1.00",
                minimum_price="0.400000",
            )
        assert market_data.fetch_quote.await_count == 1

        result = await service.record_exit(
            account_id=account_id,
            decision_id="exit-restart",
            position_id=position.position_id,
            quantity="1.00",
            minimum_price="0.400000",
        )
        assert result["status"] == "rejected"
        assert result["reason"] == "incomplete_exit_intent_rejected_after_restart"
        assert result["filled_quantity"] == "0.00"
        assert market_data.fetch_quote.await_count == 1
        assert (await service.list_positions(account_id=account_id))[0]["remaining_quantity"] == "4.00"
        assert (await service.list_accounts())[0]["journal_sequence"] == 2
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_large_position_accounting_is_independent_of_ambient_decimal_precision() -> None:
    engine, _factory, market_data, service = await _build("kalshi_paper_large_exact_position")
    try:
        huge_quantity = "123456789012345678901234567890.00"
        base_quote = _quote()
        book_evidence_json = _canonical_json({
            "ticker": "KXTEST-26",
            "yes_dollars": [["0.400000", huge_quantity]],
            "no_dollars": [["0.400000", huge_quantity]],
            "source_origin": "https://external-api.kalshi.com",
            "observed_at": base_quote.book.observed_at.isoformat(),
        })
        market_data.fetch_quote.return_value = replace(
            base_quote,
            book=PaperBook(
                ticker="KXTEST-26",
                yes_bids=(PaperBookLevel(price=Decimal("0.400000"), quantity=Decimal(huge_quantity)),),
                no_bids=(PaperBookLevel(price=Decimal("0.400000"), quantity=Decimal(huge_quantity)),),
                source_origin="https://external-api.kalshi.com",
                observed_at=base_quote.book.observed_at,
                fetched_at=base_quote.book.fetched_at,
                evidence_hash=_sha256(book_evidence_json),
                evidence_json=book_evidence_json,
            ),
        )
        account = await service.create_account(
            name="Large exact position",
            starting_cash="999999999999999999999999999999.000000000000000000",
        )
        account_id = str(account["id"])
        eligibility = await service.get_eligibility("opp-detection")
        with localcontext() as context:
            context.prec = 6
            entry = await service.record_decision(
                account_id=account_id,
                decision_id="entry-large-exact",
                action="execute",
                opportunity_id="opp-detection",
                opportunity_revision=str(eligibility["opportunity_revision"]),
                quantity=huge_quantity,
                limit_price="0.600000",
            )
            assert entry["filled_quantity"] == huge_quantity, entry["reason"]
            position_id = str((await service.list_positions(account_id=account_id))[0]["position_id"])
            exit_result = await service.record_exit(
                account_id=account_id,
                decision_id="exit-large-exact",
                position_id=position_id,
                quantity="1.00",
                minimum_price="0.400000",
            )
            position = (await service.list_positions(account_id=account_id))[0]
        assert exit_result["position_cost_basis"] == "0.600000000000000000"
        assert exit_result["realized_pnl"] == "-0.200000000000000000"
        assert position["remaining_quantity"] == "123456789012345678901234567889.00"
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_database_rejects_sell_fill_with_wrong_owned_outcome_side() -> None:
    engine, factory, _market_data, service = await _build("kalshi_paper_position_forged_sell_fill")
    try:
        account = await service.create_account(name="Forged SELL fill", starting_cash="100.00")
        account_id = str(account["id"])
        eligibility = await service.get_eligibility("opp-detection")
        await service.record_decision(
            account_id=account_id,
            decision_id="entry-for-fill-guard",
            action="execute",
            opportunity_id="opp-detection",
            opportunity_revision=str(eligibility["opportunity_revision"]),
            quantity="4.00",
            limit_price="0.600000",
        )
        position_id = str((await service.list_positions(account_id=account_id))[0]["position_id"])
        await service.record_exit(
            account_id=account_id,
            decision_id="exit-for-fill-guard",
            position_id=position_id,
            quantity="2.00",
            minimum_price="0.400000",
        )

        async with factory() as session:
            with pytest.raises(DBAPIError, match="contradicts decision evidence"):
                await session.execute(text(
                    "INSERT INTO kalshi_paper_fills "
                    "(account_id, decision_id, sequence, quantity, price, notional, fee, "
                    "source_bid_price, source_side, evidence_json, created_at) "
                    "SELECT account_id, decision_id, 2, quantity, price, notional, fee, "
                    "source_bid_price, 'no', evidence_json, created_at "
                    "FROM kalshi_paper_fills WHERE account_id=:account_id AND decision_id=:decision_id "
                    "AND sequence=1"
                ), {"account_id": account_id, "decision_id": "exit-for-fill-guard"})
                await session.commit()
            await session.rollback()

        async with factory() as session:
            with pytest.raises(DBAPIError, match="exceeds immutable source book depth"):
                await session.execute(text(
                    "INSERT INTO kalshi_paper_fills "
                    "(account_id, decision_id, sequence, quantity, price, notional, fee, "
                    "source_bid_price, source_side, evidence_json, created_at) "
                    "SELECT account_id, decision_id, 2, quantity, price, notional, fee, "
                    "source_bid_price, source_side, evidence_json, created_at "
                    "FROM kalshi_paper_fills WHERE account_id=:account_id AND decision_id=:decision_id "
                    "AND sequence=1"
                ), {"account_id": account_id, "decision_id": "exit-for-fill-guard"})
            await session.rollback()

        async with factory() as session:
            with pytest.raises(DBAPIError, match="exceeds immutable source book depth"):
                await session.execute(text(
                    "INSERT INTO kalshi_paper_fills "
                    "(account_id, decision_id, sequence, quantity, price, notional, fee, "
                    "source_bid_price, source_side, evidence_json, created_at) "
                    "SELECT account_id, decision_id, 2, quantity, 0.900000, quantity * 0.900000, fee, "
                    "0.900000, source_side, "
                    "(evidence_json::jsonb || "
                    "'{\"price\":\"0.900000\",\"source_bid_price\":\"0.900000\","
                    "\"notional\":\"1.80000000\"}'::jsonb)::text, created_at "
                    "FROM kalshi_paper_fills WHERE account_id=:account_id AND decision_id=:decision_id "
                    "AND sequence=1"
                ), {"account_id": account_id, "decision_id": "exit-for-fill-guard"})
            await session.rollback()

        position = (await service.list_positions(account_id=account_id))[0]
        assert position["sold_quantity"] == "2.00"
        assert position["remaining_quantity"] == "2.00"
    finally:
        await engine.dispose()
