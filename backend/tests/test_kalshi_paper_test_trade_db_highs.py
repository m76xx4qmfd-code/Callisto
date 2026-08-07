from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from models.database import Base
from services.kalshi_paper_service import _canonical_json
from tests.test_kalshi_paper_test_trades import NOW, _quote_with_yes, _test_service


def _request_json(request: Mapping[str, object]) -> str:
    return _canonical_json(
        {
            "account_id": str(request["account_id"]),
            "entry_limit_price": str(request["entry_limit_price"]),
            "opportunity_id": str(request["opportunity_id"]),
            "opportunity_revision": str(request["opportunity_revision"]),
            "quantity": str(request["quantity"]),
            "run_id": str(request["run_id"]),
            "stop_loss_minimum_price": str(request["stop_loss_minimum_price"]),
            "stop_loss_price": str(request["stop_loss_price"]),
            "take_profit_price": str(request["take_profit_price"]),
        }
    )


async def _insert_forged_starting_run(
    session,
    request: dict[str, str],
    *,
    suffix: str,
    request_hash: str | None = None,
    request_json_override: str | None = None,
    next_sequence: int = 2,
    started_reason: str | None = None,
) -> str:
    run_id = f"{request['run_id']}-{suffix}"
    forged_request = dict(request, run_id=run_id)
    request_json = request_json_override or _request_json(forged_request)
    digest = request_hash or hashlib.sha256(request_json.encode("utf-8")).hexdigest()
    await session.execute(
        text(
            "INSERT INTO kalshi_paper_test_runs "
            "(run_id,request_hash,request_json,account_id,opportunity_id,opportunity_revision,ticker,outcome,quantity,"
            "entry_limit_price,take_profit_price,stop_loss_price,stop_loss_minimum_price,entry_decision_id,"
            "position_id,status,next_event_sequence,last_reason,last_error,created_at,updated_at) "
            "SELECT CAST(:run_id AS text),:request_hash,:request_json,account_id,opportunity_id,opportunity_revision,ticker,outcome,quantity,"
            "entry_limit_price,take_profit_price,stop_loss_price,stop_loss_minimum_price,"
            "'paper-test-entry:'||CAST(:run_id AS text),NULL,'starting',:next_sequence,'operator_started',NULL,created_at,updated_at "
            "FROM kalshi_paper_test_runs WHERE run_id=:source_run_id"
        ),
        {
            "run_id": run_id,
            "source_run_id": request["run_id"],
            "request_hash": digest,
            "request_json": request_json,
            "next_sequence": next_sequence,
        },
    )
    if started_reason is not None:
        await session.execute(
            text(
                "INSERT INTO kalshi_paper_test_events "
                "(run_id,sequence,account_id,event_type,reason,created_at) "
                "SELECT run_id,1,account_id,'started',:reason,created_at "
                "FROM kalshi_paper_test_runs WHERE run_id=:run_id"
            ),
            {"run_id": run_id, "reason": started_reason},
        )
    return run_id


@pytest.mark.db
@pytest.mark.asyncio
async def test_run_schema_stores_canonical_request_json() -> None:
    engine, _factory, _market_data, _paper, _service, _request = await _test_service("db_high_request_column")
    try:
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync_connection: {column["name"] for column in inspect(sync_connection).get_columns("kalshi_paper_test_runs")}
            )
        assert "request_json" in columns
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "request_hash", "next_sequence", "started_reason"),
    [
        ("forged_hash", "f" * 64, 2, "operator_started"),
        ("omitted_started", None, 2, None),
        ("forged_started", None, 2, "forged"),
        ("wrong_sequence", None, 3, "operator_started"),
    ],
)
async def test_starting_run_rejects_forged_hash_or_initial_event_protocol(
    kind: str, request_hash: str | None, next_sequence: int, started_reason: str | None
) -> None:
    engine, factory, _market_data, _paper, service, request = await _test_service("db_high_forged_start")
    try:
        await service.start_run(**request)
        async with factory() as session:
            await _insert_forged_starting_run(
                session,
                request,
                suffix=kind,
                request_hash=request_hash,
                next_sequence=next_sequence,
                started_reason=started_reason,
            )
            with pytest.raises(DBAPIError):
                await session.commit()
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_run_truncate_cascade_is_rejected() -> None:
    engine, factory, _market_data, _paper, service, request = await _test_service("db_high_truncate")
    try:
        await service.start_run(**request)
        async with factory() as session:
            with pytest.raises(DBAPIError, match="projection.*erased"):
                await session.execute(text("TRUNCATE kalshi_paper_test_runs CASCADE"))
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_run_delete_is_rejected() -> None:
    engine, factory, _market_data, _paper, service, request = await _test_service("db_high_delete")
    try:
        await service.start_run(**request)
        async with factory() as session:
            with pytest.raises(DBAPIError, match="projection.*erased"):
                await session.execute(
                    text("DELETE FROM kalshi_paper_test_runs WHERE run_id=:run_id"),
                    {"run_id": request["run_id"]},
                )
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_starting_run_rejects_rehashed_alternate_request_identity() -> None:
    engine, factory, _market_data, _paper, service, request = await _test_service("db_high_request_identity")
    try:
        await service.start_run(**request)
        run_id = f"{request['run_id']}-alternate-request"
        canonical = json.loads(_request_json(dict(request, run_id=run_id)))
        canonical["extra"] = "alternate-protocol"
        alternate_json = _canonical_json(canonical)
        alternate_hash = hashlib.sha256(alternate_json.encode("utf-8")).hexdigest()
        async with factory() as session:
            await _insert_forged_starting_run(
                session,
                request,
                suffix="alternate-request",
                request_hash=alternate_hash,
                request_json_override=alternate_json,
                started_reason="operator_started",
            )
            with pytest.raises(DBAPIError, match="canonical request identity"):
                await session.commit()
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_entry_transition_requires_matching_immutable_decision_causality() -> None:
    engine, factory, _market_data, _paper, service, request = await _test_service("db_high_entry_causality")
    try:
        started = await service.start_run(**request)
        forged_run_id = f"{request['run_id']}-unbound-entry"
        async with factory() as session:
            await _insert_forged_starting_run(
                session,
                request,
                suffix="unbound-entry",
                started_reason="operator_started",
            )
            await session.commit()
        started_run = started["run"]
        assert isinstance(started_run, dict)
        async with factory() as session:
            await session.execute(
                text(
                    "UPDATE kalshi_paper_test_runs SET status='monitoring', position_id=:position_id, "
                    "next_event_sequence=3, last_reason='entry_filled' WHERE run_id=:run_id"
                ),
                {"run_id": forged_run_id, "position_id": started_run["position_id"]},
            )
            with pytest.raises(DBAPIError, match="intent or decision causality"):
                await session.commit()
            await session.rollback()
    finally:
        await engine.dispose()


def _quote_mutation(kind: str) -> str:
    quote = _quote_with_yes(("0.500000", "4.00"))
    payload = json.loads(quote.book.evidence_json)
    if kind == "extra_key":
        payload["extra"] = "forged"
        return _canonical_json(payload)
    if kind == "wrong_type":
        payload["yes_dollars"][0][0] = 0.5
        return _canonical_json(payload)
    if kind == "reordered":
        return json.dumps(payload, separators=(",", ":"), sort_keys=False)
    if kind == "whitespace":
        return json.dumps(payload, sort_keys=True)
    if kind == "wrong_time":
        payload["observed_at"] = "2026-08-07T11:59:59+00:00"
        return _canonical_json(payload)
    if kind == "equivalent_noncanonical_time":
        payload["observed_at"] = "2026-08-07T08:00:00-04:00"
        return _canonical_json(payload)
    if kind == "wrong_origin":
        payload["source_origin"] = "https://example.com"
        return _canonical_json(payload)
    raise AssertionError(kind)


@pytest.mark.db
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        "extra_key",
        "wrong_type",
        "reordered",
        "whitespace",
        "wrong_time",
        "equivalent_noncanonical_time",
        "wrong_origin",
    ],
)
async def test_direct_sql_rejects_noncanonical_or_unbound_quote_evidence(kind: str) -> None:
    engine, factory, _market_data, _paper, service, request = await _test_service(f"db_high_quote_{kind}")
    try:
        started = await service.start_run(**request)
        evidence_json = _quote_mutation(kind)
        evidence_hash = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
        async with factory() as session:
            await session.execute(
                text(
                    "INSERT INTO kalshi_paper_test_events "
                    "(run_id,sequence,account_id,event_type,best_bid,market_observed_at,book_observed_at,"
                    "quote_evidence_hash,quote_evidence_json,remaining_quantity,reason,created_at) VALUES "
                    "(:run_id,3,:account_id,'hold',0.5,:observed,:observed,:evidence_hash,:evidence_json,"
                    "4,'best_bid_between_thresholds',:created_at)"
                ),
                {
                    "run_id": request["run_id"],
                    "account_id": request["account_id"],
                    "observed": NOW.replace(tzinfo=None),
                    "evidence_hash": evidence_hash,
                    "evidence_json": evidence_json,
                    "created_at": NOW.replace(tzinfo=None),
                },
            )
            await session.execute(
                text("UPDATE kalshi_paper_test_runs SET next_event_sequence=4 WHERE run_id=:run_id"),
                {"run_id": request["run_id"]},
            )
            with pytest.raises(DBAPIError):
                await session.commit()
            await session.rollback()
        assert started["run"]["next_event_sequence"] == 3
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_real_quote_producer_evidence_is_accepted() -> None:
    engine, _factory, market_data, _paper, service, request = await _test_service("db_high_real_quote")
    try:
        await service.start_run(**request)
        quote = _quote_with_yes(("0.500000", "4.00"))
        market_data.fetch_quote.return_value = replace(quote, market=replace(quote.market, observed_at=quote.book.observed_at))
        result = await service.tick_run(str(request["run_id"]))
        assert result["events"][-1]["event_type"] == "hold"
        assert result["events"][-1]["quote_evidence_hash"] == quote.book.evidence_hash
    finally:
        await engine.dispose()


# Keep the ORM table itself in this test module's dependency graph so create_all parity
# failures cannot be hidden by only exercising migration-provisioned SQL elsewhere.
assert Base.metadata.tables["kalshi_paper_test_runs"] is not None
