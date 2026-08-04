from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from inspect import signature

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from services import venues
from services.venues.kalshi_v2 import (
    KalshiHistoricalCutoff,
    KalshiProtocolError,
    KalshiV2Client,
)


@pytest.fixture
def private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _order_payload() -> dict[str, object]:
    return {
        "order_id": "order-archive-1",
        "user_id": "user-1",
        "client_order_id": "client-archive-1",
        "ticker": "HIGHNY-24JAN01-T60",
        "outcome_side": "yes",
        "book_side": "bid",
        "type": "limit",
        "status": "executed",
        "yes_price_dollars": "0.560000",
        "no_price_dollars": "0.440000",
        "fill_count_fp": "2.50",
        "remaining_count_fp": "0.00",
        "initial_count_fp": "2.50",
        "taker_fees_dollars": "0.010000",
        "maker_fees_dollars": "0.000000",
        "taker_fill_cost_dollars": "1.400000",
        "maker_fill_cost_dollars": "0.000000",
        "created_time": "2026-07-01T12:00:00Z",
        "last_update_time": "2026-07-01T12:01:00Z",
        "subaccount_number": 0,
        "exchange_index": 2,
    }


def _fill_payload() -> dict[str, object]:
    return {
        "fill_id": "fill-archive-1",
        "trade_id": "fill-archive-1",
        "order_id": "order-archive-1",
        "ticker": "HIGHNY-24JAN01-T60",
        "market_ticker": "HIGHNY-24JAN01-T60",
        "outcome_side": "yes",
        "book_side": "bid",
        "count_fp": "2.50",
        "yes_price_dollars": "0.560000",
        "no_price_dollars": "0.440000",
        "is_taker": True,
        "fee_cost": "0.010000",
        "created_time": "2026-07-01T12:01:00Z",
        "subaccount_number": 0,
        "ts": 1_751_371_260,
    }


def test_public_venue_package_exports_historical_cutoff() -> None:
    assert venues.KalshiHistoricalCutoff is KalshiHistoricalCutoff


def test_historical_get_signatures_expose_only_documented_filters() -> None:
    expected = {"self", "ticker", "max_ts", "limit", "cursor"}
    assert set(signature(KalshiV2Client.get_historical_orders).parameters) == expected
    assert set(signature(KalshiV2Client.get_historical_fills).parameters) == expected


def test_historical_cutoff_parser_requires_timezone_aware_rfc3339() -> None:
    cutoff = KalshiHistoricalCutoff.from_payload(
        {
            "market_settled_ts": "2026-07-01T00:00:00Z",
            "trades_created_ts": "2026-07-02T01:02:03.123456+00:00",
            "orders_updated_ts": "2026-07-03T02:03:04-04:00",
            "market_positions_last_updated_ts": "2026-07-04T03:04:05Z",
        }
    )

    assert cutoff.market_settled_at == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert cutoff.trades_created_at == datetime(2026, 7, 2, 1, 2, 3, 123456, tzinfo=timezone.utc)
    assert cutoff.orders_updated_at == datetime(2026, 7, 3, 6, 3, 4, tzinfo=timezone.utc)
    assert cutoff.market_positions_last_updated_at == datetime(2026, 7, 4, 3, 4, 5, tzinfo=timezone.utc)

    with pytest.raises(KalshiProtocolError, match="market_settled_ts must be RFC3339"):
        KalshiHistoricalCutoff.from_payload(
            {
                "market_settled_ts": "2026-07-01 00:00:00",
                "trades_created_ts": "2026-07-02T00:00:00Z",
                "orders_updated_ts": "2026-07-03T00:00:00Z",
            }
        )
    with pytest.raises(KalshiProtocolError, match="trades_created_ts"):
        KalshiHistoricalCutoff.from_payload(
            {
                "market_settled_ts": "2026-07-01T00:00:00Z",
                "orders_updated_ts": "2026-07-03T00:00:00Z",
            }
        )
    with pytest.raises(KalshiProtocolError, match="market_positions_last_updated_ts"):
        KalshiHistoricalCutoff.from_payload(
            {
                "market_settled_ts": "2026-07-01T00:00:00Z",
                "trades_created_ts": "2026-07-02T00:00:00Z",
                "orders_updated_ts": "2026-07-03T00:00:00Z",
                "market_positions_last_updated_ts": None,
            }
        )
    with pytest.raises(KalshiProtocolError, match="microsecond precision"):
        KalshiHistoricalCutoff.from_payload(
            {
                "market_settled_ts": "2026-07-01T00:00:00.1234567Z",
                "trades_created_ts": "2026-07-02T00:00:00Z",
                "orders_updated_ts": "2026-07-03T00:00:00Z",
            }
        )


@pytest.mark.asyncio
async def test_historical_gets_use_only_documented_filters_and_preserve_decimals(
    private_key_pem: str,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/historical/cutoff"):
            return httpx.Response(
                200,
                json={
                    "market_settled_ts": "2026-07-01T00:00:00Z",
                    "trades_created_ts": "2026-07-02T00:00:00Z",
                    "orders_updated_ts": "2026-07-03T00:00:00Z",
                },
            )
        if request.url.path.endswith("/historical/orders"):
            return httpx.Response(200, json={"orders": [_order_payload()], "cursor": "orders-next"})
        if request.url.path.endswith("/historical/fills"):
            return httpx.Response(200, json={"fills": [_fill_payload()], "cursor": "fills-next"})
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KalshiV2Client(
            key_id="key-id",
            private_key_pem=private_key_pem,
            http_client=http,
            allow_writes=False,
            now_ms=lambda: 1_725_000_000_123,
        )
        cutoff = await client.get_historical_cutoff()
        orders = await client.get_historical_orders(
            ticker="HIGHNY-24JAN01-T60",
            max_ts=1_751_371_300,
            limit=25,
            cursor="orders-cursor",
        )
        fills = await client.get_historical_fills(
            ticker="HIGHNY-24JAN01-T60",
            max_ts=1_751_371_300,
            limit=50,
            cursor="fills-cursor",
        )

    assert cutoff.orders_updated_at == datetime(2026, 7, 3, tzinfo=timezone.utc)
    assert orders.orders[0].client_order_id == "client-archive-1"
    assert orders.orders[0].initial_count == Decimal("2.50")
    assert orders.cursor == "orders-next"
    assert fills.fills[0].order_id == "order-archive-1"
    assert fills.fills[0].count == Decimal("2.50")
    assert fills.cursor == "fills-next"
    assert [request.method for request in captured] == ["GET", "GET", "GET"]
    assert [request.url.path for request in captured] == [
        "/trade-api/v2/historical/cutoff",
        "/trade-api/v2/historical/orders",
        "/trade-api/v2/historical/fills",
    ]
    assert dict(captured[1].url.params) == {
        "ticker": "HIGHNY-24JAN01-T60",
        "max_ts": "1751371300",
        "limit": "25",
        "cursor": "orders-cursor",
    }
    assert dict(captured[2].url.params) == {
        "ticker": "HIGHNY-24JAN01-T60",
        "max_ts": "1751371300",
        "limit": "50",
        "cursor": "fills-cursor",
    }
    assert all(request.headers["KALSHI-ACCESS-TIMESTAMP"] == "1725000000123" for request in captured)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "kwargs", "message"),
    [
        ("get_historical_orders", {"limit": 0}, "limit"),
        ("get_historical_orders", {"cursor": " "}, "cursor"),
        ("get_historical_fills", {"max_ts": -1}, "max_ts"),
        ("get_historical_fills", {"max_ts": 2**63}, "max_ts"),
        ("get_historical_fills", {"ticker": " "}, "ticker"),
        ("get_historical_fills", {"ticker": 42}, "ticker"),
        ("get_historical_orders", {"cursor": 42}, "cursor"),
    ],
)
async def test_historical_gets_reject_invalid_queries_before_transport(
    private_key_pem: str,
    method_name: str,
    kwargs: dict[str, object],
    message: str,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KalshiV2Client(key_id="key-id", private_key_pem=private_key_pem, http_client=http)
        with pytest.raises(KalshiProtocolError, match=message):
            await getattr(client, method_name)(**kwargs)

    assert calls == 0
