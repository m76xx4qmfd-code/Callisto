"""Fenced, GET-only Kalshi portfolio projection persistence and DB reader."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import sessionmaker

from models.database import (
    KalshiPortfolioCoverageCheckpoint,
    KalshiPortfolioCoverageFillMembership,
    KalshiPortfolioCoverageOrderMembership,
    KalshiPortfolioFillObservation,
    KalshiPortfolioOrderObservation,
    KalshiPortfolioProjectionAttempt,
    KalshiPortfolioProjectionHead,
    KalshiPortfolioProjectionLease,
    WorkerControl,
    WorkerSnapshot,
)
from services.kalshi_portfolio_coverage import KalshiPortfolioCoverageService
from services.venues.kalshi_v2 import KalshiPositionsPage, KalshiSettlementsPage


class KalshiPortfolioProjectionFencingError(RuntimeError):
    """The expected principal lease no longer authorizes projection persistence."""


class KalshiPortfolioProjectionLeaseConflictError(RuntimeError):
    """A live lease is owned by another worker."""


class KalshiPortfolioPrincipalAmbiguityError(RuntimeError):
    """More than one persisted principal exists and none was selected."""

    def __init__(self, principal_fingerprints: tuple[str, ...]) -> None:
        super().__init__("multiple Kalshi principals exist")
        self.principal_fingerprints = principal_fingerprints


class KalshiPortfolioPrincipalNotFoundError(RuntimeError):
    """The selected principal has no durable projection history."""


@dataclass(frozen=True)
class KalshiPortfolioProjectionResult:
    principal_fingerprint: str
    projection_id: str
    status: Literal["complete", "incomplete"]
    reason: str
    retry_allowed: Literal[False] = False


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _validate_principal(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("principal_fingerprint must be 64 lowercase hexadecimal characters")
    return value


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("projection payload contains a non-finite Decimal")
    return format(value, "f")


def _exact_json(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, datetime):
        return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if is_dataclass(value):
        return _exact_json(asdict(value))
    if isinstance(value, dict):
        return {
            str(key): str(item) if str(key).endswith("_cents") and isinstance(item, int) else _exact_json(item)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [_exact_json(item) for item in value]
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"unsupported projection payload value {type(value)!r}")


def _hash(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class KalshiPortfolioLeaseService:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    async def acquire(
        self,
        principal_fingerprint: str,
        owner_id: str,
        now: datetime | Callable[[], datetime],
        ttl: timedelta,
        *,
        force: bool = False,
    ) -> int:
        principal = _validate_principal(principal_fingerprint)
        owner = owner_id.strip()
        if not owner:
            raise ValueError("owner_id is required")
        insert_time = _utc(now() if callable(now) else now)
        if ttl.total_seconds() <= 0:
            raise ValueError("lease ttl must be positive")
        async with self._session_factory() as session, session.begin():
            inserted_token = await session.scalar(
                pg_insert(KalshiPortfolioProjectionLease)
                .values(
                    principal_fingerprint=principal,
                    owner_id=owner,
                    fence_token=1,
                    expires_at=insert_time + ttl,
                    updated_at=insert_time,
                )
                .on_conflict_do_nothing(index_elements=[KalshiPortfolioProjectionLease.principal_fingerprint])
                .returning(KalshiPortfolioProjectionLease.fence_token)
            )
            if inserted_token is not None:
                inserted = await session.get(KalshiPortfolioProjectionLease, principal, with_for_update=True)
                if inserted is None:
                    raise RuntimeError("inserted lease row could not be read back")
                committed_at = _utc(now() if callable(now) else now)
                inserted.expires_at = committed_at + ttl
                inserted.updated_at = committed_at
                await session.flush()
                return int(inserted_token)
            row = await session.scalar(
                select(KalshiPortfolioProjectionLease)
                .where(KalshiPortfolioProjectionLease.principal_fingerprint == principal)
                .with_for_update()
            )
            if row is None:
                raise RuntimeError("conflict-safe lease insertion did not produce a durable row")
            locked_at = _utc(now() if callable(now) else now)
            if _utc(row.expires_at) > locked_at and row.owner_id != owner and not force:
                raise KalshiPortfolioProjectionLeaseConflictError("principal projection lease is already held")
            token = row.fence_token + 1
            row.owner_id = owner
            row.fence_token = token
            row.expires_at = locked_at + ttl
            row.updated_at = locked_at
            await session.flush()
            return token

    async def renew(
        self,
        principal_fingerprint: str,
        owner_id: str,
        fence_token: int,
        now: datetime | Callable[[], datetime],
        ttl: timedelta,
    ) -> bool:
        """Extend an unexpired owned lease without changing its fence."""
        principal = _validate_principal(principal_fingerprint)
        owner = owner_id.strip()
        if not owner or fence_token <= 0 or ttl.total_seconds() <= 0:
            raise ValueError("valid owner, fence token, and positive lease ttl are required")
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(KalshiPortfolioProjectionLease)
                .where(KalshiPortfolioProjectionLease.principal_fingerprint == principal)
                .with_for_update()
            )
            locked_at = _utc(now() if callable(now) else now)
            if (
                row is None
                or row.owner_id != owner
                or row.fence_token != fence_token
                or _utc(row.expires_at) <= locked_at
            ):
                return False
            row.expires_at = locked_at + ttl
            row.updated_at = locked_at
            await session.flush()
            return True

    async def release(
        self,
        principal_fingerprint: str,
        owner_id: str,
        fence_token: int,
        now: datetime | Callable[[], datetime],
    ) -> bool:
        """Expire an owned lease while retaining monotonic fence history."""
        principal = _validate_principal(principal_fingerprint)
        owner = owner_id.strip()
        if not owner or fence_token <= 0:
            raise ValueError("valid owner and fence token are required")
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(KalshiPortfolioProjectionLease)
                .where(KalshiPortfolioProjectionLease.principal_fingerprint == principal)
                .with_for_update()
            )
            locked_at = _utc(now() if callable(now) else now)
            if row is None or row.owner_id != owner or row.fence_token != fence_token:
                return False
            row.expires_at = locked_at
            row.updated_at = locked_at
            await session.flush()
            return True


class KalshiPortfolioProjectionSynchronizer:
    """Collect independent GET components, then commit one fenced projection version."""

    def __init__(
        self,
        session_factory: sessionmaker,
        client,
        *,
        subaccount: int,
        expected_lease_owner: str,
        expected_fence_token: int,
        correctness_freshness_bound: timedelta,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if isinstance(subaccount, bool) or not isinstance(subaccount, int) or not 0 <= subaccount <= 63:
            raise ValueError("subaccount must be an integer between 0 and 63")
        if not expected_lease_owner.strip() or expected_fence_token <= 0:
            raise ValueError("an expected lease owner and positive fence token are required")
        if correctness_freshness_bound.total_seconds() <= 0:
            raise ValueError("correctness_freshness_bound must be positive")
        self._session_factory = session_factory
        self._client = client
        self._subaccount = subaccount
        self._owner = expected_lease_owner.strip()
        self._fence = expected_fence_token
        self._bound = correctness_freshness_bound
        self._now = now or (lambda: datetime.now(timezone.utc))

    @property
    def principal_fingerprint(self) -> str:
        return self._client.principal_fingerprint

    async def synchronize(self, projection_id: str | None = None) -> KalshiPortfolioProjectionResult:
        principal = _validate_principal(self.principal_fingerprint)
        projection_id = (projection_id or str(uuid.uuid4())).strip()
        if not projection_id:
            raise ValueError("projection_id is required")
        started_at = _utc(self._now())
        async with self._session_factory() as session, session.begin():
            lease = await session.scalar(
                select(KalshiPortfolioProjectionLease)
                .where(KalshiPortfolioProjectionLease.principal_fingerprint == principal)
                .with_for_update()
            )
            lease_checked_at = _utc(self._now())
            if (
                lease is None
                or lease.owner_id != self._owner
                or lease.fence_token != self._fence
                or _utc(lease.expires_at) <= lease_checked_at
            ):
                raise KalshiPortfolioProjectionFencingError("projection lease fence changed or expired")
            existing = await session.get(
                KalshiPortfolioProjectionAttempt,
                {"principal_fingerprint": principal, "projection_id": projection_id},
            )
            if existing is not None:
                return KalshiPortfolioProjectionResult(principal, projection_id, existing.status, existing.reason)
        try:
            coverage = await KalshiPortfolioCoverageService(
                self._session_factory,
                self._client,
                expected_lease_owner=self._owner,
                expected_fence_token=self._fence,
                now=self._now,
            ).sweep(projection_id, started_at)
        except Exception as exc:  # noqa: BLE001 - failed attempts are durable and never authorize retry.
            reason = f"coverage_failed:{type(exc).__name__}"
            completed_at = _utc(self._now())
            await self._persist_failed_attempt(
                principal=principal,
                projection_id=projection_id,
                started_at=started_at,
                completed_at=completed_at,
                reason=reason,
            )
            return KalshiPortfolioProjectionResult(principal, projection_id, "incomplete", reason)
        component_times: dict[str, datetime] = {"coverage": _utc(coverage.observed_at)}
        gaps: list[str] = []
        positions_json = balance_json = settlements_json = None

        try:
            positions_json = await self._read_positions()
            component_times["positions"] = _utc(self._now())
        except Exception:  # noqa: BLE001 - component failure is durable degraded evidence.
            gaps.append("positions")
        try:
            balance_json = _exact_json(await self._client.get_balance())
            component_times["balance"] = _utc(self._now())
        except Exception:  # noqa: BLE001
            gaps.append("balance")
        try:
            settlements_json = await self._read_settlements()
            component_times["settlements"] = _utc(self._now())
        except Exception:  # noqa: BLE001
            gaps.append("settlements")

        skew = Decimal(str((max(component_times.values()) - min(component_times.values())).total_seconds()))
        if coverage.status != "complete":
            gaps.append("coverage")
        if skew > Decimal(str(self._bound.total_seconds())):
            gaps.append("component_skew")
        gaps = sorted(set(gaps))
        status: Literal["complete", "incomplete"] = "complete" if not gaps else "incomplete"
        reason = "authoritative_components_complete" if not gaps else "incomplete:" + ",".join(gaps)
        completed_at = _utc(self._now())
        payload_hash = _hash(
            {
                "coverage_hash": coverage.observed_evidence_hash,
                "subaccount": self._subaccount,
                "positions": positions_json,
                "balance": balance_json,
                "settlements": settlements_json,
                "component_times": {key: _exact_json(value) for key, value in component_times.items()},
            }
        )
        attempt = KalshiPortfolioProjectionAttempt(
            principal_fingerprint=principal,
            projection_id=projection_id,
            coverage_id=coverage.coverage_id,
            subaccount_number=self._subaccount,
            status=status,
            reason=reason,
            started_at=started_at,
            completed_at=completed_at,
            coverage_observed_at=component_times.get("coverage"),
            positions_observed_at=component_times.get("positions"),
            balance_observed_at=component_times.get("balance"),
            settlements_observed_at=component_times.get("settlements"),
            component_skew_seconds=skew,
            correctness_freshness_bound_seconds=Decimal(str(self._bound.total_seconds())),
            evidence_hash=payload_hash,
            balance_json=balance_json,
            positions_json=positions_json,
            settlements_json=settlements_json,
            gaps_json=gaps,
            retry_allowed=False,
        )
        async with self._session_factory() as session, session.begin():
            lease = await session.scalar(
                select(KalshiPortfolioProjectionLease)
                .where(KalshiPortfolioProjectionLease.principal_fingerprint == principal)
                .with_for_update()
            )
            lease_checked_at = _utc(self._now())
            if (
                lease is None
                or lease.owner_id != self._owner
                or lease.fence_token != self._fence
                or _utc(lease.expires_at) <= lease_checked_at
            ):
                raise KalshiPortfolioProjectionFencingError("projection lease fence changed or expired")
            existing = await session.get(
                KalshiPortfolioProjectionAttempt,
                {
                    "principal_fingerprint": principal,
                    "projection_id": projection_id,
                },
            )
            if existing is not None:
                return KalshiPortfolioProjectionResult(principal, projection_id, existing.status, existing.reason)
            head = await session.get(KalshiPortfolioProjectionHead, principal, with_for_update=True)
            session.add(attempt)
            if head is None:
                session.add(
                    KalshiPortfolioProjectionHead(
                        principal_fingerprint=principal,
                        latest_projection_id=projection_id,
                        healthy_projection_id=projection_id if status == "complete" else None,
                        updated_at=completed_at,
                    )
                )
            else:
                head.latest_projection_id = projection_id
                if status == "complete":
                    head.healthy_projection_id = projection_id
                head.updated_at = completed_at
            await session.flush()
        return KalshiPortfolioProjectionResult(principal, projection_id, status, reason)

    async def _persist_failed_attempt(
        self,
        *,
        principal: str,
        projection_id: str,
        started_at: datetime,
        completed_at: datetime,
        reason: str,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            lease = await session.scalar(
                select(KalshiPortfolioProjectionLease)
                .where(KalshiPortfolioProjectionLease.principal_fingerprint == principal)
                .with_for_update()
            )
            lease_checked_at = _utc(self._now())
            if (
                lease is None
                or lease.owner_id != self._owner
                or lease.fence_token != self._fence
                or _utc(lease.expires_at) <= lease_checked_at
            ):
                raise KalshiPortfolioProjectionFencingError("projection lease fence changed or expired")
            existing = await session.get(
                KalshiPortfolioProjectionAttempt,
                {
                    "principal_fingerprint": principal,
                    "projection_id": projection_id,
                },
            )
            if existing is not None:
                return
            head = await session.get(KalshiPortfolioProjectionHead, principal, with_for_update=True)
            session.add(
                KalshiPortfolioProjectionAttempt(
                    principal_fingerprint=principal,
                    projection_id=projection_id,
                    coverage_id=None,
                    subaccount_number=self._subaccount,
                    status="failed",
                    reason=reason,
                    started_at=started_at,
                    completed_at=completed_at,
                    correctness_freshness_bound_seconds=Decimal(str(self._bound.total_seconds())),
                    gaps_json=["coverage", "positions", "balance", "settlements"],
                    retry_allowed=False,
                )
            )
            if head is None:
                session.add(
                    KalshiPortfolioProjectionHead(
                        principal_fingerprint=principal,
                        latest_projection_id=projection_id,
                        healthy_projection_id=None,
                        updated_at=completed_at,
                    )
                )
            else:
                head.latest_projection_id = projection_id
                head.updated_at = completed_at
            await session.flush()

    async def _read_positions(self) -> dict[str, object]:
        cursor = None
        seen: set[str] = set()
        markets: list[object] = []
        events: list[object] = []
        while True:
            page = await self._client.get_positions(cursor=cursor, limit=1000, subaccount=self._subaccount)
            if not isinstance(page, KalshiPositionsPage):
                raise TypeError("positions endpoint returned an invalid page")
            markets.extend(_exact_json(item) for item in page.market_positions)
            events.extend(_exact_json(item) for item in page.event_positions)
            if not page.cursor:
                return {
                    "subaccount_number": str(self._subaccount),
                    "market_positions": markets,
                    "event_positions": events,
                }
            if page.cursor in seen:
                raise ValueError("positions pagination cursor repeated")
            seen.add(page.cursor)
            cursor = page.cursor

    async def _read_settlements(self) -> dict[str, object]:
        cursor = None
        seen: set[str] = set()
        settlements: list[object] = []
        while True:
            page = await self._client.get_settlements(cursor=cursor, limit=1000, subaccount=self._subaccount)
            if not isinstance(page, KalshiSettlementsPage):
                raise TypeError("settlements endpoint returned an invalid page")
            settlements.extend(_exact_json(item) for item in page.settlements)
            if not page.cursor:
                return {"subaccount_number": str(self._subaccount), "settlements": settlements}
            if page.cursor in seen:
                raise ValueError("settlements pagination cursor repeated")
            seen.add(page.cursor)
            cursor = page.cursor


class KalshiPortfolioProjectionReader:
    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        stale_after: timedelta,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if stale_after.total_seconds() <= 0:
            raise ValueError("stale_after must be positive")
        self._session_factory = session_factory
        self._stale_after = stale_after
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def read(self, principal_fingerprint: str | None = None) -> dict[str, object]:
        requested_principal = _validate_principal(principal_fingerprint) if principal_fingerprint is not None else None
        async with self._session_factory() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            principals = tuple(
                sorted(set((await session.scalars(select(KalshiPortfolioProjectionHead.principal_fingerprint))).all()))
            )
            control = await session.get(WorkerControl, "kalshi_portfolio_sync")
            worker_enabled = bool(control is not None and control.is_enabled)
            if requested_principal is None:
                if len(principals) > 1:
                    raise KalshiPortfolioPrincipalAmbiguityError(principals)
                if not principals:
                    return {
                        "principal_fingerprint": None,
                        "retry_allowed": False,
                        "readiness": "never_synchronized" if worker_enabled else "disabled",
                        "reason": "no_projection_attempt" if worker_enabled else "projection_worker_disabled",
                        "scope": None,
                        "projection_id": None,
                        "last_healthy_as_of": None,
                        "balance": None,
                        "positions": None,
                        "settlements": None,
                        "orders": [],
                        "fills": [],
                        "unknown_activity": {"order_ids": [], "client_order_ids": [], "fill_ids": []},
                        "components": {},
                        "component_skew_seconds": None,
                        "gaps": [],
                        "latest_attempt": None,
                        "sync_runtime": {
                            "running": False,
                            "ready": False,
                            "degraded": False,
                            "principal_matches": False,
                            "fresh": False,
                            "updated_at": None,
                            "error_type": None,
                        },
                    }
                principal = principals[0]
            else:
                if requested_principal not in principals:
                    raise KalshiPortfolioPrincipalNotFoundError("Kalshi principal has no durable projection history")
                principal = requested_principal
            worker_snapshot = await session.get(WorkerSnapshot, "kalshi_portfolio_sync")
            worker_stats = worker_snapshot.stats_json if worker_snapshot is not None else {}
            if not isinstance(worker_stats, dict):
                worker_stats = {}
            worker_principal_matches = worker_stats.get("principal_fingerprint") == principal
            worker_snapshot_updated_at = (
                _utc(worker_snapshot.updated_at)
                if worker_snapshot is not None and worker_snapshot.updated_at is not None
                else None
            )
            worker_snapshot_age = (
                _utc(self._now()) - worker_snapshot_updated_at if worker_snapshot_updated_at is not None else None
            )
            worker_snapshot_fresh = bool(
                worker_snapshot_age is not None and timedelta(0) <= worker_snapshot_age <= self._stale_after
            )
            head = await session.get(KalshiPortfolioProjectionHead, principal)
            base = {
                "principal_fingerprint": principal,
                "retry_allowed": False,
                "scope": None,
                "projection_id": None,
                "last_healthy_as_of": None,
                "balance": None,
                "positions": None,
                "settlements": None,
                "orders": [],
                "fills": [],
                "unknown_activity": {"order_ids": [], "client_order_ids": [], "fill_ids": []},
                "components": {},
                "component_skew_seconds": None,
                "gaps": [],
                "latest_attempt": None,
                "sync_runtime": {
                    "running": bool(
                        worker_snapshot is not None
                        and worker_snapshot.running
                        and worker_principal_matches
                        and worker_snapshot_fresh
                    ),
                    "ready": bool(
                        worker_stats.get("ready", False) and worker_principal_matches and worker_snapshot_fresh
                    ),
                    "degraded": bool(worker_stats.get("degraded", False)),
                    "principal_matches": worker_principal_matches,
                    "fresh": worker_snapshot_fresh,
                    "updated_at": _exact_json(worker_snapshot_updated_at) if worker_snapshot_updated_at else None,
                    "error_type": worker_snapshot.last_error if worker_snapshot is not None else None,
                },
            }
            if head is None:
                if worker_enabled:
                    base.update(readiness="never_synchronized", reason="no_projection_attempt")
                else:
                    base.update(readiness="disabled", reason="projection_worker_disabled")
                return base
            latest = await session.get(
                KalshiPortfolioProjectionAttempt,
                {
                    "principal_fingerprint": principal,
                    "projection_id": head.latest_projection_id,
                },
            )
            healthy = None
            if head.healthy_projection_id is not None:
                healthy = await session.get(
                    KalshiPortfolioProjectionAttempt,
                    {
                        "principal_fingerprint": principal,
                        "projection_id": head.healthy_projection_id,
                    },
                )
            if latest is None:
                raise RuntimeError("projection head references a missing latest attempt")
            base["latest_attempt"] = {
                "projection_id": latest.projection_id,
                "status": latest.status,
                "reason": latest.reason,
                "completed_at": _exact_json(_utc(latest.completed_at)),
            }
            base["gaps"] = list(latest.gaps_json)
            safety_attempt = await session.scalar(
                select(KalshiPortfolioProjectionAttempt)
                .where(
                    KalshiPortfolioProjectionAttempt.principal_fingerprint == principal,
                    KalshiPortfolioProjectionAttempt.coverage_id.is_not(None),
                )
                .order_by(
                    KalshiPortfolioProjectionAttempt.completed_at.desc(),
                    KalshiPortfolioProjectionAttempt.projection_id.desc(),
                )
                .limit(1)
            )
            safety_checkpoint = (
                await session.get(
                    KalshiPortfolioCoverageCheckpoint,
                    {
                        "principal_fingerprint": principal,
                        "coverage_id": safety_attempt.coverage_id,
                    },
                )
                if safety_attempt is not None
                else None
            )
            if safety_checkpoint is not None:
                base["unknown_activity"] = {
                    "order_ids": list(safety_checkpoint.unknown_order_ids_json),
                    "client_order_ids": list(safety_checkpoint.unknown_client_order_ids_json),
                    "fill_ids": list(safety_checkpoint.unknown_fill_ids_json),
                }
            if healthy is None:
                base.update(readiness="degraded", reason=latest.reason)
                return base
            if healthy.status != "complete":
                raise RuntimeError("healthy projection head references a non-complete attempt")
            healthy_checkpoint = await session.get(
                KalshiPortfolioCoverageCheckpoint,
                {
                    "principal_fingerprint": principal,
                    "coverage_id": healthy.coverage_id,
                },
            )
            safety_checkpoint = safety_checkpoint or healthy_checkpoint
            order_rows = (
                (
                    await session.execute(
                        select(KalshiPortfolioOrderObservation)
                        .join(
                            KalshiPortfolioCoverageOrderMembership,
                            (
                                (
                                    KalshiPortfolioCoverageOrderMembership.principal_fingerprint
                                    == KalshiPortfolioOrderObservation.principal_fingerprint
                                )
                                & (
                                    KalshiPortfolioCoverageOrderMembership.order_id
                                    == KalshiPortfolioOrderObservation.order_id
                                )
                                & (
                                    KalshiPortfolioCoverageOrderMembership.evidence_hash
                                    == KalshiPortfolioOrderObservation.evidence_hash
                                )
                            ),
                        )
                        .where(
                            KalshiPortfolioCoverageOrderMembership.principal_fingerprint == principal,
                            KalshiPortfolioCoverageOrderMembership.coverage_id == healthy.coverage_id,
                        )
                        .order_by(KalshiPortfolioOrderObservation.order_id)
                    )
                )
                .scalars()
                .all()
            )
            fill_rows = (
                (
                    await session.execute(
                        select(KalshiPortfolioFillObservation)
                        .join(
                            KalshiPortfolioCoverageFillMembership,
                            (
                                (
                                    KalshiPortfolioCoverageFillMembership.principal_fingerprint
                                    == KalshiPortfolioFillObservation.principal_fingerprint
                                )
                                & (
                                    KalshiPortfolioCoverageFillMembership.fill_id
                                    == KalshiPortfolioFillObservation.fill_id
                                )
                            ),
                        )
                        .where(
                            KalshiPortfolioCoverageFillMembership.principal_fingerprint == principal,
                            KalshiPortfolioCoverageFillMembership.coverage_id == healthy.coverage_id,
                        )
                        .order_by(KalshiPortfolioFillObservation.fill_id)
                    )
                )
                .scalars()
                .all()
            )
            component_values = {
                "coverage": healthy.coverage_observed_at,
                "positions": healthy.positions_observed_at,
                "balance": healthy.balance_observed_at,
                "settlements": healthy.settlements_observed_at,
            }
            as_of = min(_utc(value) for value in component_values.values() if value is not None)
            age = _utc(self._now()) - as_of
            if not worker_enabled:
                readiness, reason = "disabled", "projection_worker_disabled"
            elif latest.projection_id != healthy.projection_id:
                readiness, reason = "degraded", latest.reason
            elif not worker_principal_matches:
                readiness, reason = "degraded", "private_sync_runtime_principal_mismatch"
            elif not worker_snapshot_fresh:
                readiness, reason = "degraded", "private_sync_runtime_stale"
            elif not bool(worker_stats.get("ready", False)):
                readiness, reason = "degraded", "private_sync_runtime_not_ready"
            elif age > self._stale_after:
                readiness, reason = "stale", "last_healthy_projection_stale"
            else:
                readiness, reason = "healthy", "authoritative_projection_current"
            base.update(
                readiness=readiness,
                reason=reason,
                scope={
                    "orders_and_fills": {"kind": "all_subaccounts"},
                    "balance": {"kind": "account_aggregate"},
                    "positions": {
                        "kind": "subaccount",
                        "subaccount_numbers": [healthy.subaccount_number],
                    },
                    "settlements": {
                        "kind": "subaccount",
                        "subaccount_numbers": [healthy.subaccount_number],
                    },
                },
                projection_id=healthy.projection_id,
                last_healthy_as_of=_exact_json(as_of),
                balance=healthy.balance_json,
                positions=healthy.positions_json,
                settlements=healthy.settlements_json,
                orders=[row.payload_json for row in order_rows],
                fills=[row.payload_json for row in fill_rows],
                unknown_activity={
                    "order_ids": list(safety_checkpoint.unknown_order_ids_json) if safety_checkpoint else [],
                    "client_order_ids": list(safety_checkpoint.unknown_client_order_ids_json)
                    if safety_checkpoint
                    else [],
                    "fill_ids": list(safety_checkpoint.unknown_fill_ids_json) if safety_checkpoint else [],
                },
                components={
                    key: {"observed_at": _exact_json(_utc(value)) if value else None}
                    for key, value in component_values.items()
                },
                component_skew_seconds=_decimal_text(healthy.component_skew_seconds),
            )
            return base


__all__ = [
    "KalshiPortfolioLeaseService",
    "KalshiPortfolioPrincipalAmbiguityError",
    "KalshiPortfolioPrincipalNotFoundError",
    "KalshiPortfolioProjectionFencingError",
    "KalshiPortfolioProjectionLeaseConflictError",
    "KalshiPortfolioProjectionReader",
    "KalshiPortfolioProjectionResult",
    "KalshiPortfolioProjectionSynchronizer",
]
