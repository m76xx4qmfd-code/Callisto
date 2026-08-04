from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from services import venues
from services.venues.kalshi_v2 import (
    KalshiEventPosition,
    KalshiFill,
    KalshiMarketPosition,
    KalshiOrder,
    KalshiProtocolError,
    KalshiSettlement,
    KalshiV2Client,
)


def test_public_venue_package_exports_read_models() -> None:
    assert venues.KalshiOrder is KalshiOrder
    assert venues.KalshiFill is KalshiFill
    assert venues.KalshiMarketPosition is KalshiMarketPosition
    assert venues.KalshiEventPosition is KalshiEventPosition
    assert venues.KalshiSettlement is KalshiSettlement


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
        "order_id": "order-1",
        "user_id": "user-1",
        "client_order_id": "client-1",
        "ticker": "HIGHNY-24JAN01-T60",
        "outcome_side": "yes",
        "book_side": "bid",
        "type": "limit",
        "status": "resting",
        "yes_price_dollars": "0.560000",
        "no_price_dollars": "0.440000",
        "fill_count_fp": "1.25",
        "remaining_count_fp": "8.75",
        "initial_count_fp": "10.00",
        "taker_fees_dollars": "0.010000",
        "maker_fees_dollars": "0.000000",
        "taker_fill_cost_dollars": "0.700000",
        "maker_fill_cost_dollars": "0.000000",
        "created_time": "2026-08-04T12:00:00Z",
        "last_update_time": "2026-08-04T12:01:00Z",
        "subaccount_number": 0,
        "exchange_index": 2,
    }


def _fill_payload() -> dict[str, object]:
    return {
        "fill_id": "fill-1",
        "trade_id": "fill-1",
        "order_id": "order-1",
        "ticker": "HIGHNY-24JAN01-T60",
        "market_ticker": "HIGHNY-24JAN01-T60",
        "outcome_side": "yes",
        "book_side": "bid",
        "count_fp": "1.25",
        "yes_price_dollars": "0.560000",
        "no_price_dollars": "0.440000",
        "is_taker": True,
        "fee_cost": "0.010000",
        "created_time": "2026-08-04T12:01:00Z",
        "subaccount_number": 0,
        "ts": 1_754_308_860,
    }


def _market_position_payload() -> dict[str, object]:
    return {
        "ticker": "HIGHNY-24JAN01-T60",
        "total_traded_dollars": "5.600000",
        "position_fp": "10.00",
        "market_exposure_dollars": "5.600000",
        "realized_pnl_dollars": "-0.100000",
        "fees_paid_dollars": "0.020000",
        "last_updated_ts": "2026-08-04T12:02:00Z",
    }


def _event_position_payload() -> dict[str, object]:
    return {
        "event_ticker": "HIGHNY-24JAN01",
        "total_cost_dollars": "5.600000",
        "total_cost_shares_fp": "10.00",
        "event_exposure_dollars": "5.600000",
        "realized_pnl_dollars": "-0.100000",
        "fees_paid_dollars": "0.020000",
    }


def _settlement_payload() -> dict[str, object]:
    return {
        "ticker": "HIGHNY-24JAN01-T60",
        "event_ticker": "HIGHNY-24JAN01",
        "market_result": "yes",
        "yes_count_fp": "10.00",
        "yes_total_cost_dollars": "5.600000",
        "no_count_fp": "0.00",
        "no_total_cost_dollars": "0.000000",
        "revenue": 1000,
        "settled_time": "2026-08-04T13:00:00Z",
        "fee_cost": "0.020000",
        "value": 100,
    }


@pytest.mark.asyncio
async def test_get_orders_is_signed_read_only_and_preserves_cursor(
    private_key_pem: str,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"orders": [_order_payload()], "cursor": "next-orders"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KalshiV2Client(
            key_id="key-id",
            private_key_pem=private_key_pem,
            http_client=http,
            allow_writes=False,
            now_ms=lambda: 1_725_000_000_123,
        )
        page = await client.get_orders(
            ticker="HIGHNY-24JAN01-T60",
            event_tickers=("HIGHNY-24JAN01", "RAINNY-24JAN01"),
            status="resting",
            min_ts=100,
            max_ts=200,
            limit=25,
            cursor="orders-cursor",
            subaccount=0,
        )

    assert len(page.orders) == 1
    assert isinstance(page.orders[0], KalshiOrder)
    assert page.orders[0].fill_count == Decimal("1.25")
    assert page.orders[0].book_side == "bid"
    assert page.cursor == "next-orders"
    assert len(captured) == 1
    assert captured[0].method == "GET"
    assert captured[0].url.path == "/trade-api/v2/portfolio/orders"
    assert dict(captured[0].url.params) == {
        "ticker": "HIGHNY-24JAN01-T60",
        "event_ticker": "HIGHNY-24JAN01,RAINNY-24JAN01",
        "status": "resting",
        "min_ts": "100",
        "max_ts": "200",
        "limit": "25",
        "cursor": "orders-cursor",
        "subaccount": "0",
    }
    assert captured[0].headers["KALSHI-ACCESS-TIMESTAMP"] == "1725000000123"


@pytest.mark.asyncio
async def test_get_fills_parses_fixed_point_values_and_filters(
    private_key_pem: str,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"fills": [_fill_payload()], "cursor": "next-fills"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KalshiV2Client(
            key_id="key-id",
            private_key_pem=private_key_pem,
            http_client=http,
        )
        page = await client.get_fills(
            ticker="HIGHNY-24JAN01-T60",
            order_id="order-1",
            min_ts=100,
            max_ts=200,
            limit=50,
            cursor="fills-cursor",
            subaccount=1,
        )

    assert isinstance(page.fills[0], KalshiFill)
    assert page.fills[0].count == Decimal("1.25")
    assert page.fills[0].fee_cost == Decimal("0.010000")
    assert page.fills[0].is_taker is True
    assert page.cursor == "next-fills"
    assert captured[0].url.params["order_id"] == "order-1"
    assert captured[0].url.params["subaccount"] == "1"


@pytest.mark.asyncio
async def test_get_positions_parses_signed_positions_and_cursor(
    private_key_pem: str,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "market_positions": [_market_position_payload()],
                "event_positions": [_event_position_payload()],
                "cursor": "next-positions",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KalshiV2Client(
            key_id="key-id",
            private_key_pem=private_key_pem,
            http_client=http,
        )
        page = await client.get_positions(
            ticker="HIGHNY-24JAN01-T60",
            event_ticker="HIGHNY-24JAN01",
            count_filter=("position", "total_traded"),
            limit=30,
            cursor="positions-cursor",
            subaccount=0,
        )

    assert isinstance(page.market_positions[0], KalshiMarketPosition)
    assert page.market_positions[0].position == Decimal("10.00")
    assert page.market_positions[0].realized_pnl == Decimal("-0.100000")
    assert isinstance(page.event_positions[0], KalshiEventPosition)
    assert page.event_positions[0].total_cost_shares == Decimal("10.00")
    assert page.cursor == "next-positions"
    assert captured[0].url.params["count_filter"] == "position,total_traded"


@pytest.mark.asyncio
async def test_get_settlements_parses_result_revenue_and_cursor(
    private_key_pem: str,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"settlements": [_settlement_payload()], "cursor": "next-settlements"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KalshiV2Client(
            key_id="key-id",
            private_key_pem=private_key_pem,
            http_client=http,
        )
        page = await client.get_settlements(
            ticker="HIGHNY-24JAN01-T60",
            event_ticker="HIGHNY-24JAN01",
            min_ts=100,
            max_ts=200,
            limit=40,
            cursor="settlements-cursor",
            subaccount=2,
        )

    assert isinstance(page.settlements[0], KalshiSettlement)
    assert page.settlements[0].market_result == "yes"
    assert page.settlements[0].yes_count == Decimal("10.00")
    assert page.settlements[0].revenue_cents == 1000
    assert page.settlements[0].settlement_value_cents == 100
    assert page.cursor == "next-settlements"
    assert captured[0].url.params["event_ticker"] == "HIGHNY-24JAN01"


@pytest.mark.asyncio
async def test_read_query_validation_happens_before_transport(private_key_pem: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KalshiV2Client(
            key_id="key-id",
            private_key_pem=private_key_pem,
            http_client=http,
        )
        with pytest.raises(KalshiProtocolError, match="limit"):
            await client.get_orders(limit=1001)
        with pytest.raises(KalshiProtocolError, match="event_tickers"):
            await client.get_orders(event_tickers=tuple(f"EVENT-{i}" for i in range(11)))
        with pytest.raises(KalshiProtocolError, match="status"):
            await client.get_orders(status="pending")
        with pytest.raises(KalshiProtocolError, match="min_ts"):
            await client.get_fills(min_ts=200, max_ts=100)
        with pytest.raises(KalshiProtocolError, match="count_filter"):
            await client.get_positions(count_filter=("unknown",))
        with pytest.raises(KalshiProtocolError, match="subaccount"):
            await client.get_settlements(subaccount=64)

    assert calls == 0


def test_read_models_reject_missing_or_invalid_canonical_fields() -> None:
    auto_route_order = _order_payload()
    auto_route_order["exchange_index"] = -1
    assert KalshiOrder.from_payload(auto_route_order).exchange_index == -1

    invalid_exchange_order = _order_payload()
    invalid_exchange_order["exchange_index"] = -2
    with pytest.raises(KalshiProtocolError, match="exchange_index"):
        KalshiOrder.from_payload(invalid_exchange_order)

    invalid_order = _order_payload()
    invalid_order["book_side"] = "yes"
    with pytest.raises(KalshiProtocolError, match="book_side"):
        KalshiOrder.from_payload(invalid_order)

    excessive_precision_order = _order_payload()
    excessive_precision_order["yes_price_dollars"] = "0.1234567"
    with pytest.raises(KalshiProtocolError, match="more precision"):
        KalshiOrder.from_payload(excessive_precision_order)

    excessive_trailing_precision_order = _order_payload()
    excessive_trailing_precision_order["yes_price_dollars"] = "0.5600000"
    with pytest.raises(KalshiProtocolError, match="more precision"):
        KalshiOrder.from_payload(excessive_trailing_precision_order)

    non_string_price_order = _order_payload()
    non_string_price_order["yes_price_dollars"] = 0.56
    with pytest.raises(KalshiProtocolError, match="fixed-point string"):
        KalshiOrder.from_payload(non_string_price_order)

    inconsistent_direction_order = _order_payload()
    inconsistent_direction_order["book_side"] = "ask"
    with pytest.raises(KalshiProtocolError, match="inconsistent"):
        KalshiOrder.from_payload(inconsistent_direction_order)

    noncomplementary_prices_order = _order_payload()
    noncomplementary_prices_order["no_price_dollars"] = "0.430000"
    with pytest.raises(KalshiProtocolError, match="sum to 1"):
        KalshiOrder.from_payload(noncomplementary_prices_order)

    invalid_fill = _fill_payload()
    invalid_fill["is_taker"] = 1
    with pytest.raises(KalshiProtocolError, match="is_taker"):
        KalshiFill.from_payload(invalid_fill)

    inconsistent_fill_ids = _fill_payload()
    inconsistent_fill_ids["trade_id"] = "different-fill-id"
    with pytest.raises(KalshiProtocolError, match="fill_id and trade_id"):
        KalshiFill.from_payload(inconsistent_fill_ids)

    non_integer_settlement = _settlement_payload()
    non_integer_settlement["revenue"] = "1000"
    with pytest.raises(KalshiProtocolError, match="revenue"):
        KalshiSettlement.from_payload(non_integer_settlement)

    pathological_decimal_order = _order_payload()
    pathological_decimal_order["yes_price_dollars"] = "1E+999999999"
    with pytest.raises(KalshiProtocolError, match="yes_price_dollars"):
        KalshiOrder.from_payload(pathological_decimal_order)

    invalid_position = _market_position_payload()
    invalid_position.pop("position_fp")
    with pytest.raises(KalshiProtocolError, match="market position"):
        KalshiMarketPosition.from_payload(invalid_position)

    invalid_settlement = _settlement_payload()
    invalid_settlement["market_result"] = "unknown"
    with pytest.raises(KalshiProtocolError, match="market_result"):
        KalshiSettlement.from_payload(invalid_settlement)
