from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext

import httpx
import pytest

from services.kalshi_paper_execution import (
    KALSHI_OPENAPI_SHA256,
    KalshiPaperMarketDataClient,
    KalshiPaperProtocolError,
    PaperBook,
    PaperBookLevel,
    parse_quantity,
    simulate_buy_ioc,
)


NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)


def _book(*, yes: tuple[tuple[str, str], ...] = (), no: tuple[tuple[str, str], ...] = ()) -> PaperBook:
    return PaperBook(
        ticker="KXTEST-26",
        yes_bids=tuple(PaperBookLevel(price=Decimal(price), quantity=Decimal(quantity)) for price, quantity in yes),
        no_bids=tuple(PaperBookLevel(price=Decimal(price), quantity=Decimal(quantity)) for price, quantity in no),
        source_origin="https://external-api.kalshi.com",
        observed_at=NOW,
        fetched_at=NOW,
        evidence_hash="a" * 64,
        evidence_json='{"no_dollars":[],"yes_dollars":[]}',
    )


def test_buy_yes_sweeps_descending_no_bids_at_complementary_prices() -> None:
    result = simulate_buy_ioc(
        book=_book(no=(("0.400000", "3.00"), ("0.450000", "2.00"))),
        outcome="yes",
        quantity=Decimal("4.00"),
        limit_price=Decimal("0.600000"),
    )

    assert [(fill.quantity, fill.price) for fill in result.fills] == [
        (Decimal("2.00"), Decimal("0.550000")),
        (Decimal("2.00"), Decimal("0.600000")),
    ]
    assert result.status == "filled"
    assert result.filled_quantity == Decimal("4.00")
    assert result.remaining_quantity == Decimal("0.00")
    assert result.notional == Decimal("2.30000000")
    assert result.average_fill_price == Decimal("0.575000000000000000")


def test_buy_no_sweeps_descending_yes_bids_and_partially_fills() -> None:
    result = simulate_buy_ioc(
        book=_book(yes=(("0.350000", "1.25"), ("0.500000", "2.50"))),
        outcome="no",
        quantity=Decimal("5.00"),
        limit_price=Decimal("0.650000"),
    )

    assert [(fill.quantity, fill.price) for fill in result.fills] == [
        (Decimal("2.50"), Decimal("0.500000")),
        (Decimal("1.25"), Decimal("0.650000")),
    ]
    assert result.status == "partial"
    assert result.filled_quantity == Decimal("3.75")
    assert result.remaining_quantity == Decimal("1.25")
    assert result.notional == Decimal("2.06250000")


def test_ioc_no_cross_is_terminal_no_fill() -> None:
    result = simulate_buy_ioc(
        book=_book(no=(("0.300000", "10.00"),)),
        outcome="yes",
        quantity=Decimal("2.00"),
        limit_price=Decimal("0.650000"),
    )

    assert result.status == "no_fill"
    assert result.reason == "limit_does_not_cross_displayed_depth"
    assert result.fills == ()
    assert result.filled_quantity == Decimal("0.00")
    assert result.remaining_quantity == Decimal("2.00")
    assert result.notional == Decimal("0.00000000")
    assert result.average_fill_price is None


def test_fill_math_is_independent_of_ambient_decimal_context() -> None:
    original = getcontext().copy()
    try:
        getcontext().prec = 4
        result = simulate_buy_ioc(
            book=_book(no=(("0.876544", "999999999999999999.99"),)),
            outcome="yes",
            quantity=Decimal("999999999999999999.99"),
            limit_price=Decimal("0.123456"),
        )
    finally:
        getcontext().prec = original.prec
        getcontext().rounding = original.rounding
        getcontext().traps = original.traps

    assert result.filled_quantity == Decimal("999999999999999999.99")
    assert result.notional == Decimal("123455999999999999.99876544")
    assert result.average_fill_price == Decimal("0.123456000000000000")


def test_fill_engine_rejects_excess_scale_and_invalid_order_shape() -> None:
    with pytest.raises(KalshiPaperProtocolError, match="quantity has more than 2 decimal places"):
        simulate_buy_ioc(
            book=_book(no=(("0.500000", "1.00"),)),
            outcome="yes",
            quantity=Decimal("1.001"),
            limit_price=Decimal("0.500000"),
        )
    with pytest.raises(KalshiPaperProtocolError, match="limit_price has more than 6 decimal places"):
        simulate_buy_ioc(
            book=_book(no=(("0.500000", "1.00"),)),
            outcome="yes",
            quantity=Decimal("1.00"),
            limit_price=Decimal("0.5000001"),
        )
    with pytest.raises(KalshiPaperProtocolError, match="outcome"):
        simulate_buy_ioc(
            book=_book(no=(("0.500000", "1.00"),)),
            outcome="maybe",  # type: ignore[arg-type]
            quantity=Decimal("1.00"),
            limit_price=Decimal("0.500000"),
        )
    assert parse_quantity("100000000000000000000000000000000000000.00") == Decimal(
        "100000000000000000000000000000000000000.00"
    )


@pytest.mark.asyncio
async def test_read_only_client_fetches_strict_fee_waived_quote_without_auth() -> None:
    seen: list[httpx.Request] = []
    date_header = "Wed, 05 Aug 2026 12:00:00 GMT"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/markets/KXTEST-26"):
            return httpx.Response(
                200,
                headers={"Date": date_header},
                json={
                    "market": {
                        "ticker": "KXTEST-26",
                        "event_ticker": "KXTEST",
                        "market_type": "binary",
                        "status": "active",
                        "notional_value_dollars": "1.000000",
                        "fee_waiver_expiration_time": "2026-08-05T12:10:00Z",
                        "close_time": "2026-08-05T13:00:00Z",
                        "latest_expiration_time": "2026-08-05T14:00:00Z",
                        "price_level_structure": "linear_cent",
                        "price_ranges": [],
                    }
                },
            )
        assert request.url.path.endswith("/markets/KXTEST-26/orderbook")
        assert request.url.params["depth"] == "100"
        return httpx.Response(
            200,
            headers={"Date": date_header},
            content=json.dumps(
                {
                    "orderbook_fp": {
                        "yes_dollars": [["0.400000", "3.00"]],
                        "no_dollars": [["0.450000", "2.00"]],
                    }
                }
            ).encode(),
        )

    client = KalshiPaperMarketDataClient(transport=httpx.MockTransport(handler), now=lambda: NOW)
    quote = await client.fetch_quote("KXTEST-26")

    assert quote.market.ticker == "KXTEST-26"
    assert quote.market.notional_value == Decimal("1.000000")
    assert quote.market.fee == Decimal("0")
    assert quote.market.fee_rule_version == "kalshi-market-fee-waiver-v1"
    assert quote.market.fee_provenance["openapi_sha256"] == KALSHI_OPENAPI_SHA256
    assert quote.book.no_bids[0] == PaperBookLevel(price=Decimal("0.450000"), quantity=Decimal("2.00"))
    assert len(seen) == 2
    assert all(request.method == "GET" for request in seen)
    assert all("authorization" not in request.headers for request in seen)
    assert all("cookie" not in request.headers for request in seen)
    assert all(not any(name.lower().startswith("kalshi-access-") for name in request.headers) for request in seen)


def test_read_only_client_has_no_venue_mutation_surface() -> None:
    forbidden = {"post", "put", "patch", "delete", "submit", "create_order", "cancel", "amend", "decrease"}
    assert forbidden.isdisjoint(set(dir(KalshiPaperMarketDataClient)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market_patch", "date_header", "reason"),
    [
        ({"status": "closed"}, "Wed, 05 Aug 2026 12:00:00 GMT", "market is not active"),
        ({"market_type": "scalar"}, "Wed, 05 Aug 2026 12:00:00 GMT", "binary"),
        ({"notional_value_dollars": "10.000000"}, "Wed, 05 Aug 2026 12:00:00 GMT", "notional"),
        ({"fee_waiver_expiration_time": None}, "Wed, 05 Aug 2026 12:00:00 GMT", "fee waiver"),
        (
            {"fee_waiver_expiration_time": "2026-08-05T11:59:59Z"},
            "Wed, 05 Aug 2026 12:00:00 GMT",
            "fee waiver",
        ),
        ({}, "Wed, 05 Aug 2026 11:59:54 GMT", "stale"),
    ],
)
async def test_read_only_client_fails_closed_on_unsupported_or_stale_market(
    market_patch: dict[str, object], date_header: str, reason: str
) -> None:
    market = {
        "ticker": "KXTEST-26",
        "event_ticker": "KXTEST",
        "market_type": "binary",
        "status": "active",
        "notional_value_dollars": "1.000000",
        "fee_waiver_expiration_time": "2026-08-05T12:10:00Z",
        "close_time": "2026-08-05T13:00:00Z",
        "latest_expiration_time": "2026-08-05T14:00:00Z",
        "price_level_structure": "linear_cent",
        "price_ranges": [],
    }
    market.update(market_patch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Date": date_header}, json={"market": market})

    client = KalshiPaperMarketDataClient(transport=httpx.MockTransport(handler), now=lambda: NOW)
    with pytest.raises(KalshiPaperProtocolError, match=reason):
        await client.fetch_quote("KXTEST-26")


@pytest.mark.asyncio
async def test_read_only_client_rejects_bare_numeric_fixed_point_and_does_not_retry_401() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                headers={"Date": "Wed, 05 Aug 2026 12:00:00 GMT"},
                json={
                    "market": {
                        "ticker": "KXTEST-26",
                        "event_ticker": "KXTEST",
                        "market_type": "binary",
                        "status": "active",
                        "notional_value_dollars": "1.000000",
                        "fee_waiver_expiration_time": "2026-08-05T12:10:00Z",
                        "close_time": "2026-08-05T13:00:00Z",
                        "latest_expiration_time": "2026-08-05T14:00:00Z",
                        "price_level_structure": "linear_cent",
                        "price_ranges": [],
                    }
                },
            )
        return httpx.Response(
            200,
            headers={"Date": "Wed, 05 Aug 2026 12:00:00 GMT"},
            content=b'{"orderbook_fp":{"yes_dollars":[[0.4,"1.00"]],"no_dollars":[]}}',
        )

    client = KalshiPaperMarketDataClient(transport=httpx.MockTransport(handler), now=lambda: NOW)
    with pytest.raises(KalshiPaperProtocolError, match="price must be a decimal string"):
        await client.fetch_quote("KXTEST-26")
    assert calls == 2

    unauthorized_calls = 0

    def unauthorized(_request: httpx.Request) -> httpx.Response:
        nonlocal unauthorized_calls
        unauthorized_calls += 1
        return httpx.Response(401, headers={"Date": "Wed, 05 Aug 2026 12:00:00 GMT"})

    client = KalshiPaperMarketDataClient(transport=httpx.MockTransport(unauthorized), now=lambda: NOW)
    with pytest.raises(KalshiPaperProtocolError, match="HTTP 401"):
        await client.fetch_quote("KXTEST-26")
    assert unauthorized_calls == 1


def test_max_source_age_boundary_is_exact() -> None:
    assert NOW - timedelta(seconds=5) == datetime(2026, 8, 5, 11, 59, 55, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_fee_waiver_must_still_be_active_at_orderbook_observation() -> None:
    fetched_times = iter((NOW, NOW + timedelta(minutes=10)))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/markets/KXTEST-26"):
            return httpx.Response(
                200,
                headers={"Date": "Wed, 05 Aug 2026 12:00:00 GMT"},
                json={
                    "market": {
                        "ticker": "KXTEST-26",
                        "event_ticker": "KXTEST",
                        "market_type": "binary",
                        "status": "active",
                        "notional_value_dollars": "1.000000",
                        "fee_waiver_expiration_time": "2026-08-05T12:10:00Z",
                        "close_time": "2026-08-05T13:00:00Z",
                        "latest_expiration_time": "2026-08-05T14:00:00Z",
                        "price_level_structure": "linear_cent",
                    }
                },
            )
        return httpx.Response(
            200,
            headers={"Date": "Wed, 05 Aug 2026 12:10:00 GMT"},
            json={"orderbook_fp": {"yes_dollars": [["0.400000", "1.00"]], "no_dollars": []}},
        )

    client = KalshiPaperMarketDataClient(
        transport=httpx.MockTransport(handler),
        now=lambda: next(fetched_times),
    )
    with pytest.raises(KalshiPaperProtocolError, match="fee waiver is not active at orderbook observation"):
        await client.fetch_quote("KXTEST-26")
