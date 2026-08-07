from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol, cast

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from sqlalchemy.orm import sessionmaker

from models.database import (
    KalshiPaperAccount,
    KalshiPaperDecision,
    KalshiPaperPosition,
    KalshiPaperTestEvent,
    KalshiPaperTestRun,
)
from services.kalshi_paper_execution import (
    PaperQuote,
    _from_scaled_int,
    _to_scaled_int,
    decimal_string,
    parse_price,
    parse_quantity,
)
from services.kalshi_paper_service import (
    KalshiPaperService,
    PaperDecisionConflict,
    PaperOpportunityIneligible,
    PaperOpportunityNotFound,
    PaperPositionNotClosable,
)


_TERMINAL = {"entry_unfilled", "stopped", "completed", "blocked"}
_HASH_PATTERN = "0123456789abcdef"


class KalshiPaperTestRunConflict(RuntimeError):
    """A stable run ID was reused with different immutable facts."""


class KalshiPaperTestRunNotFound(LookupError):
    """The requested paper-only test run does not exist."""


class KalshiPaperTestRunTransition(RuntimeError):
    """The requested lifecycle transition is not legal."""


class _MarketDataCapability(Protocol):
    async def fetch_quote(self, ticker: str) -> PaperQuote: ...


class KalshiPaperTestTradeService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker | Callable[[], AsyncSession],
        database_engine: AsyncEngine,
        paper_service: KalshiPaperService,
        market_data_client: _MarketDataCapability,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._database_engine = database_engine
        self._paper = paper_service
        self._market_data = market_data_client
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def start_run(
        self,
        *,
        run_id: str,
        account_id: str,
        opportunity_id: str,
        opportunity_revision: str,
        quantity: str,
        entry_limit_price: str,
        take_profit_price: str,
        stop_loss_price: str,
        stop_loss_minimum_price: str,
    ) -> dict[str, object]:
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            raise ValueError("run_id, account_id, and opportunity_id are required")
        # A row lock cannot protect a not-yet-created run.  Use the same stable
        # session advisory namespace as ticks so concurrent first starts either
        # create once or observe and replay the committed immutable request.
        async with self._run_lock(normalized_run_id):
            return await self._start_run_locked(
                run_id=normalized_run_id,
                account_id=account_id,
                opportunity_id=opportunity_id,
                opportunity_revision=opportunity_revision,
                quantity=quantity,
                entry_limit_price=entry_limit_price,
                take_profit_price=take_profit_price,
                stop_loss_price=stop_loss_price,
                stop_loss_minimum_price=stop_loss_minimum_price,
            )

    async def _start_run_locked(
        self,
        *,
        run_id: str,
        account_id: str,
        opportunity_id: str,
        opportunity_revision: str,
        quantity: str,
        entry_limit_price: str,
        take_profit_price: str,
        stop_loss_price: str,
        stop_loss_minimum_price: str,
    ) -> dict[str, object]:
        normalized_run_id = str(run_id or "").strip()
        normalized_account_id = str(account_id or "").strip()
        normalized_opportunity_id = str(opportunity_id or "").strip()
        revision = str(opportunity_revision or "").strip().lower()
        if not normalized_run_id or not normalized_account_id or not normalized_opportunity_id:
            raise ValueError("run_id, account_id, and opportunity_id are required")
        if len(revision) != 64 or any(character not in _HASH_PATTERN for character in revision):
            raise ValueError("opportunity_revision must be a SHA-256 digest")
        parsed_quantity = parse_quantity(quantity)
        entry_price = parse_price(entry_limit_price, field="entry_limit_price")
        take_profit = parse_price(take_profit_price, field="take_profit_price")
        stop_loss = parse_price(stop_loss_price, field="stop_loss_price")
        stop_floor = parse_price(stop_loss_minimum_price, field="stop_loss_minimum_price")
        if not stop_floor <= stop_loss < take_profit:
            raise ValueError(
                "require stop_loss_minimum_price <= stop_loss_price < take_profit_price"
            )
        canonical = {
            "run_id": normalized_run_id,
            "account_id": normalized_account_id,
            "opportunity_id": normalized_opportunity_id,
            "opportunity_revision": revision,
            "quantity": decimal_string(parsed_quantity, scale=2),
            "entry_limit_price": decimal_string(entry_price, scale=6),
            "take_profit_price": decimal_string(take_profit, scale=6),
            "stop_loss_price": decimal_string(stop_loss, scale=6),
            "stop_loss_minimum_price": decimal_string(stop_floor, scale=6),
        }
        request_hash = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        async with self._session_factory() as session, session.begin():
            existing = await session.get(KalshiPaperTestRun, normalized_run_id)
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise KalshiPaperTestRunConflict(
                        "run_id already exists with different immutable request facts"
                    )
            else:
                if await session.get(KalshiPaperAccount, normalized_account_id) is None:
                    raise KalshiPaperTestRunNotFound("paper account not found")
                eligibility = await self._paper.get_eligibility(normalized_opportunity_id)
                if eligibility["opportunity_revision"] != revision:
                    raise KalshiPaperTestRunConflict("opportunity revision changed")
                now = self._utcnow()
                existing = KalshiPaperTestRun(
                    run_id=normalized_run_id,
                    request_hash=request_hash,
                    account_id=normalized_account_id,
                    opportunity_id=normalized_opportunity_id,
                    opportunity_revision=revision,
                    ticker=str(eligibility["ticker"]),
                    outcome=str(eligibility["outcome"]),
                    quantity=parsed_quantity,
                    entry_limit_price=entry_price,
                    take_profit_price=take_profit,
                    stop_loss_price=stop_loss,
                    stop_loss_minimum_price=stop_floor,
                    entry_decision_id=f"paper-test-entry:{normalized_run_id}",
                    position_id=None,
                    status="starting",
                    next_event_sequence=2,
                    last_reason="operator_started",
                    last_error=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(existing)
                await session.flush()
                session.add(
                    KalshiPaperTestEvent(
                        run_id=normalized_run_id,
                        sequence=1,
                        account_id=normalized_account_id,
                        event_type="started",
                        reason="operator_started",
                        created_at=now,
                    )
                )
        if existing.status == "starting":
            await self._recover_start(normalized_run_id)
        return await self.get_run(normalized_run_id)

    async def tick_run(self, run_id: str) -> dict[str, object]:
        normalized_run_id = str(run_id or "").strip()
        async with self._run_lock(normalized_run_id) as connection:
            factory = sessionmaker(bind=connection, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session:
                run = await session.get(KalshiPaperTestRun, normalized_run_id)
                if run is None:
                    raise KalshiPaperTestRunNotFound("paper test run not found")
                if run.status != "monitoring":
                    return await self.get_run(normalized_run_id)

            pending = await self._pending_trigger(factory, normalized_run_id)
            if pending is not None:
                await self._finish_exit_attempt(factory, pending)
                return await self.get_run(normalized_run_id)

            remaining, _realized = await self._position_projection(factory, normalized_run_id)
            if remaining == Decimal("0"):
                async with factory() as session, session.begin():
                    run = await self._locked_run(session, normalized_run_id)
                    if run.status == "monitoring":
                        self._append_event(
                            session,
                            run,
                            event_type="completed",
                            remaining_quantity=remaining,
                            reason="position_already_closed",
                        )
                        run.status = "completed"
                        run.last_reason = "position_already_closed"
                return await self.get_run(normalized_run_id)

            async with factory() as session:
                run = await session.get(KalshiPaperTestRun, normalized_run_id)
                assert run is not None
                ticker = run.ticker
                outcome = run.outcome
            quote = await self._market_data.fetch_quote(ticker)
            if quote.market.ticker != ticker or quote.book.ticker != ticker:
                await self._block_run(factory, normalized_run_id, "frozen_market_identity_mismatch")
                return await self.get_run(normalized_run_id)
            levels = quote.book.yes_bids if outcome == "yes" else quote.book.no_bids
            best_bid = max((level.price for level in levels), default=None)

            trigger_sequence: int | None = None
            async with factory() as session, session.begin():
                run = await self._locked_run(session, normalized_run_id)
                if run.status != "monitoring":
                    return await self.get_run(normalized_run_id)
                event_values = {
                    "market_observed_at": quote.market.observed_at,
                    "book_observed_at": quote.book.observed_at,
                    "quote_evidence_hash": quote.book.evidence_hash,
                    "quote_evidence_json": quote.book.evidence_json,
                    "remaining_quantity": remaining,
                }
                if best_bid is None:
                    self._append_event(
                        session, run, event_type="no_bid", reason="no_held_outcome_bid", **event_values
                    )
                elif best_bid >= cast(Decimal, run.take_profit_price):
                    trigger_sequence = int(run.next_event_sequence)
                    exit_id = f"paper-test-exit:{run.run_id}:{trigger_sequence}"
                    self._append_event(
                        session,
                        run,
                        event_type="take_profit_triggered",
                        best_bid=best_bid,
                        trigger_price=cast(Decimal, run.take_profit_price),
                        exit_decision_id=exit_id,
                        reason="best_bid_reached_take_profit",
                        **event_values,
                    )
                elif best_bid <= cast(Decimal, run.stop_loss_price):
                    trigger_sequence = int(run.next_event_sequence)
                    exit_id = f"paper-test-exit:{run.run_id}:{trigger_sequence}"
                    self._append_event(
                        session,
                        run,
                        event_type="stop_loss_triggered",
                        best_bid=best_bid,
                        trigger_price=cast(Decimal, run.stop_loss_price),
                        exit_decision_id=exit_id,
                        reason="best_bid_reached_stop_loss",
                        **event_values,
                    )
                else:
                    self._append_event(
                        session,
                        run,
                        event_type="hold",
                        best_bid=best_bid,
                        reason="best_bid_between_thresholds",
                        **event_values,
                    )
            if trigger_sequence is not None:
                pending = await self._pending_trigger(factory, normalized_run_id)
                assert pending is not None
                await self._finish_exit_attempt(factory, pending)
            return await self.get_run(normalized_run_id)

    async def get_run(self, run_id: str) -> dict[str, object]:
        async with self._session_factory() as session:
            run = await session.get(KalshiPaperTestRun, run_id)
            if run is None:
                raise KalshiPaperTestRunNotFound("paper test run not found")
            remaining, realized = await self._position_projection(self._session_factory, run_id)
            events = (
                await session.execute(
                    select(KalshiPaperTestEvent)
                    .where(KalshiPaperTestEvent.run_id == run_id)
                    .order_by(KalshiPaperTestEvent.sequence)
                )
            ).scalars()
            return {
                "run": self._serialize_run(run, remaining=remaining, realized_pnl=realized),
                "events": [self._serialize_event(event) for event in events],
            }

    async def list_runs(self, account_id: str) -> list[dict[str, object]]:
        async with self._session_factory() as session:
            if await session.get(KalshiPaperAccount, account_id) is None:
                raise KalshiPaperTestRunNotFound("paper account not found")
            run_ids = (
                await session.execute(
                    select(KalshiPaperTestRun.run_id)
                    .where(KalshiPaperTestRun.account_id == account_id)
                    .order_by(KalshiPaperTestRun.created_at.desc())
                )
            ).scalars()
        return [await self.get_run(run_id) for run_id in run_ids]

    async def pause_run(self, run_id: str) -> dict[str, object]:
        return await self._control(run_id, expected="monitoring", target="paused", event_type="paused")

    async def resume_run(self, run_id: str) -> dict[str, object]:
        return await self._control(run_id, expected="paused", target="monitoring", event_type="resumed")

    async def stop_run(self, run_id: str) -> dict[str, object]:
        normalized = str(run_id or "").strip()
        async with self._run_lock(normalized) as connection:
            factory = sessionmaker(bind=connection, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session, session.begin():
                run = await self._locked_run(session, normalized)
                if run.status not in {"monitoring", "paused"}:
                    raise KalshiPaperTestRunTransition("only monitoring or paused runs can be stopped")
                remaining, realized = await self._position_projection(factory, normalized)
                self._append_event(
                    session,
                    run,
                    event_type="stopped",
                    remaining_quantity=remaining,
                    realized_pnl=realized,
                    reason="operator_stopped",
                )
                run.status = "stopped"
                run.last_reason = "operator_stopped"
        return await self.get_run(normalized)

    async def _recover_start(self, run_id: str) -> None:
        async with self._session_factory() as session:
            run = await session.get(KalshiPaperTestRun, run_id)
            if run is None or run.status != "starting":
                return
            facts = {
                "account_id": run.account_id,
                "decision_id": run.entry_decision_id,
                "opportunity_id": run.opportunity_id,
                "opportunity_revision": run.opportunity_revision,
                "action": "execute",
                "quantity": decimal_string(cast(Decimal, run.quantity), scale=2),
                "limit_price": decimal_string(cast(Decimal, run.entry_limit_price), scale=6),
                "time_in_force": "immediate_or_cancel",
            }
        try:
            decision = await self._paper.record_decision(**facts)
        except (PaperDecisionConflict, PaperOpportunityIneligible, PaperOpportunityNotFound) as exc:
            await self._block_run(
                self._session_factory,
                run_id,
                f"entry_recovery_permanent_failure:{type(exc).__name__}",
            )
            return
        async with self._session_factory() as session, session.begin():
            run = await self._locked_run(session, run_id)
            if run.status != "starting":
                return
            prior = await session.scalar(
                select(KalshiPaperTestEvent).where(
                    KalshiPaperTestEvent.run_id == run_id,
                    KalshiPaperTestEvent.event_type.in_(("entry_filled", "entry_unfilled")),
                )
            )
            if prior is not None:
                run.status = "monitoring" if prior.event_type == "entry_filled" else "entry_unfilled"
                run.position_id = prior.position_id
                return
            if Decimal(str(decision["filled_quantity"])) > 0:
                position = await session.scalar(
                    select(KalshiPaperPosition).where(
                        KalshiPaperPosition.account_id == run.account_id,
                        KalshiPaperPosition.entry_decision_id == run.entry_decision_id,
                    )
                )
                if position is None:
                    await self._append_blocked(session, run, "entry_position_causality_missing")
                    return
                run.position_id = position.position_id
                run.status = "monitoring"
                run.last_reason = "entry_filled"
                self._append_event(
                    session,
                    run,
                    event_type="entry_filled",
                    position_id=position.position_id,
                    remaining_quantity=cast(Decimal, position.entry_quantity),
                    reason=str(decision["reason"]),
                )
            else:
                run.status = "entry_unfilled"
                run.last_reason = str(decision["reason"])
                self._append_event(
                    session,
                    run,
                    event_type="entry_unfilled",
                    remaining_quantity=Decimal("0"),
                    reason=str(decision["reason"]),
                )

    async def _pending_trigger(
        self,
        factory: sessionmaker | Callable[[], AsyncSession],
        run_id: str,
    ) -> KalshiPaperTestEvent | None:
        async with factory() as session:
            triggers = (
                await session.execute(
                    select(KalshiPaperTestEvent)
                    .where(
                        KalshiPaperTestEvent.run_id == run_id,
                        KalshiPaperTestEvent.event_type.in_(("take_profit_triggered", "stop_loss_triggered")),
                    )
                    .order_by(KalshiPaperTestEvent.sequence)
                )
            ).scalars()
            for trigger in triggers:
                terminal = await session.scalar(
                    select(KalshiPaperTestEvent.sequence).where(
                        KalshiPaperTestEvent.run_id == run_id,
                        KalshiPaperTestEvent.exit_decision_id == trigger.exit_decision_id,
                        KalshiPaperTestEvent.event_type.in_(("exit_filled", "exit_partial", "exit_no_fill")),
                    )
                )
                if terminal is None:
                    return trigger
        return None

    async def _finish_exit_attempt(
        self,
        factory: sessionmaker | Callable[[], AsyncSession],
        trigger: KalshiPaperTestEvent,
    ) -> None:
        async with factory() as session:
            run = await session.get(KalshiPaperTestRun, trigger.run_id)
            if run is None or run.status != "monitoring":
                return
            remaining, _ = await self._position_projection(factory, trigger.run_id)
            minimum = (
                cast(Decimal, run.take_profit_price)
                if trigger.event_type == "take_profit_triggered"
                else cast(Decimal, run.stop_loss_minimum_price)
            )
            facts = {
                "account_id": run.account_id,
                "decision_id": str(trigger.exit_decision_id),
                "position_id": str(run.position_id),
                # The trigger freezes the exact attempted quantity.  Replaying with the
                # current projection after a response-loss partial fill would conflict
                # with the already-persisted paper intent.
                "quantity": decimal_string(cast(Decimal, trigger.remaining_quantity), scale=2),
                "minimum_price": decimal_string(minimum, scale=6),
            }
        try:
            decision = await self._paper.record_exit(**facts)
        except PaperPositionNotClosable:
            remaining, realized = await self._position_projection(factory, trigger.run_id)
            if remaining == Decimal("0"):
                await self._complete_without_exit(factory, trigger.run_id, "position_closed_by_competing_path")
                return
            async with factory() as session, session.begin():
                run = await self._locked_run(session, trigger.run_id)
                self._append_event(
                    session,
                    run,
                    event_type="exit_no_fill",
                    exit_decision_id=trigger.exit_decision_id,
                    position_id=run.position_id,
                    remaining_quantity=remaining,
                    realized_pnl=realized,
                    reason="position_changed_before_exit",
                )
            return
        remaining, realized = await self._position_projection(factory, trigger.run_id)
        filled = Decimal(str(decision["filled_quantity"]))
        event_type = "exit_no_fill" if filled == 0 else ("exit_filled" if remaining == 0 else "exit_partial")
        async with factory() as session, session.begin():
            run = await self._locked_run(session, trigger.run_id)
            existing = await session.scalar(
                select(KalshiPaperTestEvent).where(
                    KalshiPaperTestEvent.run_id == trigger.run_id,
                    KalshiPaperTestEvent.event_type == event_type,
                    KalshiPaperTestEvent.exit_decision_id == trigger.exit_decision_id,
                )
            )
            if existing is None:
                self._append_event(
                    session,
                    run,
                    event_type=event_type,
                    exit_decision_id=trigger.exit_decision_id,
                    position_id=run.position_id,
                    remaining_quantity=remaining,
                    realized_pnl=realized,
                    reason=str(decision["reason"]),
                )
            if remaining == 0:
                self._append_event(
                    session,
                    run,
                    event_type="completed",
                    remaining_quantity=remaining,
                    realized_pnl=realized,
                    reason="position_fully_closed",
                )
                run.status = "completed"
                run.last_reason = "position_fully_closed"

    async def _complete_without_exit(self, factory, run_id: str, reason: str) -> None:  # noqa: ANN001
        remaining, realized = await self._position_projection(factory, run_id)
        async with factory() as session, session.begin():
            run = await self._locked_run(session, run_id)
            self._append_event(
                session,
                run,
                event_type="completed",
                remaining_quantity=remaining,
                realized_pnl=realized,
                reason=reason,
            )
            run.status = "completed"
            run.last_reason = reason

    async def _position_projection(self, factory, run_id: str) -> tuple[Decimal, Decimal]:  # noqa: ANN001
        async with factory() as session:
            run = await session.get(KalshiPaperTestRun, run_id)
            if run is None or run.position_id is None:
                return Decimal("0"), Decimal("0")
            position = await session.get(KalshiPaperPosition, (run.account_id, run.position_id))
            if position is None:
                raise KalshiPaperTestRunConflict("run position evidence is missing")
            sold = cast(
                Decimal,
                await session.scalar(
                    select(func.coalesce(func.sum(KalshiPaperDecision.filled_quantity), Decimal("0"))).where(
                        KalshiPaperDecision.account_id == run.account_id,
                        KalshiPaperDecision.position_id == run.position_id,
                        KalshiPaperDecision.order_side == "sell",
                    )
                ),
            )
            realized = cast(
                Decimal,
                await session.scalar(
                    select(func.coalesce(func.sum(KalshiPaperDecision.realized_pnl), Decimal("0"))).where(
                        KalshiPaperDecision.account_id == run.account_id,
                        KalshiPaperDecision.position_id == run.position_id,
                        KalshiPaperDecision.order_side == "sell",
                    )
                ),
            )
            entry_units = _to_scaled_int(cast(Decimal, position.entry_quantity), scale=2, field="entry_quantity")
            sold_units = _to_scaled_int(sold, scale=2, field="sold_quantity")
            return _from_scaled_int(entry_units - sold_units, scale=2), realized

    async def _control(self, run_id: str, *, expected: str, target: str, event_type: str) -> dict[str, object]:
        normalized = str(run_id or "").strip()
        async with self._run_lock(normalized) as connection:
            factory = sessionmaker(bind=connection, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session, session.begin():
                run = await self._locked_run(session, normalized)
                if run.status != expected:
                    raise KalshiPaperTestRunTransition(f"run must be {expected} to transition to {target}")
                remaining, realized = await self._position_projection(factory, normalized)
                self._append_event(
                    session,
                    run,
                    event_type=event_type,
                    remaining_quantity=remaining,
                    realized_pnl=realized,
                    reason=f"operator_{event_type}",
                )
                run.status = target
                run.last_reason = f"operator_{event_type}"
        return await self.get_run(normalized)

    async def _block_run(self, factory, run_id: str, reason: str) -> None:  # noqa: ANN001
        async with factory() as session, session.begin():
            run = await self._locked_run(session, run_id)
            await self._append_blocked(session, run, reason)

    async def _append_blocked(self, session: AsyncSession, run: KalshiPaperTestRun, reason: str) -> None:
        self._append_event(session, run, event_type="blocked", reason=reason)
        run.status = "blocked"
        run.last_error = reason
        run.last_reason = reason

    async def _locked_run(self, session: AsyncSession, run_id: str) -> KalshiPaperTestRun:
        run = (
            await session.execute(
                select(KalshiPaperTestRun)
                .where(KalshiPaperTestRun.run_id == run_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if run is None:
            raise KalshiPaperTestRunNotFound("paper test run not found")
        return run

    def _append_event(self, session: AsyncSession, run: KalshiPaperTestRun, *, event_type: str, **values) -> None:  # noqa: ANN003
        sequence = int(run.next_event_sequence)
        session.add(
            KalshiPaperTestEvent(
                run_id=run.run_id,
                sequence=sequence,
                account_id=run.account_id,
                event_type=event_type,
                created_at=self._utcnow(),
                **values,
            )
        )
        run.next_event_sequence = sequence + 1
        run.updated_at = self._utcnow()

    @asynccontextmanager
    async def _run_lock(self, run_id: str) -> AsyncIterator[AsyncConnection]:
        digest = hashlib.sha256(f"kalshi-paper-test-run\0{run_id}".encode("utf-8")).digest()
        lock_key = int.from_bytes(digest[:8], byteorder="big", signed=True)
        async with self._database_engine.connect() as connection:
            acquired = False
            try:
                await connection.execute(text("SET SESSION idle_session_timeout = 0"))
                await connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
                await connection.commit()
                acquired = True
                yield connection
            finally:
                async def cleanup() -> None:
                    try:
                        if connection.in_transaction():
                            await connection.rollback()
                        if acquired:
                            await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
                            await connection.commit()
                    finally:
                        await connection.invalidate()

                task = asyncio.create_task(cleanup())
                cancelled = False
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        cancelled = True
                task.result()
                if cancelled:
                    raise asyncio.CancelledError

    def _serialize_run(
        self,
        run: KalshiPaperTestRun,
        *,
        remaining: Decimal,
        realized_pnl: Decimal,
    ) -> dict[str, object]:
        return {
            "run_id": run.run_id,
            "account_id": run.account_id,
            "opportunity_id": run.opportunity_id,
            "opportunity_revision": run.opportunity_revision,
            "ticker": run.ticker,
            "outcome": run.outcome,
            "quantity": decimal_string(cast(Decimal, run.quantity), scale=2),
            "entry_limit_price": decimal_string(cast(Decimal, run.entry_limit_price), scale=6),
            "take_profit_price": decimal_string(cast(Decimal, run.take_profit_price), scale=6),
            "stop_loss_price": decimal_string(cast(Decimal, run.stop_loss_price), scale=6),
            "stop_loss_minimum_price": decimal_string(cast(Decimal, run.stop_loss_minimum_price), scale=6),
            "entry_decision_id": run.entry_decision_id,
            "position_id": run.position_id,
            "status": run.status,
            "next_event_sequence": int(run.next_event_sequence),
            "remaining_quantity": decimal_string(remaining, scale=2),
            "realized_pnl": decimal_string(realized_pnl, scale=18),
            "last_reason": run.last_reason,
            "last_error": run.last_error,
            "created_at": run.created_at.isoformat(),
            "updated_at": run.updated_at.isoformat(),
        }

    def _serialize_event(self, event: KalshiPaperTestEvent) -> dict[str, object]:
        return {
            "run_id": event.run_id,
            "sequence": int(event.sequence),
            "account_id": event.account_id,
            "event_type": event.event_type,
            "position_id": event.position_id,
            "best_bid": decimal_string(cast(Decimal, event.best_bid), scale=6) if event.best_bid is not None else None,
            "trigger_price": (
                decimal_string(cast(Decimal, event.trigger_price), scale=6)
                if event.trigger_price is not None
                else None
            ),
            "exit_decision_id": event.exit_decision_id,
            "market_observed_at": event.market_observed_at.isoformat() if event.market_observed_at else None,
            "book_observed_at": event.book_observed_at.isoformat() if event.book_observed_at else None,
            "quote_evidence_hash": event.quote_evidence_hash,
            "quote_evidence_json": event.quote_evidence_json,
            "remaining_quantity": (
                decimal_string(cast(Decimal, event.remaining_quantity), scale=2)
                if event.remaining_quantity is not None
                else None
            ),
            "realized_pnl": (
                decimal_string(cast(Decimal, event.realized_pnl), scale=18)
                if event.realized_pnl is not None
                else None
            ),
            "reason": event.reason,
            "created_at": event.created_at.isoformat(),
        }

    def _utcnow(self) -> datetime:
        return self._now().astimezone(timezone.utc)
