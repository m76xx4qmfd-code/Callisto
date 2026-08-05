from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from services.venues.kalshi_v2 import KALSHI_DEMO_ORIGIN, KALSHI_PRODUCTION_ORIGIN, KalshiRequestSigner
from services.venues.kalshi_v2_private_ws import (
    KalshiPrivateFill,
    KalshiPrivateMarketPosition,
    KalshiPrivateOrder,
    KalshiPrivateWSFrame,
    KalshiPrivateWSLifecycle,
    KalshiPrivateWSProtocolError,
)


def test_reviewed_asyncapi_source_is_pinned() -> None:
    source = Path(__file__).resolve().parents[2] / "docs/research/kalshi_asyncapi_20260804.yaml"
    assert (
        hashlib.sha256(source.read_bytes()).hexdigest()
        == "2a1449ecdc6dd7cd6498863016fe5ccc62cb64a090149d311733d61175196f78"
    )


def _signer() -> KalshiRequestSigner:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return KalshiRequestSigner(key_id="generated-test-key", private_key_pem=pem)


def _order_message() -> dict[str, object]:
    return {
        "order_id": "9b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a1",
        "user_id": "8b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a2",
        "ticker": "FED-23DEC-T3.00",
        "status": "resting",
        "side": "yes",
        "is_yes": True,
        "outcome_side": "yes",
        "book_side": "bid",
        "yes_price_dollars": "0.2500",
        "fill_count_fp": "1.00",
        "remaining_count_fp": "2.00",
        "initial_count_fp": "3.00",
        "taker_fill_cost_dollars": "0.2500",
        "maker_fill_cost_dollars": "0.0000",
        "taker_fees_dollars": "0.0100",
        "maker_fees_dollars": "0.0000",
        "client_order_id": "client-1",
        "created_time": "2026-08-04T20:00:00Z",
        "created_ts_ms": 1_785_873_600_000,
        "subaccount_number": 0,
    }


def _fill_message() -> dict[str, object]:
    return {
        "trade_id": "7b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a3",
        "order_id": "9b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a1",
        "market_ticker": "FED-23DEC-T3.00",
        "is_taker": True,
        "side": "no",
        "yes_price_dollars": "0.2500",
        "count_fp": "2.00",
        "fee_cost": "0.0100",
        "action": "sell",
        "outcome_side": "yes",
        "book_side": "bid",
        "ts": 1_785_873_600,
        "ts_ms": 1_785_873_600_123,
        "post_position_fp": "3.00",
        "purchased_side": "yes",
        "subaccount": 63,
    }


def _position_message() -> dict[str, object]:
    return {
        "user_id": "8b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a2",
        "market_ticker": "FED-23DEC-T3.00",
        "position_fp": "-2.00",
        "position_cost_dollars": "-0.5000",
        "realized_pnl_dollars": "0.1250",
        "fees_paid_dollars": "0.0100",
        "position_fee_cost_dollars": "0.0050",
        "volume_fp": "4.00",
        "subaccount": 2,
    }


def test_current_private_payloads_parse_to_immutable_exact_models() -> None:
    order = KalshiPrivateOrder.from_payload(_order_message())
    fill = KalshiPrivateFill.from_payload(_fill_message())
    position = KalshiPrivateMarketPosition.from_payload(_position_message())

    assert order.yes_price == Decimal("0.2500")
    assert order.initial_count == Decimal("3.00")
    assert fill.post_position == Decimal("3.00")
    assert fill.timestamp_ms == 1_785_873_600_123
    assert position.position == Decimal("-2.00")
    with pytest.raises(AttributeError):
        order.status = "executed"  # type: ignore[misc]


def test_canceled_partial_order_and_huge_exact_position_follow_the_wire_schema() -> None:
    order_payload = _order_message()
    order_payload["status"] = "canceled"
    order_payload["fill_count_fp"] = "1.00"
    order_payload["remaining_count_fp"] = "0.00"
    position_payload = _position_message()
    position_payload["position_fp"] = "10000000000000000000000000000.00"

    order = KalshiPrivateOrder.from_payload(order_payload)
    position = KalshiPrivateMarketPosition.from_payload(position_payload)

    assert order.status == "canceled"
    assert order.fill_count == Decimal("1.00")
    assert position.position == Decimal("10000000000000000000000000000.00")


def test_position_accepts_schema_string_user_id_and_order_accepts_canonical_optional_timestamps() -> None:
    position_payload = _position_message()
    position_payload["user_id"] = "provider-principal-without-uuid-format"
    position = KalshiPrivateMarketPosition.from_payload(position_payload)

    order_payload = _order_message()
    order_payload["last_updated_ts_ms"] = 1_785_873_600_123
    order = KalshiPrivateOrder.from_payload(order_payload)

    assert position.user_id == "provider-principal-without-uuid-format"
    assert order.last_update_time is None
    assert order.last_updated_timestamp_ms == 1_785_873_600_123


@pytest.mark.parametrize(
    ("factory", "field", "value"),
    [
        (_order_message, "outcome_side", "no"),
        (_order_message, "is_yes", False),
        (_order_message, "remaining_count_fp", "2.001"),
        (_order_message, "created_ts_ms", 1_785_873_600_001),
        (_fill_message, "book_side", "ask"),
        (_fill_message, "purchased_side", "no"),
        (_fill_message, "ts_ms", 1_785_873_601_000),
        (_position_message, "volume_fp", "1.001"),
        (_position_message, "subaccount", 64),
    ],
)
def test_private_payloads_reject_alias_precision_timestamp_and_range_conflicts(
    factory, field: str, value: object
) -> None:
    payload = factory()
    payload[field] = value
    model = {
        _order_message: KalshiPrivateOrder,
        _fill_message: KalshiPrivateFill,
        _position_message: KalshiPrivateMarketPosition,
    }[factory]
    with pytest.raises(KalshiPrivateWSProtocolError):
        model.from_payload(payload)


def test_private_lifecycle_is_signed_private_only_strict_and_retry_never_allowed() -> None:
    lifecycle = KalshiPrivateWSLifecycle(signer=_signer(), principal_origin=KALSHI_PRODUCTION_ORIGIN)
    instructions = lifecycle.begin_connection(timestamp_ms=1_785_873_600_000)

    assert [command.channel for command in instructions.subscriptions] == ["user_orders", "fill", "market_positions"]
    assert all(command.payload["cmd"] == "subscribe" for command in instructions.subscriptions)
    assert lifecycle.retry_allowed is False
    for sid, command in enumerate(instructions.subscriptions, start=11):
        outcome = lifecycle.receive(
            instructions.epoch_id,
            {"id": command.command_id, "type": "subscribed", "msg": {"channel": command.channel, "sid": sid}},
        )
        assert outcome.kind == "ack"
    assert lifecycle.subscriptions_acknowledged is True

    frame = lifecycle.receive(
        instructions.epoch_id,
        {"type": "user_order", "sid": 11, "msg": _order_message()},
    )
    assert isinstance(frame.frame, KalshiPrivateWSFrame)
    assert frame.frame.channel == "user_orders"

    with pytest.raises(KalshiPrivateWSProtocolError):
        lifecycle.receive(instructions.epoch_id, {"type": "fill", "sid": 99, "msg": _fill_message()})
    assert lifecycle.current_epoch_id is None


def test_private_lifecycle_rejects_duplicate_wrong_ack_stale_epoch_unknown_and_provider_principal_mismatch() -> None:
    lifecycle = KalshiPrivateWSLifecycle(signer=_signer(), principal_origin=KALSHI_PRODUCTION_ORIGIN)
    first = lifecycle.begin_connection(timestamp_ms=1_785_873_600_000)
    command = first.subscriptions[0]
    ack = {"id": command.command_id, "type": "subscribed", "msg": {"channel": command.channel, "sid": 11}}
    lifecycle.receive(first.epoch_id, ack)
    with pytest.raises(KalshiPrivateWSProtocolError):
        lifecycle.receive(first.epoch_id, ack)

    second = lifecycle.begin_connection(timestamp_ms=1_785_873_600_001)
    assert lifecycle.receive(first.epoch_id, {"type": "unknown"}).kind == "stale"
    for sid, item in enumerate(second.subscriptions, start=21):
        lifecycle.receive(
            second.epoch_id,
            {"id": item.command_id, "type": "subscribed", "msg": {"channel": item.channel, "sid": sid}},
        )
    lifecycle.receive(second.epoch_id, {"type": "user_order", "sid": 21, "msg": _order_message()})
    other = _position_message()
    other["user_id"] = "6b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a4"
    with pytest.raises(KalshiPrivateWSProtocolError):
        lifecycle.receive(second.epoch_id, {"type": "market_position", "sid": 23, "msg": other})
    assert lifecycle.current_epoch_id is None


def test_private_lifecycle_binds_demo_principal_to_demo_websocket_host() -> None:
    lifecycle = KalshiPrivateWSLifecycle(signer=_signer(), principal_origin=KALSHI_DEMO_ORIGIN)

    instructions = lifecycle.begin_connection(timestamp_ms=1_785_873_600_000)

    assert instructions.url == "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
