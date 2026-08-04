from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from services import venues
from services.venues.kalshi_v2 import KalshiRequestSigner
from services.venues.kalshi_v2_ws_lifecycle import KalshiV2WSLifecycle
from services.venues.kalshi_v2_ws_session import (
    KalshiV2WSFrameSession,
    KalshiWSSessionNoOp,
    KalshiWSSessionPrivateFrame,
    KalshiWSSessionPublished,
    KalshiWSSessionRecoveryRequested,
    KalshiWSSessionTerminated,
)

MARKET_TICKER = "FED-23DEC-T3.00"
MARKET_ID = "9b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a1"


def test_session_boundary_is_exported_from_venue_package() -> None:
    assert venues.KalshiV2WSFrameSession is KalshiV2WSFrameSession


def _session() -> KalshiV2WSFrameSession:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    lifecycle = KalshiV2WSLifecycle(
        signer=KalshiRequestSigner(key_id="test-key", private_key_pem=pem),
        market_ticker=MARKET_TICKER,
        market_id=MARKET_ID,
    )
    return KalshiV2WSFrameSession(lifecycle)


def _snapshot(sid: int, seq: int) -> dict[str, object]:
    return {
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


def _delta(sid: int, seq: int) -> dict[str, object]:
    return {
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


def _ack_all(session: KalshiV2WSFrameSession, epoch_id: int, instructions) -> dict[str, int]:
    sids = {"orderbook_delta": 11, "user_orders": 12, "fill": 13}
    for command in instructions.subscriptions:
        outcome = session.receive(
            epoch_id,
            {
                "id": command.command_id,
                "type": "subscribed",
                "msg": {"channel": command.channel, "sid": sids[command.channel]},
            },
        )
        assert isinstance(outcome, KalshiWSSessionNoOp)
    return sids


def test_happy_path_requires_all_acknowledgements_and_publishes_monotonic_views() -> None:
    session = _session()
    instructions = session.begin(timestamp_ms=1_785_844_800_000)
    orderbook = instructions.subscriptions[0]

    assert isinstance(
        session.receive(instructions.epoch_id, _snapshot(11, 10)),
        KalshiWSSessionNoOp,
    )
    assert isinstance(
        session.receive(
            instructions.epoch_id,
            {"id": orderbook.command_id, "type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 11}},
        ),
        KalshiWSSessionNoOp,
    )
    assert session.lifecycle.orderbook_publishable is False

    unpublished_delta = session.receive(instructions.epoch_id, _delta(11, 11))
    assert unpublished_delta == KalshiWSSessionNoOp(
        epoch_id=instructions.epoch_id,
        reason="delta_not_publishable",
    )

    outcomes = [
        session.receive(
            instructions.epoch_id,
            {"id": command.command_id, "type": "subscribed", "msg": {"channel": command.channel, "sid": sid}},
        )
        for command, sid in zip(instructions.subscriptions[1:], (12, 13), strict=True)
    ]
    assert isinstance(outcomes[-1], KalshiWSSessionPublished)
    assert outcomes[-1].view.seq == 11

    published = session.receive(instructions.epoch_id, _delta(11, 12))
    assert isinstance(published, KalshiWSSessionPublished)
    assert published.view.seq == 12
    assert published.view.yes_levels == ((published.view.yes_levels[0][0], published.view.yes_levels[0][1]),)
    assert str(published.view.yes_levels[0][0]) == "0.220000"
    assert str(published.view.yes_levels[0][1]) == "4.00"


def test_gap_emits_one_recovery_command_advances_watermark_and_restores_at_exact_boundary() -> None:
    session = _session()
    instructions = session.begin(timestamp_ms=1_785_844_800_000)
    _ack_all(session, instructions.epoch_id, instructions)
    assert isinstance(session.receive(instructions.epoch_id, _snapshot(11, 10)), KalshiWSSessionPublished)

    recovery = session.receive(instructions.epoch_id, _delta(11, 13))
    assert isinstance(recovery, KalshiWSSessionRecoveryRequested)
    assert recovery.command == {
        "id": 4,
        "cmd": "update_subscription",
        "params": {"sids": [11], "market_tickers": [MARKET_TICKER], "action": "get_snapshot"},
    }
    assert session.lifecycle.orderbook_publishable is False
    assert session.lifecycle.retry_allowed is False

    for seq in (14, 16):
        assert isinstance(session.receive(instructions.epoch_id, _delta(11, seq)), KalshiWSSessionNoOp)
    assert isinstance(session.receive(instructions.epoch_id, _snapshot(11, 15)), KalshiWSSessionNoOp)
    restored = session.receive(instructions.epoch_id, _snapshot(11, 16))
    assert isinstance(restored, KalshiWSSessionPublished)
    assert restored.view.seq == 16


def test_semantic_delta_failure_retains_highest_observed_recovery_sequence() -> None:
    session = _session()
    instructions = session.begin(timestamp_ms=1_785_844_800_000)
    _ack_all(session, instructions.epoch_id, instructions)
    assert isinstance(session.receive(instructions.epoch_id, _snapshot(11, 10)), KalshiWSSessionPublished)

    invalid = _delta(11, 11)
    invalid_message = invalid["msg"]
    assert isinstance(invalid_message, dict)
    invalid_message["delta_fp"] = "-3.00"
    assert isinstance(
        session.receive(instructions.epoch_id, invalid),
        KalshiWSSessionRecoveryRequested,
    )
    assert isinstance(session.receive(instructions.epoch_id, _delta(11, 14)), KalshiWSSessionNoOp)
    assert isinstance(session.receive(instructions.epoch_id, _snapshot(11, 13)), KalshiWSSessionNoOp)

    restored = session.receive(instructions.epoch_id, _snapshot(11, 14))
    assert isinstance(restored, KalshiWSSessionPublished)
    assert restored.view.seq == 14


def test_recovery_before_private_acks_rearms_later_recovery_request() -> None:
    session = _session()
    instructions = session.begin(timestamp_ms=1_785_844_800_000)
    orderbook = instructions.subscriptions[0]
    assert isinstance(
        session.receive(
            instructions.epoch_id,
            {"id": orderbook.command_id, "type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 11}},
        ),
        KalshiWSSessionNoOp,
    )
    assert isinstance(session.receive(instructions.epoch_id, _snapshot(11, 10)), KalshiWSSessionNoOp)
    assert isinstance(session.receive(instructions.epoch_id, _delta(11, 13)), KalshiWSSessionRecoveryRequested)
    assert isinstance(session.receive(instructions.epoch_id, _snapshot(11, 13)), KalshiWSSessionNoOp)

    outcomes = [
        session.receive(
            instructions.epoch_id,
            {"id": command.command_id, "type": "subscribed", "msg": {"channel": command.channel, "sid": sid}},
        )
        for command, sid in zip(instructions.subscriptions[1:], (12, 13), strict=True)
    ]
    assert isinstance(outcomes[-1], KalshiWSSessionPublished)

    second_recovery = session.receive(instructions.epoch_id, _delta(11, 15))
    assert isinstance(second_recovery, KalshiWSSessionRecoveryRequested)
    assert second_recovery.command["id"] == 5


def test_private_frames_are_typed_but_never_publish_or_promote_health() -> None:
    session = _session()
    instructions = session.begin(timestamp_ms=1_785_844_800_000)
    sids = _ack_all(session, instructions.epoch_id, instructions)

    for frame_type, channel in (("user_order", "user_orders"), ("fill", "fill")):
        outcome = session.receive(
            instructions.epoch_id,
            {"type": frame_type, "sid": sids[channel], "msg": {"opaque": "not-consumed"}},
        )
        assert outcome == KalshiWSSessionPrivateFrame(
            epoch_id=instructions.epoch_id,
            channel=channel,
            sid=sids[channel],
        )
        assert session.lifecycle.private_stream_healthy is False
        assert session.lifecycle.connection_healthy is False
        assert session.lifecycle.retry_allowed is False


def test_malformed_unknown_and_wrong_private_sid_frames_terminate_and_retract() -> None:
    payloads = (
        {"type": "orderbook_delta", "sid": 11, "seq": "bad", "msg": {}},
        {"type": "new_unmodeled_type", "sid": 11, "msg": {}},
        {"type": "fill", "sid": 99, "msg": {}},
    )
    for payload in payloads:
        session = _session()
        instructions = session.begin(timestamp_ms=1_785_844_800_000)
        _ack_all(session, instructions.epoch_id, instructions)
        assert isinstance(session.receive(instructions.epoch_id, _snapshot(11, 10)), KalshiWSSessionPublished)

        outcome = session.receive(instructions.epoch_id, payload)
        assert isinstance(outcome, KalshiWSSessionTerminated)
        assert session.lifecycle.orderbook_publishable is False
        assert session.lifecycle.orderbook_view is None
        assert session.lifecycle.current_epoch_id is None


def test_stale_epoch_frame_is_noop_and_cannot_kill_current_epoch() -> None:
    session = _session()
    first = session.begin(timestamp_ms=1_785_844_800_000)
    second = session.begin(timestamp_ms=1_785_844_800_001)

    outcome = session.receive(first.epoch_id, _snapshot(11, 10))

    assert outcome == KalshiWSSessionNoOp(epoch_id=first.epoch_id, reason="stale_epoch")
    assert session.lifecycle.current_epoch_id == second.epoch_id
    assert session.lifecycle.retry_allowed is False
