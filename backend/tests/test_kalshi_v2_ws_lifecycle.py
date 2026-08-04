from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from services.venues.kalshi_v2 import KalshiRequestSigner
from services.venues.kalshi_v2_ws import KalshiOrderbookDelta, KalshiOrderbookSnapshot
from services.venues.kalshi_v2_ws_lifecycle import (
    KALSHI_V2_WS_URL,
    KalshiV2WSLifecycle,
    KalshiWSConnectionInstructions,
    KalshiWSErrorResponse,
    KalshiWSLifecycleError,
    KalshiWSSubscribed,
)

MARKET_TICKER = "FED-23DEC-T3.00"
MARKET_ID = "9b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a1"


def _signer() -> tuple[KalshiRequestSigner, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return KalshiRequestSigner(key_id="test-key", private_key_pem=pem), key


def _lifecycle() -> tuple[KalshiV2WSLifecycle, rsa.RSAPrivateKey]:
    signer, key = _signer()
    return KalshiV2WSLifecycle(signer=signer, market_ticker=MARKET_TICKER, market_id=MARKET_ID), key


def _snapshot(*, sid: int, seq: int = 10) -> KalshiOrderbookSnapshot:
    return KalshiOrderbookSnapshot.from_payload(
        {
            "type": "orderbook_snapshot",
            "sid": sid,
            "seq": seq,
            "msg": {
                "market_ticker": MARKET_TICKER,
                "market_id": MARKET_ID,
                "yes_dollars_fp": [["0.220000", "2.00"]],
                "no_dollars_fp": [["0.560000", "3.00"]],
            },
        }
    )


def _delta(*, sid: int, seq: int) -> KalshiOrderbookDelta:
    return KalshiOrderbookDelta.from_payload(
        {
            "type": "orderbook_delta",
            "sid": sid,
            "seq": seq,
            "msg": {
                "market_ticker": MARKET_TICKER,
                "market_id": MARKET_ID,
                "price_dollars": "0.220000",
                "delta_fp": "1.00",
                "side": "yes",
            },
        }
    )


def _ack(command_id: int, channel: str, sid: int) -> KalshiWSSubscribed:
    return KalshiWSSubscribed.from_payload(
        {"id": command_id, "type": "subscribed", "msg": {"channel": channel, "sid": sid}}
    )


def _ack_all(lifecycle: KalshiV2WSLifecycle, instructions: KalshiWSConnectionInstructions) -> None:
    for sid, command in enumerate(instructions.subscriptions, start=11):
        lifecycle.receive_subscribed(
            instructions.epoch_id,
            _ack(command.command_id, command.channel, sid),
        )


def test_begin_connection_builds_fresh_signed_epoch_and_exact_subscriptions() -> None:
    lifecycle, key = _lifecycle()

    first = lifecycle.begin_connection(timestamp_ms=1_785_844_800_000)
    second = lifecycle.begin_connection(timestamp_ms=1_785_844_800_001)

    assert first.url == KALSHI_V2_WS_URL
    assert first.epoch_id == 1
    assert second.epoch_id == 2
    assert first.headers["KALSHI-ACCESS-TIMESTAMP"] != second.headers["KALSHI-ACCESS-TIMESTAMP"]
    signature = base64.b64decode(second.headers["KALSHI-ACCESS-SIGNATURE"])
    key.public_key().verify(
        signature,
        b"1785844800001GET/trade-api/ws/v2",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
        hashes.SHA256(),
    )
    assert [command.payload for command in second.subscriptions] == [
        {
            "id": 4,
            "cmd": "subscribe",
            "params": {"channels": ["orderbook_delta"], "market_ticker": MARKET_TICKER},
        },
        {"id": 5, "cmd": "subscribe", "params": {"channels": ["user_orders"]}},
        {"id": 6, "cmd": "subscribe", "params": {"channels": ["fill"]}},
    ]
    assert lifecycle.orderbook_publishable is False
    assert lifecycle.private_stream_healthy is False
    assert lifecycle.connection_healthy is False
    assert lifecycle.retry_allowed is False

    with pytest.raises(KalshiWSLifecycleError, match="strictly increase"):
        lifecycle.begin_connection(timestamp_ms=1_785_844_800_001)


def test_subscribed_and_error_responses_are_strict() -> None:
    assert _ack(1, "orderbook_delta", 11) == KalshiWSSubscribed(command_id=1, channel="orderbook_delta", sid=11)
    error = KalshiWSErrorResponse.from_payload(
        {"id": 1, "type": "error", "msg": {"code": 25, "msg": "Subscription buffer overflow"}}
    )
    assert error.code == 25
    assert error.command_id == 1

    with pytest.raises(KalshiWSLifecycleError, match="command id"):
        KalshiWSSubscribed.from_payload({"type": "subscribed", "msg": {"channel": "fill", "sid": 1}})
    with pytest.raises(KalshiWSLifecycleError, match="error code"):
        KalshiWSErrorResponse.from_payload({"id": 1, "type": "error", "msg": {"code": 0, "msg": "bad"}})


@pytest.mark.parametrize(
    "channels",
    [
        ("orderbook_delta", "user_orders", "fill"),
        ("fill", "orderbook_delta", "user_orders"),
        ("user_orders", "fill", "orderbook_delta"),
    ],
)
def test_ack_order_is_irrelevant_and_early_snapshot_stays_unpublished(channels: tuple[str, ...]) -> None:
    lifecycle, _ = _lifecycle()
    instructions = lifecycle.begin_connection(timestamp_ms=1_785_844_800_000)
    ids = {command.channel: command.command_id for command in instructions.subscriptions}
    sids = {"orderbook_delta": 11, "user_orders": 12, "fill": 13}

    lifecycle.receive_snapshot(instructions.epoch_id, _snapshot(sid=11))
    assert lifecycle.orderbook_publishable is False
    for channel in channels:
        lifecycle.receive_subscribed(instructions.epoch_id, _ack(ids[channel], channel, sids[channel]))

    assert lifecycle.orderbook_publishable is True
    assert lifecycle.orderbook_view is not None
    assert lifecycle.orderbook_view.seq == 10
    assert lifecycle.private_stream_healthy is False
    assert lifecycle.degraded_reason == "authoritative_portfolio_reconciliation_not_implemented"


def test_duplicate_mismatched_and_old_epoch_ack_fail_closed() -> None:
    lifecycle, _ = _lifecycle()
    first = lifecycle.begin_connection(timestamp_ms=1_785_844_800_000)
    orderbook = first.subscriptions[0]
    lifecycle.receive_subscribed(first.epoch_id, _ack(orderbook.command_id, orderbook.channel, 11))
    with pytest.raises(KalshiWSLifecycleError, match="already acknowledged"):
        lifecycle.receive_subscribed(first.epoch_id, _ack(orderbook.command_id, orderbook.channel, 11))
    assert lifecycle.orderbook_publishable is False
    assert lifecycle.terminal_reason == "invalid_subscription_acknowledgement"

    second = lifecycle.begin_connection(timestamp_ms=1_785_844_800_001)
    with pytest.raises(KalshiWSLifecycleError, match="current connection epoch"):
        lifecycle.receive_subscribed(first.epoch_id, _ack(first.subscriptions[1].command_id, "user_orders", 12))
    assert lifecycle.current_epoch_id == second.epoch_id

    fill = second.subscriptions[2]
    with pytest.raises(KalshiWSLifecycleError, match="channel does not match"):
        lifecycle.receive_subscribed(second.epoch_id, _ack(fill.command_id, "user_orders", 13))
    assert lifecycle.terminal_reason == "invalid_subscription_acknowledgement"


def test_disconnect_retracts_publication_synchronously_and_old_frames_cannot_restore_it() -> None:
    lifecycle, _ = _lifecycle()
    instructions = lifecycle.begin_connection(timestamp_ms=1_785_844_800_000)
    _ack_all(lifecycle, instructions)
    lifecycle.receive_snapshot(instructions.epoch_id, _snapshot(sid=11))
    assert lifecycle.orderbook_publishable is True

    lifecycle.disconnect(instructions.epoch_id)
    assert lifecycle.orderbook_publishable is False
    assert lifecycle.orderbook_view is None
    assert lifecycle.current_epoch_id is None
    with pytest.raises(KalshiWSLifecycleError, match="no active connection epoch"):
        lifecycle.receive_delta(instructions.epoch_id, _delta(sid=11, seq=11))


def test_gap_suppresses_publication_and_only_advancing_snapshot_recovers() -> None:
    lifecycle, _ = _lifecycle()
    instructions = lifecycle.begin_connection(timestamp_ms=1_785_844_800_000)
    _ack_all(lifecycle, instructions)
    lifecycle.receive_snapshot(instructions.epoch_id, _snapshot(sid=11, seq=10))

    with pytest.raises(KalshiWSLifecycleError, match="non-contiguous sequence"):
        lifecycle.receive_delta(instructions.epoch_id, _delta(sid=11, seq=13))
    assert lifecycle.orderbook_publishable is False
    assert lifecycle.recovery_command() == {
        "id": 4,
        "cmd": "update_subscription",
        "params": {"sids": [11], "market_tickers": [MARKET_TICKER], "action": "get_snapshot"},
    }
    with pytest.raises(KalshiWSLifecycleError, match="fresh snapshot"):
        lifecycle.receive_delta(instructions.epoch_id, _delta(sid=11, seq=14))
    for stale_sequence in (10, 11, 12, 13):
        with pytest.raises(KalshiWSLifecycleError, match="observed gap"):
            lifecycle.receive_snapshot(instructions.epoch_id, _snapshot(sid=11, seq=stale_sequence))
        assert lifecycle.orderbook_publishable is False

    lifecycle.receive_snapshot(instructions.epoch_id, _snapshot(sid=11, seq=14))
    assert lifecycle.orderbook_publishable is True
    assert lifecycle.orderbook_view is not None
    assert lifecycle.orderbook_view.seq == 14


def test_conflicting_same_identity_snapshot_terminates_publishable_epoch() -> None:
    lifecycle, _ = _lifecycle()
    instructions = lifecycle.begin_connection(timestamp_ms=1_785_844_800_000)
    _ack_all(lifecycle, instructions)
    lifecycle.receive_snapshot(instructions.epoch_id, _snapshot(sid=11, seq=10))

    with pytest.raises(KalshiWSLifecycleError, match="unsolicited snapshot"):
        lifecycle.receive_snapshot(instructions.epoch_id, _snapshot(sid=11, seq=11))

    assert lifecycle.orderbook_publishable is False
    assert lifecycle.orderbook_view is None
    assert lifecycle.current_epoch_id is None
    assert lifecycle.terminal_reason == "invalid_orderbook_snapshot"


def test_wrong_sid_delta_and_mismatched_snapshot_retract_publication() -> None:
    lifecycle, _ = _lifecycle()
    instructions = lifecycle.begin_connection(timestamp_ms=1_785_844_800_000)
    _ack_all(lifecycle, instructions)
    lifecycle.receive_snapshot(instructions.epoch_id, _snapshot(sid=11))

    with pytest.raises(KalshiWSLifecycleError, match="active subscription"):
        lifecycle.receive_delta(instructions.epoch_id, _delta(sid=99, seq=11))
    assert lifecycle.orderbook_publishable is False
    assert lifecycle.orderbook_view is None
    assert lifecycle.current_epoch_id is None

    replacement = lifecycle.begin_connection(timestamp_ms=1_785_844_800_001)
    _ack_all(lifecycle, replacement)
    lifecycle.receive_snapshot(replacement.epoch_id, _snapshot(sid=11))
    wrong_snapshot = KalshiOrderbookSnapshot.from_payload(
        {
            "type": "orderbook_snapshot",
            "sid": 11,
            "seq": 11,
            "msg": {
                "market_ticker": "OTHER-MARKET",
                "market_id": MARKET_ID,
            },
        }
    )
    with pytest.raises(KalshiWSLifecycleError, match="active subscription"):
        lifecycle.receive_snapshot(replacement.epoch_id, wrong_snapshot)
    assert lifecycle.orderbook_publishable is False
    assert lifecycle.orderbook_view is None
    assert lifecycle.current_epoch_id is None


@pytest.mark.parametrize("code", [10, 17, 25])
def test_terminal_channel_errors_terminate_epoch_without_retry_authorization(code: int) -> None:
    lifecycle, _ = _lifecycle()
    instructions = lifecycle.begin_connection(timestamp_ms=1_785_844_800_000)
    command = instructions.subscriptions[0]

    lifecycle.receive_error(
        instructions.epoch_id,
        KalshiWSErrorResponse(command_id=command.command_id, code=code, message="terminal"),
    )

    assert lifecycle.current_epoch_id is None
    assert lifecycle.orderbook_publishable is False
    assert lifecycle.terminal_reason == f"kalshi_ws_error:{code}"
    assert lifecycle.retry_allowed is False
