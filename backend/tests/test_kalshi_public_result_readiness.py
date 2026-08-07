from __future__ import annotations

import hashlib
import importlib.util
import json
import ssl
import sys
import threading
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI

from api.routes_kalshi_live_readiness import router as live_readiness_router
from services.kalshi_live_readiness import BLOCKERS, build_live_readiness
from services.kalshi_paper_execution import (
    KALSHI_PAPER_MARKET_DATA_ORIGIN,
    KALSHI_RESOLUTION_OPENAPI_SHA256,
    KALSHI_RESOLUTION_OPENAPI_VERSION,
    KalshiPaperProtocolError,
    KalshiPublicResultClient,
)


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)
DATE = "Fri, 07 Aug 2026 12:00:00 GMT"
TICKER = "KXTEST-26"
LIFECYCLE_STATUSES = (
    "initialized",
    "inactive",
    "active",
    "closed",
    "determined",
    "disputed",
    "amended",
    "finalized",
)


def _market(**patch: object) -> dict[str, object]:
    market: dict[str, object] = {
        "ticker": TICKER,
        "event_ticker": "KXTEST",
        "market_type": "binary",
        "status": "finalized",
        "result": "yes",
        "notional_value_dollars": "1.000000",
        "expiration_value": "71",
    }
    market.update(patch)
    return market


def _result_client(
    market: object,
    *,
    status_code: int = 200,
    date: str | None = DATE,
    content: bytes | None = None,
) -> tuple[KalshiPublicResultClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        headers = {} if date is None else {"Date": date}
        if content is not None:
            return httpx.Response(status_code, headers=headers, content=content)
        return httpx.Response(status_code, headers=headers, json={"market": market})

    return (
        KalshiPublicResultClient(transport=httpx.MockTransport(handler), now=lambda: NOW),
        seen,
    )


def _load_cli_module() -> Any:
    path = ROOT / "scripts" / "infra" / "validate_kalshi_public_result.py"
    spec = importlib.util.spec_from_file_location("validate_kalshi_public_result", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_resolution_openapi_vendor_is_exact() -> None:
    path = ROOT / "docs" / "research" / "kalshi_openapi_3_27_0_20260807.yaml"
    raw = path.read_bytes()
    document = yaml.safe_load(raw)

    assert hashlib.sha256(raw).hexdigest() == "bd80e9d42fec2f9cddd5e498ef53cf34bc79effec8fe39031b327c9d483741e2"
    assert document["openapi"] == "3.0.0"
    assert document["info"]["version"] == "3.27.0"
    assert KALSHI_RESOLUTION_OPENAPI_SHA256 == hashlib.sha256(raw).hexdigest()
    assert KALSHI_RESOLUTION_OPENAPI_VERSION == document["info"]["version"]


@pytest.mark.asyncio
async def test_result_fetch_is_exact_unauthenticated_get_only() -> None:
    client, seen = _result_client(_market())

    observation = await client.fetch_market_result(TICKER)

    assert observation.ticker == TICKER
    assert observation.final is True
    assert observation.result == "yes"
    assert observation.source_origin == KALSHI_PAPER_MARKET_DATA_ORIGIN
    assert observation.source_path == "/trade-api/v2/markets/KXTEST-26"
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "GET"
    assert str(request.url) == f"{KALSHI_PAPER_MARKET_DATA_ORIGIN}/trade-api/v2/markets/{TICKER}"
    assert "authorization" not in request.headers
    assert "cookie" not in request.headers
    assert not any(name.lower().startswith("kalshi-access-") for name in request.headers)
    assert {"post", "put", "patch", "delete", "submit", "cancel", "amend"}.isdisjoint(
        set(dir(KalshiPublicResultClient))
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", LIFECYCLE_STATUSES[:-1])
@pytest.mark.parametrize("result", ("yes", "no", "scalar", ""))
async def test_every_nonfinal_status_and_result_is_truthfully_waiting(status: str, result: str) -> None:
    client, _ = _result_client(_market(status=status, result=result))

    observation = await client.fetch_market_result(TICKER)

    assert observation.status == status
    assert observation.result == result
    assert observation.final is False
    assert observation.state == "waiting"


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ("yes", "no"))
@pytest.mark.parametrize("optional", ({}, {"settlement_ts": None, "settlement_value_dollars": None}))
async def test_final_binary_result_accepts_absent_or_null_optional_settlement_fields(
    result: str,
    optional: dict[str, object],
) -> None:
    client, _ = _result_client(_market(result=result, **optional))

    observation = await client.fetch_market_result(TICKER)

    assert observation.final is True
    assert observation.state == "final"
    assert observation.result == result
    assert observation.settlement_ts is None
    assert observation.settlement_value is None


@pytest.mark.asyncio
async def test_present_optional_settlement_fields_are_strictly_parsed_and_preserved() -> None:
    client, _ = _result_client(
        _market(
            result="no",
            settlement_ts="2026-08-07T11:59:58-00:00",
            settlement_value_dollars="0.250000",
        )
    )

    observation = await client.fetch_market_result(TICKER)

    assert observation.settlement_ts == datetime(2026, 8, 7, 11, 59, 58, tzinfo=timezone.utc)
    assert observation.settlement_value == Decimal("0.250000")
    evidence = json.loads(observation.evidence_json)
    assert set(evidence) == {
        "event_ticker",
        "expiration_value",
        "market_type",
        "notional_value_dollars",
        "observed_at",
        "resolution_openapi_sha256",
        "resolution_openapi_version",
        "result",
        "settlement_ts",
        "settlement_value_dollars",
        "source_origin",
        "source_path",
        "status",
        "ticker",
    }
    assert evidence["settlement_value_dollars"] == "0.250000"
    assert evidence["resolution_openapi_sha256"] == KALSHI_RESOLUTION_OPENAPI_SHA256
    assert evidence["resolution_openapi_version"] == KALSHI_RESOLUTION_OPENAPI_VERSION
    assert hashlib.sha256(observation.evidence_json.encode("utf-8")).hexdigest() == observation.evidence_hash
    with pytest.raises(FrozenInstanceError):
        observation.result = "yes"  # type: ignore[misc]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"ticker": "WRONG"}, "ticker identity mismatch"),
        ({"event_ticker": None}, "event_ticker"),
        ({"market_type": "scalar"}, "binary"),
        ({"status": "unknown"}, "status"),
        ({"result": None}, "result"),
        ({"notional_value_dollars": 1.0}, "decimal string"),
        ({"notional_value_dollars": "2.000000"}, "one-dollar"),
        ({"expiration_value": None}, "expiration_value"),
        ({"settlement_ts": "not-a-time"}, "RFC3339"),
        ({"settlement_value_dollars": 0.5}, "decimal string"),
        ({"settlement_value_dollars": "0.1234567"}, "decimal places"),
    ],
)
async def test_result_rejects_malformed_required_and_optional_fields(
    patch: dict[str, object],
    reason: str,
) -> None:
    client, _ = _result_client(_market(**patch))

    with pytest.raises(KalshiPaperProtocolError, match=reason):
        await client.fetch_market_result(TICKER)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ("scalar", ""))
async def test_final_scalar_or_blank_result_fails_closed(result: str) -> None:
    client, _ = _result_client(_market(result=result))

    with pytest.raises(KalshiPaperProtocolError, match="finalized binary result"):
        await client.fetch_market_result(TICKER)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("date", "reason"),
    [
        (None, "missing source Date"),
        ("not-a-date", "invalid source Date"),
        ("Fri, 07 Aug 2026 11:59:54 GMT", "stale"),
        ("Fri, 07 Aug 2026 12:00:06 GMT", "future"),
    ],
)
async def test_result_rejects_missing_malformed_stale_or_future_date(date: str | None, reason: str) -> None:
    client, _ = _result_client(_market(), date=date)

    with pytest.raises(KalshiPaperProtocolError, match=reason):
        await client.fetch_market_result(TICKER)


@pytest.mark.asyncio
async def test_result_rejects_non_200_and_malformed_json_without_retry() -> None:
    client, seen = _result_client(_market(), status_code=404)
    with pytest.raises(KalshiPaperProtocolError, match="HTTP 404"):
        await client.fetch_market_result(TICKER)
    assert len(seen) == 1

    client, seen = _result_client(_market(), content=b"not-json")
    with pytest.raises(KalshiPaperProtocolError, match="market result JSON"):
        await client.fetch_market_result(TICKER)
    assert len(seen) == 1


class _SilentHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Date", DATE)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"market": _market()}).encode("utf-8"))

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _self_signed_server(tmp_path: Path) -> tuple[HTTPServer, threading.Thread]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]), False)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    server = HTTPServer(("127.0.0.1", 0), _SilentHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.mark.asyncio
async def test_default_ca_verification_rejects_self_signed_tls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import services.kalshi_paper_execution as execution

    server, thread = _self_signed_server(tmp_path)
    origin = f"https://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(execution, "KALSHI_PAPER_MARKET_DATA_ORIGIN", origin)
    try:
        client = execution.KalshiPublicResultClient(now=lambda: NOW)
        with pytest.raises(KalshiPaperProtocolError, match="request failed"):
            await client.fetch_market_result(TICKER)
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin_name", "expected_origin"),
    [
        ("production", "https://external-api.kalshi.com"),
        ("demo", "https://demo-api.kalshi.co"),
    ],
)
async def test_cli_production_and_demo_transports_are_get_only_and_credential_free(
    origin_name: str,
    expected_origin: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _load_cli_module()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"Date": DATE}, json={"market": _market(status="active", result="")})

    def forbidden_environment_read(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("credential environment must not be read")

    monkeypatch.setattr("os.getenv", forbidden_environment_read)
    result = await cli.validate_public_result(
        origin_name=origin_name,
        ticker=TICKER,
        transport=httpx.MockTransport(handler),
        now=lambda: NOW,
    )

    assert result == {
        "schema_version": "kalshi-public-result-validation/v1",
        "origin": origin_name,
        "ticker": TICKER,
        "status": "active",
        "state": "waiting",
        "result": "",
        "evidence_hash": result["evidence_hash"],
    }
    assert len(result["evidence_hash"]) == 64
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert str(seen[0].url) == f"{expected_origin}/trade-api/v2/markets/{TICKER}"
    assert "authorization" not in seen[0].headers
    assert "cookie" not in seen[0].headers


@pytest.mark.asyncio
async def test_readiness_endpoint_is_static_blocked_and_has_strict_producer_shape() -> None:
    app = FastAPI()
    app.include_router(live_readiness_router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/kalshi/live-readiness")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "schema_version",
        "assessment",
        "effective_execution",
        "live_ready",
        "operator_policy",
        "risk_limits",
        "separate_live_authorization",
        "runtime_session_arm",
        "final_boundary_write_lease",
        "portfolio_readiness",
        "complete_live_lifecycle",
        "dormant_write_primitive",
        "blockers",
    }
    assert payload["schema_version"] == "live-readiness/v1"
    assert payload["assessment"] == "permanently_blocked"
    assert payload["effective_execution"] == "disabled"
    assert payload["live_ready"] is False
    assert payload["portfolio_readiness"] == "not_assessed"
    assert [item["id"] for item in payload["blockers"]] == [f"LR-{index:02d}" for index in range(1, 10)]
    assert all(set(item) == {"id", "claim", "status", "evidence"} for item in payload["blockers"])
    assert build_live_readiness() == payload
    assert tuple(item["id"] for item in BLOCKERS) == tuple(f"LR-{index:02d}" for index in range(1, 10))


@pytest.mark.asyncio
async def test_readiness_route_performs_no_io_or_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    import api.routes_kalshi_live_readiness as routes

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("readiness route must not perform external or persistence I/O")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    monkeypatch.setattr("os.getenv", forbidden)
    payload = await routes.get_kalshi_live_readiness()

    assert payload["live_ready"] is False
    source = (ROOT / "backend" / "services" / "kalshi_live_readiness.py").read_text(encoding="utf-8")
    assert "models.database" not in source
    assert "services.venues" not in source
    assert "live_execution" not in source
    assert "credential_manager" not in source
    assert "os.environ" not in source
    assert "getenv(" not in source


def test_main_mounts_live_readiness_route() -> None:
    import main

    assert "/api/kalshi/live-readiness" in main.app.openapi()["paths"]
    assert set(main.app.openapi()["paths"]["/api/kalshi/live-readiness"]) == {"get"}


def test_paper_worker_compose_and_import_boundary_is_credential_and_mutation_free() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    paper = compose["services"]["worker-kalshi-paper-test"]
    environment = paper["environment"]
    assert set(environment) == {"DATABASE_URL", "LOG_LEVEL", "HOMERUN_PROCESS_ROLE", "HOMERUN_WORKER_PLANE"}
    assert "x-backend-env" not in json.dumps(paper)
    assert not any("KEY" in key or "SECRET" in key or "PASSPHRASE" in key or "CREDENTIAL" in key for key in environment)

    worker = (ROOT / "backend" / "workers" / "kalshi_paper_test_trade_worker.py").read_text(encoding="utf-8")
    execution = (ROOT / "backend" / "services" / "kalshi_paper_execution.py").read_text(encoding="utf-8")
    assert "services.venues" not in worker
    assert "credential_manager" not in worker
    assert "KalshiV2Client" not in worker
    assert "os.environ" not in execution
    assert "services.venues" not in execution
    assert "credential_manager" not in execution
