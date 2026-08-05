from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api import routes_workers
from models.database import Base, KalshiPortfolioProjectionLease
from services import worker_state
from services.kalshi_portfolio_projection import (
    KalshiPortfolioLeaseService,
    KalshiPortfolioProjectionLeaseConflictError,
)
from tests.postgres_test_db import build_postgres_session_factory
from workers import kalshi_portfolio_sync_worker as worker


class _NoRowResult:
    def scalar_one_or_none(self):
        return None


class _NoRowSession:
    async def execute(self, _statement):
        return _NoRowResult()


@pytest.mark.asyncio
async def test_worker_state_default_enabled_is_explicit_and_preserves_existing_defaults():
    session = _NoRowSession()
    existing_default = await worker_state.read_worker_control(session, "events")
    safe_default = await worker_state.read_worker_control(session, worker.WORKER_NAME, default_enabled=False)
    assert existing_default["is_enabled"] is True
    assert safe_default["is_enabled"] is False


@pytest.mark.asyncio
async def test_missing_or_disabled_worker_never_touches_credentials_paths_imports_or_network(monkeypatch):
    snapshots: list[dict] = []
    transport_module_before = sys.modules.get("services.venues.kalshi_v2_ws_transport")
    entered_sleep = asyncio.Event()
    release_sleep = asyncio.Event()

    async def disabled_control(_session_factory):
        return {"is_enabled": False, "is_paused": False, "interval_seconds": 5}

    async def snapshot(_session_factory, **kwargs):
        snapshots.append(kwargs)

    async def guarded_sleep(_seconds):
        entered_sleep.set()
        await release_sleep.wait()

    compose = AsyncMock(side_effect=AssertionError("composition before enable"))
    credential_loader = Mock(side_effect=AssertionError("credential environment/path access before enable"))
    monkeypatch.setattr(worker, "_read_control", disabled_control)
    monkeypatch.setattr(worker, "_write_snapshot", snapshot)
    monkeypatch.setattr(worker, "_load_credential_config", credential_loader)

    task = asyncio.create_task(
        worker.start_loop(session_factory=object(), compose=compose, sleep=guarded_sleep, owner_id="test-owner")
    )
    await asyncio.wait_for(entered_sleep.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    compose.assert_not_awaited()
    credential_loader.assert_not_called()
    assert snapshots and snapshots[0]["running"] is False
    assert snapshots[0]["enabled"] is False
    assert snapshots[0]["activity"] == "Disabled"
    assert sys.modules.get("services.venues.kalshi_v2_ws_transport") is transport_module_before


@pytest.mark.asyncio
async def test_run_once_rejects_default_disabled_worker_without_queueing(monkeypatch):
    request = AsyncMock()
    monkeypatch.setattr(
        routes_workers,
        "_worker_detail",
        AsyncMock(return_value={"control": {"is_enabled": False, "is_paused": False}}),
    )
    monkeypatch.setattr(routes_workers, "request_worker_run", request)

    with pytest.raises(HTTPException) as excinfo:
        await routes_workers.run_worker_once(worker.WORKER_NAME, object())

    assert excinfo.value.status_code == 409
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_enabled_production_composition_is_get_only_and_uses_manifest_scope(tmp_path, monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    key_path = tmp_path / "portfolio.pem"
    key_path.write_text(pem, encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "approved_origin": "https://demo-api.kalshi.co",
                "key_id": "generated-test-key",
                "private_key_file": "portfolio.pem",
                "subaccount": 7,
            }
        ),
        encoding="utf-8",
    )

    import services.kalshi_portfolio_projection as projection_module
    import services.venues.kalshi_v2 as venue_module
    import services.venues.kalshi_v2_private_sync as sync_module
    import services.venues.kalshi_v2_private_ws as lifecycle_module
    import services.venues.kalshi_v2_ws_transport as transport_module

    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.principal_fingerprint = "a" * 64

        async def close(self):
            captured["closed"] = True

    class FakeLease:
        def __init__(self, sf):
            captured["lease_sf"] = sf

        async def acquire(self, principal, owner, now, ttl):
            captured["acquire"] = (principal, owner, ttl)
            return 13

        async def release(self, *_args):
            captured["released"] = True
            return True

    class FakeSigner:
        def __init__(self, **kwargs):
            captured["signer"] = kwargs

    class FakeSynchronizer:
        def __init__(self, sf, client, **kwargs):
            captured["synchronizer"] = kwargs
            self.principal_fingerprint = client.principal_fingerprint

    class FakeLifecycle:
        def __init__(self, **kwargs):
            captured["lifecycle"] = kwargs
            self.principal_fingerprint = "a" * 64

    class FakeTransportFactory:
        def __init__(self):
            captured["transport"] = True

    class FakeRunner:
        def __init__(self, **kwargs):
            captured["runner"] = kwargs

    monkeypatch.setattr(venue_module, "KalshiV2Client", FakeClient)
    monkeypatch.setattr(venue_module, "KalshiRequestSigner", FakeSigner)
    monkeypatch.setattr(projection_module, "KalshiPortfolioLeaseService", FakeLease)
    monkeypatch.setattr(projection_module, "KalshiPortfolioProjectionSynchronizer", FakeSynchronizer)
    monkeypatch.setattr(lifecycle_module, "KalshiPrivateWSLifecycle", FakeLifecycle)
    monkeypatch.setattr(transport_module, "KalshiWebsocketsTransportFactory", FakeTransportFactory)
    monkeypatch.setattr(sync_module, "KalshiPrivateSyncRunner", FakeRunner)

    runtime = await worker._compose_enabled_runtime(
        "session-factory",
        "owner",
        environment={worker._CREDENTIAL_MANIFEST_ENV: str(manifest_path)},
    )

    client_args = captured["client"]
    assert isinstance(client_args, dict)
    assert client_args["allow_writes"] is False
    assert client_args["origin"] == "https://demo-api.kalshi.co"
    assert captured["synchronizer"]["subaccount"] == 7
    assert runtime.fence_token == 13
    assert captured["transport"] is True
    assert "generated-test-key" not in repr(runtime)
    assert pem not in repr(runtime)


@pytest.mark.asyncio
async def test_lease_loss_cancels_and_awaits_private_runner(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class FakeRunner:
        ready = False
        degraded_reason = "starting"

        async def run(self):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    lease = SimpleNamespace(renew=AsyncMock(return_value=False))
    runtime = worker.EnabledRuntime(
        client=SimpleNamespace(),
        runner=FakeRunner(),
        lease_service=lease,
        principal_fingerprint="a" * 64,
        owner_id="owner",
        fence_token=4,
    )

    monkeypatch.setattr(worker, "LEASE_RENEW_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(
        worker,
        "_read_control",
        AsyncMock(return_value={"is_enabled": True, "is_paused": False}),
    )
    monkeypatch.setattr(worker, "_write_snapshot", AsyncMock())

    async def immediate_sleep(_seconds):
        await started.wait()

    with pytest.raises(worker.LeaseLostError):
        await worker._run_enabled_generation(
            runtime,
            session_factory=object(),
            poll_seconds=1,
            sleep=immediate_sleep,
        )

    lease.renew.assert_awaited_once()
    assert cancelled.is_set()


@pytest.mark.db
@pytest.mark.asyncio
async def test_lease_renewal_preserves_fence_and_release_prevents_stale_renewal():
    engine, session_factory = await build_postgres_session_factory(Base, "kalshi_worker_lease_renew")
    try:
        lease = KalshiPortfolioLeaseService(session_factory)
        principal = "b" * 64
        now = datetime(2026, 8, 4, 12, tzinfo=timezone.utc)
        token = await lease.acquire(principal, "owner-a", now, timedelta(seconds=30))
        assert await lease.renew(principal, "owner-a", token, now + timedelta(seconds=5), timedelta(seconds=30)) is True
        assert (
            await lease.renew(principal, "owner-b", token, now + timedelta(seconds=6), timedelta(seconds=30)) is False
        )
        assert await lease.release(principal, "owner-a", token, now + timedelta(seconds=7)) is True
        assert (
            await lease.renew(principal, "owner-a", token, now + timedelta(seconds=8), timedelta(seconds=30)) is False
        )
        next_token = await lease.acquire(principal, "owner-b", now + timedelta(seconds=8), timedelta(seconds=30))
        assert next_token == token + 1
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_concurrent_first_lease_acquisition_returns_domain_conflict():
    engine, session_factory = await build_postgres_session_factory(Base, "kalshi_worker_lease_first_race")
    try:
        lease = KalshiPortfolioLeaseService(session_factory)
        now = datetime(2026, 8, 4, 12, tzinfo=timezone.utc)
        for index in range(8):
            principal = f"{index + 1:064x}"
            results = await asyncio.gather(
                lease.acquire(principal, "owner-a", now, timedelta(seconds=30)),
                lease.acquire(principal, "owner-b", now, timedelta(seconds=30)),
                return_exceptions=True,
            )
            assert sum(isinstance(result, int) for result in results) == 1
            conflicts = [result for result in results if isinstance(result, Exception)]
            assert len(conflicts) == 1
            assert isinstance(conflicts[0], KalshiPortfolioProjectionLeaseConflictError)

        ticks = iter((now, now + timedelta(minutes=2)))
        delayed_principal = "f" * 64
        assert await lease.acquire(delayed_principal, "owner", lambda: next(ticks), timedelta(seconds=30)) == 1
        async with session_factory() as session:
            row = await session.get(KalshiPortfolioProjectionLease, delayed_principal)
        assert row is not None
        assert row.expires_at.replace(tzinfo=timezone.utc) == now + timedelta(minutes=2, seconds=30)
    finally:
        await engine.dispose()


def test_kalshi_portfolio_plane_is_isolated_and_reachable_in_all_topologies():
    gui_text = (BACKEND_ROOT.parent / "gui.py").read_text(encoding="utf-8")
    compose_text = (BACKEND_ROOT.parent / "docker-compose.yml").read_text(encoding="utf-8")
    assert '("KALSHI PORTFOLIO", "kalshi_portfolio")' in gui_text
    assert '"kalshi_portfolio_sync": "kalshi_portfolio"' in gui_text
    assert '"kalshi_portfolio": "workers.kalshi_portfolio_host"' in gui_text
    assert 'workers.kalshi_portfolio_host"]' in compose_text
    assert 'workers.host", "kalshi_portfolio"' not in compose_text
    assert "KALSHI_PORTFOLIO_CREDENTIAL_MANIFEST" in compose_text
    assert ":/run/secrets/kalshi:ro" in compose_text

    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(BACKEND_ROOT)
    probe_code = """import sys
import workers.kalshi_portfolio_host
forbidden = (
    "services.intent_runtime",
    "services.event_bus",
    "services.market_runtime",
    "services.position_monitor",
    "services.traders_copy_trade_signal_service",
    "services.ws_feeds",
)
found = [name for name in forbidden if name in sys.modules]
raise SystemExit(",".join(found) if found else 0)
"""
    import_probe = subprocess.run(
        [sys.executable, "-c", probe_code],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert import_probe.returncode == 0, import_probe.stderr
