"""Async transport coordinator for one fail-closed Kalshi WebSocket session.

The coordinator accepts an already-created transport and performs no credential
loading, route mounting, worker startup, or venue mutation. Portfolio coverage
results remain audit evidence only and can never promote health or authorize a
submission retry.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, TypeAlias

from services.venues.kalshi_v2_ws_session import (
    KalshiV2WSFrameSession,
    KalshiWSSessionOutcome,
    KalshiWSSessionRecoveryRequested,
    KalshiWSSessionTerminated,
)


class KalshiWSCoordinatorTransport(Protocol):
    async def send(self, payload: dict[str, object]) -> None: ...

    async def receive(self) -> Mapping[str, object]: ...

    async def close(self) -> None: ...


class KalshiWSCoordinatorCoverage(Protocol):
    async def sweep(self, coverage_id: str, observed_at: datetime) -> object: ...


@dataclass(frozen=True)
class KalshiWSCoordinatorStarted:
    generation: int
    epoch_id: int
    audit_coverage_evidence: object


@dataclass(frozen=True)
class KalshiWSCoordinatorNoOp:
    generation: int
    reason: Literal["stale_generation"]


@dataclass(frozen=True)
class KalshiWSCoordinatorTerminated:
    generation: int
    epoch_id: int
    reason: str


KalshiWSCoordinatorOutcome: TypeAlias = KalshiWSSessionOutcome | KalshiWSCoordinatorNoOp | KalshiWSCoordinatorTerminated


@dataclass
class _ActiveTransport:
    generation: int
    epoch_id: int
    transport: KalshiWSCoordinatorTransport
    receive_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    receive_task: asyncio.Task[KalshiWSCoordinatorOutcome] | None = field(default=None, repr=False)
    recovery_send_task: asyncio.Task[None] | None = None


class KalshiV2WSCoordinator:
    """Serialize transport generations around a decoded-frame session."""

    def __init__(self, session: KalshiV2WSFrameSession, coverage: KalshiWSCoordinatorCoverage) -> None:
        self._session = session
        self._coverage = coverage
        self._transition_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._generation_counter = 0
        self._active: _ActiveTransport | None = None
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    @property
    def session(self) -> KalshiV2WSFrameSession:
        return self._session

    @property
    def active_generation(self) -> int | None:
        return self._active.generation if self._active is not None else None

    @property
    def private_stream_healthy(self) -> Literal[False]:
        return False

    @property
    def connection_healthy(self) -> Literal[False]:
        return False

    @property
    def retry_allowed(self) -> Literal[False]:
        return False

    async def start(
        self,
        transport: KalshiWSCoordinatorTransport,
        *,
        timestamp_ms: int,
        coverage_id: str,
        observed_at: datetime,
    ) -> KalshiWSCoordinatorStarted:
        candidate_owned = True
        try:
            async with self._transition_lock:
                old_transport = await self._detach_current("reconnected")
                if old_transport is not None:
                    await self._close_transport(old_transport)
                audit_evidence = await self._coverage.sweep(coverage_id, observed_at)
                async with self._state_lock:
                    instructions = self._session.begin(timestamp_ms=timestamp_ms)
                try:
                    async with self._send_lock:
                        for command in instructions.subscriptions:
                            await transport.send(command.payload)
                except BaseException:
                    await self._terminate_epoch(instructions.epoch_id, "connection_start_failed")
                    raise

                async with self._state_lock:
                    self._generation_counter += 1
                    active = _ActiveTransport(
                        generation=self._generation_counter,
                        epoch_id=instructions.epoch_id,
                        transport=transport,
                    )
                    self._active = active
                candidate_owned = False
                return KalshiWSCoordinatorStarted(
                    generation=active.generation,
                    epoch_id=active.epoch_id,
                    audit_coverage_evidence=audit_evidence,
                )
        finally:
            if candidate_owned:
                await self._close_transport(transport)

    async def receive_once(self, generation: int) -> KalshiWSCoordinatorOutcome:
        candidate = self._active
        if candidate is None or candidate.generation != generation:
            return KalshiWSCoordinatorNoOp(generation=generation, reason="stale_generation")
        current_task = asyncio.current_task()
        try:
            async with candidate.receive_lock:
                async with self._state_lock:
                    if self._active is not candidate:
                        return KalshiWSCoordinatorNoOp(generation=generation, reason="stale_generation")
                    candidate.receive_task = current_task
                try:
                    payload = await candidate.transport.receive()
                except asyncio.CancelledError:
                    async with self._state_lock:
                        stale = self._active is not candidate
                    if stale:
                        return KalshiWSCoordinatorNoOp(generation=generation, reason="stale_generation")
                    raise
                finally:
                    async with self._state_lock:
                        if candidate.receive_task is current_task:
                            candidate.receive_task = None
                return await self._route_payload(candidate, payload)
        except asyncio.CancelledError:
            await self._abort_generation(candidate, "receive_cancelled")
            raise
        except Exception:  # noqa: BLE001 - transport boundary must fail closed.
            return await self._abort_generation(candidate, "transport_receive_failed")

    async def disconnect(
        self,
        generation: int,
        *,
        coverage_id: str,
        observed_at: datetime,
    ) -> object | KalshiWSCoordinatorNoOp:
        candidate = self._active
        if candidate is None or candidate.generation != generation:
            return KalshiWSCoordinatorNoOp(generation=generation, reason="stale_generation")
        try:
            async with self._transition_lock:
                async with self._state_lock:
                    if self._active is not candidate:
                        return KalshiWSCoordinatorNoOp(generation=generation, reason="stale_generation")
                    self._terminate_active_state(candidate, "disconnected")
                close_cancelled = False
                try:
                    await self._close_transport(candidate.transport)
                except asyncio.CancelledError:
                    close_cancelled = True
                audit_evidence = await self._coverage.sweep(coverage_id, observed_at)
                if close_cancelled:
                    raise asyncio.CancelledError
                return audit_evidence
        except asyncio.CancelledError:
            await self._abort_generation(candidate, "disconnect_cancelled")
            raise

    async def _route_payload(
        self,
        active: _ActiveTransport,
        payload: Mapping[str, object],
    ) -> KalshiWSCoordinatorOutcome:
        send_task: asyncio.Task[None] | None = None
        recovery_outcome: KalshiWSSessionRecoveryRequested | None = None
        async with self._state_lock:
            if self._active is not active:
                return KalshiWSCoordinatorNoOp(generation=active.generation, reason="stale_generation")
            try:
                outcome = self._session.receive(active.epoch_id, payload)
            except Exception:  # noqa: BLE001 - decoded frames are untrusted input.
                self._terminate_active_state(active, "malformed_decoded_frame")
                terminal_outcome = KalshiWSCoordinatorTerminated(
                    generation=active.generation,
                    epoch_id=active.epoch_id,
                    reason="malformed_decoded_frame",
                )
            else:
                if isinstance(outcome, KalshiWSSessionTerminated):
                    self._terminate_active_state(active, outcome.reason)
                    terminal_outcome = KalshiWSCoordinatorTerminated(
                        generation=active.generation,
                        epoch_id=active.epoch_id,
                        reason=outcome.reason,
                    )
                elif isinstance(outcome, KalshiWSSessionRecoveryRequested):
                    recovery_outcome = outcome
                    send_task = asyncio.create_task(self._send_recovery(active, outcome.command))
                    active.recovery_send_task = send_task
                    terminal_outcome = None
                else:
                    return outcome

        if terminal_outcome is not None:
            await self._close_transport(active.transport)
            return terminal_outcome
        if send_task is None or recovery_outcome is None:
            raise RuntimeError("recovery outcome did not create a send task")

        try:
            await send_task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                await self._abort_generation(active, "recovery_send_cancelled")
                raise
            return KalshiWSCoordinatorNoOp(generation=active.generation, reason="stale_generation")
        except Exception:  # noqa: BLE001 - transport boundary must fail closed.
            return await self._abort_generation(active, "transport_send_failed")
        finally:
            async with self._state_lock:
                if active.recovery_send_task is send_task:
                    active.recovery_send_task = None

        async with self._state_lock:
            if self._active is not active:
                return KalshiWSCoordinatorNoOp(generation=active.generation, reason="stale_generation")
        return recovery_outcome

    async def _send_recovery(self, active: _ActiveTransport, command: dict[str, object]) -> None:
        async with self._send_lock:
            await active.transport.send(command)

    async def _abort_generation(self, active: _ActiveTransport, reason: str) -> KalshiWSCoordinatorTerminated:
        async with self._state_lock:
            should_close = self._active is active
            if should_close:
                self._terminate_active_state(active, reason)
        if should_close:
            await self._close_transport(active.transport)
        return KalshiWSCoordinatorTerminated(
            generation=active.generation,
            epoch_id=active.epoch_id,
            reason=reason,
        )

    async def _detach_current(self, reason: str) -> KalshiWSCoordinatorTransport | None:
        async with self._state_lock:
            active = self._active
            if active is None:
                return None
            self._terminate_active_state(active, reason)
            return active.transport

    async def _terminate_epoch(self, epoch_id: int, reason: str) -> None:
        async with self._state_lock:
            if self._session.lifecycle.current_epoch_id == epoch_id:
                self._session.lifecycle.terminate_epoch(epoch_id, reason)

    def _terminate_active_state(self, active: _ActiveTransport, reason: str) -> None:
        if self._active is active:
            current_task = asyncio.current_task()
            if (
                active.receive_task is not None
                and active.receive_task is not current_task
                and not active.receive_task.done()
            ):
                active.receive_task.cancel()
            if active.recovery_send_task is not None and not active.recovery_send_task.done():
                active.recovery_send_task.cancel()
            if self._session.lifecycle.current_epoch_id == active.epoch_id:
                self._session.lifecycle.terminate_epoch(active.epoch_id, reason)
            self._active = None

    async def _close_transport(self, transport: KalshiWSCoordinatorTransport) -> None:
        close_task = asyncio.create_task(transport.close())
        self._cleanup_tasks.add(close_task)
        close_task.add_done_callback(self._cleanup_tasks.discard)
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            await asyncio.shield(close_task)
            raise


__all__ = [
    "KalshiV2WSCoordinator",
    "KalshiWSCoordinatorCoverage",
    "KalshiWSCoordinatorNoOp",
    "KalshiWSCoordinatorOutcome",
    "KalshiWSCoordinatorStarted",
    "KalshiWSCoordinatorTerminated",
    "KalshiWSCoordinatorTransport",
]
