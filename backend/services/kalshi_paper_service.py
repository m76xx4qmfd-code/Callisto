from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol, cast

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import sessionmaker

from models.database import (
    KalshiPaperAccount,
    KalshiPaperDecision,
    KalshiPaperFill,
    KalshiPaperIntent,
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
)

Action = Literal["execute", "pass"]
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
        if status in {"filled", "partial"} and notional_units + fee_units > cash_before_units:
            status = "rejected"
            reason = "insufficient_paper_cash"
            fills = ()
            filled_quantity = Decimal("0")
            remaining_quantity = requested_quantity or Decimal("0")
            average_fill_price = None
            notional = Decimal("0")
            fee = Decimal("0")
            notional_units = 0
            fee_units = 0
        cash_after = _from_scaled_int(cash_before_units - notional_units - fee_units, scale=18)

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
            time_in_force="immediate_or_cancel" if action == "execute" else None,
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
        account.cash_balance = cash_after
        account.journal_sequence = account_sequence
        account.updated_at = now
        await session.flush()
        return await self._serialize_decision(session, decision)

    @staticmethod
    def _serialize_account(account: KalshiPaperAccount) -> dict[str, object]:
        return {
            "id": account.id,
            "name": account.name,
            "currency": account.currency,
            "starting_cash": _money18(cast(Decimal, account.starting_cash)),
            "cash_balance": _money18(cast(Decimal, account.cash_balance)),
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
            "cash_before": _money18(cast(Decimal, decision.cash_before)),
            "cash_after": _money18(cast(Decimal, decision.cash_after)),
            "fill_formula_version": decision.fill_formula_version,
            "fee_rule_version": decision.fee_rule_version,
            "fee_provenance": json.loads(decision.fee_provenance_json),
            "market_evidence_hash": decision.market_evidence_hash,
            "book_evidence_hash": decision.book_evidence_hash,
            "opportunity_snapshot": json.loads(decision.opportunity_snapshot_json),
            "fills": fill_payloads,
            "created_at": decision.created_at.isoformat(),
        }
