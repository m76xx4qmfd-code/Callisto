from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from api import routes_kalshi_paper
from api.routes_kalshi_paper import (
    ConfirmedYesReversalCandleRequest,
    ConfirmedYesReversalEvaluationRequest,
    ConfirmedYesReversalMarketRequest,
    ConfirmedYesReversalScheduleRequest,
    ConfirmedYesReversalSettlementRequest,
    CreatePaperAccountRequest,
    PaperCancellationRequest,
    PaperDecisionRequest,
    PaperExitRequest,
    PaperTestRunRequest,
)
from services.kalshi_paper_test_trade_service import (
    KalshiPaperTestRunConflict,
    KalshiPaperTestRunNotFound,
    KalshiPaperTestRunTransition,
)
from services.kalshi_paper_service import (
    PaperAccountNotFound,
    PaperCancellationConflict,
    PaperDecisionConflict,
    PaperOpportunityIneligible,
    PaperOrderNotCancelable,
    PaperPositionNotClosable,
    PaperPositionNotFound,
)


def test_paper_decision_request_enforces_execute_and_pass_shapes() -> None:
    execute = PaperDecisionRequest(
        account_id="account",
        decision_id="decision",
        opportunity_id="opportunity",
        opportunity_revision="a" * 64,
        action="execute",
        quantity="1.00",
        limit_price="0.500000",
    )
    assert execute.quantity == "1.00"

    with pytest.raises(ValidationError):
        PaperDecisionRequest(
            account_id="account",
            decision_id="decision",
            opportunity_id="opportunity",
            opportunity_revision="a" * 64,
            action="execute",
        )
    with pytest.raises(ValidationError):
        PaperDecisionRequest(
            account_id="account",
            decision_id="decision",
            opportunity_id="opportunity",
            opportunity_revision="a" * 64,
            action="pass",
            quantity="1.00",
        )
    with pytest.raises(ValidationError):
        PaperDecisionRequest(
            account_id="account",
            decision_id="decision",
            opportunity_id="opportunity",
            opportunity_revision="not-a-hash",
            action="pass",
        )


def test_account_request_requires_exact_string_cash() -> None:
    assert CreatePaperAccountRequest(name="Paper", starting_cash="100.00").starting_cash == "100.00"
    with pytest.raises(ValidationError):
        CreatePaperAccountRequest(name="Paper", starting_cash=100)  # type: ignore[arg-type]


def test_paper_requests_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        CreatePaperAccountRequest.model_validate(
            {"name": "Paper", "starting_cash": "100.00", "unexpected": "value"}
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PaperDecisionRequest.model_validate(
            {
                "account_id": "account",
                "decision_id": "decision",
                "opportunity_id": "opportunity",
                "opportunity_revision": "a" * 64,
                "action": "pass",
                "unexpected": "value",
            }
        )


@pytest.mark.asyncio
async def test_decision_route_maps_conflict_and_ineligibility(monkeypatch) -> None:
    request = PaperDecisionRequest(
        account_id="account",
        decision_id="decision",
        opportunity_id="opportunity",
        opportunity_revision="a" * 64,
        action="pass",
    )
    record = AsyncMock(side_effect=PaperDecisionConflict("conflict"))
    monkeypatch.setattr(routes_kalshi_paper.paper_service, "record_decision", record)
    with pytest.raises(HTTPException) as conflict:
        await routes_kalshi_paper.record_paper_decision(request)
    assert conflict.value.status_code == 409

    record.side_effect = PaperOpportunityIneligible("ineligible")
    with pytest.raises(HTTPException) as ineligible:
        await routes_kalshi_paper.record_paper_decision(request)
    assert ineligible.value.status_code == 409

    record.side_effect = PaperAccountNotFound("missing")
    with pytest.raises(HTTPException) as missing:
        await routes_kalshi_paper.record_paper_decision(request)
    assert missing.value.status_code == 404


def test_gtc_and_cancellation_request_shapes_are_strict() -> None:
    decision = PaperDecisionRequest(
        account_id="account", decision_id="decision", opportunity_id="opportunity",
        opportunity_revision="a" * 64, action="execute", quantity="1.00",
        limit_price="0.500000", time_in_force="good_till_canceled",
    )
    assert decision.time_in_force == "good_till_canceled"
    cancellation = PaperCancellationRequest(account_id="account", order_id="order", cancellation_id="cancel")
    assert cancellation.order_id == "order"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PaperCancellationRequest.model_validate(
            {"account_id": "account", "order_id": "order", "cancellation_id": "cancel", "extra": True}
        )


@pytest.mark.asyncio
async def test_order_routes_delegate_and_map_terminal_conflicts(monkeypatch) -> None:
    list_orders = AsyncMock(return_value=[{"order_id": "order"}])
    monkeypatch.setattr(routes_kalshi_paper.paper_service, "list_orders", list_orders)
    assert await routes_kalshi_paper.list_paper_orders("account", 25) == [{"order_id": "order"}]
    list_orders.assert_awaited_once_with(account_id="account", limit=25)

    request = PaperCancellationRequest(account_id="account", order_id="order", cancellation_id="cancel")
    cancel = AsyncMock(side_effect=PaperOrderNotCancelable("terminal"))
    monkeypatch.setattr(routes_kalshi_paper.paper_service, "cancel_order", cancel)
    with pytest.raises(HTTPException) as terminal:
        await routes_kalshi_paper.cancel_paper_order(request)
    assert terminal.value.status_code == 409
    cancel.side_effect = PaperCancellationConflict("id conflict")
    with pytest.raises(HTTPException) as conflict:
        await routes_kalshi_paper.cancel_paper_order(request)
    assert conflict.value.status_code == 409


def test_exit_request_is_strict_and_requires_canonical_string_fields() -> None:
    request = PaperExitRequest(
        account_id="account",
        decision_id="exit-1",
        quantity="2.00",
        minimum_price="0.400000",
    )
    assert request.minimum_price == "0.400000"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PaperExitRequest.model_validate(
            {
                "account_id": "account",
                "decision_id": "exit-1",
                "quantity": "2.00",
                "minimum_price": "0.400000",
                "order_side": "sell",
            }
        )
    with pytest.raises(ValidationError):
        PaperExitRequest(
            account_id="account",
            decision_id="exit-1",
            quantity=2,  # type: ignore[arg-type]
            minimum_price="0.400000",
        )
    for quantity, minimum_price in (("2.0", "0.400000"), ("2.00", "0.4"), ("02.00", "0.400000")):
        with pytest.raises(ValidationError):
            PaperExitRequest(
                account_id="account",
                decision_id="exit-1",
                quantity=quantity,
                minimum_price=minimum_price,
            )


@pytest.mark.asyncio
async def test_position_routes_delegate_and_map_exit_boundaries(monkeypatch) -> None:
    list_positions = AsyncMock(return_value=[{"position_id": "position"}])
    monkeypatch.setattr(routes_kalshi_paper.paper_service, "list_positions", list_positions)
    assert await routes_kalshi_paper.list_paper_positions("account", 25) == [{"position_id": "position"}]
    list_positions.assert_awaited_once_with(account_id="account", limit=25)

    request = PaperExitRequest(
        account_id="account",
        decision_id="exit-1",
        quantity="2.00",
        minimum_price="0.400000",
    )
    record_exit = AsyncMock(side_effect=PaperPositionNotFound("missing"))
    monkeypatch.setattr(routes_kalshi_paper.paper_service, "record_exit", record_exit)
    with pytest.raises(HTTPException) as missing:
        await routes_kalshi_paper.exit_paper_position("position", request)
    assert missing.value.status_code == 404
    record_exit.side_effect = PaperPositionNotClosable("closed")
    with pytest.raises(HTTPException) as terminal:
        await routes_kalshi_paper.exit_paper_position("position", request)
    assert terminal.value.status_code == 409


def test_paper_test_run_request_is_strict_canonical_and_orders_thresholds() -> None:
    request = PaperTestRunRequest(
        run_id="run-1", account_id="account", opportunity_id="opportunity",
        opportunity_revision="a" * 64, quantity="2.00", entry_limit_price="0.600000",
        take_profit_price="0.700000", stop_loss_price="0.400000",
        stop_loss_minimum_price="0.300000",
    )
    assert request.quantity == "2.00"
    invalid_payloads = (
        {**request.model_dump(), "extra": True},
        {**request.model_dump(), "quantity": 2},
        {**request.model_dump(), "quantity": "2.0"},
        {**request.model_dump(), "take_profit_price": "0.7"},
        {**request.model_dump(), "stop_loss_minimum_price": "0.500000"},
        {**request.model_dump(), "entry_limit_price": "0.800000"},
        {**request.model_dump(), "entry_limit_price": "0.300000"},
        {**request.model_dump(), "take_profit_price": "1.000000"},
    )
    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            PaperTestRunRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_paper_test_run_routes_delegate_and_map_domain_errors(monkeypatch) -> None:
    request = PaperTestRunRequest(
        run_id="run-1", account_id="account", opportunity_id="opportunity",
        opportunity_revision="a" * 64, quantity="2.00", entry_limit_price="0.600000",
        take_profit_price="0.700000", stop_loss_price="0.400000",
        stop_loss_minimum_price="0.300000",
    )
    start = AsyncMock(return_value={"run": {"run_id": "run-1"}, "events": []})
    monkeypatch.setattr(routes_kalshi_paper.paper_test_trade_service, "start_run", start)
    assert (await routes_kalshi_paper.start_paper_test_run(request))["run"]["run_id"] == "run-1"
    start.assert_awaited_once_with(**request.model_dump())

    get_run = AsyncMock(side_effect=KalshiPaperTestRunNotFound("missing"))
    monkeypatch.setattr(routes_kalshi_paper.paper_test_trade_service, "get_run", get_run)
    with pytest.raises(HTTPException) as missing:
        await routes_kalshi_paper.get_paper_test_run("missing")
    assert missing.value.status_code == 404

    start.side_effect = KalshiPaperTestRunConflict("conflict")
    with pytest.raises(HTTPException) as conflict:
        await routes_kalshi_paper.start_paper_test_run(request)
    assert conflict.value.status_code == 409

    pause = AsyncMock(side_effect=KalshiPaperTestRunTransition("illegal"))
    monkeypatch.setattr(routes_kalshi_paper.paper_test_trade_service, "pause_run", pause)
    with pytest.raises(HTTPException) as illegal:
        await routes_kalshi_paper.pause_paper_test_run("run-1")
    assert illegal.value.status_code == 409


@pytest.mark.asyncio
async def test_paper_test_run_list_and_controls_delegate(monkeypatch) -> None:
    service = routes_kalshi_paper.paper_test_trade_service
    list_runs = AsyncMock(return_value=[])
    pause = AsyncMock(return_value={"run": {"status": "paused"}, "events": []})
    resume = AsyncMock(return_value={"run": {"status": "monitoring"}, "events": []})
    stop = AsyncMock(return_value={"run": {"status": "stopped"}, "events": []})
    monkeypatch.setattr(service, "list_runs", list_runs)
    monkeypatch.setattr(service, "pause_run", pause)
    monkeypatch.setattr(service, "resume_run", resume)
    monkeypatch.setattr(service, "stop_run", stop)

    assert await routes_kalshi_paper.list_paper_test_runs("account") == []
    assert (await routes_kalshi_paper.pause_paper_test_run("run"))["run"]["status"] == "paused"
    assert (await routes_kalshi_paper.resume_paper_test_run("run"))["run"]["status"] == "monitoring"
    assert (await routes_kalshi_paper.stop_paper_test_run("run"))["run"]["status"] == "stopped"
    list_runs.assert_awaited_once_with("account")
    pause.assert_awaited_once_with("run")
    resume.assert_awaited_once_with("run")
    stop.assert_awaited_once_with("run")


@pytest.mark.asyncio
async def test_paper_test_run_route_maps_retryable_database_error(monkeypatch) -> None:
    request = PaperTestRunRequest(
        run_id="run-1", account_id="account", opportunity_id="opportunity",
        opportunity_revision="a" * 64, quantity="2.00", entry_limit_price="0.600000",
        take_profit_price="0.700000", stop_loss_price="0.400000",
        stop_loss_minimum_price="0.300000",
    )
    database_error = OperationalError("SELECT", {}, RuntimeError("serialization failure"))
    monkeypatch.setattr(
        routes_kalshi_paper.paper_test_trade_service,
        "start_run",
        AsyncMock(side_effect=database_error),
    )
    monkeypatch.setattr(routes_kalshi_paper, "is_retryable_db_error", lambda exc: True)
    with pytest.raises(HTTPException) as retryable:
        await routes_kalshi_paper.start_paper_test_run(request)
    assert retryable.value.status_code == 503
    assert "retry" in str(retryable.value.detail).lower()


def _confirmed_yes_reversal_request() -> ConfirmedYesReversalEvaluationRequest:
    call = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)
    candles = [
        ConfirmedYesReversalCandleRequest(
            end_time=call - timedelta(hours=60), yes_bid="0.490000", yes_ask="0.510000", volume="1.00"
        ),
        ConfirmedYesReversalCandleRequest(
            end_time=call - timedelta(hours=48), yes_bid="0.240000", yes_ask="0.260000", volume="5.00"
        ),
        ConfirmedYesReversalCandleRequest(
            end_time=call - timedelta(hours=47), yes_bid="0.260000", yes_ask="0.280000", volume="3.00"
        ),
        ConfirmedYesReversalCandleRequest(
            end_time=call - timedelta(hours=1), yes_bid="0.400000", yes_ask="0.420000", volume="2.00"
        ),
    ]
    return ConfirmedYesReversalEvaluationRequest(
        schedule=ConfirmedYesReversalScheduleRequest(
            call_start=call,
            published_at=call - timedelta(days=30),
            observed_at=call - timedelta(days=20),
            source_url="https://investor.example.test/call",
            source_content_sha256="a" * 64,
        ),
        markets=[
            ConfirmedYesReversalMarketRequest(
                ticker="KXEARNINGSMENTIONTEST-26SEP01-WORD",
                market_open=call - timedelta(days=40),
                market_close=call + timedelta(hours=2),
                settlement=ConfirmedYesReversalSettlementRequest(
                    result="yes",
                    observed_at=call + timedelta(hours=3),
                    source_url="https://external-api.kalshi.com/trade-api/v2/markets/test",
                    evidence_sha256="b" * 64,
                    final=True,
                ),
                candles=candles,
            )
        ],
    )


def test_confirmed_yes_reversal_request_is_strict_and_has_no_execution_fields() -> None:
    request = _confirmed_yes_reversal_request()
    assert request.markets[0].candles[0].yes_bid == "0.490000"
    payload = request.model_dump()
    payload["account_id"] = "must-not-be-accepted"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ConfirmedYesReversalEvaluationRequest.model_validate(payload)


def test_confirmed_yes_reversal_specification_route_is_explicitly_paper_only() -> None:
    result = routes_kalshi_paper.get_confirmed_yes_reversal_specification()
    assert result["strategy_id"] == "strategy_1_max_roi_confirmed_yes_reversal"
    assert result["version"] == 1
    assert result["execution_authority"] == "paper_only"
    assert result["live_exchange_writes"] == "prohibited"


def test_confirmed_yes_reversal_evaluation_route_returns_selected_paper_evidence() -> None:
    result = routes_kalshi_paper.evaluate_confirmed_yes_reversal(_confirmed_yes_reversal_request())
    assert result["schema_version"] == "confirmed-yes-reversal-evaluation/v1"
    assert len(result["request_sha256"]) == 64
    assert result["selected_count"] == 1
    assert result["abstained_count"] == 0
    assert result["decisions"][0]["entry_price"] == "0.280000"
    assert result["decisions"][0]["execution_authority"] == "paper_only"


def test_confirmed_yes_reversal_evaluation_cannot_reach_account_or_market_services(monkeypatch) -> None:
    record = AsyncMock(side_effect=AssertionError("paper account mutation must be unreachable"))
    start = AsyncMock(side_effect=AssertionError("paper run mutation must be unreachable"))
    fetch = AsyncMock(side_effect=AssertionError("live market fetch must be unreachable"))
    monkeypatch.setattr(routes_kalshi_paper.paper_service, "record_decision", record)
    monkeypatch.setattr(routes_kalshi_paper.paper_test_trade_service, "start_run", start)
    monkeypatch.setattr(routes_kalshi_paper.paper_result_service, "observe_position_result", fetch)

    result = routes_kalshi_paper.evaluate_confirmed_yes_reversal(
        _confirmed_yes_reversal_request()
    )

    assert result["selected_count"] == 1
    record.assert_not_awaited()
    start.assert_not_awaited()
    fetch.assert_not_awaited()


def test_confirmed_yes_reversal_routes_are_registered_on_paper_router() -> None:
    paths = {getattr(route, "path", None) for route in routes_kalshi_paper.router.routes}
    assert "/strategies/confirmed-yes-reversal/specification" in paths
    assert "/strategies/confirmed-yes-reversal/evaluate" in paths
