from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from api.routes_kalshi_portfolio import get_projection_reader, router
from models.database import (
    Base,
    KalshiPortfolioCoverageCheckpoint,
    KalshiPortfolioCoverageFillMembership,
    KalshiPortfolioCoverageOrderMembership,
    KalshiPortfolioProjectionAttempt,
    KalshiPortfolioProjectionHead,
    WorkerControl,
    WorkerSnapshot,
)
from services.kalshi_portfolio_coverage import KalshiPortfolioCoverageService
from services.kalshi_portfolio_projection import (
    KalshiPortfolioLeaseService,
    KalshiPortfolioPrincipalNotFoundError,
    KalshiPortfolioProjectionFencingError,
    KalshiPortfolioProjectionReader,
    KalshiPortfolioProjectionSynchronizer,
)
from services.venues.kalshi_v2 import (
    KalshiBalanceSnapshot,
    KalshiEventPosition,
    KalshiFillsPage,
    KalshiOrdersPage,
    KalshiPositionsPage,
    KalshiSettlementsPage,
    KalshiSubaccountBalance,
)
from tests.postgres_test_db import build_postgres_session_factory
from tests.test_kalshi_portfolio_coverage import CUTOFF, FakeCoverageClient, _fill, _order

UTC = timezone.utc
NOW = datetime(2026, 8, 4, 12, tzinfo=UTC)


class ProjectionClient(FakeCoverageClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.position_pages = {
            None: KalshiPositionsPage(
                market_positions=(),
                event_positions=(
                    KalshiEventPosition(
                        event_ticker="EV",
                        total_cost=Decimal("12345678901234567890.123456"),
                        total_cost_shares=Decimal("1.20"),
                        event_exposure=Decimal("0.000001"),
                        realized_pnl=Decimal("-0.000001"),
                        fees_paid=Decimal("0.010000"),
                    ),
                ),
                cursor="p2",
            ),
            "p2": KalshiPositionsPage(market_positions=(), event_positions=(), cursor=""),
        }
        self.balance = KalshiBalanceSnapshot(
            balance_cents=123,
            balance_dollars=Decimal("1.230000"),
            portfolio_value_cents=456,
            updated_ts=9007199254740993,
            balance_breakdown=(KalshiSubaccountBalance(exchange_index=0, balance=Decimal("1.230000")),),
        )
        self.settlement_pages = {None: KalshiSettlementsPage(settlements=(), cursor="")}
        self.position_calls = []

    async def get_positions(self, **kwargs):
        self.position_calls.append(kwargs)
        return self.position_pages[kwargs.get("cursor")]

    async def get_balance(self):
        return self.balance

    async def get_settlements(self, **kwargs):
        return self.settlement_pages[kwargs.get("cursor")]


@pytest.mark.db
@pytest.mark.asyncio
async def test_projection_exact_membership_fixed_point_empty_and_principal_isolation() -> None:
    engine, sf = await build_postgres_session_factory(Base, "kalshi_projection_exact")
    try:
        principal = "a" * 64
        client = ProjectionClient(
            principal_fingerprint=principal,
            current_orders={None: KalshiOrdersPage(orders=(_order(),), cursor="")},
            current_fills={None: KalshiFillsPage(fills=(_fill(),), cursor="")},
        )
        lease = KalshiPortfolioLeaseService(sf)
        token = await lease.acquire(principal, "worker-a", NOW, timedelta(minutes=1))
        sync = KalshiPortfolioProjectionSynchronizer(
            sf,
            client,
            subaccount=7,
            expected_lease_owner="worker-a",
            expected_fence_token=token,
            correctness_freshness_bound=timedelta(seconds=5),
            now=lambda: NOW,
        )
        result = await sync.synchronize("projection-1")
        assert result.status == "complete"
        assert all(call["subaccount"] == 7 for call in client.position_calls)

        async with sf() as session:
            orders = (await session.execute(select(KalshiPortfolioCoverageOrderMembership))).scalars().all()
            fills = (await session.execute(select(KalshiPortfolioCoverageFillMembership))).scalars().all()
            attempt = await session.get(
                KalshiPortfolioProjectionAttempt, {"principal_fingerprint": principal, "projection_id": "projection-1"}
            )
        assert [(row.coverage_id, row.order_id) for row in orders] == [("projection-1", "order-1")]
        assert [(row.coverage_id, row.fill_id) for row in fills] == [("projection-1", "fill-1")]
        assert attempt.positions_json["event_positions"][0]["total_cost"] == "12345678901234567890.123456"
        assert attempt.balance_json["balance_cents"] == "123"
        assert attempt.balance_json["balance_dollars"] == "1.230000"
        assert attempt.balance_json["updated_ts"] == "9007199254740993"

        other = "b" * 64
        other_token = await lease.acquire(other, "worker-b", NOW, timedelta(minutes=1))
        empty = ProjectionClient(principal_fingerprint=other)
        await KalshiPortfolioProjectionSynchronizer(
            sf,
            empty,
            subaccount=0,
            expected_lease_owner="worker-b",
            expected_fence_token=other_token,
            correctness_freshness_bound=timedelta(seconds=5),
            now=lambda: NOW,
        ).synchronize("empty-1")
        reader = KalshiPortfolioProjectionReader(sf, stale_after=timedelta(minutes=5), now=lambda: NOW)
        async with sf() as session, session.begin():
            session.add(
                WorkerControl(
                    worker_name="kalshi_portfolio_sync",
                    is_enabled=True,
                    is_paused=False,
                    interval_seconds=5,
                    requested_run_at=None,
                    updated_at=NOW,
                )
            )
            session.add(
                WorkerSnapshot(
                    worker_name="kalshi_portfolio_sync",
                    updated_at=NOW,
                    running=True,
                    enabled=True,
                    current_activity="Authoritative portfolio synchronized",
                    interval_seconds=5,
                    stats_json={
                        "ready": True,
                        "degraded": False,
                        "retry_allowed": False,
                        "principal_fingerprint": principal,
                    },
                )
            )
        principal_snapshot = await reader.read(principal)
        assert principal_snapshot["component_skew_seconds"] == "0.000000"
        sync_runtime = principal_snapshot["sync_runtime"]
        assert isinstance(sync_runtime, dict)
        assert sync_runtime["ready"] is True
        assert set(principal_snapshot["components"]) == {"coverage", "positions", "balance", "settlements"}
        assert principal_snapshot["scope"] == {
            "orders_and_fills": {"kind": "all_subaccounts"},
            "balance": {"kind": "account_aggregate"},
            "positions": {"kind": "subaccount", "subaccount_numbers": [7]},
            "settlements": {"kind": "subaccount", "subaccount_numbers": [7]},
        }
        async with sf() as session, session.begin():
            worker_snapshot = await session.get(WorkerSnapshot, "kalshi_portfolio_sync")
            assert worker_snapshot is not None
            worker_snapshot.updated_at = NOW + timedelta(minutes=10)
        stale_snapshot = await KalshiPortfolioProjectionReader(
            sf, stale_after=timedelta(minutes=5), now=lambda: NOW + timedelta(minutes=10)
        ).read(principal)
        assert stale_snapshot["readiness"] == "stale"
        empty_snapshot = await reader.read(other)
        assert empty_snapshot["readiness"] == "degraded"
        assert empty_snapshot["reason"] == "private_sync_runtime_principal_mismatch"
        assert empty_snapshot["orders"] == [] and empty_snapshot["fills"] == []

        no_healthy_principal = "c" * 64
        no_healthy_token = await lease.acquire(no_healthy_principal, "worker-c", NOW, timedelta(minutes=1))
        no_healthy_client = ProjectionClient(
            principal_fingerprint=no_healthy_principal,
            current_orders={None: KalshiOrdersPage(orders=(_order(order_id="unknown-first"),), cursor="")},
        )
        no_healthy_client.get_balance = AsyncMock(side_effect=RuntimeError("read failed"))
        await KalshiPortfolioProjectionSynchronizer(
            sf,
            no_healthy_client,
            subaccount=0,
            expected_lease_owner="worker-c",
            expected_fence_token=no_healthy_token,
            correctness_freshness_bound=timedelta(seconds=5),
            now=lambda: NOW,
        ).synchronize("incomplete-first")
        no_healthy_snapshot = await reader.read(no_healthy_principal)
        assert no_healthy_snapshot["projection_id"] is None
        first_unknown_activity = no_healthy_snapshot["unknown_activity"]
        assert isinstance(first_unknown_activity, dict)
        assert first_unknown_activity["order_ids"] == ["unknown-first"]
        with pytest.raises(KalshiPortfolioPrincipalNotFoundError):
            await reader.read("e" * 64)
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_reader_surfaces_unprojected_coverage_as_degraded_safety_evidence() -> None:
    engine, sf = await build_postgres_session_factory(Base, "kalshi_projection_orphan_coverage")
    try:
        async with sf() as session, session.begin():
            session.add(
                WorkerControl(
                    worker_name="kalshi_portfolio_sync",
                    is_enabled=True,
                    is_paused=False,
                    interval_seconds=5,
                    requested_run_at=None,
                    updated_at=NOW,
                )
            )
        lease = KalshiPortfolioLeaseService(sf)
        reader = KalshiPortfolioProjectionReader(sf, stale_after=timedelta(minutes=5), now=lambda: NOW)

        first_principal = "8" * 64
        first_token = await lease.acquire(first_principal, "first-worker", NOW, timedelta(minutes=1))
        first_client = ProjectionClient(
            principal_fingerprint=first_principal,
            current_orders={None: KalshiOrdersPage(orders=(_order(order_id="first-orphan"),), cursor="")},
        )
        await KalshiPortfolioCoverageService(
            sf,
            first_client,
            expected_lease_owner="first-worker",
            expected_fence_token=first_token,
            now=lambda: NOW,
        ).sweep("first-orphan-coverage", NOW)

        first_snapshot = await reader.read(first_principal)
        assert first_snapshot["readiness"] == "degraded"
        assert first_snapshot["reason"] == "coverage_pending_projection"
        assert first_snapshot["projection_id"] is None
        first_unknown = first_snapshot["unknown_activity"]
        assert isinstance(first_unknown, dict)
        assert first_unknown["order_ids"] == ["first-orphan"]

        healthy_principal = "9" * 64
        healthy_token = await lease.acquire(healthy_principal, "healthy-worker", NOW, timedelta(minutes=1))
        healthy_client = ProjectionClient(principal_fingerprint=healthy_principal)
        await KalshiPortfolioProjectionSynchronizer(
            sf,
            healthy_client,
            subaccount=0,
            expected_lease_owner="healthy-worker",
            expected_fence_token=healthy_token,
            correctness_freshness_bound=timedelta(seconds=5),
            now=lambda: NOW,
        ).synchronize("healthy")
        healthy_client.pages["orders"] = {
            None: KalshiOrdersPage(orders=(_order(order_id="later-orphan"),), cursor="")
        }
        healthy_client.cutoffs = [CUTOFF, CUTOFF]
        await KalshiPortfolioCoverageService(
            sf,
            healthy_client,
            expected_lease_owner="healthy-worker",
            expected_fence_token=healthy_token,
            now=lambda: NOW + timedelta(seconds=1),
        ).sweep("later-orphan-coverage", NOW + timedelta(seconds=1))

        later_snapshot = await KalshiPortfolioProjectionReader(
            sf, stale_after=timedelta(minutes=5), now=lambda: NOW + timedelta(seconds=1)
        ).read(healthy_principal)
        assert later_snapshot["readiness"] == "degraded"
        assert later_snapshot["reason"] == "coverage_pending_projection"
        assert later_snapshot["projection_id"] == "healthy"
        later_unknown = later_snapshot["unknown_activity"]
        assert isinstance(later_unknown, dict)
        assert later_unknown["order_ids"] == ["later-orphan"]
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_degraded_preserves_healthy_and_fencing_conflict() -> None:
    engine, sf = await build_postgres_session_factory(Base, "kalshi_projection_degraded")
    try:
        principal = "d" * 64
        async with sf() as session, session.begin():
            session.add(
                WorkerControl(
                    worker_name="kalshi_portfolio_sync",
                    is_enabled=True,
                    is_paused=False,
                    interval_seconds=5,
                    requested_run_at=None,
                    updated_at=NOW,
                )
            )
        lease = KalshiPortfolioLeaseService(sf)
        token = await lease.acquire(principal, "worker", NOW, timedelta(minutes=1))
        client = ProjectionClient(principal_fingerprint=principal)
        clock = {"now": NOW}
        sync = KalshiPortfolioProjectionSynchronizer(
            sf,
            client,
            subaccount=0,
            expected_lease_owner="worker",
            expected_fence_token=token,
            correctness_freshness_bound=timedelta(seconds=5),
            now=lambda: clock["now"],
        )
        await sync.synchronize("healthy")
        calls_before_replay = list(client.calls)
        replayed = await sync.synchronize("healthy")
        assert replayed.status == "complete"
        assert client.calls == calls_before_replay
        client.cutoffs = [CUTOFF, CUTOFF]
        client.pages["orders"] = {None: KalshiOrdersPage(orders=(_order(order_id="unknown-latest"),), cursor="")}
        client.get_balance = AsyncMock(side_effect=RuntimeError("read failed"))
        clock["now"] = NOW + timedelta(seconds=1)
        failed = await sync.synchronize("failed")
        assert failed.status == "incomplete"
        async with sf() as session:
            head = await session.get(KalshiPortfolioProjectionHead, principal)
        assert head.latest_projection_id == "failed"
        assert head.healthy_projection_id == "healthy"
        snapshot = await KalshiPortfolioProjectionReader(sf, stale_after=timedelta(minutes=5), now=lambda: NOW).read(
            principal
        )
        assert snapshot["readiness"] == "degraded"
        assert snapshot["projection_id"] == "healthy"
        assert "balance" in snapshot["gaps"]
        unknown_activity = snapshot["unknown_activity"]
        assert isinstance(unknown_activity, dict)
        assert unknown_activity["order_ids"] == ["unknown-latest"]

        client.cutoffs = []
        clock["now"] = NOW + timedelta(seconds=2)
        coverage_failed = await sync.synchronize("coverage-failed")
        assert coverage_failed.status == "incomplete"
        async with sf() as session:
            failed_attempt = await session.get(
                KalshiPortfolioProjectionAttempt,
                {"principal_fingerprint": principal, "projection_id": "coverage-failed"},
            )
        assert failed_attempt.status == "failed"
        assert failed_attempt.coverage_id is None
        after_coverage_failure = await KalshiPortfolioProjectionReader(
            sf, stale_after=timedelta(minutes=5), now=lambda: NOW
        ).read(principal)
        latest_unknown_activity = after_coverage_failure["unknown_activity"]
        assert isinstance(latest_unknown_activity, dict)
        assert latest_unknown_activity["order_ids"] == ["unknown-latest"]

        with pytest.raises(DBAPIError, match="healthy projection must reference a complete attempt"):
            async with sf() as session, session.begin():
                await session.execute(
                    update(KalshiPortfolioProjectionHead)
                    .where(KalshiPortfolioProjectionHead.principal_fingerprint == principal)
                    .values(healthy_projection_id="coverage-failed")
                )

        stale = await KalshiPortfolioProjectionReader(
            sf, stale_after=timedelta(minutes=5), now=lambda: NOW + timedelta(minutes=10)
        ).read(principal)
        assert stale["readiness"] == "degraded"  # latest failure truthfully outranks age.

        await lease.acquire(principal, "replacement", NOW + timedelta(seconds=1), timedelta(minutes=1), force=True)
        client.cutoffs = [CUTOFF, CUTOFF]
        client.get_balance = ProjectionClient.get_balance.__get__(client, ProjectionClient)
        with pytest.raises(KalshiPortfolioProjectionFencingError):
            await sync.synchronize("fenced")
        async with sf() as session:
            assert (
                await session.get(
                    KalshiPortfolioProjectionAttempt, {"principal_fingerprint": principal, "projection_id": "fenced"}
                )
                is None
            )
            assert (
                await session.get(
                    KalshiPortfolioCoverageCheckpoint,
                    {"principal_fingerprint": principal, "coverage_id": "fenced"},
                )
                is None
            )
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_stale_generation_cannot_replay_replacement_result_after_provider_io() -> None:
    engine, sf = await build_postgres_session_factory(Base, "kalshi_projection_post_io_fence")
    try:
        principal = "f" * 64
        lease = KalshiPortfolioLeaseService(sf)
        stale_token = await lease.acquire(principal, "stale", NOW, timedelta(minutes=1))
        stale_client = ProjectionClient(principal_fingerprint=principal)
        balance_started = asyncio.Event()
        release_balance = asyncio.Event()

        async def blocked_balance():
            balance_started.set()
            await release_balance.wait()
            return stale_client.balance

        stale_client.get_balance = blocked_balance
        stale_task = asyncio.create_task(
            KalshiPortfolioProjectionSynchronizer(
                sf,
                stale_client,
                subaccount=0,
                expected_lease_owner="stale",
                expected_fence_token=stale_token,
                correctness_freshness_bound=timedelta(seconds=5),
                now=lambda: NOW,
            ).synchronize("shared-attempt")
        )
        await asyncio.wait_for(balance_started.wait(), timeout=2)

        replacement_token = await lease.acquire(
            principal,
            "replacement",
            NOW + timedelta(seconds=1),
            timedelta(minutes=1),
            force=True,
        )
        replacement = await KalshiPortfolioProjectionSynchronizer(
            sf,
            ProjectionClient(principal_fingerprint=principal),
            subaccount=0,
            expected_lease_owner="replacement",
            expected_fence_token=replacement_token,
            correctness_freshness_bound=timedelta(seconds=5),
            now=lambda: NOW + timedelta(seconds=1),
        ).synchronize("shared-attempt")
        assert replacement.status == "complete"

        release_balance.set()
        with pytest.raises(KalshiPortfolioProjectionFencingError):
            await stale_task
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_db_only_route_ambiguity_and_no_network(monkeypatch) -> None:
    engine, sf = await build_postgres_session_factory(Base, "kalshi_projection_route")
    try:
        reader = KalshiPortfolioProjectionReader(sf, stale_after=timedelta(minutes=5), now=lambda: NOW)
        app = FastAPI()
        app.include_router(router, prefix="/api")
        app.dependency_overrides[get_projection_reader] = lambda: reader

        async def network_forbidden(*args, **kwargs):
            raise AssertionError("route performed network I/O")

        for method in (
            "get_balance",
            "get_positions",
            "get_settlements",
            "get_orders",
            "get_fills",
            "get_historical_cutoff",
            "get_historical_orders",
            "get_historical_fills",
        ):
            monkeypatch.setattr(f"services.venues.kalshi_v2.KalshiV2Client.{method}", network_forbidden)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            unscoped_disabled = await http.get("/api/kalshi/portfolio/snapshot")
            explicit = await http.get("/api/kalshi/portfolio/snapshot", params={"principal_fingerprint": "e" * 64})
        assert unscoped_disabled.status_code == 200
        assert unscoped_disabled.json()["readiness"] == "disabled"
        assert unscoped_disabled.json()["principal_fingerprint"] is None
        assert explicit.status_code == 404

        async with sf() as session, session.begin():
            session.add(
                WorkerControl(
                    worker_name="kalshi_portfolio_sync",
                    is_enabled=True,
                    is_paused=False,
                    interval_seconds=5,
                    requested_run_at=None,
                    updated_at=NOW,
                )
            )
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            never_synchronized = await http.get("/api/kalshi/portfolio/snapshot")
        assert never_synchronized.status_code == 200
        assert never_synchronized.json()["readiness"] == "never_synchronized"

        class AmbiguousReader:
            async def read(self, _principal):
                from services.kalshi_portfolio_projection import KalshiPortfolioPrincipalAmbiguityError

                raise KalshiPortfolioPrincipalAmbiguityError(("1" * 64, "2" * 64))

        app.dependency_overrides[get_projection_reader] = lambda: AmbiguousReader()
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            ambiguous = await http.get("/api/kalshi/portfolio/snapshot")
        assert ambiguous.status_code == 409
    finally:
        await engine.dispose()
