from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import permutations

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError

from models.database import (
    Base,
    KalshiPortfolioCoverageCheckpoint,
    KalshiPortfolioFillObservation,
    KalshiPortfolioOrderObservation,
)
from services.kalshi_portfolio_coverage import (
    KalshiPortfolioCoverageConflictError,
    KalshiPortfolioCoverageService,
    _dedupe_orders,
    _evidence_hash,
    _order_payload,
)
from services.venue_execution_ledger import (
    VenueExecutionLedger,
    VenueInitialAcknowledgement,
    VenueIntentProvenance,
)
from services.venues.contracts import VenueOrderIntent
from services.venues.kalshi_v2 import (
    KalshiFill,
    KalshiFillsPage,
    KalshiHistoricalCutoff,
    KalshiOrder,
    KalshiOrdersPage,
)
from tests.postgres_test_db import build_postgres_session_factory

UTC = timezone.utc
CUTOFF = KalshiHistoricalCutoff(
    market_settled_at=datetime(2026, 8, 4, 11, 0, tzinfo=UTC),
    trades_created_at=datetime(2026, 8, 4, 11, 0, 0, 1, tzinfo=UTC),
    orders_updated_at=datetime(2026, 8, 4, 11, 0, 0, 500001, tzinfo=UTC),
    market_positions_last_updated_at=None,
)


def _order(
    *,
    order_id: str = "order-1",
    client_order_id: str = "unknown-client",
    status: str = "resting",
    fill_count: str = "0",
    remaining_count: str = "2",
    initial_count: str = "2",
    last_update_time: str = "2026-08-04T11:30:00Z",
) -> KalshiOrder:
    return KalshiOrder(
        order_id=order_id,
        user_id="principal-redacted",
        client_order_id=client_order_id,
        ticker="HIGHNY-24JAN01-T60",
        outcome_side="yes",
        book_side="bid",
        order_type="limit",
        status=status,  # type: ignore[arg-type]
        yes_price=Decimal("0.56"),
        no_price=Decimal("0.44"),
        fill_count=Decimal(fill_count),
        remaining_count=Decimal(remaining_count),
        initial_count=Decimal(initial_count),
        taker_fees=Decimal("0.010000"),
        maker_fees=Decimal(0),
        taker_fill_cost=Decimal("0.560000"),
        maker_fill_cost=Decimal(0),
        created_time="2026-08-04T10:00:00Z",
        last_update_time=last_update_time,
        expiration_time=None,
        subaccount_number=0,
        exchange_index=-1,
    )


def _fill(
    *,
    fill_id: str = "fill-1",
    order_id: str = "order-1",
    ticker: str = "HIGHNY-24JAN01-T60",
    outcome_side: str = "yes",
    book_side: str = "bid",
    count: str = "1.00",
    subaccount_number: int = 0,
) -> KalshiFill:
    return KalshiFill(
        fill_id=fill_id,
        trade_id=fill_id,
        order_id=order_id,
        ticker=ticker,
        market_ticker=ticker,
        outcome_side=outcome_side,  # type: ignore[arg-type]
        book_side=book_side,  # type: ignore[arg-type]
        count=Decimal(count),
        yes_price=Decimal("0.56"),
        no_price=Decimal("0.44"),
        is_taker=True,
        fee_cost=Decimal("0.010000"),
        created_time="2026-08-04T10:30:00.123456Z",
        subaccount_number=subaccount_number,
        ts=1785839400,
    )


class FakeCoverageClient:
    def __init__(
        self,
        *,
        current_orders: dict[str | None, KalshiOrdersPage] | None = None,
        current_fills: dict[str | None, KalshiFillsPage] | None = None,
        historical_orders: dict[str | None, KalshiOrdersPage] | None = None,
        historical_fills: dict[str | None, KalshiFillsPage] | None = None,
        cutoffs: list[KalshiHistoricalCutoff] | None = None,
        principal_fingerprint: str = "a" * 64,
    ) -> None:
        self.principal_fingerprint = principal_fingerprint
        self.pages = {
            "orders": current_orders or {None: KalshiOrdersPage(orders=(), cursor="")},
            "fills": current_fills or {None: KalshiFillsPage(fills=(), cursor="")},
            "historical_orders": historical_orders or {None: KalshiOrdersPage(orders=(), cursor="")},
            "historical_fills": historical_fills or {None: KalshiFillsPage(fills=(), cursor="")},
        }
        self.cutoffs = list(cutoffs or [CUTOFF, CUTOFF])
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def get_historical_cutoff(self) -> KalshiHistoricalCutoff:
        self.calls.append(("cutoff", {}))
        return self.cutoffs.pop(0)

    async def _page(self, source: str, kwargs: dict[str, object]):
        self.calls.append((source, kwargs))
        return self.pages[source][kwargs.get("cursor")]

    async def get_orders(self, **kwargs) -> KalshiOrdersPage:
        return await self._page("orders", kwargs)

    async def get_fills(self, **kwargs) -> KalshiFillsPage:
        return await self._page("fills", kwargs)

    async def get_historical_orders(self, **kwargs) -> KalshiOrdersPage:
        return await self._page("historical_orders", kwargs)

    async def get_historical_fills(self, **kwargs) -> KalshiFillsPage:
        return await self._page("historical_fills", kwargs)


@pytest.mark.db
@pytest.mark.asyncio
async def test_four_sources_exhaust_to_natural_terminus_dedupe_overlap_and_persist_unknowns() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "kalshi_portfolio_coverage_complete")
    try:
        older = _order(status="resting", last_update_time="2026-08-04T11:00:00Z")
        newer = _order(
            status="executed",
            fill_count="2",
            remaining_count="0",
            last_update_time="2026-08-04T11:30:00.123456Z",
        )
        client = FakeCoverageClient(
            current_orders={
                None: KalshiOrdersPage(orders=(older,), cursor="co-2"),
                "co-2": KalshiOrdersPage(orders=(older,), cursor=""),
            },
            current_fills={
                None: KalshiFillsPage(fills=(_fill(),), cursor="cf-2"),
                "cf-2": KalshiFillsPage(fills=(_fill(),), cursor=""),
            },
            historical_orders={
                None: KalshiOrdersPage(orders=(newer,), cursor="ho-2"),
                "ho-2": KalshiOrdersPage(orders=(newer,), cursor=""),
            },
            historical_fills={
                None: KalshiFillsPage(fills=(_fill(),), cursor="hf-2"),
                "hf-2": KalshiFillsPage(fills=(_fill(),), cursor=""),
            },
        )

        result = await KalshiPortfolioCoverageService(session_factory, client).sweep(
            "coverage-complete-1", datetime(2026, 8, 4, 12, tzinfo=UTC)
        )

        assert result.status == "complete"
        assert result.retry_allowed is False
        assert result.page_counts == {
            "current_orders": 2,
            "current_fills": 2,
            "historical_orders": 2,
            "historical_fills": 2,
        }
        assert result.unique_counts == {
            "current_orders": 1,
            "current_fills": 1,
            "historical_orders": 1,
            "historical_fills": 1,
            "orders": 1,
            "fills": 1,
        }
        assert result.unknown_order_ids == ("order-1",)
        assert result.unknown_client_order_ids == ("unknown-client",)
        assert result.unknown_fill_ids == ("fill-1",)

        current_calls = [kwargs for source, kwargs in client.calls if source in {"orders", "fills"}]
        assert all(set(call) == {"limit", "cursor"} for call in current_calls)
        historical_order_calls = [kwargs for source, kwargs in client.calls if source == "historical_orders"]
        historical_fill_calls = [kwargs for source, kwargs in client.calls if source == "historical_fills"]
        assert all(call["max_ts"] == 1785841201 for call in historical_order_calls)
        assert all(call["max_ts"] == 1785841201 for call in historical_fill_calls)
        assert all(
            set(call) == {"max_ts", "limit", "cursor"} for call in historical_order_calls + historical_fill_calls
        )

        async with session_factory() as session:
            observations = (await session.execute(select(KalshiPortfolioOrderObservation))).scalars().all()
            fill_count = await session.scalar(select(func.count()).select_from(KalshiPortfolioFillObservation))
            checkpoint = await session.get(
                KalshiPortfolioCoverageCheckpoint,
                {"principal_fingerprint": "a" * 64, "coverage_id": "coverage-complete-1"},
            )
        assert len(observations) == 1
        assert observations[0].payload_json["status"] == "executed"
        assert observations[0].payload_json["fill_count"] == "2"
        assert "user_id" not in observations[0].payload_json
        assert fill_count == 1
        assert checkpoint is not None
        assert checkpoint.observed_evidence_hash == result.observed_evidence_hash
        assert len(checkpoint.observed_evidence_hash) == 64
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_unscoped_local_ledger_cannot_suppress_unknown_principal_activity() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "kalshi_portfolio_coverage_known")
    try:
        async with session_factory() as session, session.begin():
            intent = await VenueExecutionLedger(session).record_intent(
                VenueOrderIntent(
                    venue="kalshi",
                    instrument_id="HIGHNY-24JAN01-T60",
                    client_order_id="unknown-client",
                    book_side="bid",
                    quantity=Decimal(2),
                    limit_price=Decimal("0.56"),
                    time_in_force="good_till_canceled",
                    post_only=False,
                ),
                VenueIntentProvenance(source="test"),
            )
            await VenueExecutionLedger(session).record_initial_acknowledgement(
                intent.id,
                VenueInitialAcknowledgement(
                    venue="kalshi",
                    client_order_id="unknown-client",
                    provider_order_id="order-1",
                    provider_status="resting",
                    filled_quantity=Decimal(0),
                    remaining_quantity=Decimal(2),
                    provider_timestamp=datetime(2026, 8, 4, 10, tzinfo=UTC),
                    payload={},
                ),
            )

        result = await KalshiPortfolioCoverageService(
            session_factory,
            FakeCoverageClient(
                current_orders={None: KalshiOrdersPage(orders=(_order(),), cursor="")},
                current_fills={None: KalshiFillsPage(fills=(_fill(),), cursor="")},
            ),
        ).sweep("coverage-known", datetime(2026, 8, 4, 12, tzinfo=UTC))

        assert result.status == "complete"
        assert result.unknown_order_ids == ("order-1",)
        assert result.unknown_client_order_ids == ("unknown-client",)
        assert result.unknown_fill_ids == ("fill-1",)
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_provider_and_client_id_from_different_intents_remain_unknown() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "kalshi_portfolio_coverage_cross_identity")
    try:
        async with session_factory() as session, session.begin():
            ledger = VenueExecutionLedger(session)
            first = await ledger.record_intent(
                VenueOrderIntent(
                    venue="kalshi",
                    instrument_id="HIGHNY-24JAN01-T60",
                    client_order_id="unknown-client",
                    book_side="bid",
                    quantity=Decimal(2),
                    limit_price=Decimal("0.56"),
                    time_in_force="good_till_canceled",
                    post_only=False,
                ),
                VenueIntentProvenance(source="test"),
            )
            second = await ledger.record_intent(
                VenueOrderIntent(
                    venue="kalshi",
                    instrument_id="HIGHNY-24JAN01-T60",
                    client_order_id="other-client",
                    book_side="bid",
                    quantity=Decimal(2),
                    limit_price=Decimal("0.56"),
                    time_in_force="good_till_canceled",
                    post_only=False,
                ),
                VenueIntentProvenance(source="test"),
            )
            assert first.id != second.id
            await ledger.record_initial_acknowledgement(
                second.id,
                VenueInitialAcknowledgement(
                    venue="kalshi",
                    client_order_id="other-client",
                    provider_order_id="order-1",
                    provider_status="resting",
                    filled_quantity=Decimal(0),
                    remaining_quantity=Decimal(2),
                    provider_timestamp=datetime(2026, 8, 4, 10, tzinfo=UTC),
                    payload={},
                ),
            )

        result = await KalshiPortfolioCoverageService(
            session_factory,
            FakeCoverageClient(
                current_orders={None: KalshiOrdersPage(orders=(_order(),), cursor="")},
                current_fills={None: KalshiFillsPage(fills=(_fill(),), cursor="")},
            ),
        ).sweep("coverage-cross-identity", datetime(2026, 8, 4, 12, tzinfo=UTC))

        assert result.unknown_order_ids == ("order-1",)
        assert result.unknown_client_order_ids == ("unknown-client",)
        assert result.unknown_fill_ids == ("fill-1",)
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_cutoff_drift_order_conflict_and_orphan_fill_are_terminal_incomplete() -> None:
    cases = [
        (
            "cutoff_drift",
            FakeCoverageClient(
                cutoffs=[CUTOFF, replace(CUTOFF, orders_updated_at=CUTOFF.orders_updated_at + timedelta(seconds=1))]
            ),
        ),
        (
            "equal_timestamp_order_conflict",
            FakeCoverageClient(
                current_orders={None: KalshiOrdersPage(orders=(_order(),), cursor="")},
                historical_orders={None: KalshiOrdersPage(orders=(replace(_order(), status="executed"),), cursor="")},
            ),
        ),
        (
            "orphan_fill",
            FakeCoverageClient(
                current_fills={None: KalshiFillsPage(fills=(_fill(order_id="missing-order"),), cursor="")}
            ),
        ),
    ]
    for reason, client in cases:
        engine, session_factory = await build_postgres_session_factory(Base, f"coverage_{reason}")
        try:
            result = await KalshiPortfolioCoverageService(session_factory, client).sweep(
                f"coverage-{reason}", datetime(2026, 8, 4, 12, tzinfo=UTC)
            )
            assert result.status == "incomplete"
            assert reason in result.reason
            assert result.retry_allowed is False

            calls_before_replay = list(client.calls)
            replay = await KalshiPortfolioCoverageService(session_factory, client).sweep(
                f"coverage-{reason}", datetime(2026, 8, 4, 13, tzinfo=UTC)
            )
            assert replay == result
            assert client.calls == calls_before_replay
        finally:
            await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_fill_order_mismatch_and_fill_count_mismatch_are_terminal_incomplete() -> None:
    cases = [
        (
            "fill_order_mismatch",
            _fill(
                ticker="OTHER-26AUG04-T1",
                outcome_side="no",
                book_side="ask",
                subaccount_number=1,
            ),
        ),
        ("fill_count_mismatch", _fill(count="3.00")),
    ]
    for reason, fill in cases:
        engine, session_factory = await build_postgres_session_factory(Base, f"coverage_{reason}")
        try:
            result = await KalshiPortfolioCoverageService(
                session_factory,
                FakeCoverageClient(
                    current_orders={
                        None: KalshiOrdersPage(
                            orders=(_order(fill_count="2", remaining_count="0"),),
                            cursor="",
                        )
                    },
                    current_fills={None: KalshiFillsPage(fills=(fill,), cursor="")},
                ),
            ).sweep(f"coverage-{reason}", datetime(2026, 8, 4, 12, tzinfo=UTC))

            assert result.status == "incomplete"
            assert reason in result.reason
            assert result.retry_allowed is False
        finally:
            await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_large_fixed_point_fill_sum_detects_exact_overfill() -> None:
    initial_count = "10000000000000000000000000000"
    engine, session_factory = await build_postgres_session_factory(Base, "coverage_exact_overfill")
    try:
        result = await KalshiPortfolioCoverageService(
            session_factory,
            FakeCoverageClient(
                current_orders={
                    None: KalshiOrdersPage(
                        orders=(
                            _order(
                                fill_count=initial_count,
                                remaining_count="0",
                                initial_count=initial_count,
                            ),
                        ),
                        cursor="",
                    )
                },
                current_fills={
                    None: KalshiFillsPage(
                        fills=(
                            _fill(fill_id="fill-large", count=initial_count),
                            _fill(fill_id="fill-over", count="0.01"),
                        ),
                        cursor="",
                    )
                },
            ),
        ).sweep("coverage-exact-overfill", datetime(2026, 8, 4, 12, tzinfo=UTC))

        assert result.status == "incomplete"
        assert "fill_count_mismatch:order-1" in result.reason
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_transport_and_repeated_cursor_failures_write_no_checkpoint() -> None:
    clients = [
        FakeCoverageClient(
            current_orders={
                None: KalshiOrdersPage(orders=(), cursor="again"),
                "again": KalshiOrdersPage(orders=(), cursor="again"),
            }
        ),
        FakeCoverageClient(current_orders={}),
    ]
    # The second client's first read is made to raise rather than use the default empty page.
    clients[1].pages["orders"] = {}
    for index, client in enumerate(clients):
        engine, session_factory = await build_postgres_session_factory(Base, f"coverage_failure_{index}")
        try:
            with pytest.raises((KalshiPortfolioCoverageConflictError, KeyError)):
                await KalshiPortfolioCoverageService(session_factory, client).sweep(
                    f"coverage-failure-{index}", datetime(2026, 8, 4, 12, tzinfo=UTC)
                )
            async with session_factory() as session:
                checkpoint = await session.get(
                    KalshiPortfolioCoverageCheckpoint,
                    {"principal_fingerprint": "a" * 64, "coverage_id": f"coverage-failure-{index}"},
                )
                order_count = await session.scalar(select(func.count()).select_from(KalshiPortfolioOrderObservation))
            assert checkpoint is None
            assert order_count == 0
        finally:
            await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_fill_divergence_is_fail_closed_and_persistence_is_immutable() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "coverage_fill_conflict")
    try:
        fill = _fill()
        client = FakeCoverageClient(
            current_orders={None: KalshiOrdersPage(orders=(_order(),), cursor="")},
            current_fills={
                None: KalshiFillsPage(fills=(fill,), cursor="next"),
                "next": KalshiFillsPage(fills=(replace(fill, fee_cost=Decimal("0.020000")),), cursor=""),
            },
        )
        with pytest.raises(KalshiPortfolioCoverageConflictError, match="fill_id"):
            await KalshiPortfolioCoverageService(session_factory, client).sweep(
                "coverage-fill-conflict", datetime(2026, 8, 4, 12, tzinfo=UTC)
            )
        async with session_factory() as session:
            assert (
                await session.get(
                    KalshiPortfolioCoverageCheckpoint,
                    {"principal_fingerprint": "a" * 64, "coverage_id": "coverage-fill-conflict"},
                )
                is None
            )

        complete = await KalshiPortfolioCoverageService(
            session_factory,
            FakeCoverageClient(current_orders={None: KalshiOrdersPage(orders=(_order(),), cursor="")}),
        ).sweep("coverage-immutable", datetime(2026, 8, 4, 12, tzinfo=UTC))
        assert complete.status == "complete"

        async with session_factory() as session:
            with pytest.raises(DBAPIError, match="immutable"):
                await session.execute(
                    update(KalshiPortfolioCoverageCheckpoint)
                    .where(KalshiPortfolioCoverageCheckpoint.coverage_id == "coverage-immutable")
                    .values(reason="mutated")
                )
                await session.commit()
            await session.rollback()
            with pytest.raises(DBAPIError, match="immutable"):
                await session.execute(delete(KalshiPortfolioOrderObservation))
                await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stable_id_and_observed_time_are_strictly_validated() -> None:
    service = KalshiPortfolioCoverageService(None, FakeCoverageClient())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="coverage_id"):
        await service.sweep(" ", datetime(2026, 8, 4, 12, tzinfo=UTC))
    with pytest.raises(ValueError, match="timezone-aware"):
        await service.sweep("coverage", datetime(2026, 8, 4, 12))  # noqa: DTZ001 - deliberately naive


def test_conflicting_order_group_selection_is_permutation_invariant() -> None:
    snapshots = (
        _order(status="resting", last_update_time="2026-08-04T11:00:00Z"),
        _order(status="executed", fill_count="2", remaining_count="0", last_update_time="2026-08-04T11:30:00Z"),
        _order(client_order_id="other-client", status="resting", last_update_time="2026-08-04T11:00:00Z"),
        _order(
            client_order_id="other-client",
            status="executed",
            fill_count="2",
            remaining_count="0",
            last_update_time="2026-08-04T11:30:00Z",
        ),
    )

    outcomes = set()
    for ordering in permutations(snapshots):
        selected, conflicts = _dedupe_orders(ordering)
        outcomes.add((_evidence_hash(_order_payload(selected["order-1"])), tuple(conflicts)))

    assert len(outcomes) == 1
    assert next(iter(outcomes))[1] == ("order_identity_conflict:order-1",)


def test_provider_user_identity_is_hashed_into_order_evidence() -> None:
    order = _order()
    other_principal = replace(order, user_id="other-provider-user")

    assert "user_id" not in _order_payload(order)
    assert _order_payload(order)["provider_user_hash"] != _order_payload(other_principal)["provider_user_hash"]
    selected, conflicts = _dedupe_orders((order, other_principal))
    assert selected["order-1"] in {order, other_principal}
    assert conflicts == ["order_identity_conflict:order-1"]


@pytest.mark.db
@pytest.mark.asyncio
async def test_mixed_provider_users_across_distinct_orders_are_terminal_incomplete() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "coverage_mixed_provider_users")
    try:
        result = await KalshiPortfolioCoverageService(
            session_factory,
            FakeCoverageClient(
                current_orders={
                    None: KalshiOrdersPage(
                        orders=(
                            _order(order_id="order-1"),
                            replace(_order(order_id="order-2"), user_id="different-provider-user"),
                        ),
                        cursor="",
                    )
                }
            ),
        ).sweep("coverage-mixed-provider-users", datetime(2026, 8, 4, 12, tzinfo=UTC))

        assert result.status == "incomplete"
        assert "provider_user_conflict" in result.reason
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_same_provider_ids_are_isolated_by_authenticated_principal() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "kalshi_portfolio_coverage_principals")
    try:
        for fingerprint in ("a" * 64, "b" * 64):
            result = await KalshiPortfolioCoverageService(
                session_factory,
                FakeCoverageClient(
                    principal_fingerprint=fingerprint,
                    current_orders={None: KalshiOrdersPage(orders=(_order(),), cursor="")},
                    current_fills={None: KalshiFillsPage(fills=(_fill(),), cursor="")},
                ),
            ).sweep("same-coverage-id", datetime(2026, 8, 4, 12, tzinfo=UTC))
            assert result.principal_fingerprint == fingerprint

        async with session_factory() as session:
            checkpoint_count = await session.scalar(select(func.count()).select_from(KalshiPortfolioCoverageCheckpoint))
            order_count = await session.scalar(select(func.count()).select_from(KalshiPortfolioOrderObservation))
            fill_count = await session.scalar(select(func.count()).select_from(KalshiPortfolioFillObservation))
        assert (checkpoint_count, order_count, fill_count) == (2, 2, 2)
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_concurrent_same_coverage_id_replays_one_canonical_result() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "coverage_concurrent_same")
    try:
        services = [
            KalshiPortfolioCoverageService(
                session_factory,
                FakeCoverageClient(current_orders={None: KalshiOrdersPage(orders=(_order(),), cursor="")}),
            )
            for _ in range(2)
        ]

        first, second = await asyncio.gather(
            *(service.sweep("shared-coverage", datetime(2026, 8, 4, 12, tzinfo=UTC)) for service in services)
        )

        assert first == second
        async with session_factory() as session:
            checkpoint_count = await session.scalar(select(func.count()).select_from(KalshiPortfolioCoverageCheckpoint))
        assert checkpoint_count == 1
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_concurrent_distinct_coverage_ids_share_canonical_observations() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "coverage_concurrent_distinct")
    try:
        services = [
            KalshiPortfolioCoverageService(
                session_factory,
                FakeCoverageClient(current_orders={None: KalshiOrdersPage(orders=(_order(),), cursor="")}),
            )
            for _ in range(2)
        ]

        first, second = await asyncio.gather(
            services[0].sweep("coverage-a", datetime(2026, 8, 4, 12, tzinfo=UTC)),
            services[1].sweep("coverage-b", datetime(2026, 8, 4, 12, tzinfo=UTC)),
        )

        assert first.observed_evidence_hash == second.observed_evidence_hash
        async with session_factory() as session:
            checkpoint_count = await session.scalar(select(func.count()).select_from(KalshiPortfolioCoverageCheckpoint))
            observation_count = await session.scalar(select(func.count()).select_from(KalshiPortfolioOrderObservation))
        assert (checkpoint_count, observation_count) == (2, 1)
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_concurrent_divergent_same_coverage_id_has_one_winner_and_one_conflict() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "coverage_concurrent_divergent")
    try:
        services = [
            KalshiPortfolioCoverageService(
                session_factory,
                FakeCoverageClient(
                    current_orders={
                        None: KalshiOrdersPage(
                            orders=(
                                _order(
                                    status=status,
                                    fill_count=("2" if status == "executed" else "0"),
                                    remaining_count=("0" if status == "executed" else "2"),
                                ),
                            ),
                            cursor="",
                        )
                    }
                ),
            )
            for status in ("resting", "executed")
        ]

        outcomes = await asyncio.gather(
            *(service.sweep("divergent-coverage", datetime(2026, 8, 4, 12, tzinfo=UTC)) for service in services),
            return_exceptions=True,
        )

        winners = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
        conflicts = [outcome for outcome in outcomes if isinstance(outcome, KalshiPortfolioCoverageConflictError)]
        assert len(winners) == 1
        assert len(conflicts) == 1
        async with session_factory() as session:
            checkpoint = await session.get(
                KalshiPortfolioCoverageCheckpoint,
                {"principal_fingerprint": "a" * 64, "coverage_id": "divergent-coverage"},
            )
        assert checkpoint is not None
        assert checkpoint.observed_evidence_hash == winners[0].observed_evidence_hash
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_cross_sweep_provider_user_identity_conflict_commits_no_second_checkpoint() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "coverage_cross_sweep_identity")
    try:
        await KalshiPortfolioCoverageService(
            session_factory,
            FakeCoverageClient(current_orders={None: KalshiOrdersPage(orders=(_order(),), cursor="")}),
        ).sweep("first-coverage", datetime(2026, 8, 4, 12, tzinfo=UTC))

        with pytest.raises(KalshiPortfolioCoverageConflictError, match="immutable order identity"):
            await KalshiPortfolioCoverageService(
                session_factory,
                FakeCoverageClient(
                    current_orders={
                        None: KalshiOrdersPage(
                            orders=(replace(_order(), user_id="different-provider-user"),),
                            cursor="",
                        )
                    }
                ),
            ).sweep("second-coverage", datetime(2026, 8, 4, 13, tzinfo=UTC))

        async with session_factory() as session:
            checkpoint_ids = tuple(await session.scalars(select(KalshiPortfolioCoverageCheckpoint.coverage_id)))
        assert checkpoint_ids == ("first-coverage",)
    finally:
        await engine.dispose()
