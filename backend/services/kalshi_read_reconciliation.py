"""Read-only reconciliation of persisted order intents against Kalshi GET evidence.

The service performs venue reads before opening its persistence transaction. It
never submits, retries, cancels, amends, or enables an execution runtime.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from models.database import VenueExecutionEvent, VenueOrderIntentRecord, VenueProviderAcknowledgementRecord
from services.venue_execution_ledger import (
    VenueExecutionConflictError,
    VenueExecutionLedger,
    VenueInitialAcknowledgement,
)
from services.venues.kalshi_v2 import KalshiFill, KalshiFillsPage, KalshiOrder, KalshiOrdersPage


class ReconciliationConflictError(RuntimeError):
    """Raised when venue evidence conflicts with immutable local facts."""


class KalshiReadClient(Protocol):
    async def get_orders(
        self,
        *,
        ticker: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> KalshiOrdersPage: ...

    async def get_fills(
        self,
        *,
        order_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> KalshiFillsPage: ...


@dataclass(frozen=True)
class KalshiReconciliationResult:
    outcome: Literal["matched", "inconclusive"]
    provider_order_id: str | None
    provider_status: str | None
    observed_fill_count: int
    retry_allowed: Literal[False] = False


class KalshiReadReconciliationService:
    """Reconcile one immutable Kalshi intent using current GET endpoints only."""

    def __init__(self, session_factory: sessionmaker, client: KalshiReadClient) -> None:
        self._session_factory = session_factory
        self._client = client

    async def reconcile_intent(
        self,
        intent_id: str,
        *,
        reconciliation_id: str,
        observed_at: datetime,
    ) -> KalshiReconciliationResult:
        reconciliation_id = reconciliation_id.strip()
        if not reconciliation_id:
            raise ValueError("reconciliation_id is required")
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        observed_at = observed_at.astimezone(timezone.utc)

        async with self._session_factory() as session:
            intent = await session.get(VenueOrderIntentRecord, intent_id)
            acknowledgement = await session.get(VenueProviderAcknowledgementRecord, intent_id)
            existing_attempt = await session.scalar(
                select(VenueExecutionEvent).where(
                    VenueExecutionEvent.intent_id == intent_id,
                    VenueExecutionEvent.dedupe_key == f"reconciliation_attempt:{reconciliation_id}",
                )
            )
        if intent is None:
            raise ReconciliationConflictError("unknown order intent")
        if intent.venue != "kalshi":
            raise ReconciliationConflictError("Kalshi reconciliation requires a Kalshi order intent")
        if existing_attempt is not None:
            return _result_from_attempt(existing_attempt)

        matching_orders = await self._find_current_orders(intent)
        if not matching_orders:
            async with self._session_factory() as session, session.begin():
                locked_intent = await session.scalar(
                    select(VenueOrderIntentRecord).where(VenueOrderIntentRecord.id == intent.id).with_for_update()
                )
                if locked_intent is None:
                    raise ReconciliationConflictError("order intent disappeared during reconciliation")
                current_acknowledgement = await session.get(VenueProviderAcknowledgementRecord, intent.id)
                await VenueExecutionLedger(session).record_event(
                    intent.id,
                    event_type="reconciliation_inconclusive",
                    source="kalshi_rest_reconciliation",
                    dedupe_key=f"reconciliation_attempt:{reconciliation_id}",
                    occurred_at=observed_at,
                    payload={
                        "reconciliation_id": reconciliation_id,
                        "reason": "not_found_in_current_orders",
                        "historical_search_performed": False,
                        "provider_order_id": (
                            current_acknowledgement.provider_order_id if current_acknowledgement else None
                        ),
                        "provider_status": current_acknowledgement.provider_status if current_acknowledgement else None,
                        "retry_allowed": False,
                    },
                )
            return KalshiReconciliationResult(
                outcome="inconclusive",
                provider_order_id=current_acknowledgement.provider_order_id if current_acknowledgement else None,
                provider_status=current_acknowledgement.provider_status if current_acknowledgement else None,
                observed_fill_count=0,
            )

        if len(matching_orders) != 1:
            raise ReconciliationConflictError("multiple current Kalshi orders share the persisted client_order_id")
        order = matching_orders[0]
        self._validate_order(intent, order)
        if acknowledgement is not None and acknowledgement.provider_order_id != order.order_id:
            raise ReconciliationConflictError("current provider_order_id conflicts with the immutable acknowledgement")

        fills = await self._get_current_fills(order)
        order_occurred_at = _provider_datetime(
            order.last_update_time or order.created_time,
            field_name="order last_update_time or created_time",
        )
        order_created_at = _provider_datetime(order.created_time, field_name="order created_time")
        order_payload = _order_payload(order)
        order_snapshot_hash = _evidence_hash(order_payload)

        try:
            async with self._session_factory() as session, session.begin():
                ledger = VenueExecutionLedger(session)
                persisted_acknowledgement = await session.get(VenueProviderAcknowledgementRecord, intent.id)
                if persisted_acknowledgement is None:
                    await ledger.record_initial_acknowledgement(
                        intent.id,
                        VenueInitialAcknowledgement(
                            venue="kalshi",
                            client_order_id=intent.client_order_id,
                            provider_order_id=order.order_id,
                            provider_status=order.status,
                            filled_quantity=order.fill_count,
                            remaining_quantity=order.remaining_count,
                            provider_timestamp=order_created_at,
                            payload=order_payload,
                        ),
                    )
                elif persisted_acknowledgement.provider_order_id != order.order_id:
                    raise ReconciliationConflictError(
                        "current provider_order_id conflicts with the immutable acknowledgement"
                    )

                for fill in fills:
                    await ledger.record_event(
                        intent.id,
                        event_type="fill_observed",
                        source="kalshi_rest_reconciliation",
                        dedupe_key=f"fill_observed:{order.order_id}:{fill.fill_id}",
                        provider_order_id=order.order_id,
                        provider_event_id=fill.fill_id,
                        occurred_at=_fill_datetime(fill),
                        payload=_fill_payload(fill),
                    )
                await ledger.record_event(
                    intent.id,
                    event_type="order_observed",
                    source="kalshi_rest_reconciliation",
                    dedupe_key=f"order_observed:{order.order_id}:{order_snapshot_hash}",
                    provider_order_id=order.order_id,
                    occurred_at=order_occurred_at,
                    payload=order_payload,
                )
                await ledger.record_event(
                    intent.id,
                    event_type="reconciliation_matched",
                    source="kalshi_rest_reconciliation",
                    dedupe_key=f"reconciliation_attempt:{reconciliation_id}",
                    provider_order_id=order.order_id,
                    occurred_at=observed_at,
                    payload={
                        "reconciliation_id": reconciliation_id,
                        "search_scope": "current_orders_and_fills",
                        "global_client_order_id_uniqueness_proven": False,
                        "provider_order_id": order.order_id,
                        "provider_status": order.status,
                        "observed_fill_count": len(fills),
                        "retry_allowed": False,
                    },
                )
        except VenueExecutionConflictError as exc:
            raise ReconciliationConflictError(str(exc)) from exc

        return KalshiReconciliationResult(
            outcome="matched",
            provider_order_id=order.order_id,
            provider_status=order.status,
            observed_fill_count=len(fills),
        )

    async def _find_current_orders(self, intent: VenueOrderIntentRecord) -> tuple[KalshiOrder, ...]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        matches: dict[str, KalshiOrder] = {}
        while True:
            page = await self._client.get_orders(
                ticker=intent.instrument_id,
                limit=1000,
                cursor=cursor,
            )
            for order in page.orders:
                if order.client_order_id != intent.client_order_id:
                    continue
                existing = matches.get(order.order_id)
                if existing is not None and existing != order:
                    raise ReconciliationConflictError("one provider_order_id returned conflicting order snapshots")
                matches[order.order_id] = order
            if not page.cursor:
                return tuple(matches.values())
            if page.cursor in seen_cursors:
                raise ReconciliationConflictError("Kalshi orders pagination cursor repeated")
            seen_cursors.add(page.cursor)
            cursor = page.cursor

    async def _get_current_fills(self, order: KalshiOrder) -> tuple[KalshiFill, ...]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        fills: dict[str, KalshiFill] = {}
        observed_quantity = Decimal(0)
        while True:
            page = await self._client.get_fills(
                order_id=order.order_id,
                limit=1000,
                cursor=cursor,
            )
            for fill in page.fills:
                if fill.order_id != order.order_id:
                    raise ReconciliationConflictError("Kalshi fill order_id conflicts with the matched order")
                if fill.ticker != order.ticker or fill.book_side != order.book_side:
                    raise ReconciliationConflictError("Kalshi fill instrument or side conflicts with the matched order")
                if (
                    fill.subaccount_number is not None
                    and order.subaccount_number is not None
                    and fill.subaccount_number != order.subaccount_number
                ):
                    raise ReconciliationConflictError("Kalshi fill subaccount conflicts with the matched order")
                existing = fills.get(fill.fill_id)
                if existing is not None and existing != fill:
                    raise ReconciliationConflictError("one fill_id returned conflicting fill evidence")
                if existing is None:
                    with localcontext() as decimal_context:
                        decimal_context.prec = 39
                        observed_quantity += fill.count
                    if observed_quantity > order.initial_count:
                        raise ReconciliationConflictError(
                            "observed Kalshi fill quantity exceeds initial order quantity"
                        )
                    fills[fill.fill_id] = fill
            if not page.cursor:
                return tuple(fills.values())
            if page.cursor in seen_cursors:
                raise ReconciliationConflictError("Kalshi fills pagination cursor repeated")
            seen_cursors.add(page.cursor)
            cursor = page.cursor

    @staticmethod
    def _validate_order(intent: VenueOrderIntentRecord, order: KalshiOrder) -> None:
        if order.ticker != intent.instrument_id:
            raise ReconciliationConflictError("Kalshi order ticker conflicts with the immutable intent")
        if order.book_side != intent.book_side:
            raise ReconciliationConflictError("Kalshi order book_side conflicts with the immutable intent")
        if order.order_type != "limit":
            raise ReconciliationConflictError("Kalshi order type conflicts with the immutable limit-order intent")
        if order.initial_count != intent.quantity:
            raise ReconciliationConflictError("Kalshi order initial_count conflicts with the immutable intent")
        effective_price = order.yes_price if order.book_side == "bid" else order.no_price
        if effective_price != intent.limit_price:
            raise ReconciliationConflictError("Kalshi order limit price conflicts with the immutable intent")


_RFC3339_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(?P<fraction>\d+))?(?:Z|[+-]\d{2}:\d{2})$")


def _provider_datetime(value: str | None, *, field_name: str) -> datetime:
    if value is None:
        raise ReconciliationConflictError(f"Kalshi {field_name} is required for durable evidence")
    match = _RFC3339_PATTERN.fullmatch(value)
    if match is None:
        raise ReconciliationConflictError(f"Kalshi {field_name} must be RFC3339")
    fraction = match.group("fraction")
    if fraction is not None and len(fraction) > 6:
        raise ReconciliationConflictError(f"Kalshi {field_name} exceeds database timestamp precision")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReconciliationConflictError(f"Kalshi {field_name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ReconciliationConflictError(f"Kalshi {field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _fill_datetime(fill: KalshiFill) -> datetime:
    if fill.created_time is not None:
        return _provider_datetime(fill.created_time, field_name="fill created_time")
    if fill.ts is None or fill.ts < 0:
        raise ReconciliationConflictError("Kalshi fill requires created_time or a non-negative ts")
    return datetime.fromtimestamp(fill.ts, tz=timezone.utc)


def _order_payload(order: KalshiOrder) -> dict[str, object]:
    return {
        "order_id": order.order_id,
        "client_order_id": order.client_order_id,
        "ticker": order.ticker,
        "outcome_side": order.outcome_side,
        "book_side": order.book_side,
        "order_type": order.order_type,
        "status": order.status,
        "yes_price": order.yes_price,
        "no_price": order.no_price,
        "fill_count": order.fill_count,
        "remaining_count": order.remaining_count,
        "initial_count": order.initial_count,
        "taker_fees": order.taker_fees,
        "maker_fees": order.maker_fees,
        "taker_fill_cost": order.taker_fill_cost,
        "maker_fill_cost": order.maker_fill_cost,
        "created_time": order.created_time,
        "last_update_time": order.last_update_time,
        "expiration_time": order.expiration_time,
        "subaccount_number": order.subaccount_number,
        "exchange_index": order.exchange_index,
    }


def _fill_payload(fill: KalshiFill) -> dict[str, object]:
    return {
        "fill_id": fill.fill_id,
        "trade_id": fill.trade_id,
        "order_id": fill.order_id,
        "ticker": fill.ticker,
        "market_ticker": fill.market_ticker,
        "outcome_side": fill.outcome_side,
        "book_side": fill.book_side,
        "count": fill.count,
        "yes_price": fill.yes_price,
        "no_price": fill.no_price,
        "is_taker": fill.is_taker,
        "fee_cost": fill.fee_cost,
        "created_time": fill.created_time,
        "subaccount_number": fill.subaccount_number,
        "ts": fill.ts,
    }


def _evidence_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda value: format(value, "f") if isinstance(value, Decimal) else value,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _result_from_attempt(event: VenueExecutionEvent) -> KalshiReconciliationResult:
    if event.event_type not in {"reconciliation_inconclusive", "reconciliation_matched"}:
        raise ReconciliationConflictError("reconciliation attempt key is bound to an invalid event type")
    payload = event.payload_json
    provider_order_id = payload.get("provider_order_id")
    provider_status = payload.get("provider_status")
    observed_fill_count = payload.get("observed_fill_count", 0)
    if provider_order_id is not None and not isinstance(provider_order_id, str):
        raise ReconciliationConflictError("persisted reconciliation provider_order_id is invalid")
    if provider_status is not None and not isinstance(provider_status, str):
        raise ReconciliationConflictError("persisted reconciliation provider_status is invalid")
    if isinstance(observed_fill_count, bool) or not isinstance(observed_fill_count, int) or observed_fill_count < 0:
        raise ReconciliationConflictError("persisted reconciliation observed_fill_count is invalid")
    return KalshiReconciliationResult(
        outcome="matched" if event.event_type == "reconciliation_matched" else "inconclusive",
        provider_order_id=provider_order_id,
        provider_status=provider_status,
        observed_fill_count=observed_fill_count,
    )


__all__ = [
    "KalshiReadReconciliationService",
    "KalshiReconciliationResult",
    "ReconciliationConflictError",
]
