from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api import routes_kalshi_paper
from api.routes_kalshi_paper import (
    CreatePaperAccountRequest,
    PaperCancellationRequest,
    PaperDecisionRequest,
    PaperExitRequest,
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
