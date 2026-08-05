from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api import routes_kalshi_paper
from api.routes_kalshi_paper import CreatePaperAccountRequest, PaperDecisionRequest
from services.kalshi_paper_service import PaperAccountNotFound, PaperDecisionConflict, PaperOpportunityIneligible


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
