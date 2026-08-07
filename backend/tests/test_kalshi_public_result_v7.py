from __future__ import annotations

import hashlib
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI

from api.routes_kalshi_live_readiness import router as readiness_router
from services import kalshi_paper_execution as execution
from services.kalshi_paper_execution import KalshiPaperProtocolError

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)
DATE = "Fri, 07 Aug 2026 12:00:00 GMT"
TICKER = "KXTEST-26"


def _market(**patch: object) -> dict[str, object]:
    value: dict[str, object] = {
        "ticker": TICKER,
        "event_ticker": "KXTEST",
        "market_type": "binary",
        "status": "finalized",
        "result": "yes",
        "notional_value_dollars": "1.000000",
        "expiration_value": "",
    }
    value.update(patch)
    return value


def _response(market: dict[str, object], *, date: str | None = DATE) -> httpx.Response:
    headers = {} if date is None else {"Date": date}
    return httpx.Response(200, headers=headers, json={"market": market})


def _parse(market: dict[str, object], *, date: str | None = DATE, fetched_at: datetime = NOW):
    return execution.parse_market_result_response(
        _response(market, date=date),
        requested_ticker=TICKER,
        source_origin=execution.KALSHI_PAPER_MARKET_DATA_ORIGIN,
        source_path=f"/trade-api/v2/markets/{TICKER}",
        fetched_at=fetched_at,
    )


def test_manifest_has_exact_two_fetch_provenance_and_security_absence() -> None:
    manifest = (ROOT / "docs/research/SOURCE_MANIFEST.md").read_text(encoding="utf-8")
    historical = "41d93050bf3f692cf3a898ba3a1a033f3e857fee56370ddcb18af6a4225f41cb"
    assert manifest.count(historical) == 1
    assert "Source URL: `https://docs.kalshi.com/openapi.yaml`" in manifest
    assert "First retrieval: `2026-08-07T17:35:22Z`" in manifest
    assert "Second retrieval: `2026-08-07T18:15:43.660581Z`" in manifest
    assert "Final resolved URL: `https://docs.kalshi.com/openapi.yaml`" in manifest
    assert "Root `security`: absent" in manifest
    assert "`GET /markets/{ticker}` `security`: absent" in manifest


def test_vendored_schema_security_absence_is_asserted_at_root_and_operation() -> None:
    document = yaml.safe_load(
        (ROOT / "docs/research/kalshi_openapi_3_27_0_20260807.yaml").read_bytes()
    )
    assert "security" not in document
    assert "security" not in document["paths"]["/markets/{ticker}"]["get"]


@pytest.mark.parametrize(
    ("optional", "ts_present", "value_present"),
    [
        ({}, False, False),
        ({"settlement_ts": None}, True, False),
        ({"settlement_value_dollars": None}, False, True),
        (
            {
                "settlement_ts": "2026-08-07T11:59:58Z",
                "settlement_value_dollars": "0.250000",
            },
            True,
            True,
        ),
    ],
)
def test_selected_parser_preserves_optional_absent_null_and_value(
    optional: dict[str, object], ts_present: bool, value_present: bool
) -> None:
    observation = _parse(_market(**optional))
    evidence = json.loads(observation.evidence_json)
    assert observation.expiration_value == ""
    assert observation.settlement_ts_present is ts_present
    assert observation.settlement_value_present is value_present
    assert ("settlement_ts" in evidence) is ts_present
    assert ("settlement_value_dollars" in evidence) is value_present
    if optional.get("settlement_ts", object()) is None and ts_present:
        assert evidence["settlement_ts"] is None
    if optional.get("settlement_value_dollars", object()) is None and value_present:
        assert evidence["settlement_value_dollars"] is None


@pytest.mark.parametrize(
    "market",
    [
        {key: value for key, value in _market().items() if key != "expiration_value"},
        _market(expiration_value=None),
        _market(settlement_ts=""),
        _market(settlement_ts=3),
        _market(settlement_value_dollars=""),
        _market(settlement_value_dollars="0.1234567"),
    ],
)
def test_selected_parser_rejects_missing_or_malformed_selected_values(
    market: dict[str, object],
) -> None:
    with pytest.raises(KalshiPaperProtocolError):
        _parse(market)


def test_selected_parser_rejects_duplicate_json_keys() -> None:
    duplicate = (
        b'{"market":{"ticker":"KXTEST-26","ticker":"EVIL",'
        b'"event_ticker":"KXTEST","market_type":"binary",'
        b'"status":"finalized","result":"yes",'
        b'"notional_value_dollars":"1.000000","expiration_value":""}}'
    )
    response = httpx.Response(200, headers={"Date": DATE}, content=duplicate)
    with pytest.raises(KalshiPaperProtocolError, match="duplicate JSON key"):
        execution.parse_market_result_response(
            response,
            requested_ticker=TICKER,
            source_origin=execution.KALSHI_PAPER_MARKET_DATA_ORIGIN,
            source_path=f"/trade-api/v2/markets/{TICKER}",
            fetched_at=NOW,
        )


def test_selected_parser_rejects_duplicate_date_headers() -> None:
    response = httpx.Response(
        200,
        headers=[("Date", DATE), ("Date", "Fri, 07 Aug 2026 12:00:01 GMT")],
        json={"market": _market()},
    )
    with pytest.raises(KalshiPaperProtocolError, match="multiple source Date"):
        execution.parse_market_result_response(
            response,
            requested_ticker=TICKER,
            source_origin=execution.KALSHI_PAPER_MARKET_DATA_ORIGIN,
            source_path=f"/trade-api/v2/markets/{TICKER}",
            fetched_at=NOW,
        )


def test_canonical_evidence_doc_and_independent_reference_bytes() -> None:
    document = (ROOT / "docs/architecture/KALSHI_EVIDENCE_CANONICALIZATION.md").read_text(
        encoding="utf-8"
    )
    for required in (
        "UTF-8",
        "lexicographically sorted",
        "ASCII escaping",
        "absent optional keys",
        "explicit null",
        "lowercase SHA-256",
        "strict selected settlement projection",
        "not an OpenAPI invariant",
    ):
        assert required in document

    observation = _parse(
        _market(
            event_ticker="KXCAFÉ",
            settlement_ts=None,
            settlement_value_dollars="0.250000",
        )
    )
    expected = (
        b'{"event_ticker":"KXCAF\\u00c9","expiration_value":"","market_type":"binary",'
        b'"notional_value_dollars":"1.000000","observed_at":"2026-08-07T12:00:00+00:00",'
        b'"resolution_openapi_sha256":"bd80e9d42fec2f9cddd5e498ef53cf34bc79effec8fe39031b327c9d483741e2",'
        b'"resolution_openapi_version":"3.27.0","result":"yes","settlement_ts":null,'
        b'"settlement_value_dollars":"0.250000","source_origin":"https://external-api.kalshi.com",'
        b'"source_path":"/trade-api/v2/markets/KXTEST-26","status":"finalized","ticker":"KXTEST-26"}'
    )
    assert observation.evidence_json.encode("utf-8") == expected
    assert observation.evidence_hash == hashlib.sha256(expected).hexdigest()


@pytest.mark.parametrize(
    ("date", "offset", "accepted"),
    [
        (None, timedelta(), False),
        ("", timedelta(), False),
        ("not-a-date", timedelta(), False),
        (DATE, timedelta(seconds=-5), True),
        (DATE, timedelta(seconds=5), True),
        (DATE, timedelta(seconds=-5, microseconds=-1), False),
        (DATE, timedelta(seconds=5, microseconds=1), False),
    ],
)
def test_date_requires_nonblank_valid_header_and_exact_five_second_bound(
    date: str | None, offset: timedelta, accepted: bool
) -> None:
    fetched_at = NOW + offset
    if accepted:
        assert _parse(_market(), date=date, fetched_at=fetched_at).observed_at == NOW
    else:
        with pytest.raises(KalshiPaperProtocolError):
            _parse(_market(), date=date, fetched_at=fetched_at)


def test_production_result_client_is_structurally_fixed_and_separate_from_demo() -> None:
    production = getattr(execution, "KalshiPublicResultClient")
    assert "origin" not in inspect.signature(production).parameters
    assert not hasattr(execution.KalshiPaperMarketDataClient, "fetch_market_result")
    source = inspect.getsource(production)
    assert "demo" not in source.lower()
    assert "KALSHI_PAPER_MARKET_DATA_ORIGIN" in source

    cli_source = (ROOT / "scripts/infra/validate_kalshi_public_result.py").read_text(encoding="utf-8")
    assert "KalshiPublicResultClient" not in cli_source
    assert '"production"' in cli_source and '"demo"' in cli_source


@pytest.mark.asyncio
async def test_position_final_result_route_is_mounted_get_only_and_strict() -> None:
    from api import routes_kalshi_paper

    app = FastAPI()
    app.include_router(routes_kalshi_paper.router, prefix="/api/kalshi/paper")
    operation = app.openapi()["paths"][
        "/api/kalshi/paper/positions/{position_id}/final-result"
    ]
    assert set(operation) == {"get"}
    parameters = operation["get"]["parameters"]
    assert {item["name"] for item in parameters} == {"position_id", "account_id"}


@pytest.mark.asyncio
async def test_position_observation_loads_db_ticker_once_and_never_mutates() -> None:
    from services.kalshi_paper_result_service import KalshiPaperResultService

    class Position:
        account_id = "account"
        position_id = "position"
        ticker = TICKER

    class Session:
        get_calls: list[tuple[object, object]] = []
        add_calls = 0
        commit_calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, model: object, identity: object):
            self.get_calls.append((model, identity))
            return Position()

        def add(self, _value: object) -> None:
            self.add_calls += 1

        async def commit(self) -> None:
            self.commit_calls += 1

    session = Session()
    service = KalshiPaperResultService(session_factory=lambda: session)
    calls: list[str] = []

    async def fake_fetch(_self: object, ticker: str):
        calls.append(ticker)
        return _parse(_market())

    original = execution.KalshiPublicResultClient.fetch_market_result
    execution.KalshiPublicResultClient.fetch_market_result = fake_fetch
    try:
        payload = await service.observe_position_result(
            account_id="account", position_id="position"
        )
    finally:
        execution.KalshiPublicResultClient.fetch_market_result = original

    assert calls == [TICKER]
    assert len(session.get_calls) == 1
    assert session.add_calls == 0 and session.commit_calls == 0
    assert payload["account_id"] == "account"
    assert payload["position_id"] == "position"
    assert payload["ticker"] == TICKER
    assert payload["evidence_hash"] == _parse(_market()).evidence_hash


@pytest.mark.asyncio
async def test_readiness_is_complete_evidence_defined_permanently_blocked_v1() -> None:
    app = FastAPI()
    app.include_router(readiness_router, prefix="/api")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        payload = (await client.get("/api/kalshi/live-readiness")).json()

    assert payload["schema_version"] == "live-readiness/v1"
    assert payload["assessment"] == "permanently_blocked"
    assert payload["effective_execution"] == "disabled"
    assert payload["live_ready"] is False
    assert payload["portfolio_readiness"] == "not_assessed"
    assert payload["operator_policy"] == "absent"
    assert payload["risk_limits"] == "absent"
    assert payload["dormant_write_primitive"] == {
        "exists": True,
        "callable": "KalshiV2Client.create_order(..., allow_writes=True)",
        "status": "disabled",
        "production_route_wiring": "absent",
        "production_runtime_wiring": "absent",
    }
    blockers = payload["blockers"]
    assert [item["id"] for item in blockers] == [f"LR-{index:02d}" for index in range(1, 10)]
    assert all(set(item) == {"id", "claim", "status", "evidence"} for item in blockers)
    assert [item["status"] for item in blockers] == [
        "absent",
        "not_implemented",
        "not_implemented",
        "not_implemented",
        "not_assessed",
        "not_implemented",
        "unsafe",
        "disabled",
        "absent",
    ]
    assert blockers[6]["claim"] == (
        "Mounted legacy account routes can initialize stored Kalshi credentials on read requests "
        "and are not a live-readiness or authorization boundary."
    )
    for blocker in blockers:
        assert blocker["claim"]
        assert blocker["evidence"]
        for path in blocker["evidence"]:
            assert (ROOT / path).exists(), (blocker["id"], path)
    assert not any(isinstance(value, (int, float)) and not isinstance(value, bool) for value in payload.values())


def test_static_topology_has_no_demo_or_mutation_path_into_route_worker_or_service() -> None:
    route = (ROOT / "backend/api/routes_kalshi_paper.py").read_text(encoding="utf-8")
    result_service = (ROOT / "backend/services/kalshi_paper_result_service.py").read_text(
        encoding="utf-8"
    )
    worker = (ROOT / "backend/workers/kalshi_paper_test_trade_worker.py").read_text(
        encoding="utf-8"
    )
    combined = route + result_service + worker
    assert "demo-api" not in combined
    assert "ORIGINS" not in combined
    assert "allow_writes" not in combined
    assert "KalshiV2Client" not in combined
    assert "credential_manager" not in combined
    assert "kalshi_paper_settlements" not in result_service
    assert "record_settlement" not in result_service
    assert "accounting" not in result_service.lower()
