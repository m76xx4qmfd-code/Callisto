from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.test_kalshi_paper_test_trades import NOW, _quote_with_yes, _test_service


@pytest.mark.db
@pytest.mark.asyncio
async def test_run_projection_rejects_same_account_position_rebind_and_terminal_without_event() -> None:
    engine, factory, _market_data, _paper, service, request = await _test_service("test_trade_projection_guard")
    try:
        first = await service.start_run(**request)
        second_request = dict(request, run_id=f"{request['run_id']}-second")
        second = await service.start_run(**second_request)
        async with factory() as session:
            await session.execute(
                text("UPDATE kalshi_paper_test_runs SET position_id=:position_id WHERE run_id=:run_id"),
                {"run_id": request["run_id"], "position_id": second["run"]["position_id"]},
            )
            with pytest.raises(DBAPIError, match="position causality"):
                await session.commit()
            await session.rollback()

            await session.execute(
                text("UPDATE kalshi_paper_test_runs SET status='completed' WHERE run_id=:run_id"),
                {"run_id": request["run_id"]},
            )
            with pytest.raises(DBAPIError, match="lifecycle event|status transition"):
                await session.commit()
            await session.rollback()

        assert (await service.get_run(str(request["run_id"]))) == first
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_exit_bypass_and_noncanonical_quote_evidence_are_rejected() -> None:
    engine, factory, _market_data, _paper, service, request = await _test_service("test_trade_exit_bypass")
    try:
        started = await service.start_run(**request)
        run_id = str(request["run_id"])
        account_id = str(request["account_id"])
        position_id = str(started["run"]["position_id"])
        async with factory() as session:
            await session.execute(
                text("UPDATE kalshi_paper_test_runs SET next_event_sequence=4 WHERE run_id=:run_id"),
                {"run_id": run_id},
            )
            await session.execute(
                text(
                    "INSERT INTO kalshi_paper_test_events "
                    "(run_id,sequence,account_id,event_type,position_id,exit_decision_id,remaining_quantity,"
                    "realized_pnl,reason,created_at) VALUES "
                    "(:run_id,3,:account_id,'exit_filled',:position_id,'totally-forged',0,999,"
                    "'position_changed_before_exit',:created_at)"
                ),
                {
                    "run_id": run_id,
                    "account_id": account_id,
                    "position_id": position_id,
                    "created_at": NOW.replace(tzinfo=None),
                },
            )
            with pytest.raises(DBAPIError, match="no trigger evidence|exceptional exit event"):
                await session.commit()
            await session.rollback()

            quote = _quote_with_yes(("0.500000", "4.00"))
            noncanonical = json.dumps(json.loads(quote.book.evidence_json), indent=2, sort_keys=False)
            noncanonical_hash = hashlib.sha256(noncanonical.encode("utf-8")).hexdigest()
            await session.execute(
                text("UPDATE kalshi_paper_test_runs SET next_event_sequence=4 WHERE run_id=:run_id"),
                {"run_id": run_id},
            )
            await session.execute(
                text(
                    "INSERT INTO kalshi_paper_test_events "
                    "(run_id,sequence,account_id,event_type,best_bid,market_observed_at,book_observed_at,"
                    "quote_evidence_hash,quote_evidence_json,remaining_quantity,reason,created_at) VALUES "
                    "(:run_id,3,:account_id,'hold',0.5,:observed,:observed,:evidence_hash,:evidence_json,"
                    "4,'forged',:created_at)"
                ),
                {
                    "run_id": run_id,
                    "account_id": account_id,
                    "observed": quote.book.observed_at.replace(tzinfo=None),
                    "evidence_hash": noncanonical_hash,
                    "evidence_json": noncanonical,
                    "created_at": NOW.replace(tzinfo=None),
                },
            )
            with pytest.raises(DBAPIError, match="canonical"):
                await session.commit()
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_completed_event_cannot_forge_closed_projection_or_financial_snapshot() -> None:
    engine, factory, _market_data, _paper, service, request = await _test_service(
        "test_trade_completed_projection_guard"
    )
    try:
        started = await service.start_run(**request)
        run_id = str(request["run_id"])
        async with factory() as session:
            await session.execute(
                text(
                    "INSERT INTO kalshi_paper_test_events "
                    "(run_id,sequence,account_id,event_type,position_id,remaining_quantity,realized_pnl,"
                    "reason,created_at) VALUES "
                    "(:run_id,3,:account_id,'completed',:position_id,0,999,'forged',:created_at)"
                ),
                {
                    "run_id": run_id,
                    "account_id": request["account_id"],
                    "position_id": started["run"]["position_id"],
                    "created_at": NOW.replace(tzinfo=None),
                },
            )
            await session.execute(
                text(
                    "UPDATE kalshi_paper_test_runs SET status='completed',next_event_sequence=4 "
                    "WHERE run_id=:run_id"
                ),
                {"run_id": run_id},
            )
            with pytest.raises(DBAPIError, match="completed event contradicts authoritative position"):
                await session.commit()
            await session.rollback()

        authoritative = await service.get_run(run_id)
        assert authoritative["run"]["status"] == "monitoring"
        assert authoritative["run"]["remaining_quantity"] == "4.00"
    finally:
        await engine.dispose()
