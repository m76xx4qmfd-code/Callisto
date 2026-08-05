"""Reconnectable GET-only private portfolio synchronization runtime.

Unsequenced private WebSocket frames are dirty triggers only. Readiness requires
recent successful synchronization and can never authorize an order retry.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import Literal, Protocol, TypeVar

from services.venues.kalshi_v2_private_ws import KalshiPrivateWSLifecycle
from services.venues.kalshi_v2_ws_lifecycle import KalshiWSConnectionInstructions

_T = TypeVar("_T")


class KalshiPrivateSynchronizationEvidence(Protocol):
    principal_fingerprint: str
    status: Literal["complete", "incomplete"]


class KalshiPrivateSynchronizer(Protocol):
    @property
    def principal_fingerprint(self) -> str: ...

    async def synchronize(self) -> KalshiPrivateSynchronizationEvidence: ...


class KalshiPrivateTransport(Protocol):
    async def send(self, payload: dict[str, object]) -> None: ...

    async def receive(self) -> Mapping[str, object]: ...

    async def close(self) -> None: ...


class KalshiPrivateTransportFactory(Protocol):
    async def connect(self, instructions: KalshiWSConnectionInstructions) -> KalshiPrivateTransport: ...


class _GenerationEnded(RuntimeError):
    """Internal signal that the active transport generation must end."""


class KalshiPrivatePrincipalMismatchError(RuntimeError):
    """Raised when authenticated-principal identity changes during one runner lifetime."""


class KalshiPrivateSyncRunner:
    """Maintain one private socket and debounce authoritative GET synchronization."""

    def __init__(
        self,
        *,
        lifecycle: KalshiPrivateWSLifecycle,
        transport_factory: KalshiPrivateTransportFactory,
        synchronizer: KalshiPrivateSynchronizer,
        debounce_seconds: float = 0.25,
        minimum_sync_interval_seconds: float = 1.0,
        max_staleness_seconds: float = 30.0,
        acknowledgement_timeout_seconds: float = 10.0,
        synchronization_timeout_seconds: float = 30.0,
        initial_backoff_seconds: float = 0.5,
        maximum_backoff_seconds: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        durations = {
            "debounce_seconds": debounce_seconds,
            "minimum_sync_interval_seconds": minimum_sync_interval_seconds,
            "max_staleness_seconds": max_staleness_seconds,
            "acknowledgement_timeout_seconds": acknowledgement_timeout_seconds,
            "synchronization_timeout_seconds": synchronization_timeout_seconds,
            "initial_backoff_seconds": initial_backoff_seconds,
            "maximum_backoff_seconds": maximum_backoff_seconds,
        }
        for name, value in durations.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if initial_backoff_seconds > maximum_backoff_seconds:
            raise ValueError("initial_backoff_seconds cannot exceed maximum_backoff_seconds")
        fingerprint = synchronizer.principal_fingerprint
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise ValueError("synchronizer principal_fingerprint must be non-empty")
        if lifecycle.principal_fingerprint != fingerprint:
            raise KalshiPrivatePrincipalMismatchError("WebSocket and REST credentials identify different principals")
        self._lifecycle = lifecycle
        self._transport_factory = transport_factory
        self._synchronizer = synchronizer
        self._principal_fingerprint = fingerprint
        self._debounce = float(debounce_seconds)
        self._minimum_interval = float(minimum_sync_interval_seconds)
        self._max_staleness = float(max_staleness_seconds)
        self._ack_timeout = float(acknowledgement_timeout_seconds)
        self._sync_timeout = float(synchronization_timeout_seconds)
        self._initial_backoff = float(initial_backoff_seconds)
        self._maximum_backoff = float(maximum_backoff_seconds)
        self._monotonic = monotonic
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
        self._last_connection_timestamp_ms = 0
        self._connected = False
        self._acknowledged = False
        self._ready_flag = False
        self._dirty_version = 0
        self._dirty_event = asyncio.Event()
        self._ack_event = asyncio.Event()
        self._last_synchronized_at: float | None = None
        self._terminal_reason: str | None = "not_started"

    @property
    def retry_allowed(self) -> Literal[False]:
        return False

    @property
    def ready(self) -> bool:
        synchronized_at = self._last_synchronized_at
        return (
            self._ready_flag
            and self._connected
            and self._acknowledged
            and synchronized_at is not None
            and self._monotonic() - synchronized_at <= self._max_staleness
            and self._synchronizer.principal_fingerprint == self._principal_fingerprint
        )

    @property
    def degraded_reason(self) -> str | None:
        if not self._connected:
            return self._terminal_reason or "disconnected"
        if self._synchronizer.principal_fingerprint != self._principal_fingerprint:
            return "principal_mismatch"
        if not self._acknowledged:
            return "subscriptions_pending"
        if self._dirty_event.is_set() or not self._ready_flag:
            return "portfolio_dirty"
        if self._last_synchronized_at is None:
            return "never_synchronized"
        if self._monotonic() - self._last_synchronized_at > self._max_staleness:
            return "synchronization_stale"
        return None

    async def run(self) -> None:
        """Run until cancelled, reconnecting after every fail-closed generation."""

        backoff = self._initial_backoff
        while True:
            transport: KalshiPrivateTransport | None = None
            reader: asyncio.Task[None] | None = None
            epoch_id: int | None = None
            try:
                timestamp_ms = max(self._now_ms(), self._last_connection_timestamp_ms + 1)
                self._last_connection_timestamp_ms = timestamp_ms
                instructions = self._lifecycle.begin_connection(timestamp_ms=timestamp_ms)
                epoch_id = instructions.epoch_id
                transport = await self._transport_factory.connect(instructions)
                self._connected = True
                self._terminal_reason = None
                self._acknowledged = False
                self._ready_flag = False
                self._last_synchronized_at = None
                self._dirty_event.clear()
                self._ack_event.clear()
                generation_dirty_version = self._dirty_version
                for command in instructions.subscriptions:
                    await transport.send(command.payload)
                reader = asyncio.create_task(self._read_generation(epoch_id, transport))
                await self._guard(self._ack_event.wait(), reader, timeout=self._ack_timeout)
                self._acknowledged = True
                await self._synchronize_until_clean(
                    reader,
                    require_follow_up=self._dirty_version != generation_dirty_version,
                )
                backoff = self._initial_backoff
                await self._steady_state(reader)
            except asyncio.CancelledError:
                self._retract("cancelled")
                raise
            except KalshiPrivatePrincipalMismatchError:
                self._retract("principal_mismatch")
                raise
            except Exception as exc:  # noqa: BLE001 - every boundary failure reconnects fail closed.
                self._retract(type(exc).__name__)
            finally:
                if reader is not None and not reader.done():
                    reader.cancel()
                    with suppress(asyncio.CancelledError):
                        await reader
                if epoch_id is not None:
                    self._lifecycle.disconnect(epoch_id, self._terminal_reason or "disconnected")
                if transport is not None:
                    with suppress(Exception):
                        await transport.close()
                self._connected = False
                self._acknowledged = False
                self._ready_flag = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._maximum_backoff)

    async def _read_generation(self, epoch_id: int, transport: KalshiPrivateTransport) -> None:
        try:
            while True:
                envelope = await transport.receive()
                outcome = self._lifecycle.receive(epoch_id, envelope)
                if outcome.kind == "stale":
                    raise _GenerationEnded("stale generation frame")
                if outcome.kind == "ack":
                    if self._lifecycle.subscriptions_acknowledged:
                        self._ack_event.set()
                    continue
                if outcome.kind == "frame":
                    self._dirty_version += 1
                    self._ready_flag = False
                    self._dirty_event.set()
                    continue
                raise _GenerationEnded("unknown lifecycle outcome")
        except asyncio.CancelledError:
            raise
        except Exception:
            self._retract("private_reader_failed")
            self._lifecycle.disconnect(epoch_id, "private_reader_failed")
            raise

    async def _synchronize_until_clean(
        self,
        reader: asyncio.Task[None],
        *,
        require_follow_up: bool = False,
    ) -> None:
        while True:
            if self._synchronizer.principal_fingerprint != self._principal_fingerprint:
                raise KalshiPrivatePrincipalMismatchError("principal changed before synchronization")
            observed_dirty_version = self._dirty_version
            self._dirty_event.clear()
            self._ready_flag = False
            evidence = await self._guard(self._synchronizer.synchronize(), reader, timeout=self._sync_timeout)
            if reader.done():
                await self._raise_reader(reader)
            if (
                evidence.principal_fingerprint != self._principal_fingerprint
                or self._synchronizer.principal_fingerprint != self._principal_fingerprint
            ):
                raise KalshiPrivatePrincipalMismatchError("principal changed after synchronization")
            if evidence.status != "complete":
                raise _GenerationEnded("synchronization incomplete")
            self._last_synchronized_at = self._monotonic()
            if self._dirty_version == observed_dirty_version and not require_follow_up:
                self._ready_flag = True
                return
            require_follow_up = False

    async def _steady_state(self, reader: asyncio.Task[None]) -> None:
        while True:
            synchronized_at = self._last_synchronized_at
            if synchronized_at is None:
                raise _GenerationEnded("missing synchronization timestamp")
            ttl_remaining = max(0.0, self._max_staleness - (self._monotonic() - synchronized_at))
            try:
                await self._guard(self._dirty_event.wait(), reader, timeout=ttl_remaining)
                await self._guard(asyncio.sleep(self._debounce), reader)
            except TimeoutError:
                self._ready_flag = False
            elapsed = self._monotonic() - synchronized_at
            if elapsed < self._minimum_interval:
                await self._guard(asyncio.sleep(self._minimum_interval - elapsed), reader)
            await self._synchronize_until_clean(reader)

    async def _guard(
        self,
        awaitable: Awaitable[_T],
        reader: asyncio.Task[None],
        *,
        timeout: float | None = None,
    ) -> _T:
        operation = asyncio.ensure_future(awaitable)
        try:
            done, _ = await asyncio.wait(
                {operation, reader},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError("private runtime operation timed out")
            if reader in done:
                await self._raise_reader(reader)
            return await operation
        finally:
            if not operation.done():
                operation.cancel()
                with suppress(asyncio.CancelledError):
                    await operation

    @staticmethod
    async def _raise_reader(reader: asyncio.Task[None]) -> None:
        await reader
        raise _GenerationEnded("private reader ended unexpectedly")

    def _retract(self, reason: str) -> None:
        self._ready_flag = False
        self._connected = False
        self._acknowledged = False
        self._last_synchronized_at = None
        self._terminal_reason = reason


__all__ = [
    "KalshiPrivatePrincipalMismatchError",
    "KalshiPrivateSyncRunner",
    "KalshiPrivateSynchronizationEvidence",
    "KalshiPrivateSynchronizer",
    "KalshiPrivateTransport",
    "KalshiPrivateTransportFactory",
]
