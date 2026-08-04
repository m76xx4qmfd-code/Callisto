from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from models.database import (
    Base,
    VenueExecutionEvent,
    VenueProviderAcknowledgementRecord,
)
from services.kalshi_read_reconciliation import (
    KalshiReadReconciliationService,
    ReconciliationConflictError,
)
from services.venue_execution_ledger import (
    VenueExecutionLedger,
    VenueIntentProvenance,
)
from services.venues.contracts import VenueOrderIntent
from services.venues.kalshi_v2 import (
    KalshiFill,
    KalshiFillsPage,
    KalshiOrder,
    KalshiOrdersPage,
)
from tests.postgres_test_db import build_postgres_session_factory


class FakeKalshiReadClient:
    def __init__(
        self,
        *,
        order_pages: dict[str | None, KalshiOrdersPage],
        fill_pages: dict[str | None, KalshiFillsPage] | None = None,
    ) -> None:
        self.order_pages = order_pages
        self.fill_pages = fill_pages or {None: KalshiFillsPage(fills=(), cursor="")}
        self.order_calls: list[dict[str, object]] = []
        self.fill_calls: list[dict[str, object]] = []

    async def get_orders(self, **kwargs) -> KalshiOrdersPage:
        self.order_calls.append(kwargs)
        return self.order_pages[kwargs.get("cursor")]

    async def get_fills(self, **kwargs) -> KalshiFillsPage:
        self.fill_calls.append(kwargs)
        return self.fill_pages[kwargs.get("cursor")]


def _intent() -> VenueOrderIntent:
    return VenueOrderIntent(
        venue="kalshi",
        instrument_id="HIGHNY-24JAN01-T60",
        client_order_id="client-1",
        book_side="bid",
        quantity=Decimal("12.34"),
        limit_price=Decimal("0.56"),
        time_in_force="good_till_canceled",
        post_only=True,
    )


def _order(*, client_order_id: str = "client-1", initial_count: str = "12.34") -> KalshiOrder:
    return KalshiOrder(
        order_id="provider-order-1",
        user_id="user-redacted",
        client_order_id=client_order_id,
        ticker="HIGHNY-24JAN01-T60",
        outcome_side="yes",
        book_side="bid",
        order_type="limit",
        status="resting",
        yes_price=Decimal("0.56"),
        no_price=Decimal("0.44"),
        fill_count=Decimal("1.25"),
        remaining_count=Decimal("11.09"),
        initial_count=Decimal(initial_count),
        taker_fees=Decimal("0.01"),
        maker_fees=Decimal(0),
        taker_fill_cost=Decimal("0.70"),
        maker_fill_cost=Decimal(0),
        created_time="2026-08-04T12:00:00Z",
        last_update_time="2026-08-04T12:01:00.123456Z",
        expiration_time=None,
        subaccount_number=0,
        exchange_index=0,
    )


def _fill() -> KalshiFill:
    return KalshiFill(
        fill_id="fill-1",
        trade_id="fill-1",
        order_id="provider-order-1",
        ticker="HIGHNY-24JAN01-T60",
        market_ticker="HIGHNY-24JAN01-T60",
        outcome_side="yes",
        book_side="bid",
        count=Decimal("1.25"),
        yes_price=Decimal("0.56"),
        no_price=Decimal("0.44"),
        is_taker=True,
        fee_cost=Decimal("0.01"),
        created_time="2026-08-04T12:01:00Z",
        subaccount_number=0,
        ts=1785844860,
    )


async def _persist_intent(session_factory) -> str:
    async with session_factory() as session, session.begin():
        record = await VenueExecutionLedger(session).record_intent(
            _intent(),
            VenueIntentProvenance(source="strategy_orchestrator", source_id="signal-1"),
        )
        await VenueExecutionLedger(session).record_event(
            record.id,
            event_type="submission_started",
            source="execution_boundary",
            dedupe_key="submission_started:attempt-1",
            occurred_at=datetime(2026, 8, 4, 11, 59, tzinfo=timezone.utc),
            payload={"attempt_id": "attempt-1"},
        )
        return record.id


@pytest.mark.db
@pytest.mark.asyncio
async def test_found_order_and_fills_are_persisted_atomically_and_idempotently() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "kalshi_read_reconcile_found")
    try:
        intent_id = await _persist_intent(session_factory)
        other_order = _order(client_order_id="other-client")
        client = FakeKalshiReadClient(
            order_pages={
                None: KalshiOrdersPage(orders=(other_order,), cursor="orders-next"),
                "orders-next": KalshiOrdersPage(orders=(_order(),), cursor=""),
            },
            fill_pages={
                None: KalshiFillsPage(fills=(), cursor="fills-next"),
                "fills-next": KalshiFillsPage(fills=(_fill(),), cursor=""),
            },
        )
        service = KalshiReadReconciliationService(session_factory, client)
        observed_at = datetime(2026, 8, 4, 12, 2, tzinfo=timezone.utc)

        first = await service.reconcile_intent(
            intent_id,
            reconciliation_id="reconcile-1",
            observed_at=observed_at,
        )
        order_call_count = len(client.order_calls)
        fill_call_count = len(client.fill_calls)
        client.order_pages = dict[str | None, KalshiOrdersPage]({None: KalshiOrdersPage(orders=(), cursor="")})
        replay = await service.reconcile_intent(
            intent_id,
            reconciliation_id="reconcile-1",
            observed_at=observed_at + timedelta(minutes=5),
        )

        assert first == replay
        assert first.outcome == "matched"
        assert first.provider_order_id == "provider-order-1"
        assert first.provider_status == "resting"
        assert first.observed_fill_count == 1
        assert first.retry_allowed is False
        assert len(client.order_calls) == order_call_count
        assert len(client.fill_calls) == fill_call_count
        assert [call["cursor"] for call in client.order_calls[:2]] == [None, "orders-next"]
        assert all(call["ticker"] == "HIGHNY-24JAN01-T60" for call in client.order_calls)
        assert [call["cursor"] for call in client.fill_calls[:2]] == [None, "fills-next"]
        assert all(call["order_id"] == "provider-order-1" for call in client.fill_calls)

        async with session_factory() as session:
            acknowledgement = await session.get(VenueProviderAcknowledgementRecord, intent_id)
            events = (
                (
                    await session.execute(
                        select(VenueExecutionEvent)
                        .where(VenueExecutionEvent.intent_id == intent_id)
                        .order_by(VenueExecutionEvent.sequence)
                    )
                )
                .scalars()
                .all()
            )
        assert acknowledgement is not None
        assert acknowledgement.provider_order_id == "provider-order-1"
        assert acknowledgement.filled_quantity == Decimal("1.250000000000000000")
        assert [event.event_type for event in events] == [
            "intent_recorded",
            "submission_started",
            "submission_acknowledged",
            "fill_observed",
            "order_observed",
            "reconciliation_matched",
        ]

        aged_out = await service.reconcile_intent(
            intent_id,
            reconciliation_id="reconcile-2",
            observed_at=observed_at + timedelta(minutes=10),
        )
        assert aged_out.outcome == "inconclusive"
        assert aged_out.provider_order_id == "provider-order-1"
        assert aged_out.provider_status == "resting"
        assert aged_out.retry_allowed is False
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_current_order_miss_is_durable_inconclusive_and_never_allows_retry() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "kalshi_read_reconcile_missing")
    try:
        intent_id = await _persist_intent(session_factory)
        client = FakeKalshiReadClient(order_pages={None: KalshiOrdersPage(orders=(), cursor="")})
        service = KalshiReadReconciliationService(session_factory, client)
        observed_at = datetime(2026, 8, 4, 12, 2, tzinfo=timezone.utc)

        first = await service.reconcile_intent(
            intent_id,
            reconciliation_id="reconcile-miss-1",
            observed_at=observed_at,
        )
        replay = await service.reconcile_intent(
            intent_id,
            reconciliation_id="reconcile-miss-1",
            observed_at=observed_at,
        )

        assert first == replay
        assert first.outcome == "inconclusive"
        assert first.provider_order_id is None
        assert first.retry_allowed is False
        assert client.fill_calls == []
        async with session_factory() as session:
            event_count = await session.scalar(
                select(func.count())
                .select_from(VenueExecutionEvent)
                .where(
                    VenueExecutionEvent.intent_id == intent_id,
                    VenueExecutionEvent.event_type == "reconciliation_inconclusive",
                )
            )
            event = await session.scalar(
                select(VenueExecutionEvent).where(
                    VenueExecutionEvent.intent_id == intent_id,
                    VenueExecutionEvent.event_type == "reconciliation_inconclusive",
                )
            )
        assert event_count == 1
        assert event is not None
        assert event.dedupe_key == "reconciliation_attempt:reconcile-miss-1"
        assert event.payload_json["reason"] == "not_found_in_current_orders"
        assert event.payload_json["historical_search_performed"] is False

        client.order_pages = dict[str | None, KalshiOrdersPage]({None: KalshiOrdersPage(orders=(_order(),), cursor="")})
        canonical = await service.reconcile_intent(
            intent_id,
            reconciliation_id="reconcile-miss-1",
            observed_at=observed_at + timedelta(minutes=5),
        )
        assert canonical == first
        async with session_factory() as session:
            assert await session.get(VenueProviderAcknowledgementRecord, intent_id) is None
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_conflicting_provider_order_rolls_back_all_reconciliation_evidence() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "kalshi_read_reconcile_conflict")
    try:
        intent_id = await _persist_intent(session_factory)
        client = FakeKalshiReadClient(
            order_pages={None: KalshiOrdersPage(orders=(_order(initial_count="12.35"),), cursor="")},
            fill_pages={None: KalshiFillsPage(fills=(_fill(),), cursor="")},
        )
        service = KalshiReadReconciliationService(session_factory, client)

        with pytest.raises(ReconciliationConflictError, match="initial_count"):
            await service.reconcile_intent(
                intent_id,
                reconciliation_id="reconcile-conflict-1",
                observed_at=datetime(2026, 8, 4, 12, 2, tzinfo=timezone.utc),
            )

        assert client.fill_calls == []
        async with session_factory() as session:
            acknowledgement = await session.get(VenueProviderAcknowledgementRecord, intent_id)
            reconciliation_event_count = await session.scalar(
                select(func.count())
                .select_from(VenueExecutionEvent)
                .where(
                    VenueExecutionEvent.intent_id == intent_id,
                    VenueExecutionEvent.event_type.in_(
                        ("submission_acknowledged", "fill_observed", "order_observed", "reconciliation_matched")
                    ),
                )
            )
        assert acknowledgement is None
        assert reconciliation_event_count == 0
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_market_order_and_overprecision_timestamp_fail_closed() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "kalshi_read_reconcile_protocol_conflict")
    try:
        intent_id = await _persist_intent(session_factory)
        market_client = FakeKalshiReadClient(
            order_pages={None: KalshiOrdersPage(orders=(replace(_order(), order_type="market"),), cursor="")}
        )
        with pytest.raises(ReconciliationConflictError, match="order type"):
            await KalshiReadReconciliationService(session_factory, market_client).reconcile_intent(
                intent_id,
                reconciliation_id="reconcile-market-order",
                observed_at=datetime(2026, 8, 4, 12, 2, tzinfo=timezone.utc),
            )

        timestamp_client = FakeKalshiReadClient(
            order_pages={
                None: KalshiOrdersPage(
                    orders=(replace(_order(), last_update_time="2026-08-04T12:01:00.123456789Z"),),
                    cursor="",
                )
            }
        )
        with pytest.raises(ReconciliationConflictError, match="timestamp precision"):
            await KalshiReadReconciliationService(session_factory, timestamp_client).reconcile_intent(
                intent_id,
                reconciliation_id="reconcile-overprecision-time",
                observed_at=datetime(2026, 8, 4, 12, 2, tzinfo=timezone.utc),
            )

        overfilled_client = FakeKalshiReadClient(
            order_pages={None: KalshiOrdersPage(orders=(_order(),), cursor="")},
            fill_pages={None: KalshiFillsPage(fills=(replace(_fill(), count=Decimal(13)),), cursor="")},
        )
        with pytest.raises(ReconciliationConflictError, match="fill quantity"):
            await KalshiReadReconciliationService(session_factory, overfilled_client).reconcile_intent(
                intent_id,
                reconciliation_id="reconcile-overfilled",
                observed_at=datetime(2026, 8, 4, 12, 2, tzinfo=timezone.utc),
            )
    finally:
        await engine.dispose()
