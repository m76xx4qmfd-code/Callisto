from __future__ import annotations

from typing import Literal, Self

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator
from sqlalchemy.exc import IntegrityError, OperationalError

from models.database import AsyncSessionLocal, async_engine
from services.kalshi_paper_service import (
    KalshiPaperService,
    PaperAccountNotFound,
    PaperDecisionConflict,
    PaperOpportunityIneligible,
    PaperOpportunityNotFound,
)
from utils.retry import is_retryable_db_error

router = APIRouter()
paper_service = KalshiPaperService(session_factory=AsyncSessionLocal, database_engine=async_engine)


class CreatePaperAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: StrictStr = Field(..., min_length=1, max_length=100)
    starting_cash: StrictStr = Field(..., min_length=1, max_length=80)


class PaperDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: StrictStr = Field(..., min_length=1, max_length=100)
    decision_id: StrictStr = Field(..., min_length=1, max_length=200)
    opportunity_id: StrictStr = Field(..., min_length=1, max_length=200)
    opportunity_revision: StrictStr = Field(..., pattern=r"^[0-9a-f]{64}$")
    action: Literal["execute", "pass"]
    quantity: StrictStr | None = Field(default=None, min_length=1, max_length=80)
    limit_price: StrictStr | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_action_shape(self) -> Self:
        if self.action == "execute" and (self.quantity is None or self.limit_price is None):
            raise ValueError("execute decisions require quantity and limit_price")
        if self.action == "pass" and (self.quantity is not None or self.limit_price is not None):
            raise ValueError("pass decisions cannot include quantity or limit_price")
        return self


def _handle_db_error(exc: OperationalError) -> None:
    if is_retryable_db_error(exc):
        raise HTTPException(status_code=503, detail="Database is busy; please retry.") from exc
    raise exc


@router.post("/accounts")
async def create_paper_account(request: CreatePaperAccountRequest):
    try:
        return await paper_service.create_account(name=request.name, starting_cash=request.starting_cash)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Paper account name already exists.") from exc
    except OperationalError as exc:
        _handle_db_error(exc)


@router.get("/accounts")
async def list_paper_accounts():
    try:
        return await paper_service.list_accounts()
    except OperationalError as exc:
        _handle_db_error(exc)


@router.get("/accounts/{account_id}/decisions")
async def list_paper_decisions(account_id: str, limit: int = Query(default=50, ge=1, le=500)):
    try:
        return await paper_service.list_decisions(account_id=account_id, limit=limit)
    except PaperAccountNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OperationalError as exc:
        _handle_db_error(exc)


@router.get("/opportunities/{opportunity_id}/eligibility")
async def get_paper_eligibility(opportunity_id: str):
    try:
        return await paper_service.get_eligibility(opportunity_id)
    except PaperOpportunityNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PaperOpportunityIneligible as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OperationalError as exc:
        _handle_db_error(exc)


@router.post("/decisions")
async def record_paper_decision(request: PaperDecisionRequest):
    try:
        return await paper_service.record_decision(
            account_id=request.account_id,
            decision_id=request.decision_id,
            opportunity_id=request.opportunity_id,
            opportunity_revision=request.opportunity_revision,
            action=request.action,
            quantity=request.quantity,
            limit_price=request.limit_price,
        )
    except PaperAccountNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PaperOpportunityNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PaperDecisionConflict, PaperOpportunityIneligible) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OperationalError as exc:
        _handle_db_error(exc)
