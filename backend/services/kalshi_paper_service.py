from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol, cast

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import sessionmaker

from models.database import (
    KalshiPaperAccount,
    KalshiPaperCancellation,
    KalshiPaperDecision,
    KalshiPaperFill,
    KalshiPaperIntent,
    KalshiPaperOrder,
    KalshiPaperOrderEvent,
    KalshiPaperPosition,
    OpportunityState,
)
from services.kalshi_paper_execution import (
    MAX_SOURCE_AGE_SECONDS,
    KalshiPaperMarketDataClient,
    KalshiPaperProtocolError,
    PaperFillResult,
    PaperQuote,
    _from_scaled_int,
    _to_scaled_int,
    decimal_string,
    parse_price,
    parse_quantity,
    simulate_buy_ioc,
    simulate_sell_ioc,
)

Action = Literal["execute", "pass"]
TimeInForce = Literal["immediate_or_cancel", "good_till_canceled"]
_DECIMAL_PATTERN = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d+)?$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class PaperDecisionConflict(RuntimeError):
    pass


class PaperAccountNotFound(LookupError):
    pass


class PaperOpportunityNotFound(LookupError):
    pass


class PaperOpportunityIneligible(ValueError):
    pass


class PaperCancellationConflict(RuntimeError):
    pass


class PaperOrderNotCancelable(RuntimeError):
    pass


class PaperPositionNotFound(LookupError):
    pass


class PaperPositionNotClosable(RuntimeError):
    pass


class _MarketDataCapability(Protocol):
    async def fetch_quote(self, ticker: str) -> PaperQuote: ...


@dataclass(frozen=True)
class _ResolvedOpportunity:
    opportunity_id: str
    stable_id: str
    strategy_key: str
    strategy_version: int | None
    ticker: str
    outcome: Literal["yes", "no"]
    snapshot_json: str
    revision: str


@dataclass(frozen=True)
class _PreparedDecision:
    quote: PaperQuote | None
    result: PaperFillResult | None
    status: str
    reason: str


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_cash(value: object, *, field: str) -> Decimal:
    if not isinstance(value, str) or not _DECIMAL_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a finite decimal string") from exc
    exponent = parsed.as_tuple().exponent
    if not parsed.is_finite() or not isinstance(exponent, int):
        raise ValueError(f"{field} must be a finite decimal string")
    if exponent < -18:
        raise ValueError(f"{field} has more than 18 decimal places")
    if parsed < 0:
        raise ValueError(f"{field} must be non-negative")
    return parsed


def _money18(value: Decimal) -> str:
    return decimal_string(value, scale=18)


def _decision_lock_key(account_id: str, decision_id: str) -> int:
    digest = hashlib.sha256(f"{account_id}\0{decision_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def _finish_cleanup_before_cancellation(cleanup_task: asyncio.Task[None]) -> None:
    cancelled = False
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            cancelled = True
    cleanup_task.result()
    if cancelled:
        raise asyncio.CancelledError


class KalshiPaperService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker | Callable[[], AsyncSession],
        database_engine: AsyncEngine,
        market_data_client: _MarketDataCapability | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._database_engine = database_engine
        self._market_data = market_data_client or KalshiPaperMarketDataClient()
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def create_account(self, *, name: str, starting_cash: str) -> dict[str, object]:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("name is required")
        cash = _parse_cash(starting_cash, field="starting_cash")
        now = self._now().astimezone(timezone.utc)
        account = KalshiPaperAccount(
            id=str(uuid.uuid4()),
            name=normalized_name,
            currency="USD",
            starting_cash=cash,
            cash_balance=cash,
            reserved_cash=Decimal("0"),
            journal_sequence=0,
            created_at=now,
            updated_at=now,
        )
        async with self._session_factory() as session, session.begin():
            session.add(account)
        return self._serialize_account(account)

    async def list_accounts(self) -> list[dict[str, object]]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(select(KalshiPaperAccount).order_by(KalshiPaperAccount.created_at.asc()))
            ).scalars()
            return [self._serialize_account(row) for row in rows]

    async def get_eligibility(self, opportunity_id: str) -> dict[str, object]:
        resolved = await self._resolve_opportunity(opportunity_id)
        return {
            "opportunity_id": resolved.opportunity_id,
            "opportunity_stable_id": resolved.stable_id,
            "opportunity_revision": resolved.revision,
            "strategy_key": resolved.strategy_key,
            "strategy_version": resolved.strategy_version,
            "ticker": resolved.ticker,
            "outcome": resolved.outcome,
            "order_side": "buy",
            "time_in_force": "immediate_or_cancel",
        }

    async def list_decisions(self, *, account_id: str, limit: int = 50) -> list[dict[str, object]]:
        async with self._session_factory() as session:
            account = await session.get(KalshiPaperAccount, account_id)
            if account is None:
                raise PaperAccountNotFound("paper account not found")
            decisions = (
                await session.execute(
                    select(KalshiPaperDecision)
                    .where(KalshiPaperDecision.account_id == account_id)
                    .order_by(KalshiPaperDecision.account_sequence.desc())
                    .limit(limit)
                )
            ).scalars()
            return [await self._serialize_decision(session, decision) for decision in decisions]

    async def list_orders(self, *, account_id: str, limit: int = 100) -> list[dict[str, object]]:
        async with self._session_factory() as session:
            if await session.get(KalshiPaperAccount, account_id) is None:
                raise PaperAccountNotFound("paper account not found")
            orders = (
                await session.execute(
                    select(KalshiPaperOrder)
                    .where(KalshiPaperOrder.account_id == account_id)
                    .order_by(KalshiPaperOrder.created_at.desc())
                    .limit(limit)
                )
            ).scalars()
            return [await self._serialize_order(session, order) for order in orders]

    async def list_positions(self, *, account_id: str, limit: int = 100) -> list[dict[str, object]]:
        async with self._session_factory() as session:
            if await session.get(KalshiPaperAccount, account_id) is None:
                raise PaperAccountNotFound("paper account not found")
            positions = (
                await session.execute(
                    select(KalshiPaperPosition)
                    .where(KalshiPaperPosition.account_id == account_id)
                    .order_by(KalshiPaperPosition.created_at.desc())
                    .limit(limit)
                )
            ).scalars()
            return [await self._serialize_position(session, position) for position in positions]

    async def cancel_order(
        self,
        *,
        account_id: str,
        order_id: str,
        cancellation_id: str,
    ) -> dict[str, object]:
        normalized_account_id = str(account_id or "").strip()
        normalized_order_id = str(order_id or "").strip()
        normalized_cancellation_id = str(cancellation_id or "").strip()
        if not normalized_account_id or not normalized_order_id or not normalized_cancellation_id:
            raise ValueError("account_id, order_id, and cancellation_id are required")

        async with self._session_factory() as session, session.begin():
            account = (
                await session.execute(
                    select(KalshiPaperAccount)
                    .where(KalshiPaperAccount.id == normalized_account_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if account is None:
                raise PaperAccountNotFound("paper account not found")

            existing = await session.get(
                KalshiPaperCancellation,
                (normalized_account_id, normalized_cancellation_id),
            )
            if existing is not None:
                if existing.order_id != normalized_order_id:
                    raise PaperCancellationConflict(
                        "cancellation_id already exists for a different immutable order target"
                    )
                return self._serialize_cancellation(existing)

            order = (
                await session.execute(
                    select(KalshiPaperOrder)
                    .where(
                        KalshiPaperOrder.account_id == normalized_account_id,
                        KalshiPaperOrder.order_id == normalized_order_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if order is None:
                raise PaperOrderNotCancelable("paper order is not cancelable")
            prior = await session.scalar(
                select(KalshiPaperCancellation).where(
                    KalshiPaperCancellation.account_id == normalized_account_id,
                    KalshiPaperCancellation.order_id == normalized_order_id,
                )
            )
            reservation = cast(Decimal, order.reserved_cash)
            if prior is not None or cast(Decimal, order.open_quantity) <= 0 or reservation <= 0:
                raise PaperOrderNotCancelable("paper order is already terminal and not cancelable")

            reserved_units = _to_scaled_int(cast(Decimal, account.reserved_cash), scale=18, field="reserved_cash")
            release_units = _to_scaled_int(reservation, scale=18, field="order.reserved_cash")
            if release_units > reserved_units:
                raise RuntimeError("paper account reservation contradicts immutable order evidence")
            now = self._now().astimezone(timezone.utc)
            cancellation = KalshiPaperCancellation(
                account_id=normalized_account_id,
                cancellation_id=normalized_cancellation_id,
                order_id=normalized_order_id,
                released_cash=reservation,
                status="cancelled",
                created_at=now,
            )
            session.add(cancellation)
            session.add(
                KalshiPaperOrderEvent(
                    account_id=normalized_account_id,
                    order_id=normalized_order_id,
                    sequence=2,
                    event_type="cancelled",
                    cancellation_id=normalized_cancellation_id,
                    quantity=cast(Decimal, order.open_quantity),
                    reserved_cash=reservation,
                    created_at=now,
                )
            )
            account.reserved_cash = _from_scaled_int(reserved_units - release_units, scale=18)
            account.updated_at = now
            await session.flush()
            return self._serialize_cancellation(cancellation)

    async def record_exit(
        self,
        *,
        account_id: str,
        decision_id: str,
        position_id: str,
        quantity: str,
        minimum_price: str,
    ) -> dict[str, object]:
        normalized_account_id = str(account_id or "").strip()
        normalized_decision_id = str(decision_id or "").strip()
        normalized_position_id = str(position_id or "").strip()
        if not normalized_account_id or not normalized_decision_id or not normalized_position_id:
            raise ValueError("account_id, decision_id, and position_id are required")
        requested_quantity = parse_quantity(quantity)
        parsed_minimum_price = parse_price(minimum_price, field="minimum_price")
        canonical_request = _canonical_json(
            {
                "account_id": normalized_account_id,
                "decision_id": normalized_decision_id,
                "position_id": normalized_position_id,
                "action": "execute",
                "order_side": "sell",
                "time_in_force": "immediate_or_cancel",
                "quantity": decimal_string(requested_quantity, scale=2),
                "minimum_price": decimal_string(parsed_minimum_price, scale=6),
            }
        )
        request_hash = _sha256(canonical_request)
        existing = await self._read_existing(
            account_id=normalized_account_id,
            decision_id=normalized_decision_id,
            request_hash=request_hash,
        )
        if existing is not None:
            return existing

        decision_lock = _decision_lock_key(normalized_account_id, normalized_decision_id)
        position_lock = _decision_lock_key(normalized_account_id, f"position:{normalized_position_id}")
        lock_keys = [decision_lock] if decision_lock == position_lock else [decision_lock, position_lock]
        async with self._database_engine.connect() as connection:
            acquired: list[int] = []
            try:
                for lock_key in lock_keys:
                    await connection.execute(text("SELECT pg_advisory_lock(:lock_key)"), {"lock_key": lock_key})
                    acquired.append(lock_key)
                await connection.commit()

                bound_factory = sessionmaker(bind=connection, class_=AsyncSession, expire_on_commit=False)
                resumed_intent = False
                resolved: _ResolvedOpportunity
                async with bound_factory() as session, session.begin():
                    decision = await session.get(
                        KalshiPaperDecision,
                        (normalized_account_id, normalized_decision_id),
                        populate_existing=True,
                    )
                    if decision is not None:
                        if decision.request_hash != request_hash:
                            raise PaperDecisionConflict("decision_id already exists with different immutable request facts")
                        return await self._serialize_decision(session, decision)
                    intent = await session.get(
                        KalshiPaperIntent,
                        (normalized_account_id, normalized_decision_id),
                        populate_existing=True,
                    )
                    if intent is not None:
                        if intent.request_hash != request_hash:
                            raise PaperDecisionConflict("decision_id already exists with different immutable request facts")
                        resumed_intent = True
                        resolved = self._resolved_from_intent(intent)
                    else:
                        if await session.get(KalshiPaperAccount, normalized_account_id) is None:
                            raise PaperAccountNotFound("paper account not found")
                        position = await session.get(
                            KalshiPaperPosition,
                            (normalized_account_id, normalized_position_id),
                        )
                        if position is None:
                            raise PaperPositionNotFound("paper position not found")
                        entry = await session.get(
                            KalshiPaperDecision,
                            (normalized_account_id, position.entry_decision_id),
                        )
                        if entry is None:
                            raise PaperPositionNotFound("paper position entry evidence not found")
                        unresolved_exit_id = await session.scalar(
                            select(KalshiPaperIntent.decision_id)
                            .where(
                                KalshiPaperIntent.account_id == normalized_account_id,
                                KalshiPaperIntent.position_id == normalized_position_id,
                                KalshiPaperIntent.order_side == "sell",
                                KalshiPaperIntent.decision_id.not_in(
                                    select(KalshiPaperDecision.decision_id).where(
                                        KalshiPaperDecision.account_id == normalized_account_id
                                    )
                                ),
                            )
                            .limit(1)
                        )
                        if unresolved_exit_id is not None:
                            raise PaperPositionNotClosable(
                                f"paper position has unresolved exit intent {unresolved_exit_id}; retry it first"
                            )
                        sold_quantity = cast(
                            Decimal,
                            await session.scalar(
                                select(func.coalesce(func.sum(KalshiPaperDecision.filled_quantity), Decimal("0"))).where(
                                    KalshiPaperDecision.account_id == normalized_account_id,
                                    KalshiPaperDecision.position_id == normalized_position_id,
                                    KalshiPaperDecision.order_side == "sell",
                                )
                            ),
                        )
                        entry_quantity_units = _to_scaled_int(
                            cast(Decimal, position.entry_quantity), scale=2, field="entry_quantity"
                        )
                        sold_quantity_units = _to_scaled_int(
                            sold_quantity, scale=2, field="sold_quantity"
                        )
                        remaining_units = entry_quantity_units - sold_quantity_units
                        if remaining_units <= 0:
                            raise PaperPositionNotClosable("paper position is already closed")
                        if _to_scaled_int(requested_quantity, scale=2, field="requested_quantity") > remaining_units:
                            raise PaperPositionNotClosable("exit quantity exceeds the remaining paper position")
                        resolved = _ResolvedOpportunity(
                            opportunity_id=entry.opportunity_id,
                            stable_id=entry.opportunity_stable_id,
                            strategy_key=entry.strategy_key,
                            strategy_version=entry.strategy_version,
                            ticker=position.ticker,
                            outcome=cast(Literal["yes", "no"], position.outcome),
                            snapshot_json=entry.opportunity_snapshot_json,
                            revision=entry.opportunity_revision,
                        )
                        session.add(
                            KalshiPaperIntent(
                                account_id=normalized_account_id,
                                decision_id=normalized_decision_id,
                                request_hash=request_hash,
                                action="execute",
                                opportunity_id=resolved.opportunity_id,
                                opportunity_stable_id=resolved.stable_id,
                                opportunity_revision=resolved.revision,
                                opportunity_snapshot_json=resolved.snapshot_json,
                                strategy_key=resolved.strategy_key,
                                strategy_version=resolved.strategy_version,
                                ticker=resolved.ticker,
                                outcome=resolved.outcome,
                                order_side="sell",
                                position_id=normalized_position_id,
                                time_in_force="immediate_or_cancel",
                                requested_quantity=requested_quantity,
                                limit_price=parsed_minimum_price,
                                created_at=self._now().astimezone(timezone.utc),
                            )
                        )

                if resumed_intent:
                    prepared = _PreparedDecision(
                        quote=None,
                        result=None,
                        status="rejected",
                        reason="incomplete_exit_intent_rejected_after_restart",
                    )
                else:
                    quote = await self._market_data.fetch_quote(resolved.ticker)
                    if quote.market.ticker != resolved.ticker or quote.book.ticker != resolved.ticker:
                        raise KalshiPaperProtocolError("paper quote ticker does not match the held position")
                    result = simulate_sell_ioc(
                        book=quote.book,
                        outcome=resolved.outcome,
                        quantity=requested_quantity,
                        minimum_price=parsed_minimum_price,
                        price_ranges=quote.market.price_ranges,
                    )
                    prepared = _PreparedDecision(quote=quote, result=result, status=result.status, reason=result.reason)

                async with bound_factory() as session, session.begin():
                    return await self._commit_exit(
                        session=session,
                        account_id=normalized_account_id,
                        decision_id=normalized_decision_id,
                        position_id=normalized_position_id,
                        request_hash=request_hash,
                        resolved=resolved,
                        requested_quantity=requested_quantity,
                        minimum_price=parsed_minimum_price,
                        prepared=prepared,
                    )
            finally:
                async def release_and_discard_connection() -> None:
                    try:
                        if connection.in_transaction():
                            await connection.rollback()
                        for lock_key in reversed(acquired):
                            await connection.execute(
                                text("SELECT pg_advisory_unlock(:lock_key)"),
                                {"lock_key": lock_key},
                            )
                        if acquired:
                            await connection.commit()
                    finally:
                        await connection.invalidate()

                cleanup_task = asyncio.create_task(release_and_discard_connection())
                await _finish_cleanup_before_cancellation(cleanup_task)

    async def record_decision(
        self,
        *,
        account_id: str,
        decision_id: str,
        opportunity_id: str,
        opportunity_revision: str,
        action: Action,
        quantity: str | None = None,
        limit_price: str | None = None,
        time_in_force: TimeInForce = "immediate_or_cancel",
    ) -> dict[str, object]:
        normalized_account_id = str(account_id or "").strip()
        normalized_decision_id = str(decision_id or "").strip()
        normalized_opportunity_id = str(opportunity_id or "").strip()
        normalized_revision = str(opportunity_revision or "").strip().lower()
        if not normalized_account_id or not normalized_decision_id or not normalized_opportunity_id:
            raise ValueError("account_id, decision_id, and opportunity_id are required")
        if not _HASH_PATTERN.fullmatch(normalized_revision):
            raise ValueError("opportunity_revision must be a SHA-256 digest")
        if action not in {"execute", "pass"}:
            raise ValueError("action must be execute or pass")
        if time_in_force not in {"immediate_or_cancel", "good_till_canceled"}:
            raise ValueError("time_in_force must be immediate_or_cancel or good_till_canceled")

        parsed_quantity: Decimal | None = None
        parsed_limit: Decimal | None = None
        if action == "execute":
            parsed_quantity = parse_quantity(quantity, field="quantity")
            parsed_limit = parse_price(limit_price, field="limit_price")
            canonical_quantity = decimal_string(parsed_quantity, scale=2)
            canonical_limit = decimal_string(parsed_limit, scale=6)
        else:
            if quantity is not None or limit_price is not None:
                raise ValueError("pass decisions cannot include quantity or limit_price")
            if time_in_force != "immediate_or_cancel":
                raise ValueError("pass decisions cannot include good_till_canceled")
            canonical_quantity = None
            canonical_limit = None

        request_payload = {
            "account_id": normalized_account_id,
            "decision_id": normalized_decision_id,
            "opportunity_id": normalized_opportunity_id,
            "opportunity_revision": normalized_revision,
            "action": action,
            "quantity": canonical_quantity,
            "limit_price": canonical_limit,
        }
        if action == "execute" and time_in_force == "good_till_canceled":
            request_payload["time_in_force"] = time_in_force
        request_hash = _sha256(_canonical_json(request_payload))
        existing = await self._read_existing(
            account_id=normalized_account_id,
            decision_id=normalized_decision_id,
            request_hash=request_hash,
        )
        if existing is not None:
            return existing

        lock_key = _decision_lock_key(normalized_account_id, normalized_decision_id)
        async with self._database_engine.connect() as connection:
            lock_acquired = False
            try:
                await connection.execute(text("SET SESSION idle_session_timeout = 0"))
                await connection.execute(text("SELECT pg_advisory_lock(:lock_key)"), {"lock_key": lock_key})
                await connection.commit()
                lock_acquired = True

                async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                    async with session.begin():
                        decision = await session.get(
                            KalshiPaperDecision,
                            (normalized_account_id, normalized_decision_id),
                        )
                        if decision is not None:
                            if decision.request_hash != request_hash:
                                raise PaperDecisionConflict(
                                    "decision_id already exists with different immutable request facts"
                                )
                            return await self._serialize_decision(session, decision)

                        intent = await session.get(
                            KalshiPaperIntent,
                            (normalized_account_id, normalized_decision_id),
                        )
                        if intent is not None:
                            if intent.request_hash != request_hash:
                                raise PaperDecisionConflict(
                                    "decision_id already exists with different immutable request facts"
                                )
                            resolved = self._resolved_from_intent(intent)
                            prepared = _PreparedDecision(
                                quote=None,
                                result=None,
                                status="rejected",
                                reason="expired_after_restart",
                            )
                            return await self._commit_prepared(
                                session=session,
                                account_id=normalized_account_id,
                                decision_id=normalized_decision_id,
                                request_hash=request_hash,
                                action=action,
                                resolved=resolved,
                                requested_quantity=parsed_quantity,
                                limit_price=parsed_limit,
                                time_in_force=time_in_force,
                                prepared=prepared,
                            )

                        account = await session.get(KalshiPaperAccount, normalized_account_id)
                        if account is None:
                            raise PaperAccountNotFound("paper account not found")
                        resolved = await self._resolve_opportunity(normalized_opportunity_id, session=session)
                        if resolved.revision != normalized_revision:
                            raise PaperOpportunityIneligible("opportunity revision changed; refresh eligibility")
                        session.add(
                            KalshiPaperIntent(
                                account_id=normalized_account_id,
                                decision_id=normalized_decision_id,
                                request_hash=request_hash,
                                action=action,
                                opportunity_id=resolved.opportunity_id,
                                opportunity_stable_id=resolved.stable_id,
                                opportunity_revision=resolved.revision,
                                opportunity_snapshot_json=resolved.snapshot_json,
                                strategy_key=resolved.strategy_key,
                                strategy_version=resolved.strategy_version,
                                ticker=resolved.ticker,
                                outcome=resolved.outcome,
                                order_side=None,
                                position_id=None,
                                time_in_force=(
                                    "good_till_canceled"
                                    if action == "execute" and time_in_force == "good_till_canceled"
                                    else None
                                ),
                                requested_quantity=parsed_quantity,
                                limit_price=parsed_limit,
                                created_at=self._now().astimezone(timezone.utc),
                            )
                        )

                    if action == "pass":
                        prepared = _PreparedDecision(
                            quote=None,
                            result=None,
                            status="passed",
                            reason="operator_passed",
                        )
                    else:
                        assert parsed_quantity is not None and parsed_limit is not None
                        try:
                            quote = await self._market_data.fetch_quote(resolved.ticker)
                            if quote.market.ticker != resolved.ticker or quote.book.ticker != resolved.ticker:
                                raise KalshiPaperProtocolError("Kalshi quote ticker identity mismatch")
                            result = simulate_buy_ioc(
                                book=quote.book,
                                outcome=resolved.outcome,
                                quantity=parsed_quantity,
                                limit_price=parsed_limit,
                                price_ranges=quote.market.price_ranges,
                            )
                            if time_in_force == "good_till_canceled":
                                result = replace(
                                    result,
                                    reason={
                                        "displayed_depth_filled_ioc": "displayed_depth_filled_gtc_open",
                                        "displayed_depth_partially_filled_ioc": (
                                            "displayed_depth_partially_filled_gtc_open"
                                        ),
                                    }.get(result.reason, result.reason),
                                    formula_version="kalshi-complementary-depth-gtc-open-v1",
                                )
                            prepared = _PreparedDecision(
                                quote=quote,
                                result=result,
                                status=result.status,
                                reason=result.reason,
                            )
                        except KalshiPaperProtocolError:
                            prepared = _PreparedDecision(
                                quote=None,
                                result=None,
                                status="rejected",
                                reason="market_data_rejected",
                            )

                    async with session.begin():
                        return await self._commit_prepared(
                            session=session,
                            account_id=normalized_account_id,
                            decision_id=normalized_decision_id,
                            request_hash=request_hash,
                            action=action,
                            resolved=resolved,
                            requested_quantity=parsed_quantity,
                            limit_price=parsed_limit,
                            time_in_force=time_in_force,
                            prepared=prepared,
                        )
            finally:
                async def release_and_discard_connection() -> None:
                    if lock_acquired:
                        try:
                            if connection.in_transaction():
                                await connection.rollback()
                            await connection.execute(
                                text("SELECT pg_advisory_unlock(:lock_key)"),
                                {"lock_key": lock_key},
                            )
                            await connection.commit()
                        finally:
                            await connection.invalidate()
                    else:
                        await connection.invalidate()

                cleanup_task = asyncio.create_task(release_and_discard_connection())
                await _finish_cleanup_before_cancellation(cleanup_task)

    async def _read_existing(
        self,
        *,
        account_id: str,
        decision_id: str,
        request_hash: str,
    ) -> dict[str, object] | None:
        async with self._session_factory() as session:
            decision = await session.get(KalshiPaperDecision, (account_id, decision_id))
            if decision is None:
                return None
            if decision.request_hash != request_hash:
                raise PaperDecisionConflict("decision_id already exists with different immutable request facts")
            return await self._serialize_decision(session, decision)

    async def _resolve_opportunity(
        self,
        opportunity_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> _ResolvedOpportunity:
        requested = str(opportunity_id or "").strip()
        if not requested:
            raise PaperOpportunityNotFound("opportunity not found")
        if session is None:
            async with self._session_factory() as owned_session:
                return await self._resolve_opportunity(requested, session=owned_session)
        states = (
            await session.execute(select(OpportunityState).where(OpportunityState.is_active.is_(True)))
        ).scalars()
        payload = None
        stable_id = None
        for state in states:
            candidate = state.opportunity_json
            if not isinstance(candidate, dict):
                continue
            if requested in {
                str(candidate.get("id") or ""),
                str(candidate.get("stable_id") or ""),
                state.stable_id,
            }:
                payload = candidate
                stable_id = state.stable_id
                break
        if payload is None or stable_id is None:
            raise PaperOpportunityNotFound("active opportunity not found")

        positions = payload.get("positions_to_take")
        if not isinstance(positions, list) or len(positions) != 1 or not isinstance(positions[0], dict):
            raise PaperOpportunityIneligible("paper execution requires exactly one position")
        position = positions[0]
        if str(position.get("platform") or "").strip().lower() != "kalshi":
            raise PaperOpportunityIneligible("paper execution requires one Kalshi position")
        if str(position.get("action") or "").strip().lower() != "buy":
            raise PaperOpportunityIneligible("paper execution is buy-only")
        outcome_text = str(position.get("outcome") or "").strip().lower()
        if outcome_text not in {"yes", "no"}:
            raise PaperOpportunityIneligible("paper execution requires a YES or NO outcome")
        outcome = cast(Literal["yes", "no"], outcome_text)
        ticker = str(position.get("ticker") or position.get("market_id") or "").strip().upper()
        if not ticker:
            raise PaperOpportunityIneligible("Kalshi ticker is missing")
        position_market_id = str(position.get("market_id") or ticker).strip().upper()
        if position_market_id != ticker:
            raise PaperOpportunityIneligible("Kalshi ticker and market identity disagree")

        markets = payload.get("markets")
        if not isinstance(markets, list) or len(markets) != 1 or not isinstance(markets[0], dict):
            raise PaperOpportunityIneligible("paper execution requires exactly one market")
        market = markets[0]
        if str(market.get("platform") or "").strip().lower() != "kalshi":
            raise PaperOpportunityIneligible("paper execution requires one Kalshi market")
        market_ticker = str(market.get("ticker") or market.get("id") or "").strip().upper()
        if market_ticker != ticker:
            raise PaperOpportunityIneligible("opportunity market and position ticker disagree")

        canonical_id = str(payload.get("id") or "").strip()
        canonical_stable_id = str(payload.get("stable_id") or stable_id).strip()
        strategy_key = str(payload.get("strategy") or "").strip()
        if not canonical_id or not canonical_stable_id or not strategy_key:
            raise PaperOpportunityIneligible("opportunity identity or strategy provenance is missing")
        raw_version = payload.get("revision")
        strategy_version = raw_version if isinstance(raw_version, int) and not isinstance(raw_version, bool) else None
        snapshot = {
            "opportunity_id": canonical_id,
            "opportunity_stable_id": canonical_stable_id,
            "strategy_key": strategy_key,
            "strategy_version": str(strategy_version) if strategy_version is not None else "",
            "title": str(payload.get("title") or ""),
            "description": str(payload.get("description") or ""),
            "platform": "kalshi",
            "ticker": ticker,
            "outcome": outcome,
            "action": "buy",
        }
        snapshot_json = _canonical_json(snapshot)
        return _ResolvedOpportunity(
            opportunity_id=canonical_id,
            stable_id=canonical_stable_id,
            strategy_key=strategy_key,
            strategy_version=strategy_version,
            ticker=ticker,
            outcome=outcome,
            snapshot_json=snapshot_json,
            revision=_sha256(snapshot_json),
        )

    @staticmethod
    def _resolved_from_intent(intent: KalshiPaperIntent) -> _ResolvedOpportunity:
        return _ResolvedOpportunity(
            opportunity_id=intent.opportunity_id,
            stable_id=intent.opportunity_stable_id,
            strategy_key=intent.strategy_key,
            strategy_version=intent.strategy_version,
            ticker=intent.ticker,
            outcome=cast(Literal["yes", "no"], intent.outcome),
            snapshot_json=intent.opportunity_snapshot_json,
            revision=intent.opportunity_revision,
        )

    async def _commit_exit(
        self,
        *,
        session: AsyncSession,
        account_id: str,
        decision_id: str,
        position_id: str,
        request_hash: str,
        resolved: _ResolvedOpportunity,
        requested_quantity: Decimal,
        minimum_price: Decimal,
        prepared: _PreparedDecision,
    ) -> dict[str, object]:
        account = (
            await session.execute(
                select(KalshiPaperAccount)
                .where(KalshiPaperAccount.id == account_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if account is None:
            raise PaperAccountNotFound("paper account not found")
        existing = await session.get(KalshiPaperDecision, (account_id, decision_id))
        if existing is not None:
            if existing.request_hash != request_hash:
                raise PaperDecisionConflict("decision_id already exists with different immutable request facts")
            return await self._serialize_decision(session, existing)
        position = (
            await session.execute(
                select(KalshiPaperPosition)
                .where(
                    KalshiPaperPosition.account_id == account_id,
                    KalshiPaperPosition.position_id == position_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if position is None:
            raise PaperPositionNotFound("paper position not found")

        prior_sold, prior_cost = (
            await session.execute(
                select(
                    func.coalesce(func.sum(KalshiPaperDecision.filled_quantity), Decimal("0")),
                    func.coalesce(func.sum(KalshiPaperDecision.position_cost_basis), Decimal("0")),
                ).where(
                    KalshiPaperDecision.account_id == account_id,
                    KalshiPaperDecision.position_id == position_id,
                    KalshiPaperDecision.order_side == "sell",
                )
            )
        ).one()
        prior_sold = cast(Decimal, prior_sold)
        prior_cost = cast(Decimal, prior_cost)
        position_quantity = cast(Decimal, position.entry_quantity)
        entry_quantity_units = _to_scaled_int(position_quantity, scale=2, field="entry_quantity")
        prior_sold_units = _to_scaled_int(prior_sold, scale=2, field="prior_sold_quantity")
        position_remaining_units = entry_quantity_units - prior_sold_units

        quote = prepared.quote
        fill_result = prepared.result
        status = prepared.status
        reason = prepared.reason
        fills = fill_result.fills if fill_result is not None else ()
        filled_quantity = fill_result.filled_quantity if fill_result is not None else Decimal("0")
        remaining_quantity = fill_result.remaining_quantity if fill_result is not None else requested_quantity
        average_fill_price = fill_result.average_fill_price if fill_result is not None else None
        notional = fill_result.notional if fill_result is not None else Decimal("0")
        fee = fill_result.fee if fill_result is not None else Decimal("0")
        now = self._now().astimezone(timezone.utc)
        quote_ages = (
            (
                Decimal(str((now - quote.market.observed_at).total_seconds())),
                Decimal(str((now - quote.book.observed_at).total_seconds())),
            )
            if quote is not None
            else ()
        )
        if _to_scaled_int(requested_quantity, scale=2, field="requested_quantity") > position_remaining_units:
            status = "rejected"
            reason = "exit_quantity_exceeds_remaining_position"
        elif quote is not None and any(
            age < -MAX_SOURCE_AGE_SECONDS or age > MAX_SOURCE_AGE_SECONDS for age in quote_ages
        ):
            status = "rejected"
            reason = "market_data_stale_before_commit"
        elif quote is not None and quote.market.fee_waiver_expiration <= now:
            status = "rejected"
            reason = "fee_waiver_expired_before_commit"
        if status == "rejected":
            fills = ()
            filled_quantity = Decimal("0")
            remaining_quantity = requested_quantity
            average_fill_price = None
            notional = Decimal("0")
            fee = Decimal("0")

        filled_units = _to_scaled_int(filled_quantity, scale=2, field="filled_quantity")
        sold_after_units = prior_sold_units + filled_units
        if sold_after_units > entry_quantity_units:
            raise PaperPositionNotClosable("exit would oversell the paper position")
        entry_total_units = _to_scaled_int(
            cast(Decimal, position.entry_notional), scale=18, field="entry_notional"
        ) + _to_scaled_int(cast(Decimal, position.entry_fee), scale=18, field="entry_fee")
        prior_cost_units = _to_scaled_int(prior_cost, scale=18, field="prior_position_cost_basis")
        if sold_after_units == entry_quantity_units:
            cumulative_cost_units = entry_total_units
        else:
            cumulative_cost_units = entry_total_units * sold_after_units // entry_quantity_units
        position_cost_units = cumulative_cost_units - prior_cost_units
        notional_units = _to_scaled_int(notional, scale=18, field="notional")
        fee_units = _to_scaled_int(fee, scale=18, field="fee")
        realized_pnl_units = notional_units - fee_units - position_cost_units
        position_cost_basis = _from_scaled_int(position_cost_units, scale=18)
        realized_pnl = _from_scaled_int(realized_pnl_units, scale=18)

        cash_before = cast(Decimal, account.cash_balance)
        cash_before_units = _to_scaled_int(cash_before, scale=18, field="cash_balance")
        cash_after = _from_scaled_int(cash_before_units + notional_units - fee_units, scale=18)
        account_sequence = int(account.journal_sequence) + 1
        fee_provenance_json = _canonical_json(dict(quote.market.fee_provenance)) if quote is not None else "{}"
        decision = KalshiPaperDecision(
            account_id=account_id,
            decision_id=decision_id,
            account_sequence=account_sequence,
            request_hash=request_hash,
            action="execute",
            opportunity_id=resolved.opportunity_id,
            opportunity_stable_id=resolved.stable_id,
            opportunity_revision=resolved.revision,
            opportunity_snapshot_json=resolved.snapshot_json,
            strategy_key=resolved.strategy_key,
            strategy_version=resolved.strategy_version,
            ticker=resolved.ticker,
            event_ticker=quote.market.event_ticker if quote is not None else None,
            outcome=resolved.outcome,
            order_side="sell",
            position_id=position_id,
            time_in_force="immediate_or_cancel",
            requested_quantity=requested_quantity,
            limit_price=minimum_price,
            status=status,
            reason=reason,
            source_origin=quote.book.source_origin if quote is not None else None,
            market_observed_at=quote.market.observed_at if quote is not None else None,
            market_fetched_at=quote.market.fetched_at if quote is not None else None,
            market_evidence_hash=quote.market.evidence_hash if quote is not None else None,
            market_evidence_json=quote.market.evidence_json if quote is not None else None,
            book_observed_at=quote.book.observed_at if quote is not None else None,
            book_fetched_at=quote.book.fetched_at if quote is not None else None,
            book_evidence_hash=quote.book.evidence_hash if quote is not None else None,
            book_evidence_json=quote.book.evidence_json if quote is not None else None,
            fill_formula_version=(fill_result.formula_version if fill_result is not None else "not_evaluated"),
            fee_rule_version=(quote.market.fee_rule_version if quote is not None else "not_evaluated"),
            fee_provenance_json=fee_provenance_json,
            filled_quantity=filled_quantity,
            remaining_quantity=remaining_quantity,
            average_fill_price=average_fill_price,
            notional=notional,
            fee=fee,
            position_cost_basis=position_cost_basis,
            realized_pnl=realized_pnl,
            cash_before=cash_before,
            cash_after=cash_after,
            created_at=now,
        )
        session.add(decision)
        for fill in fills:
            session.add(
                KalshiPaperFill(
                    account_id=account_id,
                    decision_id=decision_id,
                    sequence=fill.sequence,
                    quantity=fill.quantity,
                    price=fill.price,
                    notional=fill.notional,
                    fee=Decimal("0"),
                    source_bid_price=fill.source_bid_price,
                    source_side=fill.source_side,
                    evidence_json=_canonical_json(
                        {
                            "formula_version": fill_result.formula_version if fill_result is not None else "",
                            "quantity": decimal_string(fill.quantity, scale=2),
                            "price": decimal_string(fill.price, scale=6),
                            "notional": decimal_string(fill.notional, scale=8),
                            "fee": _money18(Decimal("0")),
                            "source_bid_price": decimal_string(fill.source_bid_price, scale=6),
                            "source_side": fill.source_side,
                            "book_evidence_hash": quote.book.evidence_hash if quote is not None else "",
                            "position_id": position_id,
                        }
                    ),
                    created_at=now,
                )
            )
        account.cash_balance = cash_after
        account.journal_sequence = account_sequence
        account.updated_at = now
        await session.flush()
        return await self._serialize_decision(session, decision)

    async def _commit_prepared(
        self,
        *,
        session: AsyncSession,
        account_id: str,
        decision_id: str,
        request_hash: str,
        action: Action,
        resolved: _ResolvedOpportunity,
        requested_quantity: Decimal | None,
        limit_price: Decimal | None,
        time_in_force: TimeInForce,
        prepared: _PreparedDecision,
    ) -> dict[str, object]:
        account = (
            await session.execute(
                select(KalshiPaperAccount)
                .where(KalshiPaperAccount.id == account_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if account is None:
            raise PaperAccountNotFound("paper account not found")
        existing = await session.get(KalshiPaperDecision, (account_id, decision_id))
        if existing is not None:
            if existing.request_hash != request_hash:
                raise PaperDecisionConflict("decision_id already exists with different immutable request facts")
            return await self._serialize_decision(session, existing)

        cash_before = cast(Decimal, account.cash_balance)
        cash_before_units = _to_scaled_int(cash_before, scale=18, field="cash_balance")
        reserved_before_units = _to_scaled_int(
            cast(Decimal, account.reserved_cash), scale=18, field="reserved_cash"
        )
        quote = prepared.quote
        fill_result = prepared.result
        status = prepared.status
        reason = prepared.reason
        fills = fill_result.fills if fill_result is not None else ()
        filled_quantity = fill_result.filled_quantity if fill_result is not None else Decimal("0")
        remaining_quantity = (
            fill_result.remaining_quantity
            if fill_result is not None
            else (requested_quantity if requested_quantity is not None else Decimal("0"))
        )
        average_fill_price = fill_result.average_fill_price if fill_result is not None else None
        notional = fill_result.notional if fill_result is not None else Decimal("0")
        fee = fill_result.fee if fill_result is not None else Decimal("0")
        now = self._now().astimezone(timezone.utc)
        quote_ages = (
            (
                Decimal(str((now - quote.market.observed_at).total_seconds())),
                Decimal(str((now - quote.book.observed_at).total_seconds())),
            )
            if quote is not None
            else ()
        )
        if quote is not None and any(
            age < -MAX_SOURCE_AGE_SECONDS or age > MAX_SOURCE_AGE_SECONDS for age in quote_ages
        ):
            status = "rejected"
            reason = "market_data_stale_before_commit"
            fills = ()
            filled_quantity = Decimal("0")
            remaining_quantity = requested_quantity or Decimal("0")
            average_fill_price = None
            notional = Decimal("0")
            fee = Decimal("0")
        elif quote is not None and quote.market.fee_waiver_expiration <= now:
            status = "rejected"
            reason = "fee_waiver_expired_before_commit"
            fills = ()
            filled_quantity = Decimal("0")
            remaining_quantity = requested_quantity or Decimal("0")
            average_fill_price = None
            notional = Decimal("0")
            fee = Decimal("0")

        notional_units = _to_scaled_int(notional, scale=18, field="notional")
        fee_units = _to_scaled_int(fee, scale=18, field="fee")
        reservation_units = 0
        creates_gtc_order = (
            action == "execute"
            and time_in_force == "good_till_canceled"
            and status in {"filled", "partial", "no_fill"}
            and requested_quantity is not None
            and limit_price is not None
        )
        if creates_gtc_order:
            open_quantity_units = _to_scaled_int(remaining_quantity, scale=2, field="open_quantity")
            limit_price_units = _to_scaled_int(limit_price, scale=6, field="limit_price")
            reservation_units = open_quantity_units * limit_price_units * 10**10
        available_cash_units = cash_before_units - reserved_before_units
        if (
            status in {"filled", "partial", "no_fill"}
            and notional_units + fee_units + reservation_units > available_cash_units
        ):
            status = "rejected"
            reason = "insufficient_paper_cash"
            creates_gtc_order = False
            fills = ()
            filled_quantity = Decimal("0")
            remaining_quantity = requested_quantity or Decimal("0")
            average_fill_price = None
            notional = Decimal("0")
            fee = Decimal("0")
            notional_units = 0
            fee_units = 0
            reservation_units = 0
        cash_after = _from_scaled_int(cash_before_units - notional_units - fee_units, scale=18)
        reservation_added = _from_scaled_int(reservation_units, scale=18)

        account_sequence = int(account.journal_sequence) + 1
        fee_provenance_json = _canonical_json(dict(quote.market.fee_provenance)) if quote is not None else "{}"
        decision = KalshiPaperDecision(
            account_id=account_id,
            decision_id=decision_id,
            account_sequence=account_sequence,
            request_hash=request_hash,
            action=action,
            opportunity_id=resolved.opportunity_id,
            opportunity_stable_id=resolved.stable_id,
            opportunity_revision=resolved.revision,
            opportunity_snapshot_json=resolved.snapshot_json,
            strategy_key=resolved.strategy_key,
            strategy_version=resolved.strategy_version,
            ticker=resolved.ticker,
            event_ticker=quote.market.event_ticker if quote is not None else None,
            outcome=resolved.outcome,
            order_side="buy" if action == "execute" else None,
            position_id=None,
            time_in_force=time_in_force if action == "execute" else None,
            requested_quantity=requested_quantity,
            limit_price=limit_price,
            status=status,
            reason=reason,
            source_origin=quote.book.source_origin if quote is not None else None,
            market_observed_at=quote.market.observed_at if quote is not None else None,
            market_fetched_at=quote.market.fetched_at if quote is not None else None,
            market_evidence_hash=quote.market.evidence_hash if quote is not None else None,
            market_evidence_json=quote.market.evidence_json if quote is not None else None,
            book_observed_at=quote.book.observed_at if quote is not None else None,
            book_fetched_at=quote.book.fetched_at if quote is not None else None,
            book_evidence_hash=quote.book.evidence_hash if quote is not None else None,
            book_evidence_json=quote.book.evidence_json if quote is not None else None,
            fill_formula_version=(fill_result.formula_version if fill_result is not None else "not_evaluated"),
            fee_rule_version=(quote.market.fee_rule_version if quote is not None else "not_evaluated"),
            fee_provenance_json=fee_provenance_json,
            filled_quantity=filled_quantity,
            remaining_quantity=remaining_quantity,
            average_fill_price=average_fill_price,
            notional=notional,
            fee=fee,
            position_cost_basis=None,
            realized_pnl=None,
            cash_before=cash_before,
            cash_after=cash_after,
            created_at=now,
        )
        session.add(decision)
        for fill in fills:
            session.add(
                KalshiPaperFill(
                    account_id=account_id,
                    decision_id=decision_id,
                    sequence=fill.sequence,
                    quantity=fill.quantity,
                    price=fill.price,
                    notional=fill.notional,
                    fee=Decimal("0"),
                    source_bid_price=fill.source_bid_price,
                    source_side=fill.source_side,
                    evidence_json=_canonical_json(
                        {
                            "formula_version": fill_result.formula_version if fill_result is not None else "",
                            "quantity": decimal_string(fill.quantity, scale=2),
                            "price": decimal_string(fill.price, scale=6),
                            "notional": decimal_string(fill.notional, scale=8),
                            "fee": _money18(Decimal("0")),
                            "source_bid_price": decimal_string(fill.source_bid_price, scale=6),
                            "source_side": fill.source_side,
                            "book_evidence_hash": quote.book.evidence_hash if quote is not None else "",
                        }
                    ),
                    created_at=now,
                )
            )
        if action == "execute" and status in {"filled", "partial"} and filled_quantity > 0:
            position_id = f"paper-position:{_sha256(account_id + chr(0) + decision_id)[:32]}"
            session.add(
                KalshiPaperPosition(
                    account_id=account_id,
                    position_id=position_id,
                    entry_decision_id=decision_id,
                    ticker=resolved.ticker,
                    outcome=resolved.outcome,
                    entry_quantity=filled_quantity,
                    entry_notional=notional,
                    entry_fee=fee,
                    created_at=now,
                )
            )
        if creates_gtc_order:
            order_id = f"paper-order:{_sha256(account_id + chr(0) + decision_id)[:32]}"
            order = KalshiPaperOrder(
                account_id=account_id,
                order_id=order_id,
                decision_id=decision_id,
                ticker=resolved.ticker,
                outcome=resolved.outcome,
                side="buy",
                time_in_force="good_till_canceled",
                requested_quantity=requested_quantity,
                filled_quantity=filled_quantity,
                open_quantity=remaining_quantity,
                limit_price=limit_price,
                decision_status=status,
                reserved_cash=reservation_added,
                created_at=now,
            )
            session.add(order)
            session.add(
                KalshiPaperOrderEvent(
                    account_id=account_id,
                    order_id=order_id,
                    sequence=1,
                    event_type="opened",
                    cancellation_id=None,
                    quantity=remaining_quantity,
                    reserved_cash=reservation_added,
                    created_at=now,
                )
            )
        account.cash_balance = cash_after
        account.reserved_cash = _from_scaled_int(reserved_before_units + reservation_units, scale=18)
        account.journal_sequence = account_sequence
        account.updated_at = now
        await session.flush()
        return await self._serialize_decision(session, decision)

    @staticmethod
    def _serialize_account(account: KalshiPaperAccount) -> dict[str, object]:
        cash_units = _to_scaled_int(cast(Decimal, account.cash_balance), scale=18, field="cash_balance")
        reserved_units = _to_scaled_int(cast(Decimal, account.reserved_cash), scale=18, field="reserved_cash")
        return {
            "id": account.id,
            "name": account.name,
            "currency": account.currency,
            "starting_cash": _money18(cast(Decimal, account.starting_cash)),
            "cash_balance": _money18(cast(Decimal, account.cash_balance)),
            "reserved_cash": _money18(cast(Decimal, account.reserved_cash)),
            "available_cash": _money18(_from_scaled_int(cash_units - reserved_units, scale=18)),
            "journal_sequence": int(account.journal_sequence),
            "created_at": account.created_at.isoformat(),
            "updated_at": account.updated_at.isoformat(),
        }

    @staticmethod
    async def _serialize_decision(session: AsyncSession, decision: KalshiPaperDecision) -> dict[str, object]:
        fills = (
            await session.execute(
                select(KalshiPaperFill)
                .where(
                    KalshiPaperFill.account_id == decision.account_id,
                    KalshiPaperFill.decision_id == decision.decision_id,
                )
                .order_by(KalshiPaperFill.sequence.asc())
            )
        ).scalars()
        fill_payloads = [
            {
                "sequence": int(fill.sequence),
                "quantity": decimal_string(cast(Decimal, fill.quantity), scale=2),
                "price": decimal_string(cast(Decimal, fill.price), scale=6),
                "notional": decimal_string(cast(Decimal, fill.notional), scale=8),
                "fee": _money18(cast(Decimal, fill.fee)),
                "source_bid_price": decimal_string(cast(Decimal, fill.source_bid_price), scale=6),
                "source_side": fill.source_side,
            }
            for fill in fills
        ]
        order = await session.scalar(
            select(KalshiPaperOrder).where(
                KalshiPaperOrder.account_id == decision.account_id,
                KalshiPaperOrder.decision_id == decision.decision_id,
            )
        )
        return {
            "account_id": decision.account_id,
            "decision_id": decision.decision_id,
            "account_sequence": int(decision.account_sequence),
            "action": decision.action,
            "opportunity_id": decision.opportunity_id,
            "opportunity_stable_id": decision.opportunity_stable_id,
            "opportunity_revision": decision.opportunity_revision,
            "strategy_key": decision.strategy_key,
            "strategy_version": decision.strategy_version,
            "ticker": decision.ticker,
            "event_ticker": decision.event_ticker,
            "outcome": decision.outcome,
            "order_side": decision.order_side,
            "position_id": decision.position_id,
            "time_in_force": decision.time_in_force,
            "requested_quantity": (
                decimal_string(cast(Decimal, decision.requested_quantity), scale=2)
                if decision.requested_quantity is not None
                else None
            ),
            "limit_price": (
                decimal_string(cast(Decimal, decision.limit_price), scale=6)
                if decision.limit_price is not None
                else None
            ),
            "status": decision.status,
            "reason": decision.reason,
            "filled_quantity": decimal_string(cast(Decimal, decision.filled_quantity), scale=2),
            "remaining_quantity": decimal_string(cast(Decimal, decision.remaining_quantity), scale=2),
            "average_fill_price": (
                _money18(cast(Decimal, decision.average_fill_price))
                if decision.average_fill_price is not None
                else None
            ),
            "notional": decimal_string(cast(Decimal, decision.notional), scale=8),
            "fee": _money18(cast(Decimal, decision.fee)),
            "position_cost_basis": (
                _money18(cast(Decimal, decision.position_cost_basis))
                if decision.position_cost_basis is not None
                else None
            ),
            "realized_pnl": (
                _money18(cast(Decimal, decision.realized_pnl))
                if decision.realized_pnl is not None
                else None
            ),
            "cash_before": _money18(cast(Decimal, decision.cash_before)),
            "cash_after": _money18(cast(Decimal, decision.cash_after)),
            "fill_formula_version": decision.fill_formula_version,
            "fee_rule_version": decision.fee_rule_version,
            "fee_provenance": json.loads(decision.fee_provenance_json),
            "market_evidence_hash": decision.market_evidence_hash,
            "book_evidence_hash": decision.book_evidence_hash,
            "order_id": order.order_id if order is not None else None,
            "reserved_cash": _money18(cast(Decimal, order.reserved_cash)) if order is not None else _money18(Decimal("0")),
            "opportunity_snapshot": json.loads(decision.opportunity_snapshot_json),
            "fills": fill_payloads,
            "created_at": decision.created_at.isoformat(),
        }

    @staticmethod
    def _serialize_cancellation(cancellation: KalshiPaperCancellation) -> dict[str, object]:
        return {
            "account_id": cancellation.account_id,
            "order_id": cancellation.order_id,
            "cancellation_id": cancellation.cancellation_id,
            "status": cancellation.status,
            "released_cash": _money18(cast(Decimal, cancellation.released_cash)),
            "created_at": cancellation.created_at.isoformat(),
        }

    @staticmethod
    async def _serialize_order(session: AsyncSession, order: KalshiPaperOrder) -> dict[str, object]:
        cancellation = await session.scalar(
            select(KalshiPaperCancellation).where(
                KalshiPaperCancellation.account_id == order.account_id,
                KalshiPaperCancellation.order_id == order.order_id,
            )
        )
        cancelable = (
            cancellation is None
            and cast(Decimal, order.open_quantity) > 0
            and cast(Decimal, order.reserved_cash) > 0
        )
        return {
            "account_id": order.account_id,
            "order_id": order.order_id,
            "decision_id": order.decision_id,
            "ticker": order.ticker,
            "outcome": order.outcome,
            "side": order.side,
            "time_in_force": order.time_in_force,
            "requested_quantity": decimal_string(cast(Decimal, order.requested_quantity), scale=2),
            "filled_quantity": decimal_string(cast(Decimal, order.filled_quantity), scale=2),
            "open_quantity": decimal_string(cast(Decimal, order.open_quantity), scale=2),
            "limit_price": decimal_string(cast(Decimal, order.limit_price), scale=6),
            "reserved_cash": _money18(cast(Decimal, order.reserved_cash)),
            "cancelable": cancelable,
            "status": "open" if cancelable else ("cancelled" if cancellation is not None else "filled"),
            "cancellation_id": cancellation.cancellation_id if cancellation is not None else None,
            "later_matching_supported": False,
            "created_at": order.created_at.isoformat(),
        }

    @staticmethod
    async def _serialize_position(session: AsyncSession, position: KalshiPaperPosition) -> dict[str, object]:
        exits = (
            await session.execute(
                select(KalshiPaperDecision)
                .where(
                    KalshiPaperDecision.account_id == position.account_id,
                    KalshiPaperDecision.position_id == position.position_id,
                    KalshiPaperDecision.order_side == "sell",
                )
                .order_by(KalshiPaperDecision.account_sequence.asc())
            )
        ).scalars()
        sold_quantity_units = 0
        exit_notional_units = 0
        exit_fee_units = 0
        allocated_cost_units = 0
        realized_pnl_units = 0
        exit_ids: list[str] = []
        for exit_decision in exits:
            sold_quantity_units += _to_scaled_int(
                cast(Decimal, exit_decision.filled_quantity), scale=2, field="filled_quantity"
            )
            exit_notional_units += _to_scaled_int(
                cast(Decimal, exit_decision.notional), scale=18, field="notional"
            )
            exit_fee_units += _to_scaled_int(cast(Decimal, exit_decision.fee), scale=18, field="fee")
            allocated_cost_units += _to_scaled_int(
                cast(Decimal, exit_decision.position_cost_basis), scale=18, field="position_cost_basis"
            )
            realized_pnl_units += _to_scaled_int(
                cast(Decimal, exit_decision.realized_pnl), scale=18, field="realized_pnl"
            )
            exit_ids.append(exit_decision.decision_id)
        entry_quantity = cast(Decimal, position.entry_quantity)
        entry_quantity_units = _to_scaled_int(entry_quantity, scale=2, field="entry_quantity")
        remaining_quantity_units = entry_quantity_units - sold_quantity_units
        return {
            "account_id": position.account_id,
            "position_id": position.position_id,
            "entry_decision_id": position.entry_decision_id,
            "ticker": position.ticker,
            "outcome": position.outcome,
            "entry_quantity": decimal_string(entry_quantity, scale=2),
            "entry_notional": _money18(cast(Decimal, position.entry_notional)),
            "entry_fee": _money18(cast(Decimal, position.entry_fee)),
            "sold_quantity": decimal_string(_from_scaled_int(sold_quantity_units, scale=2), scale=2),
            "remaining_quantity": decimal_string(_from_scaled_int(remaining_quantity_units, scale=2), scale=2),
            "exit_notional": _money18(_from_scaled_int(exit_notional_units, scale=18)),
            "exit_fee": _money18(_from_scaled_int(exit_fee_units, scale=18)),
            "allocated_entry_cost": _money18(_from_scaled_int(allocated_cost_units, scale=18)),
            "realized_pnl": _money18(_from_scaled_int(realized_pnl_units, scale=18)),
            "status": "closed" if remaining_quantity_units == 0 else "open",
            "closable": remaining_quantity_units > 0,
            "exit_decision_ids": exit_ids,
            "created_at": position.created_at.isoformat(),
        }
