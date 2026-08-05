from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from services import venues
from services.venues.kalshi_v2 import KalshiRequestSigner
from services.venues.kalshi_v2_ws_coordinator import (
    KalshiV2WSCoordinator,
    KalshiWSCoordinatorNoOp,
    KalshiWSCoordinatorStarted,
    KalshiWSCoordinatorTerminated,
)
from services.venues.kalshi_v2_ws_lifecycle import KalshiV2WSLifecycle
from services.venues.kalshi_v2_ws_session import (
    KalshiV2WSFrameSession,
    KalshiWSSessionNoOp,
    KalshiWSSessionRecoveryRequested,
)

MARKET_TICKER = "FED-23DEC-T3.00"
MARKET_ID = "9b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a1"
OBSERVED_AT = datetime(2026, 8, 4, 20, tzinfo=timezone.utc)


class FakeCoverage:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.results: dict[str, object] = {}
        self.failure: Exception | None = None
        self.release: asyncio.Event | None = None

    async def sweep(self, coverage_id: str, observed_at: datetime) -> object:
        self.events.append(f"coverage:{coverage_id}")
        if self.release is not None:
            await self.release.wait()
        if self.failure is not None:
            raise self.failure
        return self.results.setdefault(coverage_id, {"audit_only": coverage_id, "observed_at": observed_at})


class FakeTransport:
    def __init__(self, events: list[str], name: str) -> None:
        self.events = events
        self.name = name
        self.sent: list[dict[str, object]] = []
        self.receives: asyncio.Queue[Mapping[str, object]] = asyncio.Queue()
        self.closed = False
        self.active_reads = 0
        self.max_active_reads = 0

    async def send(self, payload: dict[str, object]) -> None:
        self.events.append(f"send:{self.name}:{payload['id']}")
        self.sent.append(payload)

    async def receive(self) -> Mapping[str, object]:
        self.active_reads += 1
        self.max_active_reads = max(self.max_active_reads, self.active_reads)
        try:
            return await self.receives.get()
        finally:
            self.active_reads -= 1

    async def close(self) -> None:
        self.events.append(f"close:{self.name}")
        self.closed = True


class ExplodingMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise RuntimeError("decoded mapping failed")

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0

    def get(self, key: str, default: object = None) -> object:
        raise RuntimeError("decoded mapping failed")


class SlowCloseTransport(FakeTransport):
    def __init__(self, events: list[str], name: str) -> None:
        super().__init__(events, name)
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()

    async def close(self) -> None:
        self.close_started.set()
        await self.release_close.wait()
        await super().close()


class BlockingRecoveryTransport(FakeTransport):
    def __init__(self, events: list[str], name: str) -> None:
        super().__init__(events, name)
        self.recovery_started = asyncio.Event()
        self.release_recovery = asyncio.Event()

    async def send(self, payload: dict[str, object]) -> None:
        if payload.get("cmd") == "update_subscription":
            self.recovery_started.set()
            await self.release_recovery.wait()
        await super().send(payload)


def _coordinator(events: list[str]) -> tuple[KalshiV2WSCoordinator, FakeCoverage]:
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
    coverage = FakeCoverage(events)
    return KalshiV2WSCoordinator(KalshiV2WSFrameSession(lifecycle), coverage), coverage


def _ack(command_id: int, channel: str, sid: int) -> dict[str, object]:
    return {"id": command_id, "type": "subscribed", "msg": {"channel": channel, "sid": sid}}


def _snapshot(seq: int) -> dict[str, object]:
    return {
        "type": "orderbook_snapshot",
        "sid": 11,
        "seq": seq,
        "msg": {
            "market_ticker": MARKET_TICKER,
            "market_id": MARKET_ID,
            "yes_dollars_fp": [["0.220000", "2.00"]],
            "no_dollars_fp": [["0.560000", "3.00"]],
        },
    }


def _delta(seq: int) -> dict[str, object]:
    return {
        "type": "orderbook_delta",
        "sid": 11,
        "seq": seq,
        "msg": {
            "market_ticker": MARKET_TICKER,
            "market_id": MARKET_ID,
            "price_dollars": "0.220000",
            "delta_fp": "1.00",
            "side": "yes",
        },
    }


def test_coordinator_is_exported_and_cold_start_sweeps_before_ordered_subscriptions() -> None:
    async def scenario() -> None:
        events: list[str] = []
        coordinator, _ = _coordinator(events)
        transport = FakeTransport(events, "first")

        started = await coordinator.start(
            transport,
            timestamp_ms=1_785_844_800_000,
            coverage_id="cold-start-1",
            observed_at=OBSERVED_AT,
        )

        assert venues.KalshiV2WSCoordinator is KalshiV2WSCoordinator
        assert isinstance(started, KalshiWSCoordinatorStarted)
        assert started.generation == 1
        assert started.epoch_id == 1
        assert events == ["coverage:cold-start-1", "send:first:1", "send:first:2", "send:first:3"]
        assert [payload["params"]["channels"] for payload in transport.sent] == [
            ["orderbook_delta"],
            ["user_orders"],
            ["fill"],
        ]
        assert coordinator.private_stream_healthy is False
        assert coordinator.connection_healthy is False
        assert coordinator.retry_allowed is False

    asyncio.run(scenario())


def test_coverage_failure_opens_no_epoch_sends_nothing_and_start_can_retry() -> None:
    async def scenario() -> None:
        events: list[str] = []
        coordinator, coverage = _coordinator(events)
        first = FakeTransport(events, "first")
        coverage.failure = RuntimeError("coverage unavailable")

        try:
            await coordinator.start(
                first,
                timestamp_ms=1_785_844_800_000,
                coverage_id="failed",
                observed_at=OBSERVED_AT,
            )
        except RuntimeError as exc:
            assert str(exc) == "coverage unavailable"
        else:
            raise AssertionError("coverage failure must propagate")

        assert first.sent == []
        assert coordinator.active_generation is None
        assert coordinator.session.lifecycle.current_epoch_id is None

        coverage.failure = None
        second = FakeTransport(events, "second")
        started = await coordinator.start(
            second,
            timestamp_ms=1_785_844_800_001,
            coverage_id="retry",
            observed_at=OBSERVED_AT,
        )
        assert started.generation == 1
        assert len(second.sent) == 3

    asyncio.run(scenario())


def test_recovery_command_is_serialized_once_on_the_bound_transport() -> None:
    async def scenario() -> None:
        events: list[str] = []
        coordinator, _ = _coordinator(events)
        transport = FakeTransport(events, "first")
        started = await coordinator.start(
            transport,
            timestamp_ms=1_785_844_800_000,
            coverage_id="cold-start",
            observed_at=OBSERVED_AT,
        )
        for command, sid in zip(transport.sent[:3], (11, 12, 13), strict=True):
            await transport.receives.put(_ack(command["id"], command["params"]["channels"][0], sid))
            await coordinator.receive_once(started.generation)
        await transport.receives.put(_snapshot(10))
        await coordinator.receive_once(started.generation)
        await transport.receives.put(_delta(13))

        outcome = await coordinator.receive_once(started.generation)

        assert isinstance(outcome, KalshiWSSessionRecoveryRequested)
        assert transport.sent[-1] == outcome.command
        assert len(transport.sent) == 4
        assert coordinator.retry_allowed is False

    asyncio.run(scenario())


def test_reconnect_discards_delayed_old_receive_without_routing_or_closing_new_transport() -> None:
    async def scenario() -> None:
        events: list[str] = []
        coordinator, _ = _coordinator(events)
        first = FakeTransport(events, "first")
        first_started = await coordinator.start(
            first,
            timestamp_ms=1_785_844_800_000,
            coverage_id="first",
            observed_at=OBSERVED_AT,
        )
        delayed = asyncio.create_task(coordinator.receive_once(first_started.generation))
        await asyncio.sleep(0)

        second = FakeTransport(events, "second")
        second_started = await coordinator.start(
            second,
            timestamp_ms=1_785_844_800_001,
            coverage_id="second",
            observed_at=OBSERVED_AT,
        )
        second_orderbook = second.sent[0]
        await second.receives.put(
            _ack(
                second_orderbook["id"],
                second_orderbook["params"]["channels"][0],
                21,
            )
        )
        second_outcome = await coordinator.receive_once(second_started.generation)
        outcome = await delayed

        assert isinstance(second_outcome, KalshiWSSessionNoOp)
        assert outcome == KalshiWSCoordinatorNoOp(generation=first_started.generation, reason="stale_generation")
        assert first.closed is True
        assert second.closed is False
        assert coordinator.active_generation == second_started.generation
        assert coordinator.session.lifecycle.current_epoch_id == second_started.epoch_id

    asyncio.run(scenario())


def test_disconnect_retracts_closes_and_invokes_audit_coverage_before_cancellation_returns() -> None:
    async def scenario() -> None:
        events: list[str] = []
        coordinator, coverage = _coordinator(events)
        transport = FakeTransport(events, "first")
        started = await coordinator.start(
            transport,
            timestamp_ms=1_785_844_800_000,
            coverage_id="cold-start",
            observed_at=OBSERVED_AT,
        )
        coverage.release = asyncio.Event()
        task = asyncio.create_task(
            coordinator.disconnect(
                started.generation,
                coverage_id="disconnect-1",
                observed_at=OBSERVED_AT,
            )
        )
        while "coverage:disconnect-1" not in events:
            await asyncio.sleep(0)
        task.cancel()
        coverage.release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("caller cancellation must propagate after cleanup")

        assert coordinator.active_generation is None
        assert coordinator.session.lifecycle.current_epoch_id is None
        assert transport.closed is True
        assert events.index("close:first") < events.index("coverage:disconnect-1")
        assert coordinator.private_stream_healthy is False
        assert coordinator.connection_healthy is False
        assert coordinator.retry_allowed is False

        coverage.release = None
        replacement = FakeTransport(events, "replacement")
        replacement_started = await coordinator.start(
            replacement,
            timestamp_ms=1_785_844_800_001,
            coverage_id="replacement",
            observed_at=OBSERVED_AT,
        )
        assert coordinator.active_generation == replacement_started.generation

    asyncio.run(scenario())


def test_terminal_frame_fails_closed_and_closes_active_transport() -> None:
    async def scenario() -> None:
        events: list[str] = []
        coordinator, _ = _coordinator(events)
        transport = FakeTransport(events, "first")
        started = await coordinator.start(
            transport,
            timestamp_ms=1_785_844_800_000,
            coverage_id="cold-start",
            observed_at=OBSERVED_AT,
        )
        await transport.receives.put({"type": "not-modeled"})

        outcome = await coordinator.receive_once(started.generation)

        assert isinstance(outcome, KalshiWSCoordinatorTerminated)
        assert outcome.reason == "unknown_frame_type"
        assert coordinator.active_generation is None
        assert coordinator.session.lifecycle.current_epoch_id is None
        assert transport.closed is True

    asyncio.run(scenario())


def test_concurrent_receive_calls_never_overlap_transport_reads() -> None:
    async def scenario() -> None:
        events: list[str] = []
        coordinator, _ = _coordinator(events)
        transport = FakeTransport(events, "first")
        started = await coordinator.start(
            transport,
            timestamp_ms=1_785_844_800_000,
            coverage_id="cold-start",
            observed_at=OBSERVED_AT,
        )
        first = asyncio.create_task(coordinator.receive_once(started.generation))
        second = asyncio.create_task(coordinator.receive_once(started.generation))
        await asyncio.sleep(0)
        assert transport.max_active_reads == 1

        await transport.receives.put(_ack(1, "orderbook_delta", 11))
        await transport.receives.put(_ack(2, "user_orders", 12))
        await asyncio.gather(first, second)

        assert transport.max_active_reads == 1
        assert coordinator.active_generation == started.generation

    asyncio.run(scenario())


def test_unexpected_decoded_mapping_failure_retracts_publication_and_closes() -> None:
    async def scenario() -> None:
        events: list[str] = []
        coordinator, _ = _coordinator(events)
        transport = FakeTransport(events, "first")
        started = await coordinator.start(
            transport,
            timestamp_ms=1_785_844_800_000,
            coverage_id="cold-start",
            observed_at=OBSERVED_AT,
        )
        for command, sid in zip(transport.sent[:3], (11, 12, 13), strict=True):
            await transport.receives.put(_ack(command["id"], command["params"]["channels"][0], sid))
            await coordinator.receive_once(started.generation)
        await transport.receives.put(_snapshot(10))
        await coordinator.receive_once(started.generation)
        assert coordinator.session.lifecycle.orderbook_publishable is True

        await transport.receives.put(ExplodingMapping())
        outcome = await coordinator.receive_once(started.generation)

        assert outcome == KalshiWSCoordinatorTerminated(
            generation=started.generation,
            epoch_id=started.epoch_id,
            reason="malformed_decoded_frame",
        )
        assert coordinator.session.lifecycle.orderbook_publishable is False
        assert coordinator.active_generation is None
        assert transport.closed is True

    asyncio.run(scenario())


def test_cancelled_reconnect_closes_candidate_and_leaves_no_active_generation() -> None:
    async def scenario() -> None:
        events: list[str] = []
        coordinator, _ = _coordinator(events)
        transport = BlockingRecoveryTransport(events, "first")
        started = await coordinator.start(
            transport,
            timestamp_ms=1_785_844_800_000,
            coverage_id="cold-start",
            observed_at=OBSERVED_AT,
        )
        for command, sid in zip(transport.sent[:3], (11, 12, 13), strict=True):
            await transport.receives.put(_ack(command["id"], command["params"]["channels"][0], sid))
            await coordinator.receive_once(started.generation)
        await transport.receives.put(_snapshot(10))
        await coordinator.receive_once(started.generation)
        await transport.receives.put(_delta(13))
        recovery = asyncio.create_task(coordinator.receive_once(started.generation))
        await transport.recovery_started.wait()

        candidate = FakeTransport(events, "candidate")
        reconnect = asyncio.create_task(
            coordinator.start(
                candidate,
                timestamp_ms=1_785_844_800_001,
                coverage_id="reconnect",
                observed_at=OBSERVED_AT,
            )
        )
        await asyncio.sleep(0)
        reconnect.cancel()
        try:
            await reconnect
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("reconnect cancellation must propagate")

        assert candidate.closed is True
        assert coordinator.active_generation is None
        assert coordinator.session.lifecycle.current_epoch_id is None
        outcome = await recovery
        assert outcome == KalshiWSCoordinatorNoOp(generation=started.generation, reason="stale_generation")

    asyncio.run(scenario())


def test_cancelled_queued_receive_fails_closed_after_inflight_recovery_finishes() -> None:
    async def scenario() -> None:
        events: list[str] = []
        coordinator, _ = _coordinator(events)
        transport = BlockingRecoveryTransport(events, "first")
        started = await coordinator.start(
            transport,
            timestamp_ms=1_785_844_800_000,
            coverage_id="cold-start",
            observed_at=OBSERVED_AT,
        )
        for command, sid in zip(transport.sent[:3], (11, 12, 13), strict=True):
            await transport.receives.put(_ack(command["id"], command["params"]["channels"][0], sid))
            await coordinator.receive_once(started.generation)
        await transport.receives.put(_snapshot(10))
        await coordinator.receive_once(started.generation)
        await transport.receives.put(_delta(13))
        recovery = asyncio.create_task(coordinator.receive_once(started.generation))
        await transport.recovery_started.wait()

        queued = asyncio.create_task(coordinator.receive_once(started.generation))
        await asyncio.sleep(0)
        queued.cancel()
        try:
            await queued
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("receive cancellation must propagate after fail-closed cleanup")

        assert coordinator.active_generation is None
        assert coordinator.session.lifecycle.current_epoch_id is None
        assert transport.closed is True
        outcome = await recovery
        assert outcome == KalshiWSCoordinatorNoOp(generation=started.generation, reason="stale_generation")

    asyncio.run(scenario())


def test_disconnect_cancels_blocked_recovery_send_without_waiting_for_transport_send() -> None:
    async def scenario() -> None:
        events: list[str] = []
        coordinator, _ = _coordinator(events)
        transport = BlockingRecoveryTransport(events, "first")
        started = await coordinator.start(
            transport,
            timestamp_ms=1_785_844_800_000,
            coverage_id="cold-start",
            observed_at=OBSERVED_AT,
        )
        for command, sid in zip(transport.sent[:3], (11, 12, 13), strict=True):
            await transport.receives.put(_ack(command["id"], command["params"]["channels"][0], sid))
            await coordinator.receive_once(started.generation)
        await transport.receives.put(_snapshot(10))
        await coordinator.receive_once(started.generation)
        await transport.receives.put(_delta(13))
        recovery = asyncio.create_task(coordinator.receive_once(started.generation))
        await transport.recovery_started.wait()

        result = await coordinator.disconnect(
            started.generation,
            coverage_id="disconnect-blocked-recovery",
            observed_at=OBSERVED_AT,
        )

        assert result == {
            "audit_only": "disconnect-blocked-recovery",
            "observed_at": OBSERVED_AT,
        }
        assert transport.closed is True
        assert coordinator.active_generation is None
        outcome = await recovery
        assert outcome == KalshiWSCoordinatorNoOp(generation=started.generation, reason="stale_generation")

    asyncio.run(scenario())


def test_disconnect_cancellation_waits_for_close_and_invokes_coverage_before_returning() -> None:
    async def scenario() -> None:
        events: list[str] = []
        coordinator, _ = _coordinator(events)
        transport = SlowCloseTransport(events, "first")
        started = await coordinator.start(
            transport,
            timestamp_ms=1_785_844_800_000,
            coverage_id="cold-start",
            observed_at=OBSERVED_AT,
        )
        disconnect = asyncio.create_task(
            coordinator.disconnect(
                started.generation,
                coverage_id="disconnect-slow-close",
                observed_at=OBSERVED_AT,
            )
        )
        await transport.close_started.wait()
        disconnect.cancel()
        await asyncio.sleep(0)
        assert disconnect.done() is False

        transport.release_close.set()
        try:
            await disconnect
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("disconnect cancellation must propagate after cleanup")

        assert transport.closed is True
        assert "coverage:disconnect-slow-close" in events
        assert coordinator.active_generation is None
        assert coordinator.session.lifecycle.current_epoch_id is None

    asyncio.run(scenario())
