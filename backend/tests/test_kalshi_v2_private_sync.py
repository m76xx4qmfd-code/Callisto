from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from services.venues.kalshi_v2 import KALSHI_PRODUCTION_ORIGIN, KalshiRequestSigner
from services.venues.kalshi_v2_private_sync import (
    KalshiPrivatePrincipalMismatchError,
    KalshiPrivateSyncRunner,
)
from services.venues.kalshi_v2_private_ws import KalshiPrivateWSLifecycle


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.frames: asyncio.Queue[Mapping[str, object] | BaseException] = asyncio.Queue()
        self.closed = False

    async def send(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)

    async def receive(self) -> Mapping[str, object]:
        value = await self.frames.get()
        if isinstance(value, BaseException):
            raise value
        return value

    async def close(self) -> None:
        self.closed = True


class FakeFactory:
    def __init__(self, transports: list[FakeTransport]) -> None:
        self.transports = transports
        self.calls = 0

    async def connect(self, instructions) -> FakeTransport:
        transport = self.transports[self.calls]
        self.calls += 1
        return transport


class FakeSynchronizer:
    def __init__(self, principal_fingerprint: str) -> None:
        self.principal_fingerprint = principal_fingerprint
        self.calls = 0
        self.started: asyncio.Queue[int] = asyncio.Queue()
        self.block_call: int | None = None
        self.release = asyncio.Event()
        self.release.set()
        self.cancelled = asyncio.Event()
        self.status: Literal["complete", "incomplete"] = "complete"

    async def synchronize(self) -> FakeSynchronizationEvidence:
        self.calls += 1
        await self.started.put(self.calls)
        if self.block_call == self.calls:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        return FakeSynchronizationEvidence(
            principal_fingerprint=self.principal_fingerprint,
            status=self.status,
        )


@dataclass(frozen=True)
class FakeSynchronizationEvidence:
    principal_fingerprint: str
    status: Literal["complete", "incomplete"]


def _lifecycle() -> KalshiPrivateWSLifecycle:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return KalshiPrivateWSLifecycle(
        signer=KalshiRequestSigner(key_id="generated-test-key", private_key_pem=pem),
        principal_origin=KALSHI_PRODUCTION_ORIGIN,
    )


def _position(sid: int = 13) -> dict[str, object]:
    return {
        "type": "market_position",
        "sid": sid,
        "msg": {
            "user_id": "8b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a2",
            "market_ticker": "FED-23DEC-T3.00",
            "position_fp": "1.00",
            "position_cost_dollars": "0.2500",
            "realized_pnl_dollars": "0.0000",
            "fees_paid_dollars": "0.0100",
            "position_fee_cost_dollars": "0.0050",
            "volume_fp": "1.00",
            "subaccount": 0,
        },
    }


async def _ack_all(transport: FakeTransport) -> None:
    while len(transport.sent) < 3:
        await asyncio.sleep(0)
    for sid, command in enumerate(transport.sent, start=11):
        params = command["params"]
        assert isinstance(params, dict)
        channels = params["channels"]
        assert isinstance(channels, list)
        await transport.frames.put(
            {"id": command["id"], "type": "subscribed", "msg": {"channel": channels[0], "sid": sid}}
        )


def test_runner_subscribes_then_syncs_and_dirty_during_sync_forces_follow_up() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        lifecycle = _lifecycle()
        synchronizer = FakeSynchronizer(lifecycle.principal_fingerprint)
        synchronizer.block_call = 1
        synchronizer.release.clear()
        runner = KalshiPrivateSyncRunner(
            lifecycle=lifecycle,
            transport_factory=FakeFactory([transport]),
            synchronizer=synchronizer,
            debounce_seconds=0.01,
            minimum_sync_interval_seconds=0.01,
            max_staleness_seconds=10,
            acknowledgement_timeout_seconds=1,
        )
        task = asyncio.create_task(runner.run())
        await _ack_all(transport)
        assert await synchronizer.started.get() == 1
        await transport.frames.put(_position())
        synchronizer.release.set()
        assert await synchronizer.started.get() == 2
        while not runner.ready:
            await asyncio.sleep(0)

        assert [item["cmd"] for item in transport.sent] == ["subscribe", "subscribe", "subscribe"]
        assert runner.retry_allowed is False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert transport.closed is True
        assert runner.ready is False

    asyncio.run(scenario())


def test_private_frame_persists_invalidation_before_recovery_sync() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        lifecycle = _lifecycle()
        synchronizer = FakeSynchronizer(lifecycle.principal_fingerprint)
        invalidation_started = asyncio.Event()
        release_invalidation = asyncio.Event()
        invalidations: list[tuple[str, int]] = []

        async def persist_invalidation(reason: str, readiness_revision: int) -> None:
            invalidations.append((reason, readiness_revision))
            invalidation_started.set()
            await release_invalidation.wait()

        runner = KalshiPrivateSyncRunner(
            lifecycle=lifecycle,
            transport_factory=FakeFactory([transport]),
            synchronizer=synchronizer,
            debounce_seconds=0.01,
            minimum_sync_interval_seconds=0.01,
            acknowledgement_timeout_seconds=1,
            on_invalidation=persist_invalidation,
        )
        task = asyncio.create_task(runner.run())
        await _ack_all(transport)
        assert await synchronizer.started.get() == 1
        while not runner.ready:
            await asyncio.sleep(0)

        await transport.frames.put(_position())
        await asyncio.wait_for(invalidation_started.wait(), timeout=1)
        assert runner.ready is False
        assert synchronizer.calls == 1
        assert invalidations == [("private_frame", 2)]

        release_invalidation.set()
        assert await asyncio.wait_for(synchronizer.started.get(), timeout=1) == 2
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert invalidations[-1] == ("cancelled", 3)

    asyncio.run(scenario())


def test_frame_between_subscription_acks_forces_post_bootstrap_follow_up() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        lifecycle = _lifecycle()
        synchronizer = FakeSynchronizer(lifecycle.principal_fingerprint)
        runner = KalshiPrivateSyncRunner(
            lifecycle=lifecycle,
            transport_factory=FakeFactory([transport]),
            synchronizer=synchronizer,
            debounce_seconds=0.01,
            minimum_sync_interval_seconds=0.01,
            acknowledgement_timeout_seconds=1,
        )
        task = asyncio.create_task(runner.run())
        while len(transport.sent) < 3:
            await asyncio.sleep(0)
        position_command = transport.sent[2]
        await transport.frames.put(
            {
                "id": position_command["id"],
                "type": "subscribed",
                "msg": {"channel": "market_positions", "sid": 13},
            }
        )
        await transport.frames.put(_position())
        for sid, command in ((11, transport.sent[0]), (12, transport.sent[1])):
            params = command["params"]
            assert isinstance(params, dict)
            channels = params["channels"]
            assert isinstance(channels, list)
            await transport.frames.put(
                {"id": command["id"], "type": "subscribed", "msg": {"channel": channels[0], "sid": sid}}
            )
        assert await synchronizer.started.get() == 1
        assert await asyncio.wait_for(synchronizer.started.get(), timeout=1) == 2
        while not runner.ready:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_runner_coalesces_bursts_and_ttl_bounds_readiness() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        lifecycle = _lifecycle()
        synchronizer = FakeSynchronizer(lifecycle.principal_fingerprint)
        clock = [100.0]
        runner = KalshiPrivateSyncRunner(
            lifecycle=lifecycle,
            transport_factory=FakeFactory([transport]),
            synchronizer=synchronizer,
            debounce_seconds=0.02,
            minimum_sync_interval_seconds=0.01,
            max_staleness_seconds=5,
            acknowledgement_timeout_seconds=1,
            monotonic=lambda: clock[0],
        )
        task = asyncio.create_task(runner.run())
        await _ack_all(transport)
        assert await synchronizer.started.get() == 1
        while not runner.ready:
            await asyncio.sleep(0)
        await transport.frames.put(_position())
        await transport.frames.put(_position())
        assert await synchronizer.started.get() == 2
        await asyncio.sleep(0.03)
        assert synchronizer.calls == 2
        clock[0] = 106.0
        assert runner.ready is False
        assert runner.degraded_reason == "synchronization_stale"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


def test_malformed_disconnect_retracts_and_reconnects_with_fresh_full_sync() -> None:
    async def scenario() -> None:
        first = FakeTransport()
        second = FakeTransport()
        lifecycle = _lifecycle()
        synchronizer = FakeSynchronizer(lifecycle.principal_fingerprint)
        factory = FakeFactory([first, second])
        runner = KalshiPrivateSyncRunner(
            lifecycle=lifecycle,
            transport_factory=factory,
            synchronizer=synchronizer,
            debounce_seconds=0.001,
            minimum_sync_interval_seconds=0.001,
            max_staleness_seconds=10,
            acknowledgement_timeout_seconds=1,
            initial_backoff_seconds=0.001,
            maximum_backoff_seconds=0.002,
        )
        task = asyncio.create_task(runner.run())
        await _ack_all(first)
        assert await synchronizer.started.get() == 1
        while not runner.ready:
            await asyncio.sleep(0)
        await first.frames.put({"type": "unknown"})
        while factory.calls < 2:
            await asyncio.sleep(0)
        assert runner.ready is False
        assert first.closed is True
        await _ack_all(second)
        assert await synchronizer.started.get() == 2
        while not runner.ready:
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


def test_incomplete_rest_evidence_never_becomes_ready_and_forces_reconnect() -> None:
    async def scenario() -> None:
        first = FakeTransport()
        second = FakeTransport()
        lifecycle = _lifecycle()
        synchronizer = FakeSynchronizer(lifecycle.principal_fingerprint)
        synchronizer.status = "incomplete"
        factory = FakeFactory([first, second])
        runner = KalshiPrivateSyncRunner(
            lifecycle=lifecycle,
            transport_factory=factory,
            synchronizer=synchronizer,
            acknowledgement_timeout_seconds=1,
            initial_backoff_seconds=0.001,
            maximum_backoff_seconds=0.002,
        )
        task = asyncio.create_task(runner.run())
        await _ack_all(first)
        assert await synchronizer.started.get() == 1
        while factory.calls < 2:
            await asyncio.sleep(0)
        assert runner.ready is False
        assert first.closed is True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert second.closed is True

    asyncio.run(scenario())


def test_principal_change_during_sync_fails_closed_without_reconnect() -> None:
    async def scenario() -> None:
        first = FakeTransport()
        lifecycle = _lifecycle()
        synchronizer = FakeSynchronizer(lifecycle.principal_fingerprint)
        synchronizer.block_call = 1
        synchronizer.release.clear()
        factory = FakeFactory([first])
        runner = KalshiPrivateSyncRunner(
            lifecycle=lifecycle,
            transport_factory=factory,
            synchronizer=synchronizer,
            acknowledgement_timeout_seconds=1,
            initial_backoff_seconds=0.001,
            maximum_backoff_seconds=0.002,
        )
        task = asyncio.create_task(runner.run())
        await _ack_all(first)
        assert await synchronizer.started.get() == 1
        synchronizer.principal_fingerprint = "principal-b"
        synchronizer.release.set()
        with pytest.raises(KalshiPrivatePrincipalMismatchError):
            await task
        assert runner.ready is False
        assert runner.degraded_reason == "principal_mismatch"
        assert factory.calls == 1
        assert first.closed is True

    asyncio.run(scenario())


def test_mismatched_ws_and_rest_credentials_are_rejected_before_connect() -> None:
    lifecycle = _lifecycle()
    synchronizer = FakeSynchronizer("0" * 64)
    factory = FakeFactory([])

    with pytest.raises(KalshiPrivatePrincipalMismatchError):
        KalshiPrivateSyncRunner(
            lifecycle=lifecycle,
            transport_factory=factory,
            synchronizer=synchronizer,
        )

    assert factory.calls == 0


def test_cancelling_blocked_synchronization_cancels_owned_operation() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        lifecycle = _lifecycle()
        synchronizer = FakeSynchronizer(lifecycle.principal_fingerprint)
        synchronizer.block_call = 1
        synchronizer.release.clear()
        runner = KalshiPrivateSyncRunner(
            lifecycle=lifecycle,
            transport_factory=FakeFactory([transport]),
            synchronizer=synchronizer,
            acknowledgement_timeout_seconds=1,
        )
        task = asyncio.create_task(runner.run())
        await _ack_all(transport)
        assert await synchronizer.started.get() == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(synchronizer.cancelled.wait(), timeout=1)
        assert transport.closed is True

    asyncio.run(scenario())


def test_cancelling_during_transport_close_waits_for_close_before_propagating() -> None:
    class SlowCloseTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.close_started = asyncio.Event()
            self.release_close = asyncio.Event()
            self.close_cancelled = False

        async def close(self) -> None:
            self.close_started.set()
            try:
                await self.release_close.wait()
            except asyncio.CancelledError:
                self.close_cancelled = True
                raise
            self.closed = True

    async def scenario() -> None:
        transport = SlowCloseTransport()
        lifecycle = _lifecycle()
        synchronizer = FakeSynchronizer(lifecycle.principal_fingerprint)
        runner = KalshiPrivateSyncRunner(
            lifecycle=lifecycle,
            transport_factory=FakeFactory([transport]),
            synchronizer=synchronizer,
            acknowledgement_timeout_seconds=1,
            initial_backoff_seconds=1,
            maximum_backoff_seconds=1,
        )
        task = asyncio.create_task(runner.run())
        await _ack_all(transport)
        assert await synchronizer.started.get() == 1
        while not runner.ready:
            await asyncio.sleep(0)
        await transport.frames.put({"type": "unknown"})
        await asyncio.wait_for(transport.close_started.wait(), timeout=1)

        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
        transport.release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert transport.closed is True
        assert transport.close_cancelled is False

    asyncio.run(scenario())
