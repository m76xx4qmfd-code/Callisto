"""Default-off authoritative Kalshi portfolio synchronization worker.

The disabled/paused control gate intentionally precedes every credential-related
environment read, path operation, venue import, client construction, and network
operation.  Private WebSocket frames are invalidation triggers only; all persisted
portfolio values come from the fenced GET-only projection synchronizer.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from sqlalchemy.dialects.postgresql import insert as pg_insert

from models.database import AsyncSessionLocal, WorkerControl, WorkerSnapshot

WORKER_NAME = "kalshi_portfolio_sync"
DEFAULT_CONTROL_POLL_SECONDS = 5
LEASE_TTL = timedelta(seconds=30)
LEASE_RENEW_INTERVAL_SECONDS = 10.0
CORRECTNESS_FRESHNESS_BOUND = timedelta(seconds=30)
_CREDENTIAL_MANIFEST_ENV = "KALSHI_PORTFOLIO_CREDENTIAL_MANIFEST"
_MAX_MANIFEST_BYTES = 16 * 1024
_MAX_PRIVATE_KEY_BYTES = 64 * 1024
_APPROVED_ORIGINS = frozenset(
    {
        "https://external-api.kalshi.com",
        "https://api.elections.kalshi.com",
        "https://external-api.demo.kalshi.co",
        "https://demo-api.kalshi.co",
    }
)


@dataclass(frozen=True)
class _CredentialConfig:
    approved_origin: str
    key_id: str = field(repr=False)
    private_key_pem: str = field(repr=False)
    subaccount: int


@dataclass
class EnabledRuntime:
    client: Any
    runner: Any
    lease_service: Any
    principal_fingerprint: str
    owner_id: str
    fence_token: int


class LeaseLostError(RuntimeError):
    """The worker no longer owns the principal's fenced projection lease."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _read_bounded_text(path: Path, *, maximum_bytes: int, label: str) -> str:
    info = path.stat()
    if not path.is_file():
        raise ValueError(f"{label} must be a regular file")
    if info.st_size <= 0 or info.st_size > maximum_bytes:
        raise ValueError(f"{label} has an invalid size")
    # Open read-only. Deployment mounts the enclosing credential directory :ro.
    return path.read_text(encoding="utf-8")


def _load_credential_config(environment: Mapping[str, str] | None = None) -> _CredentialConfig:
    """Resolve and read the external manifest/key; call only after enable gate."""
    env = os.environ if environment is None else environment
    raw_manifest_path = str(env.get(_CREDENTIAL_MANIFEST_ENV, "")).strip()
    if not raw_manifest_path:
        raise ValueError("Kalshi portfolio credential manifest is not configured")
    manifest_path = Path(raw_manifest_path).expanduser().resolve(strict=True)
    manifest_text = _read_bounded_text(manifest_path, maximum_bytes=_MAX_MANIFEST_BYTES, label="credential manifest")
    try:
        payload = json.loads(manifest_text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("credential manifest is not valid JSON") from exc
    required = {"approved_origin", "key_id", "private_key_file", "subaccount"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("credential manifest must contain exactly the approved fields")

    origin = payload.get("approved_origin")
    key_id = payload.get("key_id")
    key_file_value = payload.get("private_key_file")
    subaccount = payload.get("subaccount")
    if not isinstance(origin, str) or origin.strip().rstrip("/") not in _APPROVED_ORIGINS:
        raise ValueError("credential manifest origin is not approved")
    if not isinstance(key_id, str) or not key_id.strip():
        raise ValueError("credential manifest key_id is required")
    if not isinstance(key_file_value, str) or not key_file_value.strip():
        raise ValueError("credential manifest private_key_file is required")
    if isinstance(subaccount, bool) or not isinstance(subaccount, int) or not 0 <= subaccount <= 63:
        raise ValueError("credential manifest subaccount must be an integer between 0 and 63")

    key_path = Path(key_file_value).expanduser()
    if not key_path.is_absolute():
        key_path = manifest_path.parent / key_path
    key_path = key_path.resolve(strict=True)
    private_key_pem = _read_bounded_text(key_path, maximum_bytes=_MAX_PRIVATE_KEY_BYTES, label="private key file")
    return _CredentialConfig(
        approved_origin=origin.strip().rstrip("/"),
        key_id=key_id.strip(),
        private_key_pem=private_key_pem,
        subaccount=subaccount,
    )


async def _persist_private_invalidation(
    session_factory: Any,
    principal_fingerprint: str,
    reason: str,
) -> None:
    """Retract durable readiness before private-frame recovery may publish again."""
    async with session_factory() as session, session.begin():
        snapshot = await session.get(WorkerSnapshot, WORKER_NAME, with_for_update=True)
        if snapshot is None:
            return
        stats = snapshot.stats_json if isinstance(snapshot.stats_json, dict) else {}
        if stats.get("principal_fingerprint") != principal_fingerprint:
            return
        snapshot.updated_at = _utcnow()
        snapshot.current_activity = "Authoritative portfolio synchronization invalidated"
        snapshot.stats_json = {
            **stats,
            "retry_allowed": False,
            "ready": False,
            "degraded": True,
            "invalidation_reason": reason,
        }
        await session.flush()


async def _compose_enabled_runtime(
    session_factory: Any,
    owner_id: str,
    *,
    now: Callable[[], datetime] = _utcnow,
    environment: Mapping[str, str] | None = None,
) -> EnabledRuntime:
    """Build the production GET-only REST/private-WS runtime after enable."""
    # These imports are deliberately below the control gate. In particular,
    # importing the production transport imports the WebSocket networking stack.
    from services.kalshi_portfolio_projection import (
        KalshiPortfolioLeaseService,
        KalshiPortfolioProjectionSynchronizer,
    )
    from services.venues.kalshi_v2 import KalshiRequestSigner, KalshiV2Client
    from services.venues.kalshi_v2_private_sync import KalshiPrivateSyncRunner
    from services.venues.kalshi_v2_private_ws import KalshiPrivateWSLifecycle
    from services.venues.kalshi_v2_ws_transport import KalshiWebsocketsTransportFactory

    credentials = _load_credential_config(environment)
    client = KalshiV2Client(
        key_id=credentials.key_id,
        private_key_pem=credentials.private_key_pem,
        origin=credentials.approved_origin,
        allow_writes=False,
    )
    lease_service = KalshiPortfolioLeaseService(session_factory)
    principal = client.principal_fingerprint
    fence_token: int | None = None
    try:
        fence_token = await lease_service.acquire(
            principal,
            owner_id,
            now,
            LEASE_TTL,
        )
        signer = KalshiRequestSigner(
            key_id=credentials.key_id,
            private_key_pem=credentials.private_key_pem,
        )
        synchronizer = KalshiPortfolioProjectionSynchronizer(
            session_factory,
            client,
            subaccount=credentials.subaccount,
            expected_lease_owner=owner_id,
            expected_fence_token=fence_token,
            correctness_freshness_bound=CORRECTNESS_FRESHNESS_BOUND,
            now=now,
        )
        lifecycle = KalshiPrivateWSLifecycle(
            signer=signer,
            principal_origin=credentials.approved_origin,
        )
        async def persist_invalidation(reason: str) -> None:
            await _persist_private_invalidation(session_factory, principal, reason)

        runner = KalshiPrivateSyncRunner(
            lifecycle=lifecycle,
            transport_factory=KalshiWebsocketsTransportFactory(),
            synchronizer=cast(Any, synchronizer),
            on_invalidation=persist_invalidation,
        )
        return EnabledRuntime(
            client=client,
            runner=runner,
            lease_service=lease_service,
            principal_fingerprint=principal,
            owner_id=owner_id,
            fence_token=fence_token,
        )
    except BaseException:
        if fence_token is not None:
            with suppress(Exception):
                await lease_service.release(principal, owner_id, fence_token, now)
        with suppress(Exception):
            await client.close()
        raise


async def _cancel_and_await(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _read_control(session_factory: Any) -> dict[str, Any]:
    async with session_factory() as session:
        row = await session.get(WorkerControl, WORKER_NAME)
        if row is None:
            return {
                "worker_name": WORKER_NAME,
                "is_enabled": False,
                "is_paused": False,
                "interval_seconds": DEFAULT_CONTROL_POLL_SECONDS,
                "requested_run_at": None,
                "updated_at": None,
            }
        return {
            "worker_name": row.worker_name,
            "is_enabled": bool(row.is_enabled),
            "is_paused": bool(row.is_paused),
            "interval_seconds": int(row.interval_seconds or DEFAULT_CONTROL_POLL_SECONDS),
            "requested_run_at": row.requested_run_at,
            "updated_at": row.updated_at,
        }


async def _write_snapshot(
    session_factory: Any,
    *,
    running: bool,
    enabled: bool,
    activity: str,
    interval_seconds: int,
    last_run_at: datetime | None = None,
    error_type: str | None = None,
    stats: dict[str, Any] | None = None,
) -> None:
    snapshot = {
        "worker_name": WORKER_NAME,
        "updated_at": _utcnow(),
        "last_run_at": last_run_at,
        "running": running,
        "enabled": enabled,
        "current_activity": activity,
        "interval_seconds": interval_seconds,
        "lag_seconds": None,
        "last_error": error_type,
        "stats_json": {"retry_allowed": False, **(stats or {})},
    }
    async with session_factory() as session, session.begin():
        statement = pg_insert(WorkerSnapshot).values(**snapshot)
        statement = statement.on_conflict_do_update(
            index_elements=[WorkerSnapshot.worker_name],
            set_={key: value for key, value in snapshot.items() if key != "worker_name"},
        )
        await session.execute(statement)


async def _run_enabled_generation(
    runtime: EnabledRuntime,
    *,
    session_factory: Any,
    poll_seconds: float,
    now: Callable[[], datetime] = _utcnow,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    """Run until disable/pause/lease loss; always cancel and await the runner."""
    runner_task = asyncio.create_task(runtime.runner.run(), name="kalshi-private-portfolio-runner")
    reason = "runner_stopped"
    last_renewed = asyncio.get_running_loop().time()
    try:
        while True:
            await sleep(poll_seconds)
            control = await _read_control(session_factory)
            if not bool(control.get("is_enabled", False)):
                reason = "disabled"
                return reason
            if bool(control.get("is_paused", False)):
                reason = "paused"
                return reason
            if runner_task.done():
                await runner_task
                return reason
            monotonic_now = asyncio.get_running_loop().time()
            if monotonic_now - last_renewed >= LEASE_RENEW_INTERVAL_SECONDS:
                renewed = await runtime.lease_service.renew(
                    runtime.principal_fingerprint,
                    runtime.owner_id,
                    runtime.fence_token,
                    now,
                    LEASE_TTL,
                )
                if not renewed:
                    reason = "lease_lost"
                    raise LeaseLostError("principal projection lease was lost")
                last_renewed = monotonic_now
            await _write_snapshot(
                session_factory,
                running=True,
                enabled=True,
                activity=(
                    "Authoritative portfolio synchronized"
                    if bool(getattr(runtime.runner, "ready", False))
                    else "Authoritative portfolio synchronization active"
                ),
                interval_seconds=max(1, int(poll_seconds)),
                stats={
                    "lease_held": True,
                    "ready": bool(getattr(runtime.runner, "ready", False)),
                    "degraded": bool(getattr(runtime.runner, "degraded_reason", None)),
                    "principal_fingerprint": runtime.principal_fingerprint,
                },
            )
    finally:
        await _cancel_and_await(runner_task)


async def start_loop(
    *,
    session_factory: Any = AsyncSessionLocal,
    compose: Callable[..., Awaitable[EnabledRuntime]] = _compose_enabled_runtime,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], datetime] = _utcnow,
    owner_id: str | None = None,
) -> None:
    """Own the default-off worker lifecycle until the host cancels it."""
    owner = owner_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
    while True:
        control = await _read_control(session_factory)
        interval = max(1, int(control.get("interval_seconds") or DEFAULT_CONTROL_POLL_SECONDS))
        enabled = bool(control.get("is_enabled", False))
        paused = bool(control.get("is_paused", False))
        if not enabled or paused:
            await _write_snapshot(
                session_factory,
                running=False,
                enabled=enabled,
                activity="Disabled" if not enabled else "Paused",
                interval_seconds=interval,
                stats={"lease_held": False, "ready": False, "degraded": False},
            )
            await sleep(min(interval, DEFAULT_CONTROL_POLL_SECONDS))
            continue

        runtime: EnabledRuntime | None = None
        last_run_at: datetime | None = None
        try:
            await _write_snapshot(
                session_factory,
                running=False,
                enabled=True,
                activity="Initializing authoritative portfolio synchronization",
                interval_seconds=interval,
                stats={"lease_held": False, "ready": False, "degraded": True},
            )
            runtime = await compose(session_factory, owner, now=now)
            await _write_snapshot(
                session_factory,
                running=True,
                enabled=True,
                activity="Authoritative portfolio synchronization active",
                interval_seconds=interval,
                stats={
                    "lease_held": True,
                    "ready": False,
                    "degraded": True,
                    "principal_fingerprint": runtime.principal_fingerprint,
                },
            )
            reason = await _run_enabled_generation(
                runtime,
                session_factory=session_factory,
                poll_seconds=min(interval, DEFAULT_CONTROL_POLL_SECONDS),
                now=now,
                sleep=sleep,
            )
            last_run_at = now()
            await _write_snapshot(
                session_factory,
                running=False,
                enabled=reason not in {"disabled", "paused"},
                activity={"disabled": "Disabled", "paused": "Paused"}.get(reason, "Synchronization stopped"),
                interval_seconds=interval,
                last_run_at=last_run_at,
                stats={"lease_held": False, "ready": False, "degraded": reason not in {"disabled", "paused"}},
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - errors are redacted to type-only worker health.
            await _write_snapshot(
                session_factory,
                running=False,
                enabled=True,
                activity="Authoritative portfolio synchronization degraded",
                interval_seconds=interval,
                last_run_at=last_run_at,
                error_type=type(exc).__name__,
                stats={"lease_held": False, "ready": False, "degraded": True},
            )
        finally:
            if runtime is not None:
                with suppress(Exception):
                    await runtime.lease_service.release(
                        runtime.principal_fingerprint,
                        runtime.owner_id,
                        runtime.fence_token,
                        now,
                    )
                with suppress(Exception):
                    await runtime.client.close()
        await sleep(min(interval, DEFAULT_CONTROL_POLL_SECONDS))


if __name__ == "__main__":
    asyncio.run(start_loop())
