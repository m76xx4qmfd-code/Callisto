from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, localcontext

import pytest
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError

from models.database import (
    Base,
    VenueExecutionEvent,
    VenueOrderIntentRecord,
    VenueProviderAcknowledgementRecord,
)
from services.venue_execution_ledger import (
    VenueExecutionConflictError,
    VenueExecutionLedger,
    VenueInitialAcknowledgement,
    VenueIntentProvenance,
)
from services.venues.contracts import VenueOrderIntent
from tests.postgres_test_db import build_postgres_session_factory


def _intent(*, client_order_id: str = "client-1", quantity: str = "12.34") -> VenueOrderIntent:
    return VenueOrderIntent(
        venue="kalshi",
        instrument_id="HIGHNY-24JAN01-T60",
        client_order_id=client_order_id,
        book_side="bid",
        quantity=Decimal(quantity),
        limit_price=Decimal("0.560000"),
        time_in_force="good_till_canceled",
        post_only=True,
    )


def _provenance(*, fingerprint: str | None = "a" * 64) -> VenueIntentProvenance:
    return VenueIntentProvenance(
        source="strategy_orchestrator",
        source_id="signal-1",
        decision_id="decision-1",
        strategy_key="weather_edge",
        strategy_version=3,
        trace_id="trace-1",
        authenticated_principal_fingerprint=fingerprint,
    )


def _ack(*, provider_order_id: str = "provider-order-1") -> VenueInitialAcknowledgement:
    return VenueInitialAcknowledgement(
        venue="kalshi",
        client_order_id="client-1",
        provider_order_id=provider_order_id,
        provider_status="resting",
        filled_quantity=Decimal("1.25"),
        remaining_quantity=Decimal("11.09"),
        provider_timestamp=datetime(2026, 8, 4, 12, 1, tzinfo=timezone.utc),
        payload={"order_id": provider_order_id, "fill_count_fp": "1.25"},
    )


def test_principal_fingerprint_must_be_an_opaque_lowercase_sha256() -> None:
    assert (
        VenueIntentProvenance(
            source="test",
            authenticated_principal_fingerprint="a" * 64,
        ).authenticated_principal_fingerprint
        == "a" * 64
    )

    for invalid in ("", "A" * 64, "a" * 63, "not-a-fingerprint"):
        with pytest.raises(ValueError, match="authenticated principal fingerprint"):
            VenueIntentProvenance(source="test", authenticated_principal_fingerprint=invalid)


@pytest.mark.db
@pytest.mark.asyncio
async def test_record_intent_persists_exact_values_and_initial_event() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "venue_ledger_intent")
    try:
        async with session_factory() as session, session.begin():
            record = await VenueExecutionLedger(session).record_intent(_intent(), _provenance())
            record_id = record.id

        async with session_factory() as session:
            persisted = await session.get(VenueOrderIntentRecord, record_id)
            events = (
                (await session.execute(select(VenueExecutionEvent).where(VenueExecutionEvent.intent_id == record_id)))
                .scalars()
                .all()
            )

        assert persisted is not None
        assert persisted.quantity == Decimal("12.340000000000000000")
        assert persisted.limit_price == Decimal("0.560000000000000000")
        assert persisted.strategy_version == 3
        assert persisted.authenticated_principal_fingerprint == "a" * 64
        assert [(event.sequence, event.event_type, event.dedupe_key) for event in events] == [
            (1, "intent_recorded", "intent_recorded:v1")
        ]
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_identical_intent_replay_is_idempotent_but_collision_fails() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "venue_ledger_replay")
    try:
        async with session_factory() as session, session.begin():
            first = await VenueExecutionLedger(session).record_intent(_intent(), _provenance())
        async with session_factory() as session, session.begin():
            replay = await VenueExecutionLedger(session).record_intent(_intent(), _provenance())
        assert replay.id == first.id

        async with session_factory() as session:
            event_count = await session.scalar(select(func.count()).select_from(VenueExecutionEvent))
        assert event_count == 1

        async with session_factory() as session, session.begin():
            with pytest.raises(VenueExecutionConflictError, match="client_order_id"):
                await VenueExecutionLedger(session).record_intent(_intent(quantity="12.35"), _provenance())
        async with session_factory() as session, session.begin():
            with pytest.raises(VenueExecutionConflictError, match="client_order_id"):
                await VenueExecutionLedger(session).record_intent(_intent(), _provenance(fingerprint="b" * 64))
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_intent_and_initial_event_roll_back_together() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "venue_ledger_rollback")
    try:
        async with session_factory() as session:
            ledger = VenueExecutionLedger(session)
            ledger._initial_event_type = ""
            with pytest.raises(DBAPIError):
                async with session.begin():
                    await ledger.record_intent(_intent(), _provenance())

        async with session_factory() as session:
            intent_count = await session.scalar(select(func.count()).select_from(VenueOrderIntentRecord))
            event_count = await session.scalar(select(func.count()).select_from(VenueExecutionEvent))
        assert intent_count == 0
        assert event_count == 0
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_initial_acknowledgement_is_atomic_exact_and_idempotent() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "venue_ledger_ack")
    try:
        async with session_factory() as session, session.begin():
            intent = await VenueExecutionLedger(session).record_intent(_intent(), _provenance())
            intent_id = intent.id

        async with session_factory() as session, session.begin():
            first = await VenueExecutionLedger(session).record_initial_acknowledgement(intent_id, _ack())
        async with session_factory() as session, session.begin():
            replay = await VenueExecutionLedger(session).record_initial_acknowledgement(intent_id, _ack())

        assert replay.intent_id == first.intent_id
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
        assert acknowledgement.filled_quantity == Decimal("1.250000000000000000")
        assert acknowledgement.remaining_quantity == Decimal("11.090000000000000000")
        assert [event.event_type for event in events] == [
            "intent_recorded",
            "submission_acknowledged",
        ]
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_acknowledgement_identity_and_quantity_invariants_fail_closed() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "venue_ledger_ack_guard")
    try:
        async with session_factory() as session, session.begin():
            intent = await VenueExecutionLedger(session).record_intent(_intent(), _provenance())
            intent_id = intent.id

        mismatched_client = VenueInitialAcknowledgement(
            venue="kalshi",
            client_order_id="different-client",
            provider_order_id="provider-order-1",
            provider_status="resting",
            filled_quantity=Decimal("1.25"),
            remaining_quantity=Decimal("11.09"),
            provider_timestamp=datetime(2026, 8, 4, 12, 1, tzinfo=timezone.utc),
            payload={},
        )
        async with session_factory() as session, session.begin():
            with pytest.raises(VenueExecutionConflictError, match="client_order_id"):
                await VenueExecutionLedger(session).record_initial_acknowledgement(intent_id, mismatched_client)

        bad_total = VenueInitialAcknowledgement(
            venue="kalshi",
            client_order_id="client-1",
            provider_order_id="provider-order-1",
            provider_status="resting",
            filled_quantity=Decimal("1.25"),
            remaining_quantity=Decimal("11.08"),
            provider_timestamp=datetime(2026, 8, 4, 12, 1, tzinfo=timezone.utc),
            payload={},
        )
        async with session_factory() as session, session.begin():
            with pytest.raises(VenueExecutionConflictError, match="quantity"):
                await VenueExecutionLedger(session).record_initial_acknowledgement(intent_id, bad_total)
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_provider_order_id_cannot_attach_to_another_intent() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "venue_ledger_provider_identity")
    try:
        async with session_factory() as session, session.begin():
            ledger = VenueExecutionLedger(session)
            first = await ledger.record_intent(_intent(), _provenance())
            second = await ledger.record_intent(
                _intent(client_order_id="client-2"),
                VenueIntentProvenance(source="strategy_orchestrator", source_id="signal-2"),
            )

        async with session_factory() as session, session.begin():
            await VenueExecutionLedger(session).record_initial_acknowledgement(first.id, _ack())

        second_ack = VenueInitialAcknowledgement(
            venue="kalshi",
            client_order_id="client-2",
            provider_order_id="provider-order-1",
            provider_status="resting",
            filled_quantity=Decimal("1.25"),
            remaining_quantity=Decimal("11.09"),
            provider_timestamp=datetime(2026, 8, 4, 12, 1, tzinfo=timezone.utc),
            payload={},
        )
        async with session_factory() as session, session.begin():
            with pytest.raises(VenueExecutionConflictError, match="provider_order_id"):
                await VenueExecutionLedger(session).record_initial_acknowledgement(second.id, second_ack)
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_execution_event_replay_is_idempotent_and_collision_fails() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "venue_ledger_event")
    try:
        async with session_factory() as session, session.begin():
            intent = await VenueExecutionLedger(session).record_intent(_intent(), _provenance())
            intent_id = intent.id

        occurred_at = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
        async with session_factory() as session, session.begin():
            first = await VenueExecutionLedger(session).record_event(
                intent_id,
                event_type="submission_started",
                source="execution_boundary",
                dedupe_key="submission_started:attempt-1",
                occurred_at=occurred_at,
                payload={"attempt_id": "attempt-1"},
            )
        async with session_factory() as session, session.begin():
            replay = await VenueExecutionLedger(session).record_event(
                intent_id,
                event_type="submission_started",
                source="execution_boundary",
                dedupe_key="submission_started:attempt-1",
                occurred_at=occurred_at,
                payload={"attempt_id": "attempt-1"},
            )
        assert replay.id == first.id
        assert replay.sequence == 2

        async with session_factory() as session, session.begin():
            with pytest.raises(VenueExecutionConflictError, match="dedupe_key"):
                await VenueExecutionLedger(session).record_event(
                    intent_id,
                    event_type="submission_unknown",
                    source="execution_boundary",
                    dedupe_key="submission_started:attempt-1",
                    occurred_at=occurred_at,
                    payload={"attempt_id": "attempt-1"},
                )
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_provider_event_identity_is_bound_to_one_acknowledged_order() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "venue_ledger_provider_event")
    try:
        async with session_factory() as session, session.begin():
            ledger = VenueExecutionLedger(session)
            first = await ledger.record_intent(_intent(), _provenance())
            second = await ledger.record_intent(
                _intent(client_order_id="client-2"),
                VenueIntentProvenance(source="strategy_orchestrator", source_id="signal-2"),
            )
        async with session_factory() as session, session.begin():
            ledger = VenueExecutionLedger(session)
            await ledger.record_initial_acknowledgement(first.id, _ack())
            await ledger.record_initial_acknowledgement(
                second.id,
                VenueInitialAcknowledgement(
                    venue="kalshi",
                    client_order_id="client-2",
                    provider_order_id="provider-order-2",
                    provider_status="resting",
                    filled_quantity=Decimal("1.25"),
                    remaining_quantity=Decimal("11.09"),
                    provider_timestamp=datetime(2026, 8, 4, 12, 1, tzinfo=timezone.utc),
                    payload={},
                ),
            )

        occurred_at = datetime(2026, 8, 4, 12, 2, tzinfo=timezone.utc)
        async with session_factory() as session, session.begin():
            with pytest.raises(VenueExecutionConflictError, match="provider_order_id"):
                await VenueExecutionLedger(session).record_event(
                    first.id,
                    event_type="fill_observed",
                    source="kalshi_rest_reconciliation",
                    dedupe_key="fill_observed:wrong-order:fill-1",
                    provider_order_id="provider-order-2",
                    provider_event_id="fill-1",
                    occurred_at=occurred_at,
                    payload={},
                )

        async with session_factory() as session, session.begin():
            await VenueExecutionLedger(session).record_event(
                first.id,
                event_type="fill_observed",
                source="kalshi_rest_reconciliation",
                dedupe_key="fill_observed:provider-order-1:fill-1",
                provider_order_id="provider-order-1",
                provider_event_id="fill-1",
                occurred_at=occurred_at,
                payload={},
            )
        async with session_factory() as session, session.begin():
            with pytest.raises(VenueExecutionConflictError, match="provider_event_id"):
                await VenueExecutionLedger(session).record_event(
                    second.id,
                    event_type="fill_observed",
                    source="kalshi_rest_reconciliation",
                    dedupe_key="fill_observed:provider-order-2:fill-1",
                    provider_order_id="provider-order-2",
                    provider_event_id="fill-1",
                    occurred_at=occurred_at,
                    payload={},
                )
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_database_rejects_cross_order_provider_evidence() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "venue_ledger_provider_event_db_guard")
    try:
        async with session_factory() as session, session.begin():
            ledger = VenueExecutionLedger(session)
            first = await ledger.record_intent(_intent(), _provenance())
            second = await ledger.record_intent(
                _intent(client_order_id="client-2"),
                VenueIntentProvenance(source="strategy_orchestrator", source_id="signal-2"),
            )
            await ledger.record_initial_acknowledgement(first.id, _ack())
            await ledger.record_initial_acknowledgement(
                second.id,
                VenueInitialAcknowledgement(
                    venue="kalshi",
                    client_order_id="client-2",
                    provider_order_id="provider-order-2",
                    provider_status="resting",
                    filled_quantity=Decimal("1.25"),
                    remaining_quantity=Decimal("11.09"),
                    provider_timestamp=datetime(2026, 8, 4, 12, 1, tzinfo=timezone.utc),
                    payload={},
                ),
            )

        async with session_factory() as session:
            with pytest.raises(DBAPIError, match="provider order identity"):
                await session.execute(
                    insert(VenueExecutionEvent).values(
                        id="bad-provider-event",
                        intent_id=first.id,
                        venue="kalshi",
                        sequence=3,
                        event_type="fill_observed",
                        source="raw_probe",
                        dedupe_key="fill_observed:wrong-order:fill-raw",
                        provider_order_id="provider-order-2",
                        provider_event_id="fill-raw",
                        occurred_at=datetime(2026, 8, 4, 12, 2, tzinfo=timezone.utc),
                        payload_json={},
                        created_at=datetime(2026, 8, 4, 12, 2, tzinfo=timezone.utc),
                    )
                )
                await session.commit()
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_database_rejects_execution_ledger_mutation() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "venue_ledger_immutable")
    try:
        async with session_factory() as session, session.begin():
            intent = await VenueExecutionLedger(session).record_intent(_intent(), _provenance())
            intent_id = intent.id

        async with session_factory() as session:
            with pytest.raises(DBAPIError, match="immutable"):
                await session.execute(
                    update(VenueOrderIntentRecord)
                    .where(VenueOrderIntentRecord.id == intent_id)
                    .values(source="mutated")
                )
                await session.commit()
            await session.rollback()

        async with session_factory() as session:
            with pytest.raises(DBAPIError, match="immutable"):
                await session.execute(delete(VenueExecutionEvent).where(VenueExecutionEvent.intent_id == intent_id))
                await session.commit()
            await session.rollback()

        async with session_factory() as session:
            with pytest.raises(DBAPIError, match="immutable"):
                await session.execute(text("TRUNCATE venue_execution_events"))
                await session.commit()
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_database_enforces_acknowledgement_identity_and_quantity() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "venue_ledger_ack_db_guard")
    try:
        async with session_factory() as session, session.begin():
            intent = await VenueExecutionLedger(session).record_intent(_intent(), _provenance())
            intent_id = intent.id

        base_values = {
            "intent_id": intent_id,
            "venue": "kalshi",
            "client_order_id": "client-1",
            "provider_order_id": "provider-order-raw",
            "provider_status": "resting",
            "filled_quantity": Decimal("1.25"),
            "remaining_quantity": Decimal("11.09"),
            "provider_timestamp": datetime(2026, 8, 4, 12, 1, tzinfo=timezone.utc),
            "payload_json": {},
            "created_at": datetime(2026, 8, 4, 12, 1, tzinfo=timezone.utc),
        }
        async with session_factory() as session:
            with pytest.raises(DBAPIError):
                await session.execute(
                    insert(VenueProviderAcknowledgementRecord).values(
                        **{**base_values, "client_order_id": "wrong-client"}
                    )
                )
                await session.commit()
            await session.rollback()

        async with session_factory() as session:
            with pytest.raises(DBAPIError, match="quantity"):
                await session.execute(
                    insert(VenueProviderAcknowledgementRecord).values(
                        **{**base_values, "remaining_quantity": Decimal("11.08")}
                    )
                )
                await session.commit()
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_database_rejects_non_finite_intent_quantity() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "venue_ledger_non_finite_quantity")
    try:
        async with session_factory() as session:
            with pytest.raises(DBAPIError):
                await session.execute(
                    insert(VenueOrderIntentRecord).values(
                        id="nan-intent",
                        venue="kalshi",
                        client_order_id="nan-client",
                        instrument_id="HIGHNY-24JAN01-T60",
                        book_side="bid",
                        quantity=Decimal("NaN"),
                        limit_price=Decimal("0.56"),
                        time_in_force="good_till_canceled",
                        post_only=False,
                        source="raw_probe",
                        created_at=datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc),
                    )
                )
                await session.commit()
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_numeric_envelope_rejects_unrepresentable_precision() -> None:
    with pytest.raises(ValueError, match="18 decimal places"):
        await VenueExecutionLedger(None).record_intent(  # type: ignore[arg-type]
            _intent(quantity="1.0000000000000000001"),
            _provenance(),
        )


@pytest.mark.db
@pytest.mark.asyncio
async def test_numeric_envelope_accepts_full_twenty_integer_digits() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "venue_ledger_numeric_envelope")
    try:
        async with session_factory() as session, session.begin():
            with localcontext() as decimal_context:
                decimal_context.prec = 2
                record = await VenueExecutionLedger(session).record_intent(
                    _intent(quantity="99999999999999999999.000000000000000001"),
                    _provenance(),
                )
            record_id = record.id
        async with session_factory() as session:
            persisted = await session.get(VenueOrderIntentRecord, record_id)
        assert persisted is not None
        assert persisted.quantity == Decimal("99999999999999999999.000000000000000001")
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_acknowledgement_quantity_check_ignores_ambient_decimal_context() -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "venue_ledger_ack_decimal_context")
    try:
        async with session_factory() as session, session.begin():
            intent = await VenueExecutionLedger(session).record_intent(_intent(), _provenance())
            intent_id = intent.id
        async with session_factory() as session, session.begin():
            with localcontext() as decimal_context:
                decimal_context.prec = 2
                acknowledgement = await VenueExecutionLedger(session).record_initial_acknowledgement(intent_id, _ack())
        assert acknowledgement.filled_quantity == Decimal("1.250000000000000000")
        assert acknowledgement.remaining_quantity == Decimal("11.090000000000000000")
    finally:
        await engine.dispose()
