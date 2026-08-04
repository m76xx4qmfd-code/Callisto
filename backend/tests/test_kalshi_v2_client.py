from __future__ import annotations

import base64
import json
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from services.venues.contracts import VenueOrderIntent
from services.venues.kalshi_v2 import (
    KALSHI_API_PREFIX,
    KalshiAPIError,
    KalshiBalanceSnapshot,
    KalshiEventOrderRequest,
    KalshiProtocolError,
    KalshiRequestSigner,
    KalshiSubmissionUnknown,
    KalshiV2Client,
    LiveTradingNotArmedError,
    event_order_from_intent,
)


@pytest.fixture
def private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def test_request_signer_uses_rsa_pss_and_excludes_query(private_key_pem: str) -> None:
    signer = KalshiRequestSigner(key_id="key-id", private_key_pem=private_key_pem)

    headers = signer.headers(
        timestamp_ms=1_725_000_000_123,
        method="get",
        path=f"{KALSHI_API_PREFIX}/portfolio/orders?status=resting",
    )

    assert headers["KALSHI-ACCESS-KEY"] == "key-id"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1725000000123"
    assert "Authorization" not in headers

    public_key = serialization.load_pem_private_key(private_key_pem.encode("ascii"), password=None).public_key()
    public_key.verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        b"1725000000123GET/trade-api/v2/portfolio/orders",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
        hashes.SHA256(),
    )


def test_event_order_payload_uses_current_v2_fixed_point_shape() -> None:
    request = KalshiEventOrderRequest(
        ticker="HIGHNY-24JAN01-T60",
        client_order_id="8c35ecb3-328f-4f52-8c7c-0f4b9862f8d1",
        side="bid",
        count=Decimal(10),
        price=Decimal("0.56"),
        time_in_force="good_till_canceled",
        post_only=True,
        cancel_order_on_pause=True,
        reduce_only=False,
        subaccount=0,
        exchange_index=0,
    )

    assert request.to_payload() == {
        "ticker": "HIGHNY-24JAN01-T60",
        "client_order_id": "8c35ecb3-328f-4f52-8c7c-0f4b9862f8d1",
        "side": "bid",
        "count": "10.00",
        "price": "0.560000",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": True,
        "cancel_order_on_pause": True,
        "reduce_only": False,
        "subaccount": 0,
        "exchange_index": 0,
    }


def test_event_order_accepts_six_decimal_price_and_auto_route_exchange_index() -> None:
    order = KalshiEventOrderRequest(
        ticker="HIGHNY-24JAN01-T60",
        client_order_id="six-decimal",
        side="bid",
        count=Decimal(1),
        price=Decimal("0.123456"),
        exchange_index=-1,
    )

    assert order.to_payload()["price"] == "0.123456"
    assert order.to_payload()["exchange_index"] == -1


def test_event_order_rejects_invalid_wire_values() -> None:
    common = {
        "ticker": "HIGHNY-24JAN01-T60",
        "client_order_id": "invalid-wire-value",
        "side": "bid",
        "count": Decimal(1),
        "price": Decimal("0.500000"),
    }

    with pytest.raises(KalshiProtocolError, match="self_trade_prevention_type"):
        KalshiEventOrderRequest(**common, self_trade_prevention_type="unknown")
    with pytest.raises(KalshiProtocolError, match="subaccount must be an integer"):
        KalshiEventOrderRequest(**common, subaccount=1.5)  # type: ignore[arg-type]
    with pytest.raises(KalshiProtocolError, match="exchange_index must be an integer"):
        KalshiEventOrderRequest(**common, exchange_index=True)  # type: ignore[arg-type]
    with pytest.raises(KalshiProtocolError, match="exchange_index cannot be less than -1"):
        KalshiEventOrderRequest(**common, exchange_index=-2)
    with pytest.raises(KalshiProtocolError, match="more precision"):
        KalshiEventOrderRequest(**{**common, "price": Decimal("0.1234567")})


def test_client_rejects_untrusted_origin_before_credentials_can_be_sent(
    private_key_pem: str,
) -> None:
    with pytest.raises(KalshiProtocolError, match="approved Kalshi origin"):
        KalshiV2Client(
            key_id="key-id",
            private_key_pem=private_key_pem,
            origin="https://example.invalid",
        )


def test_venue_neutral_intent_translates_to_kalshi_wire_order() -> None:
    intent = VenueOrderIntent(
        venue="kalshi",
        instrument_id="HIGHNY-24JAN01-T60",
        client_order_id="intent-1",
        book_side="bid",
        quantity=Decimal(3),
        limit_price=Decimal("0.4700"),
        time_in_force="immediate_or_cancel",
        post_only=False,
    )

    order = event_order_from_intent(
        intent,
        cancel_order_on_pause=True,
        reduce_only=True,
        subaccount=4,
        exchange_index=2,
    )

    assert order.ticker == "HIGHNY-24JAN01-T60"
    assert order.side == "bid"
    assert order.count == Decimal(3)
    assert order.price == Decimal("0.4700")
    assert order.time_in_force == "immediate_or_cancel"
    assert order.cancel_order_on_pause is True
    assert order.reduce_only is True
    assert order.subaccount == 4
    assert order.exchange_index == 2


def test_kalshi_translator_rejects_another_venue() -> None:
    intent = VenueOrderIntent(
        venue="polymarket",
        instrument_id="token-1",
        client_order_id="intent-2",
        book_side="ask",
        quantity=Decimal(1),
        limit_price=Decimal("0.5000"),
    )

    with pytest.raises(ValueError, match="Kalshi translator"):
        event_order_from_intent(intent)


@pytest.mark.asyncio
async def test_get_balance_is_signed_read_only_and_parses_fixed_point_fields(
    private_key_pem: str,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "balance": 12345,
                "balance_dollars": "123.4500",
                "portfolio_value": 13000,
                "updated_ts": 1_725_000_000_456,
                "balance_breakdown": [{"exchange_index": 0, "balance": "123.4500"}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KalshiV2Client(
            key_id="key-id",
            private_key_pem=private_key_pem,
            http_client=http,
            allow_writes=False,
            now_ms=lambda: 1_725_000_000_123,
        )
        balance = await client.get_balance()

    assert isinstance(balance, KalshiBalanceSnapshot)
    assert balance.balance_cents == 12345
    assert balance.balance_dollars == Decimal("123.4500")
    assert balance.portfolio_value_cents == 13000
    assert balance.balance_breakdown[0].exchange_index == 0
    assert balance.balance_breakdown[0].balance == Decimal("123.4500")
    assert len(captured) == 1
    sent = captured[0]
    assert sent.method == "GET"
    assert sent.url.path == "/trade-api/v2/portfolio/balance"
    assert sent.headers["KALSHI-ACCESS-KEY"] == "key-id"
    assert sent.headers["KALSHI-ACCESS-TIMESTAMP"] == "1725000000123"


def test_balance_parser_rejects_noncanonical_wire_types() -> None:
    payload: dict[str, object] = {
        "balance": 12345,
        "balance_dollars": "123.4500",
        "portfolio_value": 13000,
        "updated_ts": 1_725_000_000_456,
        "balance_breakdown": [{"exchange_index": 0, "balance": "123.4500"}],
    }

    with pytest.raises(KalshiProtocolError, match="balance_dollars"):
        KalshiBalanceSnapshot.from_payload({**payload, "balance_dollars": 123.45})
    with pytest.raises(KalshiProtocolError, match="balance must be an integer"):
        KalshiBalanceSnapshot.from_payload({**payload, "balance": "12345"})
    with pytest.raises(KalshiProtocolError, match="cannot be negative"):
        KalshiBalanceSnapshot.from_payload({**payload, "balance_dollars": "-123.4500"})
    with pytest.raises(KalshiProtocolError, match="does not match"):
        KalshiBalanceSnapshot.from_payload({**payload, "balance_dollars": "123.4400"})
    with pytest.raises(KalshiProtocolError, match="breakdown balance cannot be negative"):
        KalshiBalanceSnapshot.from_payload(
            {**payload, "balance_breakdown": [{"exchange_index": 0, "balance": "-1.0000"}]}
        )


@pytest.mark.asyncio
async def test_create_order_is_blocked_until_writes_are_explicitly_armed(private_key_pem: str) -> None:
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
            allow_writes=False,
        )
        with pytest.raises(LiveTradingNotArmedError):
            await client.create_order(
                KalshiEventOrderRequest(
                    ticker="HIGHNY-24JAN01-T60",
                    client_order_id="client-1",
                    side="bid",
                    count=Decimal(1),
                    price=Decimal("0.5000"),
                )
            )

    assert calls == 0


@pytest.mark.asyncio
async def test_create_order_posts_current_v2_shape_and_parses_ack(private_key_pem: str) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            201,
            json={
                "order_id": "order-1",
                "fill_count": "0.00",
                "remaining_count": "2.00",
                "ts_ms": 1_725_000_000_456,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KalshiV2Client(
            key_id="key-id",
            private_key_pem=private_key_pem,
            http_client=http,
            allow_writes=True,
            now_ms=lambda: 1_725_000_000_123,
        )
        ack = await client.create_order(
            KalshiEventOrderRequest(
                ticker="HIGHNY-24JAN01-T60",
                client_order_id="client-1",
                side="ask",
                count=Decimal(2),
                price=Decimal("0.6100"),
                reduce_only=True,
            )
        )

    assert ack.order_id == "order-1"
    assert ack.client_order_id == "client-1"
    assert ack.fill_count == Decimal("0.00")
    assert ack.remaining_count == Decimal("2.00")
    assert len(captured) == 1
    sent = captured[0]
    assert sent.method == "POST"
    assert sent.url.path == "/trade-api/v2/portfolio/events/orders"
    assert sent.headers["KALSHI-ACCESS-KEY"] == "key-id"
    assert sent.headers["KALSHI-ACCESS-TIMESTAMP"] == "1725000000123"
    payload = json.loads(sent.content)
    assert payload["side"] == "ask"
    assert payload["count"] == "2.00"
    assert payload["price"] == "0.610000"
    assert payload["reduce_only"] is True


@pytest.mark.asyncio
async def test_transport_failure_is_unknown_and_is_not_retried(private_key_pem: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectTimeout("venue acknowledgement unknown", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KalshiV2Client(
            key_id="key-id",
            private_key_pem=private_key_pem,
            http_client=http,
            allow_writes=True,
        )
        with pytest.raises(KalshiSubmissionUnknown) as exc_info:
            await client.create_order(
                KalshiEventOrderRequest(
                    ticker="HIGHNY-24JAN01-T60",
                    client_order_id="client-unknown",
                    side="bid",
                    count=Decimal(1),
                    price=Decimal("0.4000"),
                )
            )

    assert exc_info.value.client_order_id == "client-unknown"
    assert calls == 1


@pytest.mark.asyncio
async def test_explicit_order_rejection_is_not_retried(private_key_pem: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(409, json={"error": "duplicate client order id"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KalshiV2Client(
            key_id="key-id",
            private_key_pem=private_key_pem,
            http_client=http,
            allow_writes=True,
        )
        with pytest.raises(KalshiAPIError) as exc_info:
            await client.create_order(
                KalshiEventOrderRequest(
                    ticker="HIGHNY-24JAN01-T60",
                    client_order_id="client-rejected",
                    side="bid",
                    count=Decimal(1),
                    price=Decimal("0.4000"),
                )
            )

    assert exc_info.value.status_code == 409
    assert exc_info.value.client_order_id == "client-rejected"
    assert calls == 1


@pytest.mark.asyncio
async def test_server_error_after_submission_is_unknown(private_key_pem: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "temporary venue failure"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KalshiV2Client(
            key_id="key-id",
            private_key_pem=private_key_pem,
            http_client=http,
            allow_writes=True,
        )
        with pytest.raises(KalshiSubmissionUnknown) as exc_info:
            await client.create_order(
                KalshiEventOrderRequest(
                    ticker="HIGHNY-24JAN01-T60",
                    client_order_id="client-server-error",
                    side="bid",
                    count=Decimal(1),
                    price=Decimal("0.400000"),
                )
            )

    assert exc_info.value.client_order_id == "client-server-error"
    assert isinstance(exc_info.value.cause, KalshiAPIError)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_payload",
    [
        {},
        {
            "order_id": "order-1",
            "client_order_id": "different-client-id",
            "fill_count": "0.00",
            "remaining_count": "1.00",
            "ts_ms": 1_725_000_000_456,
        },
    ],
)
async def test_successful_but_unusable_acknowledgement_is_submission_unknown(
    private_key_pem: str,
    response_payload: dict[str, object],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=response_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KalshiV2Client(
            key_id="key-id",
            private_key_pem=private_key_pem,
            http_client=http,
            allow_writes=True,
        )
        with pytest.raises(KalshiSubmissionUnknown) as exc_info:
            await client.create_order(
                KalshiEventOrderRequest(
                    ticker="HIGHNY-24JAN01-T60",
                    client_order_id="client-ambiguous",
                    side="bid",
                    count=Decimal(1),
                    price=Decimal("0.400000"),
                )
            )

    assert exc_info.value.client_order_id == "client-ambiguous"
