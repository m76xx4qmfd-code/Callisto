"""Deterministic decoded-frame boundary for one Kalshi WebSocket epoch.

The session performs no socket or REST I/O. Private frames are envelope-checked
and surfaced as unconsumed observations; they never promote stream health or
become execution evidence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypeAlias, cast

from services.venues.kalshi_v2_ws import (
    KalshiOrderbookDelta,
    KalshiOrderbookSnapshot,
    KalshiOrderbookView,
    KalshiWSProtocolError,
)
from services.venues.kalshi_v2_ws_lifecycle import (
    KalshiV2WSLifecycle,
    KalshiWSConnectionInstructions,
    KalshiWSErrorResponse,
    KalshiWSLifecycleError,
    KalshiWSSubscribed,
)


@dataclass(frozen=True)
class KalshiWSSessionNoOp:
    epoch_id: int
    reason: str


@dataclass(frozen=True)
class KalshiWSSessionPublished:
    epoch_id: int
    view: KalshiOrderbookView


@dataclass(frozen=True)
class KalshiWSSessionRecoveryRequested:
    epoch_id: int
    command_id: int
    sid: int
    market_ticker: str

    @property
    def command(self) -> dict[str, object]:
        return {
            "id": self.command_id,
            "cmd": "update_subscription",
            "params": {
                "sids": [self.sid],
                "market_tickers": [self.market_ticker],
                "action": "get_snapshot",
            },
        }


@dataclass(frozen=True)
class KalshiWSSessionPrivateFrame:
    epoch_id: int
    channel: Literal["user_orders", "fill"]
    sid: int


@dataclass(frozen=True)
class KalshiWSSessionTerminated:
    epoch_id: int
    reason: str


KalshiWSSessionOutcome: TypeAlias = (
    KalshiWSSessionNoOp
    | KalshiWSSessionPublished
    | KalshiWSSessionRecoveryRequested
    | KalshiWSSessionPrivateFrame
    | KalshiWSSessionTerminated
)


class KalshiV2WSFrameSession:
    """Route decoded frames through one fail-closed lifecycle epoch."""

    def __init__(self, lifecycle: KalshiV2WSLifecycle) -> None:
        self._lifecycle = lifecycle
        self._epoch_id: int | None = None
        self._channel_sids: dict[str, int] = {}
        self._recovery_emitted = False

    @property
    def lifecycle(self) -> KalshiV2WSLifecycle:
        return self._lifecycle

    def begin(self, *, timestamp_ms: int) -> KalshiWSConnectionInstructions:
        instructions = self._lifecycle.begin_connection(timestamp_ms=timestamp_ms)
        self._epoch_id = instructions.epoch_id
        self._channel_sids = {}
        self._recovery_emitted = False
        return instructions

    def receive(self, epoch_id: int, payload: Mapping[str, object]) -> KalshiWSSessionOutcome:
        if epoch_id != self._epoch_id or epoch_id != self._lifecycle.current_epoch_id:
            return KalshiWSSessionNoOp(epoch_id=epoch_id, reason="stale_epoch")
        if not isinstance(payload, Mapping):
            return self._terminate(epoch_id, "malformed_frame")

        frame_type = payload.get("type")
        if not isinstance(frame_type, str):
            return self._terminate(epoch_id, "malformed_frame")

        try:
            if frame_type == "subscribed":
                subscribed = KalshiWSSubscribed.from_payload(payload)
                self._lifecycle.receive_subscribed(epoch_id, subscribed)
                self._channel_sids[subscribed.channel] = subscribed.sid
                view = self._lifecycle.orderbook_view
                if view is not None:
                    return KalshiWSSessionPublished(epoch_id=epoch_id, view=view)
                return KalshiWSSessionNoOp(epoch_id=epoch_id, reason="subscription_acknowledged")

            if frame_type == "error":
                error = KalshiWSErrorResponse.from_payload(payload)
                self._lifecycle.receive_error(epoch_id, error)
                return KalshiWSSessionTerminated(
                    epoch_id=epoch_id,
                    reason=self._lifecycle.terminal_reason or "kalshi_ws_error",
                )

            if frame_type == "orderbook_snapshot":
                snapshot = KalshiOrderbookSnapshot.from_payload(payload)
                try:
                    view = self._lifecycle.receive_snapshot(epoch_id, snapshot)
                except KalshiWSLifecycleError:
                    if self._lifecycle.current_epoch_id is None:
                        return KalshiWSSessionTerminated(
                            epoch_id=epoch_id,
                            reason=self._lifecycle.terminal_reason or "invalid_orderbook_snapshot",
                        )
                    return KalshiWSSessionNoOp(epoch_id=epoch_id, reason="recovery_snapshot_below_watermark")
                if view is not None:
                    self._recovery_emitted = False
                    if self._lifecycle.orderbook_publishable:
                        return KalshiWSSessionPublished(epoch_id=epoch_id, view=view)
                return KalshiWSSessionNoOp(epoch_id=epoch_id, reason="snapshot_not_publishable")

            if frame_type == "orderbook_delta":
                delta = KalshiOrderbookDelta.from_payload(payload)
                try:
                    view = self._lifecycle.receive_delta(epoch_id, delta)
                except KalshiWSLifecycleError:
                    if self._lifecycle.current_epoch_id is None:
                        return KalshiWSSessionTerminated(
                            epoch_id=epoch_id,
                            reason=self._lifecycle.terminal_reason or "invalid_orderbook_delta",
                        )
                    if self._recovery_emitted:
                        return KalshiWSSessionNoOp(epoch_id=epoch_id, reason="recovery_in_progress")
                    command = self._lifecycle.recovery_command()
                    params = command["params"]
                    if not isinstance(params, Mapping):
                        return self._terminate(epoch_id, "invalid_recovery_command")
                    sids = params.get("sids")
                    market_tickers = params.get("market_tickers")
                    command_id = command.get("id")
                    if (
                        isinstance(command_id, bool)
                        or not isinstance(command_id, int)
                        or not isinstance(sids, list)
                        or len(sids) != 1
                        or not isinstance(sids[0], int)
                        or not isinstance(market_tickers, list)
                        or len(market_tickers) != 1
                        or not isinstance(market_tickers[0], str)
                    ):
                        return self._terminate(epoch_id, "invalid_recovery_command")
                    self._recovery_emitted = True
                    return KalshiWSSessionRecoveryRequested(
                        epoch_id=epoch_id,
                        command_id=command_id,
                        sid=sids[0],
                        market_ticker=market_tickers[0],
                    )
                if self._lifecycle.orderbook_publishable:
                    return KalshiWSSessionPublished(epoch_id=epoch_id, view=view)
                return KalshiWSSessionNoOp(epoch_id=epoch_id, reason="delta_not_publishable")

            if frame_type in {"user_order", "fill"}:
                return self._private_frame(
                    epoch_id,
                    cast(Literal["user_order", "fill"], frame_type),
                    payload,
                )
        except (KalshiWSLifecycleError, KalshiWSProtocolError, KeyError, TypeError, ValueError):
            if self._lifecycle.current_epoch_id is None:
                return KalshiWSSessionTerminated(
                    epoch_id=epoch_id,
                    reason=self._lifecycle.terminal_reason or "invalid_frame",
                )
            return self._terminate(epoch_id, "malformed_frame")

        return self._terminate(epoch_id, "unknown_frame_type")

    def _private_frame(
        self,
        epoch_id: int,
        frame_type: Literal["user_order", "fill"],
        payload: Mapping[str, object],
    ) -> KalshiWSSessionOutcome:
        channel: Literal["user_orders", "fill"] = "user_orders" if frame_type == "user_order" else "fill"
        sid = payload.get("sid")
        message = payload.get("msg")
        if (
            isinstance(sid, bool)
            or not isinstance(sid, int)
            or sid <= 0
            or not isinstance(message, Mapping)
            or self._channel_sids.get(channel) != sid
        ):
            return self._terminate(epoch_id, "invalid_private_frame_envelope")
        return KalshiWSSessionPrivateFrame(epoch_id=epoch_id, channel=channel, sid=sid)

    def _terminate(self, epoch_id: int, reason: str) -> KalshiWSSessionTerminated:
        if self._lifecycle.current_epoch_id == epoch_id:
            self._lifecycle.terminate_epoch(epoch_id, reason)
        return KalshiWSSessionTerminated(epoch_id=epoch_id, reason=reason)


__all__ = [
    "KalshiV2WSFrameSession",
    "KalshiWSSessionNoOp",
    "KalshiWSSessionOutcome",
    "KalshiWSSessionPrivateFrame",
    "KalshiWSSessionPublished",
    "KalshiWSSessionRecoveryRequested",
    "KalshiWSSessionTerminated",
]
