from __future__ import annotations

from typing import Literal, Self

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator
from sqlalchemy.exc import IntegrityError, OperationalError

from models.database import AsyncSessionLocal, async_engine
from services.kalshi_paper_service import (
    KalshiPaperService,
    PaperAccountNotFound,
    PaperCancellationConflict,
    PaperDecisionConflict,
    PaperOpportunityIneligible,
    PaperOpportunityNotFound,
    PaperOrderNotCancelable,
    PaperPositionNotClosable,
    PaperPositionNotFound,
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
    time_in_force: Literal["immediate_or_cancel", "good_till_canceled"] = "immediate_or_cancel"

    @model_validator(mode="after")
    def validate_action_shape(self) -> Self:
        if self.action == "execute" and (self.quantity is None or self.limit_price is None):
            raise ValueError("execute decisions require quantity and limit_price")
        if self.action == "pass" and (self.quantity is not None or self.limit_price is not None):
            raise ValueError("pass decisions cannot include quantity or limit_price")
        if self.action == "pass" and self.time_in_force != "immediate_or_cancel":
            raise ValueError("pass decisions cannot be good_till_canceled")
        return self


class PaperCancellationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: StrictStr = Field(..., min_length=1, max_length=100)
    order_id: StrictStr = Field(..., min_length=1, max_length=200)
    cancellation_id: StrictStr = Field(..., min_length=1, max_length=200)


class PaperExitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: StrictStr = Field(..., min_length=1, max_length=100)
    decision_id: StrictStr = Field(..., min_length=1, max_length=200)
    quantity: StrictStr = Field(..., pattern=r"^(?:0|[1-9][0-9]*)\.[0-9]{2}$", max_length=80)
    minimum_price: StrictStr = Field(..., pattern=r"^(?:0|[1-9][0-9]*)\.[0-9]{6}$", max_length=80)


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


@router.get("/accounts/{account_id}/orders")
async def list_paper_orders(account_id: str, limit: int = Query(default=100, ge=1, le=500)):
    try:
        return await paper_service.list_orders(account_id=account_id, limit=limit)
    except PaperAccountNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OperationalError as exc:
        _handle_db_error(exc)


@router.get("/accounts/{account_id}/positions")
async def list_paper_positions(account_id: str, limit: int = Query(default=100, ge=1, le=500)):
    try:
        return await paper_service.list_positions(account_id=account_id, limit=limit)
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
            time_in_force=request.time_in_force,
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


@router.post("/cancellations")
async def cancel_paper_order(request: PaperCancellationRequest):
    try:
        return await paper_service.cancel_order(
            account_id=request.account_id,
            order_id=request.order_id,
            cancellation_id=request.cancellation_id,
        )
    except PaperAccountNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PaperCancellationConflict, PaperOrderNotCancelable) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OperationalError as exc:
        _handle_db_error(exc)


@router.post("/positions/{position_id}/exits")
async def exit_paper_position(position_id: str, request: PaperExitRequest):
    try:
        return await paper_service.record_exit(
            account_id=request.account_id,
            decision_id=request.decision_id,
            position_id=position_id,
            quantity=request.quantity,
            minimum_price=request.minimum_price,
        )
    except (PaperAccountNotFound, PaperPositionNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PaperDecisionConflict, PaperPositionNotClosable) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OperationalError as exc:
        _handle_db_error(exc)
