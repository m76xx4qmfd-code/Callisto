from __future__ import annotations

from decimal import Decimal

import pytest

from services import venues
from services.venues.kalshi_v2_ws import (
    KalshiOrderbookDelta,
    KalshiOrderbookSnapshot,
    KalshiOrderbookState,
    KalshiWSContinuityError,
    KalshiWSProtocolError,
)

MARKET_TICKER = "FED-23DEC-T3.00"
MARKET_ID = "9b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a1"


def test_public_venue_package_exports_orderbook_boundaries() -> None:
    assert venues.KalshiOrderbookSnapshot is KalshiOrderbookSnapshot
    assert venues.KalshiOrderbookDelta is KalshiOrderbookDelta
    assert venues.KalshiOrderbookState is KalshiOrderbookState


def _state(*, sid: int = 2, market_ticker: str = MARKET_TICKER, market_id: str = MARKET_ID) -> KalshiOrderbookState:
    return KalshiOrderbookState(expected_sid=sid, market_ticker=market_ticker, market_id=market_id)


def _snapshot_payload(
    *,
    sid: object = 2,
    seq: object = 10,
    market_ticker: object = MARKET_TICKER,
    market_id: object = MARKET_ID,
    yes: object = None,
    no: object = None,
) -> dict[str, object]:
    msg: dict[str, object] = {
        "market_ticker": market_ticker,
        "market_id": market_id,
    }
    if yes is not None:
        msg["yes_dollars_fp"] = yes
    if no is not None:
        msg["no_dollars_fp"] = no
    return {"type": "orderbook_snapshot", "sid": sid, "seq": seq, "msg": msg}


def _delta_payload(
    *,
    sid: object = 2,
    seq: object = 11,
    market_ticker: object = MARKET_TICKER,
    market_id: object = MARKET_ID,
    price: object = "0.220000",
    delta: object = "1.25",
    side: object = "yes",
) -> dict[str, object]:
    return {
        "type": "orderbook_delta",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": market_ticker,
            "market_id": market_id,
            "price_dollars": price,
            "delta_fp": delta,
            "side": side,
        },
    }


def test_parses_official_snapshot_shape_as_exact_decimal_levels() -> None:
    snapshot = KalshiOrderbookSnapshot.from_payload(
        _snapshot_payload(
            yes=[["0.0800", "300.00"], ["0.2200", "333.00"]],
            no=[["0.5400", "20.00"], ["0.5600", "146.00"]],
        )
    )

    assert snapshot.sid == 2
    assert snapshot.seq == 10
    assert snapshot.market_ticker == MARKET_TICKER
    assert snapshot.market_id == MARKET_ID
    assert snapshot.yes_levels == (
        (Decimal("0.0800"), Decimal("300.00")),
        (Decimal("0.2200"), Decimal("333.00")),
    )
    assert snapshot.no_levels == (
        (Decimal("0.5400"), Decimal("20.00")),
        (Decimal("0.5600"), Decimal("146.00")),
    )
    assert type(snapshot.yes_levels[0][0]) is Decimal
    assert type(snapshot.yes_levels[0][1]) is Decimal


def test_missing_snapshot_sides_are_empty_and_duplicate_prices_fail_closed() -> None:
    snapshot = KalshiOrderbookSnapshot.from_payload(_snapshot_payload())
    assert snapshot.yes_levels == ()
    assert snapshot.no_levels == ()

    with pytest.raises(KalshiWSProtocolError, match="duplicate yes price level"):
        KalshiOrderbookSnapshot.from_payload(_snapshot_payload(yes=[["0.500000", "1.00"], ["0.500000", "2.00"]]))


@pytest.mark.parametrize(
    ("field", "payload", "message"),
    [
        ("price", _delta_payload(price=0.22), "price_dollars must be a fixed-point string"),
        ("delta", _delta_payload(delta=1), "delta_fp must be a fixed-point string"),
        ("price", _delta_payload(price="1.0000001"), "price_dollars has more precision"),
        ("delta", _delta_payload(delta="0.001"), "delta_fp has more precision"),
        ("price", _delta_payload(price="1.000001"), "price_dollars must be between 0 and 1"),
        ("price", _delta_payload(price="NaN"), "price_dollars must be a finite decimal"),
    ],
)
def test_fixed_point_fields_reject_numeric_json_excess_precision_and_invalid_ranges(
    field: str,
    payload: dict[str, object],
    message: str,
) -> None:
    assert field
    with pytest.raises(KalshiWSProtocolError, match=message):
        KalshiOrderbookDelta.from_payload(payload)


def test_fixed_point_boundaries_and_optional_delta_fields_are_strict() -> None:
    lower = KalshiOrderbookDelta.from_payload(_delta_payload(price="0", delta="-1.50"))
    upper = KalshiOrderbookDelta.from_payload(_delta_payload(price="1.000000", delta="1.50"))

    assert lower.price == Decimal(0)
    assert lower.delta == Decimal("-1.50")
    assert upper.price == Decimal("1.000000")
    assert upper.delta == Decimal("1.50")

    payload = _delta_payload()
    payload["msg"] = {**payload["msg"], "subaccount": True}  # type: ignore[dict-item]
    with pytest.raises(KalshiWSProtocolError, match="subaccount must be an integer"):
        KalshiOrderbookDelta.from_payload(payload)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (_snapshot_payload(sid=0), "sid must be a positive integer"),
        (_snapshot_payload(seq=True), "seq must be a positive integer"),
        (_snapshot_payload(market_ticker="bad ticker"), "invalid market_ticker"),
        (_snapshot_payload(market_id="not-a-uuid"), "market_id must be a UUID"),
        (_snapshot_payload(yes=[["0.10"]]), "price level must contain exactly two strings"),
        (_snapshot_payload(yes=[["0.10", 1]]), "price level must contain exactly two strings"),
        (_snapshot_payload(yes=[["0.10", "0.00"]]), "snapshot quantity must be positive"),
    ],
)
def test_snapshot_wire_identity_and_level_shape_fail_closed(payload: dict[str, object], message: str) -> None:
    with pytest.raises(KalshiWSProtocolError, match=message):
        KalshiOrderbookSnapshot.from_payload(payload)


def test_state_applies_contiguous_deltas_deletes_zero_levels_and_is_immutable() -> None:
    state = _state()
    initial = state.apply_snapshot(
        KalshiOrderbookSnapshot.from_payload(_snapshot_payload(yes=[["0.220000", "2.00"]], no=[["0.560000", "3.00"]]))
    )
    updated = state.apply_delta(KalshiOrderbookDelta.from_payload(_delta_payload(delta="1.25")))
    deleted = state.apply_delta(
        KalshiOrderbookDelta.from_payload(_delta_payload(seq=12, price="0.560000", delta="-3.00", side="no"))
    )

    assert initial.yes_levels == ((Decimal("0.220000"), Decimal("2.00")),)
    assert updated.yes_levels == ((Decimal("0.220000"), Decimal("3.25")),)
    assert deleted.yes_levels == ((Decimal("0.220000"), Decimal("3.25")),)
    assert deleted.no_levels == ()
    assert deleted.seq == 12
    assert state.view is deleted
    assert state.is_valid is True


def test_reapplying_snapshot_replaces_state_without_accumulation() -> None:
    state = _state()
    snapshot = KalshiOrderbookSnapshot.from_payload(_snapshot_payload(yes=[["0.220000", "2.00"]]))

    first = state.apply_snapshot(snapshot)
    second = state.apply_snapshot(snapshot)

    assert first == second
    assert second.yes_levels == ((Decimal("0.220000"), Decimal("2.00")),)


@pytest.mark.parametrize("seq", [11, 9, 13])
def test_duplicate_regression_and_forward_gap_invalidate_matching_stream(seq: int) -> None:
    state = _state()
    state.apply_snapshot(KalshiOrderbookSnapshot.from_payload(_snapshot_payload()))
    if seq == 11:
        state.apply_delta(KalshiOrderbookDelta.from_payload(_delta_payload(seq=11)))

    with pytest.raises(KalshiWSContinuityError, match="non-contiguous sequence"):
        state.apply_delta(KalshiOrderbookDelta.from_payload(_delta_payload(seq=seq)))

    assert state.is_valid is False
    assert state.view is None
    recovery = state.recovery_required
    assert recovery is not None
    assert recovery.sid == 2
    assert recovery.market_ticker == MARKET_TICKER
    assert recovery.command(command_id=17) == {
        "id": 17,
        "cmd": "update_subscription",
        "params": {
            "sids": [2],
            "market_tickers": [MARKET_TICKER],
            "action": "get_snapshot",
        },
    }


@pytest.mark.parametrize(
    "payload",
    [
        _delta_payload(market_id="11111111-1111-1111-1111-111111111111"),
        _delta_payload(market_ticker="OTHER-MARKET"),
        _delta_payload(delta="-1.00"),
    ],
)
def test_matching_stream_identity_or_negative_level_error_invalidates_state(payload: dict[str, object]) -> None:
    state = _state()
    state.apply_snapshot(KalshiOrderbookSnapshot.from_payload(_snapshot_payload()))

    with pytest.raises(KalshiWSContinuityError):
        state.apply_delta(KalshiOrderbookDelta.from_payload(payload))

    assert state.is_valid is False
    assert state.view is None


def test_post_invalidation_refuses_deltas_until_a_new_snapshot() -> None:
    state = _state()
    state.apply_snapshot(KalshiOrderbookSnapshot.from_payload(_snapshot_payload()))

    with pytest.raises(KalshiWSContinuityError):
        state.apply_delta(KalshiOrderbookDelta.from_payload(_delta_payload(seq=13)))
    with pytest.raises(KalshiWSContinuityError, match="requires a fresh snapshot"):
        state.apply_delta(KalshiOrderbookDelta.from_payload(_delta_payload(seq=14)))

    recovered = state.apply_snapshot(KalshiOrderbookSnapshot.from_payload(_snapshot_payload(seq=20)))
    updated = state.apply_delta(KalshiOrderbookDelta.from_payload(_delta_payload(seq=21)))
    assert recovered.seq == 20
    assert updated.seq == 21
    assert state.is_valid is True


def test_stale_or_wrong_identity_snapshots_cannot_replace_acknowledged_stream() -> None:
    state = _state()
    initial = state.apply_snapshot(KalshiOrderbookSnapshot.from_payload(_snapshot_payload(seq=10)))
    current = state.apply_delta(KalshiOrderbookDelta.from_payload(_delta_payload(seq=11)))

    stale_snapshots = (
        _snapshot_payload(sid=3, seq=12),
        _snapshot_payload(seq=12, market_ticker="OTHER-MARKET"),
        _snapshot_payload(seq=12, market_id="11111111-1111-1111-1111-111111111111"),
        _snapshot_payload(seq=9),
    )
    for payload in stale_snapshots:
        with pytest.raises(KalshiWSContinuityError):
            state.apply_snapshot(KalshiOrderbookSnapshot.from_payload(payload))
        assert state.is_valid is True
        assert state.view is current
        assert state.recovery_required is None

    replacement = _state(sid=3)
    replaced = replacement.apply_snapshot(KalshiOrderbookSnapshot.from_payload(_snapshot_payload(sid=3, seq=1)))
    assert initial.sid == 2
    assert replaced.sid == 3


def test_recovery_snapshot_must_match_binding_and_advance_sequence() -> None:
    state = _state()
    state.apply_snapshot(KalshiOrderbookSnapshot.from_payload(_snapshot_payload(seq=10)))
    with pytest.raises(KalshiWSContinuityError):
        state.apply_delta(KalshiOrderbookDelta.from_payload(_delta_payload(seq=13)))

    for payload in (_snapshot_payload(sid=3, seq=14), _snapshot_payload(seq=10)):
        with pytest.raises(KalshiWSContinuityError):
            state.apply_snapshot(KalshiOrderbookSnapshot.from_payload(payload))
        assert state.is_valid is False
        assert state.view is None
        assert state.recovery_required is not None

    recovered = state.apply_snapshot(KalshiOrderbookSnapshot.from_payload(_snapshot_payload(seq=14)))
    assert recovered.seq == 14
    assert state.is_valid is True
    assert state.recovery_required is None


def test_state_requires_snapshot_before_any_delta() -> None:
    state = _state()

    with pytest.raises(KalshiWSContinuityError, match="requires a snapshot"):
        state.apply_delta(KalshiOrderbookDelta.from_payload(_delta_payload()))

    assert state.is_valid is False
    assert state.recovery_required is None


def test_recovery_command_requires_positive_correlatable_command_id() -> None:
    state = _state()
    state.apply_snapshot(KalshiOrderbookSnapshot.from_payload(_snapshot_payload()))
    with pytest.raises(KalshiWSContinuityError):
        state.apply_delta(KalshiOrderbookDelta.from_payload(_delta_payload(seq=13)))

    recovery = state.recovery_required
    assert recovery is not None
    with pytest.raises(KalshiWSProtocolError, match="command_id must be a positive integer"):
        recovery.command(command_id=0)
