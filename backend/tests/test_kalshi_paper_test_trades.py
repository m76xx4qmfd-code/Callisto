from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from models.database import (
    KalshiPaperPosition,
    KalshiPaperTestEvent,
    KalshiPaperTestRun,
)
from services.kalshi_paper_service import _canonical_json, _sha256
from services.kalshi_paper_test_trade_service import (
    KalshiPaperTestRunConflict,
    KalshiPaperTestTradeService,
)
from tests.test_kalshi_paper_ledger import _build, _quote


NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)


def _quote_with_yes(*levels: tuple[str, str]):
    quote = _quote()
    evidence = _canonical_json(
        {
            "ticker": quote.book.ticker,
            "yes_dollars": [[price, quantity] for price, quantity in levels],
            "no_dollars": [["0.400000", "3.00"], ["0.450000", "2.00"]],
            "source_origin": quote.book.source_origin,
            "observed_at": quote.book.observed_at.isoformat(),
        }
    )
    return replace(
        quote,
        book=replace(
            quote.book,
            yes_bids=tuple(
                replace(quote.book.yes_bids[0], price=Decimal(price), quantity=Decimal(quantity))
                for price, quantity in levels
            ),
            evidence_json=evidence,
            evidence_hash=_sha256(evidence),
        ),
    )


async def _test_service(name: str):
    engine, factory, market_data, paper = await _build(name)
    service = KalshiPaperTestTradeService(
        session_factory=factory,
        database_engine=engine,
        paper_service=paper,
        market_data_client=market_data,
        now=lambda: NOW,
    )
    account = await paper.create_account(name=name, starting_cash="100.00")
    eligibility = await paper.get_eligibility("opp-detection")
    request = {
        "run_id": f"run-{name}",
        "account_id": str(account["id"]),
        "opportunity_id": "opp-detection",
        "opportunity_revision": str(eligibility["opportunity_revision"]),
        "quantity": "4.00",
        "entry_limit_price": "0.600000",
        "take_profit_price": "0.700000",
        "stop_loss_price": "0.400000",
        "stop_loss_minimum_price": "0.300000",
    }
    return engine, factory, market_data, paper, service, request


@pytest.mark.db
@pytest.mark.asyncio
async def test_start_binds_exact_entry_position_and_replays_without_second_entry_read() -> None:
    engine, factory, market_data, _paper, service, request = await _test_service("test_trade_start")
    try:
        started = await service.start_run(**request)
        assert started["run"]["status"] == "monitoring"
        assert started["run"]["entry_decision_id"] == f"paper-test-entry:{request['run_id']}"
        assert started["run"]["position_id"].startswith("paper-position:")
        assert [event["event_type"] for event in started["events"]] == ["started", "entry_filled"]
        assert market_data.fetch_quote.await_count == 1

        replay = await service.start_run(**request)
        assert replay == started
        assert market_data.fetch_quote.await_count == 1

        conflicting = dict(request, quantity="5.00")
        with pytest.raises(KalshiPaperTestRunConflict):
            await service.start_run(**conflicting)
        assert market_data.fetch_quote.await_count == 1

        async with factory() as session:
            position = await session.scalar(select(KalshiPaperPosition))
            assert position is not None
            assert started["run"]["position_id"] == position.position_id
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_no_fill_entry_is_terminal_and_does_not_monitor() -> None:
    engine, _factory, market_data, _paper, service, request = await _test_service("test_trade_no_entry")
    try:
        quote = _quote()
        market_data.fetch_quote.return_value = replace(
            quote,
            book=replace(quote.book, no_bids=()),
        )
        result = await service.start_run(**request)
        assert result["run"]["status"] == "entry_unfilled"
        assert result["run"]["position_id"] is None
        assert result["events"][-1]["event_type"] == "entry_unfilled"
        assert await service.tick_run(str(request["run_id"])) == result
        assert market_data.fetch_quote.await_count == 1
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_hold_no_bid_and_take_profit_exit_use_fresh_held_outcome_bid() -> None:
    engine, factory, market_data, _paper, service, request = await _test_service("test_trade_tick")
    try:
        await service.start_run(**request)
        market_data.fetch_quote.return_value = _quote_with_yes()
        no_bid = await service.tick_run(str(request["run_id"]))
        assert no_bid["events"][-1]["event_type"] == "no_bid"

        market_data.fetch_quote.return_value = _quote_with_yes(("0.500000", "3.00"))
        held = await service.tick_run(str(request["run_id"]))
        assert held["events"][-1]["event_type"] == "hold"
        assert held["events"][-1]["best_bid"] == "0.500000"

        market_data.fetch_quote.return_value = _quote_with_yes(("0.700000", "4.00"))
        closed = await service.tick_run(str(request["run_id"]))
        assert closed["run"]["status"] == "completed"
        assert [event["event_type"] for event in closed["events"][-3:]] == [
            "take_profit_triggered",
            "exit_filled",
            "completed",
        ]
        trigger = closed["events"][-3]
        assert trigger["exit_decision_id"] == f"paper-test-exit:{request['run_id']}:{trigger['sequence']}"
        assert closed["run"]["remaining_quantity"] == "0.00"
        reads = market_data.fetch_quote.await_count
        assert await service.tick_run(str(request["run_id"])) == closed
        assert market_data.fetch_quote.await_count == reads

        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(KalshiPaperTestEvent)) == 7
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_stop_loss_gap_uses_explicit_floor_and_partial_exit_returns_to_monitoring() -> None:
    engine, _factory, market_data, paper, service, request = await _test_service("test_trade_stop")
    try:
        await service.start_run(**request)
        market_data.fetch_quote.return_value = _quote_with_yes(("0.350000", "2.00"))
        result = await service.tick_run(str(request["run_id"]))
        assert result["run"]["status"] == "monitoring"
        assert result["events"][-2]["event_type"] == "stop_loss_triggered"
        assert result["events"][-1]["event_type"] == "exit_partial"
        assert result["run"]["remaining_quantity"] == "2.00"
        decision = (await paper.list_decisions(account_id=str(request["account_id"])))[0]
        assert decision["limit_price"] == "0.300000"
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_pause_resume_stop_are_serialized_and_stop_never_closes_position() -> None:
    engine, _factory, market_data, paper, service, request = await _test_service("test_trade_control")
    try:
        await service.start_run(**request)
        paused = await service.pause_run(str(request["run_id"]))
        assert paused["run"]["status"] == "paused"
        reads = market_data.fetch_quote.await_count
        await service.tick_run(str(request["run_id"]))
        assert market_data.fetch_quote.await_count == reads
        resumed = await service.resume_run(str(request["run_id"]))
        assert resumed["run"]["status"] == "monitoring"
        stopped = await service.stop_run(str(request["run_id"]))
        assert stopped["run"]["status"] == "stopped"
        position = (await paper.list_positions(account_id=str(request["account_id"])))[0]
        assert position["remaining_quantity"] == "4.00"
        assert [event["event_type"] for event in stopped["events"][-3:]] == ["paused", "resumed", "stopped"]
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_database_rejects_immutable_run_facts_event_mutation_and_cross_account_causality() -> None:
    engine, factory, _market_data, _paper, service, request = await _test_service("test_trade_sql_guards")
    try:
        await service.start_run(**request)
        async with factory() as session:
            with pytest.raises(DBAPIError, match="immutable request facts"):
                await session.execute(
                    text("UPDATE kalshi_paper_test_runs SET quantity=5 WHERE run_id=:run_id"),
                    {"run_id": request["run_id"]},
                )
            await session.rollback()
            with pytest.raises(DBAPIError, match="immutable"):
                await session.execute(
                    text("DELETE FROM kalshi_paper_test_events WHERE run_id=:run_id"),
                    {"run_id": request["run_id"]},
                )
            await session.rollback()
            event = await session.scalar(
                select(KalshiPaperTestEvent).where(KalshiPaperTestEvent.run_id == request["run_id"]).limit(1)
            )
            assert event is not None
            with pytest.raises(DBAPIError, match="hash"):
                event.quote_evidence_json = '{"ticker":"forged"}'
                event.quote_evidence_hash = "0" * 64
                session.add(event)
                await session.commit()
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_take_profit_and_stop_loss_exact_boundaries_and_exit_no_fill() -> None:
    engine, _factory, market_data, _paper, service, request = await _test_service("test_trade_boundaries")
    try:
        await service.start_run(**request)
        market_data.fetch_quote.side_effect = [
            _quote_with_yes(("0.700000", "4.00")),
            _quote_with_yes(("0.690000", "4.00")),
        ]
        no_fill = await service.tick_run(str(request["run_id"]))
        assert no_fill["run"]["status"] == "monitoring"
        assert [event["event_type"] for event in no_fill["events"][-2:]] == [
            "take_profit_triggered", "exit_no_fill",
        ]

        market_data.fetch_quote.side_effect = [
            _quote_with_yes(("0.400000", "4.00")),
            _quote_with_yes(("0.400000", "4.00")),
        ]
        closed = await service.tick_run(str(request["run_id"]))
        assert closed["run"]["status"] == "completed"
        assert closed["events"][-3]["event_type"] == "stop_loss_triggered"
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_start_recovers_after_entry_commit_response_loss() -> None:
    engine, factory, market_data, paper, service, request = await _test_service("test_trade_start_restart")
    original = paper.record_decision
    lost_once = False

    async def commit_then_lose(**facts):
        nonlocal lost_once
        decision = await original(**facts)
        if not lost_once:
            lost_once = True
            raise TimeoutError("simulated response loss")
        return decision

    paper.record_decision = commit_then_lose
    try:
        with pytest.raises(TimeoutError, match="response loss"):
            await service.start_run(**request)
        async with factory() as session:
            run = await session.get(KalshiPaperTestRun, request["run_id"])
            assert run is not None and run.status == "starting"
        recovered = await service.start_run(**request)
        assert recovered["run"]["status"] == "monitoring"
        assert [event["event_type"] for event in recovered["events"]] == ["started", "entry_filled"]
        assert market_data.fetch_quote.await_count == 1
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_exit_recovers_after_decision_commit_response_loss() -> None:
    engine, _factory, market_data, paper, service, request = await _test_service("test_trade_exit_restart")
    original = paper.record_exit
    lost_once = False

    async def commit_then_lose(**facts):
        nonlocal lost_once
        decision = await original(**facts)
        if not lost_once:
            lost_once = True
            raise TimeoutError("simulated response loss")
        return decision

    paper.record_exit = commit_then_lose
    try:
        await service.start_run(**request)
        market_data.fetch_quote.return_value = _quote_with_yes(("0.700000", "4.00"))
        with pytest.raises(TimeoutError, match="response loss"):
            await service.tick_run(str(request["run_id"]))
        reads = market_data.fetch_quote.await_count
        recovered = await service.tick_run(str(request["run_id"]))
        assert recovered["run"]["status"] == "completed"
        assert [event["event_type"] for event in recovered["events"][-3:]] == [
            "take_profit_triggered", "exit_filled", "completed",
        ]
        assert market_data.fetch_quote.await_count == reads
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_two_worker_ticks_serialize_to_one_exit_attempt() -> None:
    engine, _factory, market_data, paper, service, request = await _test_service("test_trade_worker_race")
    try:
        await service.start_run(**request)
        market_data.fetch_quote.return_value = _quote_with_yes(("0.700000", "4.00"))
        first, second = await asyncio.gather(
            service.tick_run(str(request["run_id"])),
            service.tick_run(str(request["run_id"])),
        )
        assert first["run"]["status"] == second["run"]["status"] == "completed"
        exits = [
            decision for decision in await paper.list_decisions(account_id=str(request["account_id"]))
            if decision["order_side"] == "sell"
        ]
        assert len(exits) == 1
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_manual_close_winning_during_worker_get_completes_without_oversell() -> None:
    engine, _factory, market_data, paper, service, request = await _test_service("test_trade_manual_race")
    entered_get = asyncio.Event()
    release_get = asyncio.Event()
    calls = 0

    async def racing_quote(_ticker: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered_get.set()
            await release_get.wait()
        return _quote_with_yes(("0.700000", "4.00"))

    try:
        started = await service.start_run(**request)
        market_data.fetch_quote.side_effect = racing_quote
        worker = asyncio.create_task(service.tick_run(str(request["run_id"])))
        await entered_get.wait()
        await paper.record_exit(
            account_id=str(request["account_id"]),
            decision_id="manual-close",
            position_id=str(started["run"]["position_id"]),
            quantity="4.00",
            minimum_price="0.300000",
        )
        release_get.set()
        completed = await worker
        assert completed["run"]["status"] == "completed"
        assert completed["run"]["remaining_quantity"] == "0.00"
        assert completed["events"][-1]["reason"] == "position_closed_by_competing_path"
    finally:
        release_get.set()
        await engine.dispose()


def test_service_import_boundary_is_paper_only() -> None:
    from pathlib import Path

    source = Path(__file__).parents[1].joinpath("services", "kalshi_paper_test_trade_service.py").read_text()
    forbidden = ("kalshi_client", "kalshi_auth", "live_execution", "shadow", "simulation", "trading_proxy")
    assert not [module for module in forbidden if module in source]


@pytest.mark.db
@pytest.mark.asyncio
async def test_concurrent_identical_start_replays_without_raw_uniqueness_error() -> None:
    engine, _factory, market_data, _paper, service, request = await _test_service("test_trade_start_race")
    try:
        first, second = await asyncio.gather(
            service.start_run(**request),
            service.start_run(**request),
        )
        assert first == second
        assert first["run"]["status"] == "monitoring"
        assert market_data.fetch_quote.await_count == 1
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_partial_exit_response_loss_replays_frozen_trigger_quantity() -> None:
    engine, _factory, market_data, paper, service, request = await _test_service("test_trade_partial_restart")
    original = paper.record_exit
    lost_once = False

    async def commit_then_lose(**facts):
        nonlocal lost_once
        decision = await original(**facts)
        if not lost_once:
            lost_once = True
            raise TimeoutError("simulated partial response loss")
        return decision

    paper.record_exit = commit_then_lose
    try:
        await service.start_run(**request)
        market_data.fetch_quote.return_value = _quote_with_yes(("0.700000", "2.00"))
        with pytest.raises(TimeoutError, match="partial response loss"):
            await service.tick_run(str(request["run_id"]))
        reads = market_data.fetch_quote.await_count
        recovered = await service.tick_run(str(request["run_id"]))
        assert recovered["run"]["status"] == "monitoring"
        assert recovered["run"]["remaining_quantity"] == "2.00"
        assert recovered["events"][-1]["event_type"] == "exit_partial"
        assert market_data.fetch_quote.await_count == reads
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_transient_quote_error_writes_no_observation_and_identity_mismatch_blocks() -> None:
    engine, _factory, market_data, _paper, service, request = await _test_service("test_trade_quote_failures")
    try:
        started = await service.start_run(**request)
        market_data.fetch_quote.side_effect = TimeoutError("temporary market data failure")
        with pytest.raises(TimeoutError, match="temporary"):
            await service.tick_run(str(request["run_id"]))
        unchanged = await service.get_run(str(request["run_id"]))
        assert unchanged == started

        wrong = _quote_with_yes(("0.500000", "4.00"))
        market_data.fetch_quote.side_effect = None
        market_data.fetch_quote.return_value = replace(
            wrong,
            market=replace(wrong.market, ticker="WRONG"),
            book=replace(wrong.book, ticker="WRONG"),
        )
        blocked = await service.tick_run(str(request["run_id"]))
        assert blocked["run"]["status"] == "blocked"
        assert blocked["events"][-1]["event_type"] == "blocked"
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_direct_sql_rejects_missing_quote_evidence_trigger_arithmetic_and_cross_account() -> None:
    engine, factory, _market_data, _paper, service, request = await _test_service("test_trade_sql_shapes")
    quote = _quote_with_yes(("0.500000", "4.00"))
    common = {
        "run_id": request["run_id"],
        "account_id": request["account_id"],
        "observed": quote.book.observed_at.replace(tzinfo=None),
        "evidence_hash": quote.book.evidence_hash,
        "evidence_json": quote.book.evidence_json,
        "created": NOW.replace(tzinfo=None),
        "exit_decision_id": f"paper-test-exit:{request['run_id']}:3",
    }
    try:
        await service.start_run(**request)
        async with factory() as session:
            await session.execute(
                text("UPDATE kalshi_paper_test_runs SET next_event_sequence=4 WHERE run_id=:run_id"), common
            )
            await session.execute(
                text(
                    "INSERT INTO kalshi_paper_test_events "
                    "(run_id,sequence,account_id,event_type,best_bid,remaining_quantity,reason,created_at) "
                    "VALUES (:run_id,3,:account_id,'hold',0.5,4,'forged',:created)"
                ),
                common,
            )
            with pytest.raises(DBAPIError, match="evidence is incomplete"):
                await session.commit()
            await session.rollback()

            await session.execute(
                text("UPDATE kalshi_paper_test_runs SET next_event_sequence=4 WHERE run_id=:run_id"), common
            )
            await session.execute(
                text(
                    "INSERT INTO kalshi_paper_test_events "
                    "(run_id,sequence,account_id,event_type,best_bid,trigger_price,exit_decision_id,"
                    "market_observed_at,book_observed_at,quote_evidence_hash,quote_evidence_json,"
                    "remaining_quantity,reason,created_at) VALUES "
                    "(:run_id,3,:account_id,'take_profit_triggered',0.5,0.7,"
                    ":exit_decision_id,:observed,:observed,:evidence_hash,:evidence_json,"
                    "4,'forged',:created)"
                ),
                common,
            )
            with pytest.raises(DBAPIError, match="take-profit trigger arithmetic"):
                await session.commit()
            await session.rollback()

            wrong_account = dict(common, account_id="different-account")
            await session.execute(
                text("UPDATE kalshi_paper_test_runs SET next_event_sequence=4 WHERE run_id=:run_id"), wrong_account
            )
            await session.execute(
                text(
                    "INSERT INTO kalshi_paper_test_events "
                    "(run_id,sequence,account_id,event_type,reason,created_at) "
                    "VALUES (:run_id,3,:account_id,'paused','forged',:created)"
                ),
                wrong_account,
            )
            with pytest.raises(DBAPIError, match="sequence or account"):
                await session.commit()
            await session.rollback()
    finally:
        await engine.dispose()
