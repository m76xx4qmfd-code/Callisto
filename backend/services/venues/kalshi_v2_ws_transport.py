"""Production websockets 12 transport with a strict JSON-object boundary."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

import websockets

from services.venues.kalshi_v2_ws_lifecycle import KALSHI_V2_WS_URL, KalshiWSConnectionInstructions

_ALLOWED_PRIVATE_CHANNELS = frozenset({"user_orders", "fill", "market_positions"})
_SILENT_WEBSOCKETS_LOGGER = logging.getLogger("callisto.kalshi.private_ws")
_SILENT_WEBSOCKETS_LOGGER.setLevel(logging.CRITICAL + 1)
_SILENT_WEBSOCKETS_LOGGER.disabled = True
_SILENT_WEBSOCKETS_LOGGER.propagate = False


class KalshiWSTransportProtocolError(ValueError):
    """Raised when a WebSocket frame violates the transport boundary."""


class _WebSocket(Protocol):
    async def recv(self) -> str | bytes: ...

    async def send(self, payload: str) -> None: ...

    async def close(self) -> None: ...


ConnectCallable = Callable[..., Awaitable[_WebSocket]]


class KalshiWebsocketsTransport:
    """Decode inbound text objects and permit private subscribe commands only."""

    def __init__(self, websocket: _WebSocket) -> None:
        self._websocket = websocket

    async def receive(self) -> Mapping[str, object]:
        raw = await self._websocket.recv()
        if not isinstance(raw, str):
            raise KalshiWSTransportProtocolError("binary WebSocket frames are not accepted")
        try:
            payload = json.loads(
                raw,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid JSON constant {value}")),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise KalshiWSTransportProtocolError("WebSocket frame must contain valid JSON") from exc
        if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
            raise KalshiWSTransportProtocolError("WebSocket frame must contain a JSON object")
        return payload

    async def send(self, payload: dict[str, object]) -> None:
        if payload.get("cmd") != "subscribe":
            raise KalshiWSTransportProtocolError("only subscribe commands are permitted")
        params = payload.get("params")
        if not isinstance(params, Mapping):
            raise KalshiWSTransportProtocolError("subscribe params must be an object")
        channels = params.get("channels")
        if (
            not isinstance(channels, list)
            or len(channels) != 1
            or not isinstance(channels[0], str)
            or channels[0] not in _ALLOWED_PRIVATE_CHANNELS
        ):
            raise KalshiWSTransportProtocolError("subscribe command must target one private channel")
        if set(params) != {"channels"}:
            raise KalshiWSTransportProtocolError("private subscribe command contains unsupported parameters")
        try:
            encoded = json.dumps(payload, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise KalshiWSTransportProtocolError("subscribe command is not JSON serializable") from exc
        await self._websocket.send(encoded)

    async def close(self) -> None:
        await self._websocket.close()


class KalshiWebsocketsTransportFactory:
    """Open bounded authenticated connections using the websockets 12 API."""

    def __init__(
        self,
        *,
        connect: ConnectCallable | None = None,
        open_timeout: float = 10.0,
        close_timeout: float = 5.0,
        ping_interval: float = 20.0,
        ping_timeout: float = 20.0,
        max_size: int = 1_048_576,
    ) -> None:
        for name, value in {
            "open_timeout": open_timeout,
            "close_timeout": close_timeout,
            "ping_interval": ping_interval,
            "ping_timeout": ping_timeout,
        }.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if isinstance(max_size, bool) or not isinstance(max_size, int) or max_size <= 0:
            raise ValueError("max_size must be a positive integer")
        self._connect: ConnectCallable = connect or websockets.connect  # type: ignore[assignment]
        self._open_timeout = float(open_timeout)
        self._close_timeout = float(close_timeout)
        self._ping_interval = float(ping_interval)
        self._ping_timeout = float(ping_timeout)
        self._max_size = max_size

    async def connect(self, instructions: KalshiWSConnectionInstructions) -> KalshiWebsocketsTransport:
        if instructions.url != KALSHI_V2_WS_URL:
            raise KalshiWSTransportProtocolError("credentials may only be sent to the approved Kalshi WebSocket URL")
        socket = await self._connect(
            instructions.url,
            extra_headers=dict(instructions.headers),
            open_timeout=self._open_timeout,
            close_timeout=self._close_timeout,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_timeout,
            max_size=self._max_size,
            logger=_SILENT_WEBSOCKETS_LOGGER,
        )
        return KalshiWebsocketsTransport(socket)


__all__ = [
    "KalshiWSTransportProtocolError",
    "KalshiWebsocketsTransport",
    "KalshiWebsocketsTransportFactory",
]
