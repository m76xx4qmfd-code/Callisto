"""Transactional persistence for venue-neutral order execution evidence.

This module has no venue client, network, credential, route, or worker dependency.
Callers own the transaction. A future execution boundary must commit an intent and
`submission_started` event before transmitting anything to a venue.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    VenueExecutionEvent,
    VenueOrderIntentRecord,
    VenueProviderAcknowledgementRecord,
)
from services.venues.contracts import VenueOrderIntent

_NUMERIC_SCALE = 18
_NUMERIC_LIMIT = Decimal("1e20")


class VenueExecutionConflictError(RuntimeError):
    """Raised when a stable venue identity maps to conflicting financial facts."""


@dataclass(frozen=True)
class VenueIntentProvenance:
    source: str
    source_id: str | None = None
    decision_id: str | None = None
    strategy_key: str | None = None
    strategy_version: int | None = None
    trace_id: str | None = None
    authenticated_principal_fingerprint: str | None = None

    def __post_init__(self) -> None:
        source = self.source.strip()
        if not source:
            raise ValueError("provenance source is required")
        fingerprint = self.authenticated_principal_fingerprint
        if fingerprint is not None and re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
            raise ValueError("authenticated principal fingerprint must be a lowercase SHA-256")
        object.__setattr__(self, "source", source)


@dataclass(frozen=True)
class VenueInitialAcknowledgement:
    """The first usable provider acknowledgement, never a mutable status snapshot."""

    venue: str
    client_order_id: str
    provider_order_id: str
    provider_status: str
    filled_quantity: Decimal
    remaining_quantity: Decimal
    provider_timestamp: datetime
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        venue = self.venue.strip()
        client_order_id = self.client_order_id.strip()
        provider_order_id = self.provider_order_id.strip()
        provider_status = self.provider_status.strip()
        if venue not in {"kalshi", "polymarket"}:
            raise ValueError("unsupported acknowledgement venue")
        if not client_order_id or not provider_order_id or not provider_status:
            raise ValueError("acknowledgement identities and status are required")
        filled_quantity = _exact_numeric(self.filled_quantity, "filled_quantity")
        remaining_quantity = _exact_numeric(self.remaining_quantity, "remaining_quantity")
        if filled_quantity < 0 or remaining_quantity < 0:
            raise ValueError("acknowledgement quantities cannot be negative")
        provider_timestamp = self.provider_timestamp
        if provider_timestamp.tzinfo is None:
            raise ValueError("provider_timestamp must be timezone-aware")
        provider_timestamp = provider_timestamp.astimezone(timezone.utc)
        object.__setattr__(self, "venue", venue)
        object.__setattr__(self, "client_order_id", client_order_id)
        object.__setattr__(self, "provider_order_id", provider_order_id)
        object.__setattr__(self, "provider_status", provider_status)
        object.__setattr__(self, "filled_quantity", filled_quantity)
        object.__setattr__(self, "remaining_quantity", remaining_quantity)
        object.__setattr__(self, "provider_timestamp", provider_timestamp)
        object.__setattr__(self, "payload", _json_evidence(self.payload))


class VenueExecutionLedger:
    """Append-only order ledger operations within a caller-owned transaction."""

    _initial_event_type = "intent_recorded"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_intent(
        self,
        intent: VenueOrderIntent,
        provenance: VenueIntentProvenance,
    ) -> VenueOrderIntentRecord:
        quantity = _exact_numeric(intent.quantity, "quantity")
        limit_price = _exact_numeric(intent.limit_price, "limit_price")
        values = {
            "id": str(uuid4()),
            "venue": intent.venue,
            "client_order_id": intent.client_order_id,
            "instrument_id": intent.instrument_id,
            "book_side": intent.book_side,
            "quantity": quantity,
            "limit_price": limit_price,
            "time_in_force": intent.time_in_force,
            "post_only": intent.post_only,
            "source": provenance.source,
            "source_id": _optional_text(provenance.source_id),
            "decision_id": _optional_text(provenance.decision_id),
            "strategy_key": _optional_text(provenance.strategy_key),
            "strategy_version": provenance.strategy_version,
            "trace_id": _optional_text(provenance.trace_id),
            "authenticated_principal_fingerprint": provenance.authenticated_principal_fingerprint,
            "created_at": datetime.now(timezone.utc),
        }
        inserted_id = await self._session.scalar(
            pg_insert(VenueOrderIntentRecord)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[
                    VenueOrderIntentRecord.venue,
                    VenueOrderIntentRecord.client_order_id,
                ]
            )
            .returning(VenueOrderIntentRecord.id)
        )
        if inserted_id is None:
            existing = await self._session.scalar(
                select(VenueOrderIntentRecord).where(
                    VenueOrderIntentRecord.venue == intent.venue,
                    VenueOrderIntentRecord.client_order_id == intent.client_order_id,
                )
            )
            if existing is None:
                raise VenueExecutionConflictError("client_order_id conflict did not expose a canonical intent")
            if not _intent_matches(existing, values):
                raise VenueExecutionConflictError(
                    "client_order_id is already bound to different immutable intent facts"
                )
            return existing

        event = VenueExecutionEvent(
            id=str(uuid4()),
            intent_id=inserted_id,
            venue=intent.venue,
            sequence=1,
            event_type=self._initial_event_type,
            source=provenance.source,
            dedupe_key="intent_recorded:v1",
            occurred_at=values["created_at"],
            payload_json={},
            created_at=values["created_at"],
        )
        self._session.add(event)
        await self._session.flush()
        record = await self._session.get(VenueOrderIntentRecord, inserted_id)
        if record is None:
            raise RuntimeError("inserted venue order intent was not readable")
        return record

    async def record_event(
        self,
        intent_id: str,
        *,
        event_type: str,
        source: str,
        dedupe_key: str,
        occurred_at: datetime,
        payload: Mapping[str, Any] | None = None,
        provider_order_id: str | None = None,
        provider_event_id: str | None = None,
    ) -> VenueExecutionEvent:
        """Append idempotent lifecycle evidence while serializing per intent.

        Stable attempt IDs belong in ``dedupe_key``. In particular, a committed
        ``submission_started`` without a conclusive later event is treated as
        submission-unknown after restart and must be reconciled before retry.
        """

        event_type = event_type.strip()
        source = source.strip()
        dedupe_key = dedupe_key.strip()
        provider_order_id = _optional_text(provider_order_id)
        provider_event_id = _optional_text(provider_event_id)
        if not event_type or event_type == "intent_recorded":
            raise ValueError("event_type is required and intent_recorded is reserved")
        if not source or not dedupe_key:
            raise ValueError("event source and dedupe_key are required")
        if occurred_at.tzinfo is None:
            raise ValueError("event occurred_at must be timezone-aware")
        occurred_at = occurred_at.astimezone(timezone.utc)
        payload_json = _json_evidence(payload or {})

        intent = await self._session.scalar(
            select(VenueOrderIntentRecord).where(VenueOrderIntentRecord.id == intent_id).with_for_update()
        )
        if intent is None:
            raise VenueExecutionConflictError("unknown order intent")
        if provider_event_id is not None and provider_order_id is None:
            raise ValueError("provider_event_id requires provider_order_id")
        if provider_order_id is not None:
            acknowledgement = await self._session.get(VenueProviderAcknowledgementRecord, intent_id)
            if acknowledgement is None or acknowledgement.provider_order_id != provider_order_id:
                raise VenueExecutionConflictError(
                    "provider_order_id does not match the intent's immutable acknowledgement"
                )
        existing = await self._session.scalar(
            select(VenueExecutionEvent).where(
                VenueExecutionEvent.intent_id == intent_id,
                VenueExecutionEvent.dedupe_key == dedupe_key,
            )
        )
        expected = {
            "venue": intent.venue,
            "event_type": event_type,
            "source": source,
            "dedupe_key": dedupe_key,
            "provider_order_id": provider_order_id,
            "provider_event_id": provider_event_id,
            "occurred_at": occurred_at,
            "payload_json": payload_json,
        }
        if existing is not None:
            if not _event_matches(existing, expected):
                raise VenueExecutionConflictError("event dedupe_key is already bound to different immutable evidence")
            return existing

        if provider_event_id is not None:
            provider_event_owner = await self._session.scalar(
                select(VenueExecutionEvent).where(
                    VenueExecutionEvent.venue == intent.venue,
                    VenueExecutionEvent.provider_event_id == provider_event_id,
                )
            )
            if provider_event_owner is not None:
                raise VenueExecutionConflictError("provider_event_id is already bound to different immutable evidence")

        next_sequence = (
            await self._session.scalar(
                select(func.max(VenueExecutionEvent.sequence)).where(VenueExecutionEvent.intent_id == intent_id)
            )
            or 0
        ) + 1
        event = VenueExecutionEvent(
            id=str(uuid4()),
            intent_id=intent_id,
            sequence=next_sequence,
            **expected,
            created_at=datetime.now(timezone.utc),
        )
        self._session.add(event)
        await self._session.flush()
        return event

    async def record_initial_acknowledgement(
        self,
        intent_id: str,
        acknowledgement: VenueInitialAcknowledgement,
    ) -> VenueProviderAcknowledgementRecord:
        intent = await self._session.scalar(
            select(VenueOrderIntentRecord).where(VenueOrderIntentRecord.id == intent_id).with_for_update()
        )
        if intent is None:
            raise VenueExecutionConflictError("unknown order intent")
        if intent.venue != acknowledgement.venue:
            raise VenueExecutionConflictError("acknowledgement venue does not match intent")
        if intent.client_order_id != acknowledgement.client_order_id:
            raise VenueExecutionConflictError("acknowledgement client_order_id does not match intent")
        with localcontext() as decimal_context:
            decimal_context.prec = 39
            acknowledged_quantity = acknowledgement.filled_quantity + acknowledgement.remaining_quantity
        if acknowledged_quantity != intent.quantity:
            raise VenueExecutionConflictError("acknowledgement quantity does not match the intended quantity")

        created_at = datetime.now(timezone.utc)
        values = {
            "intent_id": intent.id,
            "venue": intent.venue,
            "client_order_id": intent.client_order_id,
            "provider_order_id": acknowledgement.provider_order_id,
            "provider_status": acknowledgement.provider_status,
            "filled_quantity": acknowledgement.filled_quantity,
            "remaining_quantity": acknowledgement.remaining_quantity,
            "provider_timestamp": acknowledgement.provider_timestamp,
            "payload_json": dict(acknowledgement.payload),
            "created_at": created_at,
        }
        inserted_intent_id = await self._session.scalar(
            pg_insert(VenueProviderAcknowledgementRecord)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(VenueProviderAcknowledgementRecord.intent_id)
        )
        if inserted_intent_id is None:
            existing = await self._session.get(VenueProviderAcknowledgementRecord, intent.id)
            if existing is not None:
                if not _acknowledgement_matches(existing, values):
                    raise VenueExecutionConflictError(
                        "intent already has a different immutable provider acknowledgement"
                    )
                return existing
            provider_owner = await self._session.scalar(
                select(VenueProviderAcknowledgementRecord).where(
                    VenueProviderAcknowledgementRecord.venue == acknowledgement.venue,
                    VenueProviderAcknowledgementRecord.provider_order_id == acknowledgement.provider_order_id,
                )
            )
            if provider_owner is not None:
                raise VenueExecutionConflictError("provider_order_id is already bound to another order intent")
            raise VenueExecutionConflictError("provider acknowledgement conflict did not expose a canonical record")

        next_sequence = (
            await self._session.scalar(
                select(func.max(VenueExecutionEvent.sequence)).where(VenueExecutionEvent.intent_id == intent.id)
            )
            or 0
        ) + 1
        self._session.add(
            VenueExecutionEvent(
                id=str(uuid4()),
                intent_id=intent.id,
                venue=intent.venue,
                sequence=next_sequence,
                event_type="submission_acknowledged",
                source="provider_acknowledgement",
                dedupe_key=f"submission_acknowledged:{acknowledgement.provider_order_id}",
                provider_order_id=acknowledgement.provider_order_id,
                occurred_at=acknowledgement.provider_timestamp,
                payload_json=dict(acknowledgement.payload),
                created_at=created_at,
            )
        )
        await self._session.flush()
        record = await self._session.get(VenueProviderAcknowledgementRecord, intent.id)
        if record is None:
            raise RuntimeError("inserted provider acknowledgement was not readable")
        return record


def _exact_numeric(value: Decimal, field_name: str) -> Decimal:
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except ValueError as exc:
        raise ValueError(f"{field_name} is outside the exact numeric envelope") from exc
    exponent = decimal_value.as_tuple().exponent
    if not decimal_value.is_finite() or not isinstance(exponent, int) or exponent < -_NUMERIC_SCALE:
        raise ValueError(f"{field_name} exceeds 18 decimal places")
    if decimal_value.copy_abs() >= _NUMERIC_LIMIT:
        raise ValueError(f"{field_name} exceeds 20 integer digits")
    return decimal_value


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _json_evidence(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_evidence(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_evidence(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("JSON evidence datetimes must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat()
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        raise TypeError("JSON financial evidence cannot contain binary floats")
    raise TypeError(f"unsupported JSON evidence type: {type(value).__name__}")


def _intent_matches(record: VenueOrderIntentRecord, values: Mapping[str, Any]) -> bool:
    fields = (
        "venue",
        "client_order_id",
        "instrument_id",
        "book_side",
        "quantity",
        "limit_price",
        "time_in_force",
        "post_only",
        "source",
        "source_id",
        "decision_id",
        "strategy_key",
        "strategy_version",
        "trace_id",
        "authenticated_principal_fingerprint",
    )
    return all(getattr(record, field_name) == values[field_name] for field_name in fields)


def _event_matches(record: VenueExecutionEvent, values: Mapping[str, Any]) -> bool:
    fields = (
        "venue",
        "event_type",
        "source",
        "dedupe_key",
        "provider_order_id",
        "provider_event_id",
        "occurred_at",
        "payload_json",
    )
    return all(getattr(record, field_name) == values[field_name] for field_name in fields)


def _acknowledgement_matches(
    record: VenueProviderAcknowledgementRecord,
    values: Mapping[str, Any],
) -> bool:
    fields = (
        "intent_id",
        "venue",
        "client_order_id",
        "provider_order_id",
        "provider_status",
        "filled_quantity",
        "remaining_quantity",
        "provider_timestamp",
        "payload_json",
    )
    return all(getattr(record, field_name) == values[field_name] for field_name in fields)


__all__ = [
    "VenueExecutionConflictError",
    "VenueExecutionLedger",
    "VenueInitialAcknowledgement",
    "VenueIntentProvenance",
]
