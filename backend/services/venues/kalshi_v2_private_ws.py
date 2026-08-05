"""Strict private Kalshi WebSocket payloads and private-only lifecycle.

Private channel messages are unsequenced. Parsed frames are invalidation signals
only; this module deliberately exposes no venue mutation command.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal, TypeAlias, cast
from uuid import UUID

from services.venues.kalshi_v2 import KALSHI_WS_PATH, KalshiRequestSigner, kalshi_websocket_url_for_origin
from services.venues.kalshi_v2_ws_lifecycle import (
    KalshiWSConnectionInstructions,
    KalshiWSSubscriptionCommand,
)

PrivateChannel = Literal["user_orders", "fill", "market_positions"]
_PRIVATE_CHANNELS: tuple[PrivateChannel, ...] = ("user_orders", "fill", "market_positions")
_TICKER = re.compile(r"^[A-Z0-9.-]+$")
_COUNT_QUANTUM = Decimal("0.01")
_DOLLAR_QUANTUM = Decimal("0.0001")
_INT64_MAX = 2**63 - 1
_RFC3339_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(?P<fraction>\d{1,6}))?(?:Z|[+-]\d{2}:\d{2})$")


class KalshiPrivateWSProtocolError(ValueError):
    """Raised when private WebSocket input violates the current schema."""


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise KalshiPrivateWSProtocolError(f"{field} must be an object")
    return value


def _text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise KalshiPrivateWSProtocolError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(payload: Mapping[str, object], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise KalshiPrivateWSProtocolError(f"{field} must be a non-empty string when present")
    return value.strip()


def _uuid(payload: Mapping[str, object], field: str) -> str:
    value = _text(payload, field)
    try:
        UUID(value)
    except ValueError as exc:
        raise KalshiPrivateWSProtocolError(f"{field} must be a UUID") from exc
    return value


def _ticker(payload: Mapping[str, object], field: str) -> str:
    value = _text(payload, field)
    if _TICKER.fullmatch(value) is None:
        raise KalshiPrivateWSProtocolError(f"{field} is invalid")
    return value


def _integer(value: object, field: str, *, minimum: int = 0, maximum: int = _INT64_MAX) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise KalshiPrivateWSProtocolError(f"{field} must be an integer between {minimum} and {maximum}")
    return value


def _optional_subaccount(payload: Mapping[str, object], field: str) -> int | None:
    value = payload.get(field)
    return None if value is None else _integer(value, field, maximum=63)


def _boolean(payload: Mapping[str, object], field: str) -> bool:
    value = payload.get(field)
    if not isinstance(value, bool):
        raise KalshiPrivateWSProtocolError(f"{field} must be a boolean")
    return value


def _fixed(payload: Mapping[str, object], field: str, quantum: Decimal) -> Decimal:
    raw = payload.get(field)
    if not isinstance(raw, str):
        raise KalshiPrivateWSProtocolError(f"{field} must be a fixed-point string")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise KalshiPrivateWSProtocolError(f"{field} must be a finite decimal") from exc
    if not value.is_finite():
        raise KalshiPrivateWSProtocolError(f"{field} must be a finite decimal")
    exponent = value.as_tuple().exponent
    permitted = quantum.as_tuple().exponent
    if not isinstance(exponent, int) or not isinstance(permitted, int) or exponent < permitted:
        raise KalshiPrivateWSProtocolError(f"{field} has more precision than permitted")
    return value


def _side(payload: Mapping[str, object], field: str) -> Literal["yes", "no"]:
    value = _text(payload, field)
    if value not in {"yes", "no"}:
        raise KalshiPrivateWSProtocolError(f"{field} must be 'yes' or 'no'")
    return cast(Literal["yes", "no"], value)


def _book_side(payload: Mapping[str, object]) -> Literal["bid", "ask"]:
    value = _text(payload, "book_side")
    if value not in {"bid", "ask"}:
        raise KalshiPrivateWSProtocolError("book_side must be 'bid' or 'ask'")
    return cast(Literal["bid", "ask"], value)


def _validate_direction(outcome: str, book: str) -> None:
    if (outcome == "yes") != (book == "bid"):
        raise KalshiPrivateWSProtocolError("outcome_side and book_side are inconsistent")


def _rfc3339_ms(value: str, field: str) -> int:
    if _RFC3339_PATTERN.fullmatch(value) is None:
        raise KalshiPrivateWSProtocolError(f"{field} must be RFC3339 with a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise KalshiPrivateWSProtocolError(f"{field} must be RFC3339 with a timezone") from exc
    if parsed.tzinfo is None:
        raise KalshiPrivateWSProtocolError(f"{field} must be RFC3339 with a timezone")
    utc_value = parsed.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = utc_value - epoch
    return (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000


def _timestamp_pair(
    payload: Mapping[str, object], text_field: str, ms_field: str, *, required: bool
) -> tuple[str | None, int | None]:
    text_value = payload.get(text_field)
    ms_value = payload.get(ms_field)
    if text_value is None and ms_value is None and not required:
        return None, None
    timestamp_ms = _integer(ms_value, ms_field, minimum=1)
    if text_value is None and not required:
        return None, timestamp_ms
    if not isinstance(text_value, str) or not text_value.strip():
        raise KalshiPrivateWSProtocolError(f"{text_field} must accompany {ms_field}")
    if _rfc3339_ms(text_value, text_field) != timestamp_ms:
        raise KalshiPrivateWSProtocolError(f"{text_field} and {ms_field} are inconsistent")
    return text_value, timestamp_ms


@dataclass(frozen=True)
class KalshiPrivateOrder:
    order_id: str
    user_id: str
    ticker: str
    status: Literal["resting", "canceled", "executed"]
    outcome_side: Literal["yes", "no"]
    book_side: Literal["bid", "ask"]
    yes_price: Decimal
    fill_count: Decimal
    remaining_count: Decimal
    initial_count: Decimal
    taker_fill_cost: Decimal
    maker_fill_cost: Decimal
    taker_fees: Decimal
    maker_fees: Decimal
    client_order_id: str
    order_group_id: str | None
    created_time: str
    created_timestamp_ms: int
    last_update_time: str | None
    last_updated_timestamp_ms: int | None
    expiration_time: str | None
    expiration_timestamp_ms: int | None
    subaccount_number: int | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> KalshiPrivateOrder:
        outcome = _side(payload, "outcome_side")
        book = _book_side(payload)
        _validate_direction(outcome, book)
        legacy_side = _side(payload, "side")
        if legacy_side != outcome or _boolean(payload, "is_yes") != (outcome == "yes"):
            raise KalshiPrivateWSProtocolError("legacy order direction conflicts with canonical direction")
        status = _text(payload, "status")
        if status not in {"resting", "canceled", "executed"}:
            raise KalshiPrivateWSProtocolError("invalid order status")
        yes_price = _fixed(payload, "yes_price_dollars", _DOLLAR_QUANTUM)
        fill_count = _fixed(payload, "fill_count_fp", _COUNT_QUANTUM)
        remaining_count = _fixed(payload, "remaining_count_fp", _COUNT_QUANTUM)
        initial_count = _fixed(payload, "initial_count_fp", _COUNT_QUANTUM)
        money = tuple(
            _fixed(payload, field, _DOLLAR_QUANTUM)
            for field in (
                "taker_fill_cost_dollars",
                "maker_fill_cost_dollars",
                "taker_fees_dollars",
                "maker_fees_dollars",
            )
        )
        if not Decimal(0) <= yes_price <= Decimal(1):
            raise KalshiPrivateWSProtocolError("yes_price_dollars must be between 0 and 1")
        if any(value < 0 for value in (fill_count, remaining_count, initial_count, *money)):
            raise KalshiPrivateWSProtocolError("order counts, costs, and fees cannot be negative")

        created_time, created_ms = _timestamp_pair(payload, "created_time", "created_ts_ms", required=True)
        updated_time, updated_ms = _timestamp_pair(payload, "last_update_time", "last_updated_ts_ms", required=False)
        expiration_time, expiration_ms = _timestamp_pair(payload, "expiration_time", "expiration_ts_ms", required=False)
        return cls(
            order_id=_uuid(payload, "order_id"),
            user_id=_uuid(payload, "user_id"),
            ticker=_ticker(payload, "ticker"),
            status=cast(Literal["resting", "canceled", "executed"], status),
            outcome_side=outcome,
            book_side=book,
            yes_price=yes_price,
            fill_count=fill_count,
            remaining_count=remaining_count,
            initial_count=initial_count,
            taker_fill_cost=money[0],
            maker_fill_cost=money[1],
            taker_fees=money[2],
            maker_fees=money[3],
            client_order_id=_text(payload, "client_order_id"),
            order_group_id=_optional_text(payload, "order_group_id"),
            created_time=cast(str, created_time),
            created_timestamp_ms=cast(int, created_ms),
            last_update_time=updated_time,
            last_updated_timestamp_ms=updated_ms,
            expiration_time=expiration_time,
            expiration_timestamp_ms=expiration_ms,
            subaccount_number=_optional_subaccount(payload, "subaccount_number"),
        )


@dataclass(frozen=True)
class KalshiPrivateFill:
    trade_id: str
    order_id: str
    market_ticker: str
    is_taker: bool
    outcome_side: Literal["yes", "no"]
    book_side: Literal["bid", "ask"]
    yes_price: Decimal
    count: Decimal
    fee_cost: Decimal
    timestamp_ms: int
    post_position: Decimal
    client_order_id: str | None
    subaccount: int | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> KalshiPrivateFill:
        outcome = _side(payload, "outcome_side")
        book = _book_side(payload)
        _validate_direction(outcome, book)
        side = _side(payload, "side")
        action = _text(payload, "action")
        if action not in {"buy", "sell"}:
            raise KalshiPrivateWSProtocolError("action must be 'buy' or 'sell'")
        expected = side if action == "buy" else ("no" if side == "yes" else "yes")
        if outcome != expected or _side(payload, "purchased_side") != outcome:
            raise KalshiPrivateWSProtocolError("legacy fill direction conflicts with canonical direction")
        timestamp_seconds = _integer(payload.get("ts"), "ts", minimum=1)
        timestamp_ms = _integer(payload.get("ts_ms"), "ts_ms", minimum=1)
        if timestamp_ms // 1000 != timestamp_seconds:
            raise KalshiPrivateWSProtocolError("ts and ts_ms are inconsistent")
        yes_price = _fixed(payload, "yes_price_dollars", _DOLLAR_QUANTUM)
        count = _fixed(payload, "count_fp", _COUNT_QUANTUM)
        fee = _fixed(payload, "fee_cost", _DOLLAR_QUANTUM)
        post_position = _fixed(payload, "post_position_fp", _COUNT_QUANTUM)
        if not Decimal(0) <= yes_price <= Decimal(1) or count <= 0 or fee < 0:
            raise KalshiPrivateWSProtocolError("invalid fill price, count, or fee")
        return cls(
            trade_id=_uuid(payload, "trade_id"),
            order_id=_uuid(payload, "order_id"),
            market_ticker=_ticker(payload, "market_ticker"),
            is_taker=_boolean(payload, "is_taker"),
            outcome_side=outcome,
            book_side=book,
            yes_price=yes_price,
            count=count,
            fee_cost=fee,
            timestamp_ms=timestamp_ms,
            post_position=post_position,
            client_order_id=_optional_text(payload, "client_order_id"),
            subaccount=_optional_subaccount(payload, "subaccount"),
        )


@dataclass(frozen=True)
class KalshiPrivateMarketPosition:
    user_id: str
    market_ticker: str
    position: Decimal
    position_cost: Decimal
    realized_pnl: Decimal
    fees_paid: Decimal
    position_fee_cost: Decimal
    volume: Decimal
    subaccount: int | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> KalshiPrivateMarketPosition:
        position = _fixed(payload, "position_fp", _COUNT_QUANTUM)
        position_cost = _fixed(payload, "position_cost_dollars", _DOLLAR_QUANTUM)
        realized_pnl = _fixed(payload, "realized_pnl_dollars", _DOLLAR_QUANTUM)
        fees = _fixed(payload, "fees_paid_dollars", _DOLLAR_QUANTUM)
        position_fee = _fixed(payload, "position_fee_cost_dollars", _DOLLAR_QUANTUM)
        volume = _fixed(payload, "volume_fp", _COUNT_QUANTUM)
        if fees < 0 or position_fee < 0 or volume < 0:
            raise KalshiPrivateWSProtocolError("position fees and volume cannot be negative")
        return cls(
            user_id=_text(payload, "user_id"),
            market_ticker=_ticker(payload, "market_ticker"),
            position=position,
            position_cost=position_cost,
            realized_pnl=realized_pnl,
            fees_paid=fees,
            position_fee_cost=position_fee,
            volume=volume,
            subaccount=_optional_subaccount(payload, "subaccount"),
        )


PrivatePayload: TypeAlias = KalshiPrivateOrder | KalshiPrivateFill | KalshiPrivateMarketPosition


@dataclass(frozen=True)
class KalshiPrivateWSFrame:
    epoch_id: int
    channel: PrivateChannel
    sid: int
    payload: PrivatePayload


@dataclass(frozen=True)
class KalshiPrivateWSOutcome:
    kind: Literal["ack", "frame", "stale"]
    frame: KalshiPrivateWSFrame | None = None


class KalshiPrivateWSLifecycle:
    """Fail-closed connection epoch for the three private invalidation channels."""

    def __init__(self, *, signer: KalshiRequestSigner, principal_origin: str) -> None:
        self._signer = signer
        self._principal_fingerprint = signer.principal_fingerprint(origin=principal_origin)
        self._websocket_url = kalshi_websocket_url_for_origin(origin=principal_origin)
        self._epoch_counter = 0
        self._command_counter = 0
        self._last_timestamp_ms = 0
        self._current_epoch_id: int | None = None
        self._pending: dict[int, PrivateChannel] = {}
        self._sids: dict[PrivateChannel, int] = {}
        self._provider_user_id: str | None = None
        self._terminal_reason: str | None = None

    @property
    def current_epoch_id(self) -> int | None:
        return self._current_epoch_id

    @property
    def terminal_reason(self) -> str | None:
        return self._terminal_reason

    @property
    def principal_fingerprint(self) -> str:
        return self._principal_fingerprint

    @property
    def subscriptions_acknowledged(self) -> bool:
        return self._current_epoch_id is not None and not self._pending and len(self._sids) == len(_PRIVATE_CHANNELS)

    @property
    def retry_allowed(self) -> Literal[False]:
        return False

    def begin_connection(self, *, timestamp_ms: int) -> KalshiWSConnectionInstructions:
        timestamp_ms = _integer(timestamp_ms, "timestamp_ms", minimum=1)
        if timestamp_ms <= self._last_timestamp_ms:
            raise KalshiPrivateWSProtocolError("connection timestamp_ms must strictly increase")
        self._last_timestamp_ms = timestamp_ms
        self._epoch_counter += 1
        self._current_epoch_id = self._epoch_counter
        self._pending = {}
        self._sids = {}
        self._provider_user_id = None
        self._terminal_reason = None
        commands: list[KalshiWSSubscriptionCommand] = []
        for channel in _PRIVATE_CHANNELS:
            self._command_counter += 1
            payload: dict[str, object] = {
                "id": self._command_counter,
                "cmd": "subscribe",
                "params": {"channels": [channel]},
            }
            command = KalshiWSSubscriptionCommand(self._command_counter, channel, payload)
            commands.append(command)
            self._pending[command.command_id] = channel
        return KalshiWSConnectionInstructions(
            epoch_id=self._epoch_counter,
            timestamp_ms=timestamp_ms,
            url=self._websocket_url,
            headers=self._signer.headers(timestamp_ms=timestamp_ms, method="GET", path=KALSHI_WS_PATH),
            subscriptions=tuple(commands),
        )

    def receive(self, epoch_id: int, envelope: Mapping[str, object]) -> KalshiPrivateWSOutcome:
        if epoch_id != self._current_epoch_id:
            return KalshiPrivateWSOutcome(kind="stale")
        try:
            frame_type = _text(envelope, "type")
            if frame_type == "subscribed":
                self._receive_ack(envelope)
                return KalshiPrivateWSOutcome(kind="ack")
            if frame_type == "error":
                message = _mapping(envelope.get("msg"), "error msg")
                code = _integer(message.get("code"), "error code", minimum=1, maximum=28)
                self._terminate(f"kalshi_ws_error:{code}")
                raise KalshiPrivateWSProtocolError("Kalshi rejected a private subscription")
            model: type[PrivatePayload]
            channel: PrivateChannel
            if frame_type == "user_order":
                channel, model = "user_orders", KalshiPrivateOrder
            elif frame_type == "fill":
                channel, model = "fill", KalshiPrivateFill
            elif frame_type == "market_position":
                channel, model = "market_positions", KalshiPrivateMarketPosition
            else:
                raise KalshiPrivateWSProtocolError("unknown private frame type")
            sid = _integer(envelope.get("sid"), "sid", minimum=1)
            if self._sids.get(channel) != sid:
                raise KalshiPrivateWSProtocolError("private frame sid does not match its acknowledgement")
            payload = model.from_payload(_mapping(envelope.get("msg"), "msg"))
            provider_user_id = getattr(payload, "user_id", None)
            if provider_user_id is not None:
                if self._provider_user_id is not None and provider_user_id != self._provider_user_id:
                    raise KalshiPrivateWSProtocolError("provider principal changed within the connection epoch")
                self._provider_user_id = provider_user_id
            return KalshiPrivateWSOutcome(
                kind="frame",
                frame=KalshiPrivateWSFrame(epoch_id=epoch_id, channel=channel, sid=sid, payload=payload),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if self._current_epoch_id == epoch_id:
                self._terminate("malformed_private_frame")
            if isinstance(exc, KalshiPrivateWSProtocolError):
                raise
            raise KalshiPrivateWSProtocolError("malformed private frame") from exc

    def disconnect(self, epoch_id: int, reason: str = "disconnected") -> None:
        if epoch_id == self._current_epoch_id:
            self._terminate(reason)

    def _receive_ack(self, envelope: Mapping[str, object]) -> None:
        command_id = _integer(envelope.get("id"), "subscription command id", minimum=1)
        message = _mapping(envelope.get("msg"), "subscription msg")
        channel = _text(message, "channel")
        sid = _integer(message.get("sid"), "subscription sid", minimum=1)
        expected = self._pending.get(command_id)
        if expected is None or channel != expected or channel in self._sids or sid in self._sids.values():
            raise KalshiPrivateWSProtocolError("invalid subscription acknowledgement")
        del self._pending[command_id]
        self._sids[expected] = sid

    def _terminate(self, reason: str) -> None:
        self._terminal_reason = reason
        self._current_epoch_id = None
        self._pending = {}
        self._sids = {}
        self._provider_user_id = None


__all__ = [
    "KalshiPrivateFill",
    "KalshiPrivateMarketPosition",
    "KalshiPrivateOrder",
    "KalshiPrivateWSFrame",
    "KalshiPrivateWSLifecycle",
    "KalshiPrivateWSOutcome",
    "KalshiPrivateWSProtocolError",
    "PrivateChannel",
]
