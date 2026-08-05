"""Pure Kalshi V2 WebSocket connection-lifecycle boundary.

The coordinator creates transport instructions and consumes already-decoded
protocol objects. It performs no socket, REST, credential, or venue-write I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from services.venues.kalshi_v2 import KALSHI_PRODUCTION_WS_URL, KALSHI_WS_PATH, KalshiRequestSigner
from services.venues.kalshi_v2_ws import (
    KalshiOrderbookDelta,
    KalshiOrderbookSnapshot,
    KalshiOrderbookState,
    KalshiOrderbookView,
    KalshiWSContinuityError,
    KalshiWSProtocolError,
)

KALSHI_V2_WS_PATH = KALSHI_WS_PATH
KALSHI_V2_WS_URL = KALSHI_PRODUCTION_WS_URL
_PRIVATE_COVERAGE_REASON = "authoritative_portfolio_reconciliation_not_implemented"
_CHANNELS = ("orderbook_delta", "user_orders", "fill")


class KalshiWSLifecycleError(RuntimeError):
    """Raised when a frame or transition cannot belong to the active epoch."""


def _positive_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise KalshiWSLifecycleError(f"{field} must be a positive integer")
    return value


def _mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise KalshiWSLifecycleError(f"{field} must be an object")
    return value


def _text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KalshiWSLifecycleError(f"{field} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class KalshiWSSubscribed:
    command_id: int
    channel: str
    sid: int

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> KalshiWSSubscribed:
        if payload.get("type") != "subscribed":
            raise KalshiWSLifecycleError("subscription response type must be 'subscribed'")
        message = _mapping(payload.get("msg"), field="subscription msg")
        return cls(
            command_id=_positive_integer(payload.get("id"), field="subscription command id"),
            channel=_text(message.get("channel"), field="subscription channel"),
            sid=_positive_integer(message.get("sid"), field="subscription sid"),
        )


@dataclass(frozen=True)
class KalshiWSErrorResponse:
    command_id: int
    code: int
    message: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> KalshiWSErrorResponse:
        if payload.get("type") != "error":
            raise KalshiWSLifecycleError("error response type must be 'error'")
        message = _mapping(payload.get("msg"), field="error msg")
        code = _positive_integer(message.get("code"), field="error code")
        if code > 28:
            raise KalshiWSLifecycleError("error code must be between 1 and 28")
        return cls(
            command_id=_positive_integer(payload.get("id"), field="error command id"),
            code=code,
            message=_text(message.get("msg"), field="error message"),
        )


@dataclass(frozen=True)
class KalshiWSSubscriptionCommand:
    command_id: int
    channel: str
    payload: dict[str, object]


@dataclass(frozen=True)
class KalshiWSConnectionInstructions:
    epoch_id: int
    timestamp_ms: int
    url: str
    headers: dict[str, str]
    subscriptions: tuple[KalshiWSSubscriptionCommand, ...]


class KalshiV2WSLifecycle:
    """Connection-scoped fail-closed lifecycle for one Kalshi orderbook."""

    def __init__(self, *, signer: KalshiRequestSigner, market_ticker: str, market_id: str) -> None:
        try:
            KalshiOrderbookState(expected_sid=1, market_ticker=market_ticker, market_id=market_id)
        except (KalshiWSProtocolError, ValueError) as exc:
            raise KalshiWSLifecycleError(str(exc)) from exc
        self._signer = signer
        self._market_ticker = market_ticker
        self._market_id = market_id
        self._epoch_counter = 0
        self._command_counter = 0
        self._last_timestamp_ms = 0
        self._current_epoch_id: int | None = None
        self._epoch_commands: dict[int, str] = {}
        self._pending: dict[int, str] = {}
        self._acknowledged: dict[str, int] = {}
        self._early_snapshot: KalshiOrderbookSnapshot | None = None
        self._orderbook_state: KalshiOrderbookState | None = None
        self._minimum_recovery_sequence: int | None = None
        self._terminal_reason: str | None = None

    @property
    def current_epoch_id(self) -> int | None:
        return self._current_epoch_id

    @property
    def terminal_reason(self) -> str | None:
        return self._terminal_reason

    @property
    def retry_allowed(self) -> bool:
        return False

    @property
    def degraded_reason(self) -> str:
        return _PRIVATE_COVERAGE_REASON

    @property
    def private_stream_healthy(self) -> bool:
        return False

    @property
    def connection_healthy(self) -> bool:
        return False

    @property
    def orderbook_publishable(self) -> bool:
        return (
            self._current_epoch_id is not None
            and self._terminal_reason is None
            and len(self._acknowledged) == len(_CHANNELS)
            and self._orderbook_state is not None
            and self._orderbook_state.is_valid
        )

    @property
    def orderbook_view(self) -> KalshiOrderbookView | None:
        if not self.orderbook_publishable or self._orderbook_state is None:
            return None
        return self._orderbook_state.view

    def begin_connection(self, *, timestamp_ms: int) -> KalshiWSConnectionInstructions:
        _positive_integer(timestamp_ms, field="timestamp_ms")
        if timestamp_ms <= self._last_timestamp_ms:
            raise KalshiWSLifecycleError("connection timestamp_ms must strictly increase")
        self._last_timestamp_ms = timestamp_ms
        self._epoch_counter += 1
        self._current_epoch_id = self._epoch_counter
        self._terminal_reason = None
        self._epoch_commands = {}
        self._pending = {}
        self._acknowledged = {}
        self._early_snapshot = None
        self._orderbook_state = None
        self._minimum_recovery_sequence = None

        commands: list[KalshiWSSubscriptionCommand] = []
        for channel in _CHANNELS:
            self._command_counter += 1
            params: dict[str, object] = {"channels": [channel]}
            if channel == "orderbook_delta":
                params["market_ticker"] = self._market_ticker
            payload = {"id": self._command_counter, "cmd": "subscribe", "params": params}
            command = KalshiWSSubscriptionCommand(
                command_id=self._command_counter,
                channel=channel,
                payload=payload,
            )
            commands.append(command)
            self._epoch_commands[command.command_id] = channel
            self._pending[command.command_id] = channel

        return KalshiWSConnectionInstructions(
            epoch_id=self._current_epoch_id,
            timestamp_ms=timestamp_ms,
            url=KALSHI_V2_WS_URL,
            headers=self._signer.headers(timestamp_ms=timestamp_ms, method="GET", path=KALSHI_V2_WS_PATH),
            subscriptions=tuple(commands),
        )

    def receive_subscribed(self, epoch_id: int, response: KalshiWSSubscribed) -> None:
        self._require_epoch(epoch_id)
        expected_channel = self._pending.get(response.command_id)
        if expected_channel is None:
            if response.command_id in self._acknowledged_command_ids():
                self._terminate("invalid_subscription_acknowledgement")
                raise KalshiWSLifecycleError("subscription command was already acknowledged")
            self._terminate("invalid_subscription_acknowledgement")
            raise KalshiWSLifecycleError("subscription acknowledgement has an unknown command id")
        if response.channel != expected_channel:
            self._terminate("invalid_subscription_acknowledgement")
            raise KalshiWSLifecycleError("subscription acknowledgement channel does not match its command")
        if response.channel in self._acknowledged:
            self._terminate("invalid_subscription_acknowledgement")
            raise KalshiWSLifecycleError("subscription channel was already acknowledged")

        del self._pending[response.command_id]
        self._acknowledged[response.channel] = response.sid
        if response.channel == "orderbook_delta":
            state = KalshiOrderbookState(
                expected_sid=response.sid,
                market_ticker=self._market_ticker,
                market_id=self._market_id,
            )
            if self._early_snapshot is not None:
                try:
                    state.apply_snapshot(self._early_snapshot)
                except KalshiWSContinuityError as exc:
                    self._terminate("invalid_orderbook_snapshot")
                    raise KalshiWSLifecycleError(str(exc)) from exc
                self._early_snapshot = None
            self._orderbook_state = state

    def receive_snapshot(self, epoch_id: int, snapshot: KalshiOrderbookSnapshot) -> KalshiOrderbookView | None:
        self._require_epoch(epoch_id)
        if snapshot.market_ticker != self._market_ticker or snapshot.market_id != self._market_id:
            self._terminate("invalid_orderbook_identity")
            raise KalshiWSLifecycleError("snapshot does not match the active subscription")
        if self._orderbook_state is None:
            if self._early_snapshot is not None and self._early_snapshot != snapshot:
                self._terminate("conflicting_early_orderbook_snapshot")
                raise KalshiWSLifecycleError("conflicting orderbook snapshots arrived before acknowledgement")
            self._early_snapshot = snapshot
            return None
        expected_sid = self._acknowledged["orderbook_delta"]
        if snapshot.sid != expected_sid:
            self._terminate("invalid_orderbook_identity")
            raise KalshiWSLifecycleError("snapshot does not match the active subscription")
        if self._minimum_recovery_sequence is not None and snapshot.seq < self._minimum_recovery_sequence:
            raise KalshiWSLifecycleError("recovery snapshot does not advance through the observed gap")
        was_valid = self._orderbook_state.is_valid
        try:
            view = self._orderbook_state.apply_snapshot(snapshot)
        except KalshiWSContinuityError as exc:
            if was_valid:
                self._terminate("invalid_orderbook_snapshot")
            raise KalshiWSLifecycleError(str(exc)) from exc
        self._minimum_recovery_sequence = None
        return view

    def receive_delta(self, epoch_id: int, delta: KalshiOrderbookDelta) -> KalshiOrderbookView:
        self._require_epoch(epoch_id)
        if self._orderbook_state is None:
            raise KalshiWSLifecycleError("orderbook subscription acknowledgement and snapshot are required")
        if (
            delta.sid != self._acknowledged["orderbook_delta"]
            or delta.market_ticker != self._market_ticker
            or delta.market_id != self._market_id
        ):
            self._terminate("invalid_orderbook_identity")
            raise KalshiWSLifecycleError("delta does not match the active subscription")
        if self._minimum_recovery_sequence is not None:
            self._minimum_recovery_sequence = max(self._minimum_recovery_sequence, delta.seq)
        current = self._orderbook_state.view
        if current is not None and delta.seq != current.seq + 1:
            self._minimum_recovery_sequence = max(current.seq + 1, delta.seq)
        try:
            return self._orderbook_state.apply_delta(delta)
        except KalshiWSContinuityError as exc:
            if current is not None:
                recovery_sequence = max(current.seq + 1, delta.seq)
                self._minimum_recovery_sequence = max(
                    self._minimum_recovery_sequence or recovery_sequence,
                    recovery_sequence,
                )
            raise KalshiWSLifecycleError(str(exc)) from exc

    def recovery_command(self) -> dict[str, object]:
        if self._current_epoch_id is None or self._orderbook_state is None:
            raise KalshiWSLifecycleError("no active orderbook recovery is available")
        recovery = self._orderbook_state.recovery_required
        if recovery is None:
            raise KalshiWSLifecycleError("orderbook recovery is not required")
        self._command_counter += 1
        return recovery.command(command_id=self._command_counter)

    def receive_error(self, epoch_id: int, response: KalshiWSErrorResponse) -> None:
        self._require_epoch(epoch_id)
        if response.command_id not in self._epoch_commands:
            self._terminate("invalid_error_correlation")
            raise KalshiWSLifecycleError("error response has an unknown command id")
        self._terminate(f"kalshi_ws_error:{response.code}")

    def disconnect(self, epoch_id: int) -> None:
        self.terminate_epoch(epoch_id, "disconnected")

    def terminate_epoch(self, epoch_id: int, reason: str) -> None:
        self._require_epoch(epoch_id)
        if not isinstance(reason, str) or not reason.strip():
            raise KalshiWSLifecycleError("termination reason must be a non-empty string")
        self._terminate(reason.strip())

    def _acknowledged_command_ids(self) -> set[int]:
        return set(self._epoch_commands) - set(self._pending)

    def _require_epoch(self, epoch_id: int) -> None:
        if self._current_epoch_id is None:
            raise KalshiWSLifecycleError("no active connection epoch")
        if epoch_id != self._current_epoch_id:
            raise KalshiWSLifecycleError("frame does not belong to the current connection epoch")
        if self._terminal_reason is not None:
            raise KalshiWSLifecycleError("current connection epoch is terminal")

    def _terminate(self, reason: str) -> None:
        self._terminal_reason = reason
        self._current_epoch_id = None
        self._epoch_commands = {}
        self._pending = {}
        self._acknowledged = {}
        self._early_snapshot = None
        self._orderbook_state = None
        self._minimum_recovery_sequence = None


__all__ = [
    "KALSHI_V2_WS_PATH",
    "KALSHI_V2_WS_URL",
    "KalshiV2WSLifecycle",
    "KalshiWSConnectionInstructions",
    "KalshiWSErrorResponse",
    "KalshiWSLifecycleError",
    "KalshiWSSubscribed",
    "KalshiWSSubscriptionCommand",
]
