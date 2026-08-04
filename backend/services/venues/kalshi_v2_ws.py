"""Strict read-only Kalshi V2 orderbook WebSocket models and continuity state.

The module performs no network I/O. It models one acknowledged orderbook
subscription at a time so sequence continuity is evaluated only within that
subscription's snapshot/delta stream.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, cast
from uuid import UUID

PriceLevel = tuple[Decimal, Decimal]
MarketSide = Literal["yes", "no"]

_MARKET_TICKER = re.compile(r"^[A-Z0-9.-]+$")
_PRICE_QUANTUM = Decimal("0.000001")
_QUANTITY_QUANTUM = Decimal("0.01")


class KalshiWSProtocolError(ValueError):
    """Raised when a WebSocket payload violates Callisto's V2 boundary."""


class KalshiWSContinuityError(RuntimeError):
    """Raised when orderbook state cannot be used without a fresh snapshot."""


def _mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise KalshiWSProtocolError(f"{field} must be an object")
    return value


def _positive_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise KalshiWSProtocolError(f"{field} must be a positive integer")
    return value


def _optional_nonnegative_integer(payload: Mapping[str, object], field: str) -> int | None:
    value = payload.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise KalshiWSProtocolError(f"{field} must be an integer")
    return value


def _text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise KalshiWSProtocolError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(payload: Mapping[str, object], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise KalshiWSProtocolError(f"{field} must be a non-empty string when present")
    return value.strip()


def _fixed_point(value: object, *, field: str, quantum: Decimal) -> Decimal:
    if not isinstance(value, str):
        raise KalshiWSProtocolError(f"{field} must be a fixed-point string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise KalshiWSProtocolError(f"{field} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise KalshiWSProtocolError(f"{field} must be a finite decimal")
    exponent = parsed.as_tuple().exponent
    quantum_exponent = quantum.as_tuple().exponent
    if not isinstance(exponent, int) or not isinstance(quantum_exponent, int) or exponent < quantum_exponent:
        raise KalshiWSProtocolError(f"{field} has more precision than Callisto permits ({quantum})")
    try:
        if parsed.quantize(quantum) != parsed:
            raise KalshiWSProtocolError(f"{field} has more precision than Callisto permits ({quantum})")
    except InvalidOperation as exc:
        raise KalshiWSProtocolError(f"{field} is outside Callisto's fixed-point range") from exc
    return parsed


def _identity(payload: Mapping[str, object]) -> tuple[str, str]:
    market_ticker = _text(payload, "market_ticker")
    if _MARKET_TICKER.fullmatch(market_ticker) is None:
        raise KalshiWSProtocolError("invalid market_ticker")
    market_id = _text(payload, "market_id")
    try:
        UUID(market_id)
    except ValueError as exc:
        raise KalshiWSProtocolError("market_id must be a UUID") from exc
    return market_ticker, market_id


def _envelope(payload: Mapping[str, object], *, expected_type: str) -> tuple[int, int, Mapping[str, object]]:
    if payload.get("type") != expected_type:
        raise KalshiWSProtocolError(f"type must be '{expected_type}'")
    sid = _positive_integer(payload.get("sid"), field="sid")
    seq = _positive_integer(payload.get("seq"), field="seq")
    return sid, seq, _mapping(payload.get("msg"), field="msg")


def _levels(payload: Mapping[str, object], field: str, *, side: MarketSide) -> tuple[PriceLevel, ...]:
    raw_levels = payload.get(field, [])
    if not isinstance(raw_levels, list):
        raise KalshiWSProtocolError(f"{field} must be an array")
    levels: dict[Decimal, Decimal] = {}
    for raw_level in raw_levels:
        if (
            not isinstance(raw_level, list)
            or len(raw_level) != 2
            or not all(isinstance(item, str) for item in raw_level)
        ):
            raise KalshiWSProtocolError("price level must contain exactly two strings")
        price = _fixed_point(raw_level[0], field=f"{field} price", quantum=_PRICE_QUANTUM)
        quantity = _fixed_point(raw_level[1], field=f"{field} quantity", quantum=_QUANTITY_QUANTUM)
        if not Decimal(0) <= price <= Decimal(1):
            raise KalshiWSProtocolError("snapshot price must be between 0 and 1")
        if quantity <= 0:
            raise KalshiWSProtocolError("snapshot quantity must be positive")
        if price in levels:
            raise KalshiWSProtocolError(f"duplicate {side} price level")
        levels[price] = quantity
    return tuple(sorted(levels.items()))


@dataclass(frozen=True)
class KalshiOrderbookSnapshot:
    sid: int
    seq: int
    market_ticker: str
    market_id: str
    yes_levels: tuple[PriceLevel, ...]
    no_levels: tuple[PriceLevel, ...]

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> KalshiOrderbookSnapshot:
        sid, seq, message = _envelope(payload, expected_type="orderbook_snapshot")
        market_ticker, market_id = _identity(message)
        return cls(
            sid=sid,
            seq=seq,
            market_ticker=market_ticker,
            market_id=market_id,
            yes_levels=_levels(message, "yes_dollars_fp", side="yes"),
            no_levels=_levels(message, "no_dollars_fp", side="no"),
        )


@dataclass(frozen=True)
class KalshiOrderbookDelta:
    sid: int
    seq: int
    market_ticker: str
    market_id: str
    price: Decimal
    delta: Decimal
    side: MarketSide
    client_order_id: str | None
    subaccount: int | None
    timestamp_ms: int | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> KalshiOrderbookDelta:
        sid, seq, message = _envelope(payload, expected_type="orderbook_delta")
        market_ticker, market_id = _identity(message)
        price = _fixed_point(message.get("price_dollars"), field="price_dollars", quantum=_PRICE_QUANTUM)
        delta = _fixed_point(message.get("delta_fp"), field="delta_fp", quantum=_QUANTITY_QUANTUM)
        if not Decimal(0) <= price <= Decimal(1):
            raise KalshiWSProtocolError("price_dollars must be between 0 and 1")
        side = message.get("side")
        if side not in {"yes", "no"}:
            raise KalshiWSProtocolError("side must be 'yes' or 'no'")
        return cls(
            sid=sid,
            seq=seq,
            market_ticker=market_ticker,
            market_id=market_id,
            price=price,
            delta=delta,
            side=cast(MarketSide, side),
            client_order_id=_optional_text(message, "client_order_id"),
            subaccount=_optional_nonnegative_integer(message, "subaccount"),
            timestamp_ms=_optional_nonnegative_integer(message, "ts_ms"),
        )


@dataclass(frozen=True)
class KalshiOrderbookView:
    sid: int
    seq: int
    market_ticker: str
    market_id: str
    yes_levels: tuple[PriceLevel, ...]
    no_levels: tuple[PriceLevel, ...]


@dataclass(frozen=True)
class KalshiOrderbookRecovery:
    sid: int
    market_ticker: str

    def command(self, *, command_id: int) -> dict[str, object]:
        if isinstance(command_id, bool) or not isinstance(command_id, int) or command_id <= 0:
            raise KalshiWSProtocolError("command_id must be a positive integer")
        return {
            "id": command_id,
            "cmd": "update_subscription",
            "params": {
                "sids": [self.sid],
                "market_tickers": [self.market_ticker],
                "action": "get_snapshot",
            },
        }


class KalshiOrderbookState:
    """Reconstruct one acknowledged subscription and fail closed on discontinuity."""

    def __init__(self, *, expected_sid: int, market_ticker: str, market_id: str) -> None:
        self._expected_sid = _positive_integer(expected_sid, field="expected_sid")
        self._market_ticker, self._market_id = _identity({"market_ticker": market_ticker, "market_id": market_id})
        self._view: KalshiOrderbookView | None = None
        self._recovery: KalshiOrderbookRecovery | None = None
        self._valid = False

    @property
    def is_valid(self) -> bool:
        return self._valid

    @property
    def view(self) -> KalshiOrderbookView | None:
        return self._view if self._valid else None

    @property
    def recovery_required(self) -> KalshiOrderbookRecovery | None:
        return self._recovery

    def apply_snapshot(self, snapshot: KalshiOrderbookSnapshot) -> KalshiOrderbookView:
        if (
            snapshot.sid != self._expected_sid
            or snapshot.market_ticker != self._market_ticker
            or snapshot.market_id != self._market_id
        ):
            raise KalshiWSContinuityError("snapshot does not match the acknowledged subscription")
        view = KalshiOrderbookView(
            sid=snapshot.sid,
            seq=snapshot.seq,
            market_ticker=snapshot.market_ticker,
            market_id=snapshot.market_id,
            yes_levels=snapshot.yes_levels,
            no_levels=snapshot.no_levels,
        )
        current = self._view
        if current is not None:
            if self._valid:
                if view == current:
                    return current
                raise KalshiWSContinuityError("unsolicited snapshot cannot replace a valid orderbook")
            if snapshot.seq <= current.seq:
                raise KalshiWSContinuityError("recovery snapshot must advance the orderbook sequence")
        self._view = view
        self._recovery = None
        self._valid = True
        return view

    def apply_delta(self, delta: KalshiOrderbookDelta) -> KalshiOrderbookView:
        current = self._view
        if current is None:
            raise KalshiWSContinuityError("orderbook requires a snapshot before any delta")
        if delta.sid != current.sid:
            raise KalshiWSContinuityError("delta does not belong to the active subscription")
        if not self._valid:
            raise KalshiWSContinuityError("orderbook requires a fresh snapshot before more deltas")
        if delta.market_ticker != current.market_ticker or delta.market_id != current.market_id:
            self._invalidate()
            raise KalshiWSContinuityError("delta market identity does not match the active snapshot")
        if delta.seq != current.seq + 1:
            self._invalidate()
            raise KalshiWSContinuityError(f"non-contiguous sequence: expected {current.seq + 1}, received {delta.seq}")

        yes_levels = dict(current.yes_levels)
        no_levels = dict(current.no_levels)
        target = yes_levels if delta.side == "yes" else no_levels
        quantity = target.get(delta.price, Decimal(0)) + delta.delta
        if quantity < 0:
            self._invalidate()
            raise KalshiWSContinuityError("delta would make an orderbook level negative")
        if quantity == 0:
            target.pop(delta.price, None)
        else:
            target[delta.price] = quantity
        view = KalshiOrderbookView(
            sid=current.sid,
            seq=delta.seq,
            market_ticker=current.market_ticker,
            market_id=current.market_id,
            yes_levels=tuple(sorted(yes_levels.items())),
            no_levels=tuple(sorted(no_levels.items())),
        )
        self._view = view
        return view

    def _invalidate(self) -> None:
        current = self._view
        self._valid = False
        if current is not None:
            self._recovery = KalshiOrderbookRecovery(sid=current.sid, market_ticker=current.market_ticker)
