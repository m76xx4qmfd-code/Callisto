from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import ANY

import pytest

from services.venues.kalshi_v2_ws_lifecycle import KalshiWSConnectionInstructions
from services.venues.kalshi_v2_ws_transport import (
    KalshiWebsocketsTransportFactory,
    KalshiWSTransportProtocolError,
)


class FakeSocket:
    def __init__(self, frames: list[str | bytes]) -> None:
        self.frames: asyncio.Queue[str | bytes] = asyncio.Queue()
        for frame in frames:
            self.frames.put_nowait(frame)
        self.sent: list[str] = []
        self.closed = False

    async def recv(self) -> str | bytes:
        return await self.frames.get()

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True


def _instructions() -> KalshiWSConnectionInstructions:
    return KalshiWSConnectionInstructions(
        epoch_id=1,
        timestamp_ms=1,
        url="wss://external-api-ws.kalshi.com/trade-api/ws/v2",
        headers={"KALSHI-ACCESS-KEY": "secret-key", "KALSHI-ACCESS-SIGNATURE": "secret-signature"},
        subscriptions=(),
    )


def test_websockets_factory_applies_bounded_v12_settings_and_transport_json_boundary() -> None:
    async def scenario() -> None:
        socket = FakeSocket(['{"type":"subscribed","id":1,"msg":{"channel":"fill","sid":2}}'])
        calls: list[tuple[str, dict[str, object]]] = []

        async def connect(url: str, **kwargs: object) -> FakeSocket:
            calls.append((url, kwargs))
            return socket

        factory = KalshiWebsocketsTransportFactory(
            connect=connect,
            open_timeout=3.0,
            close_timeout=2.0,
            ping_interval=10.0,
            ping_timeout=5.0,
            max_size=4096,
        )
        transport = await factory.connect(_instructions())
        assert await transport.receive() == {
            "type": "subscribed",
            "id": 1,
            "msg": {"channel": "fill", "sid": 2},
        }
        await transport.send({"id": 1, "cmd": "subscribe", "params": {"channels": ["fill"]}})
        assert json.loads(socket.sent[0])["cmd"] == "subscribe"
        await transport.close()
        assert socket.closed is True
        assert calls == [
            (
                _instructions().url,
                {
                    "extra_headers": _instructions().headers,
                    "open_timeout": 3.0,
                    "close_timeout": 2.0,
                    "ping_interval": 10.0,
                    "ping_timeout": 5.0,
                    "max_size": 4096,
                    "logger": ANY,
                },
            )
        ]
        logger = calls[0][1]["logger"]
        assert isinstance(logger, logging.Logger)
        assert logger.disabled is True

    asyncio.run(scenario())


@pytest.mark.parametrize("frame", [b"{}", "[]", "null", "not-json", '"text"'])
def test_transport_rejects_binary_malformed_and_non_object_frames(frame: str | bytes) -> None:
    async def scenario() -> None:
        socket = FakeSocket([frame])

        async def connect(url: str, **kwargs: object) -> FakeSocket:
            return socket

        transport = await KalshiWebsocketsTransportFactory(connect=connect).connect(_instructions())
        with pytest.raises(KalshiWSTransportProtocolError):
            await transport.receive()

    asyncio.run(scenario())


def test_transport_outbound_surface_rejects_non_subscribe_commands() -> None:
    async def scenario() -> None:
        socket = FakeSocket([])

        async def connect(url: str, **kwargs: object) -> FakeSocket:
            return socket

        transport = await KalshiWebsocketsTransportFactory(connect=connect).connect(_instructions())
        with pytest.raises(KalshiWSTransportProtocolError):
            await transport.send({"id": 1, "cmd": "update_subscription", "params": {}})
        assert socket.sent == []

    asyncio.run(scenario())


def test_factory_rejects_hostile_url_before_credentials_reach_connect() -> None:
    async def scenario() -> None:
        calls = 0

        async def connect(url: str, **kwargs: object) -> FakeSocket:
            nonlocal calls
            calls += 1
            return FakeSocket([])

        instructions = _instructions()
        hostile = KalshiWSConnectionInstructions(
            epoch_id=instructions.epoch_id,
            timestamp_ms=instructions.timestamp_ms,
            url="wss://attacker.example/trade-api/ws/v2",
            headers=instructions.headers,
            subscriptions=instructions.subscriptions,
        )
        with pytest.raises(KalshiWSTransportProtocolError):
            await KalshiWebsocketsTransportFactory(connect=connect).connect(hostile)
        assert calls == 0

    asyncio.run(scenario())
