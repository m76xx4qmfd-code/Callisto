"""Dedicated worker for explicit Kalshi paper-only test-trade runs.

The process starts idle and only recovers persisted ``starting`` runs or ticks
persisted ``monitoring`` runs.  It has no authenticated venue capability and is
never registered with the broad trading worker host.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import AsyncSessionLocal, KalshiPaperTestRun, async_engine
from services.kalshi_paper_execution import KalshiPaperMarketDataClient
from services.kalshi_paper_service import KalshiPaperService
from services.kalshi_paper_test_trade_service import KalshiPaperTestTradeService
from services.worker_state import read_worker_control, write_worker_snapshot
from utils.logger import get_logger

WORKER_NAME = "kalshi_paper_test_trades"
DEFAULT_INTERVAL_SECONDS = 2
_ACTIVE_STATUSES = ("starting", "monitoring")
logger = get_logger(WORKER_NAME)


def build_service() -> KalshiPaperTestTradeService:
    market_data = KalshiPaperMarketDataClient()
    paper_service = KalshiPaperService(
        session_factory=AsyncSessionLocal,
        database_engine=async_engine,
        market_data_client=market_data,
    )
    return KalshiPaperTestTradeService(
        session_factory=AsyncSessionLocal,
        database_engine=async_engine,
        paper_service=paper_service,
        market_data_client=market_data,
    )


async def process_run(service: Any, payload: dict[str, object]) -> None:
    run = payload.get("run")
    if not isinstance(run, dict):
        raise ValueError("paper test run payload is malformed")
    status = run.get("status")
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("paper test run payload has no run_id")
    if status == "starting":
        required = (
            "account_id",
            "opportunity_id",
            "opportunity_revision",
            "quantity",
            "entry_limit_price",
            "take_profit_price",
            "stop_loss_price",
            "stop_loss_minimum_price",
        )
        facts = {name: run.get(name) for name in required}
        if not all(isinstance(value, str) and value for value in facts.values()):
            raise ValueError("starting paper test run has incomplete immutable facts")
        await service.start_run(run_id=run_id, **facts)
    elif status == "monitoring":
        await service.tick_run(run_id)


async def run_iteration(
    service: KalshiPaperTestTradeService,
    session_factory: Callable[[], AsyncSession] = AsyncSessionLocal,
) -> dict[str, int]:
    async with session_factory() as session:
        run_ids = list(
            (
                await session.execute(
                    select(KalshiPaperTestRun.run_id)
                    .where(KalshiPaperTestRun.status.in_(_ACTIVE_STATUSES))
                    .order_by(KalshiPaperTestRun.updated_at.asc())
                )
            ).scalars()
        )

    processed = 0
    failures = 0
    for run_id in run_ids:
        try:
            payload = await service.get_run(run_id)
            await process_run(service, payload)
            processed += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures += 1
            logger.error("Paper test run iteration failed", run_id=run_id, exc_info=exc)
    return {"eligible_runs": len(run_ids), "processed_runs": processed, "failed_runs": failures}


async def _write_snapshot(
    *,
    running: bool,
    enabled: bool,
    activity: str,
    interval_seconds: int,
    last_run_at: datetime | None = None,
    last_error: str | None = None,
    stats: dict[str, int] | None = None,
) -> None:
    async with AsyncSessionLocal() as session:
        await write_worker_snapshot(
            session,
            WORKER_NAME,
            running=running,
            enabled=enabled,
            current_activity=activity,
            interval_seconds=interval_seconds,
            last_run_at=last_run_at,
            last_error=last_error,
            stats=stats,
        )


async def start_loop() -> None:
    service = build_service()
    logger.info("Dedicated paper-only test-trade worker started")
    while True:
        interval = DEFAULT_INTERVAL_SECONDS
        try:
            async with AsyncSessionLocal() as session:
                control = await read_worker_control(
                    session,
                    WORKER_NAME,
                    default_interval=DEFAULT_INTERVAL_SECONDS,
                    default_enabled=True,
                )
            interval = max(1, int(control.get("interval_seconds") or DEFAULT_INTERVAL_SECONDS))
            enabled = bool(control.get("is_enabled", True))
            paused = bool(control.get("is_paused", False))
            if not enabled or paused:
                await _write_snapshot(
                    running=False,
                    enabled=enabled and not paused,
                    activity="Paper-only worker disabled" if not enabled else "Paper-only worker paused",
                    interval_seconds=interval,
                )
            else:
                stats = await run_iteration(service)
                now = datetime.now(timezone.utc)
                await _write_snapshot(
                    running=True,
                    enabled=True,
                    activity=(
                        "Monitoring explicit paper-only test runs"
                        if stats["eligible_runs"]
                        else "Idle; waiting for explicit paper-only test runs"
                    ),
                    interval_seconds=interval,
                    last_run_at=now if stats["eligible_runs"] else None,
                    last_error="PaperTestRunIterationError" if stats["failed_runs"] else None,
                    stats=stats,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Paper-only worker loop failed", exc_info=exc)
            try:
                await _write_snapshot(
                    running=False,
                    enabled=True,
                    activity="Paper-only worker retrying after error",
                    interval_seconds=interval,
                    last_error=type(exc).__name__,
                )
            except Exception as snapshot_exc:
                logger.error("Paper-only worker snapshot failed", exc_info=snapshot_exc)
        await asyncio.sleep(interval)
