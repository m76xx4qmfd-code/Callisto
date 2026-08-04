"""Disconnected durable authenticated-principal Kalshi portfolio evidence sweep.

All venue I/O is GET-only and happens before the persistence transaction.  A
``complete`` checkpoint means only that the four bounded traversals terminated,
the modeled observations agreed, fills linked to observed orders, and the two
archive boundary reads were equal.  It is not a transactional snapshot, a
private-stream health signal, or retry authorization.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from models.database import (
    KalshiPortfolioCoverageCheckpoint,
    KalshiPortfolioFillObservation,
    KalshiPortfolioOrderIdentity,
    KalshiPortfolioOrderObservation,
    VenueExecutionEvent,
    VenueOrderIntentRecord,
    VenueProviderAcknowledgementRecord,
)
from services.venues.kalshi_v2 import (
    KalshiFill,
    KalshiFillsPage,
    KalshiHistoricalCutoff,
    KalshiOrder,
    KalshiOrdersPage,
)


class KalshiPortfolioCoverageConflictError(RuntimeError):
    """Evidence or caller-stable identity conflicted and no checkpoint was committed."""


class KalshiPortfolioReadClient(Protocol):
    @property
    def principal_fingerprint(self) -> str: ...

    async def get_historical_cutoff(self) -> KalshiHistoricalCutoff: ...

    async def get_orders(self, *, limit: int = 100, cursor: str | None = None) -> KalshiOrdersPage: ...

    async def get_fills(self, *, limit: int = 100, cursor: str | None = None) -> KalshiFillsPage: ...

    async def get_historical_orders(
        self, *, max_ts: int | None = None, limit: int = 100, cursor: str | None = None
    ) -> KalshiOrdersPage: ...

    async def get_historical_fills(
        self, *, max_ts: int | None = None, limit: int = 100, cursor: str | None = None
    ) -> KalshiFillsPage: ...


@dataclass(frozen=True)
class KalshiPortfolioCoverageResult:
    principal_fingerprint: str
    coverage_id: str
    observed_at: datetime
    status: Literal["complete", "incomplete"]
    reason: str
    page_counts: dict[str, int]
    unique_counts: dict[str, int]
    observed_evidence_hash: str
    unknown_order_ids: tuple[str, ...]
    unknown_client_order_ids: tuple[str, ...]
    unknown_fill_ids: tuple[str, ...]
    retry_allowed: Literal[False] = False


@dataclass(frozen=True)
class _Traversal:
    records: tuple[object, ...]
    pages: int


_RFC3339_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(?P<fraction>\d+))?(?:Z|[+-]\d{2}:\d{2})$")
_ORDER_IMMUTABLE_FIELDS = (
    "order_id",
    "provider_user_hash",
    "client_order_id",
    "ticker",
    "outcome_side",
    "book_side",
    "order_type",
    "yes_price",
    "no_price",
    "initial_count",
    "created_time",
    "expiration_time",
    "subaccount_number",
    "exchange_index",
)


class KalshiPortfolioCoverageService:
    def __init__(self, session_factory: sessionmaker, client: KalshiPortfolioReadClient) -> None:
        self._session_factory = session_factory
        self._client = client

    async def sweep(self, coverage_id: str, observed_at: datetime) -> KalshiPortfolioCoverageResult:
        principal_fingerprint = self._client.principal_fingerprint
        if not isinstance(principal_fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", principal_fingerprint) is None:
            raise KalshiPortfolioCoverageConflictError("Kalshi client principal fingerprint is invalid")
        if not isinstance(coverage_id, str):
            raise TypeError("coverage_id must be a string")
        coverage_id = coverage_id.strip()
        if not coverage_id:
            raise ValueError("coverage_id is required")
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        observed_at = observed_at.astimezone(timezone.utc)

        existing = await self._load_checkpoint(principal_fingerprint, coverage_id)
        if existing is not None:
            return _result_from_checkpoint(existing)

        cutoff_before = await self._client.get_historical_cutoff()
        current_orders = await self._traverse("current orders", self._client.get_orders)
        current_fills = await self._traverse("current fills", self._client.get_fills)
        historical_orders = await self._traverse(
            "historical orders",
            self._client.get_historical_orders,
            max_ts=_ceil_epoch(cutoff_before.orders_updated_at),
        )
        historical_fills = await self._traverse(
            "historical fills",
            self._client.get_historical_fills,
            max_ts=_ceil_epoch(cutoff_before.trades_created_at),
        )
        cutoff_after = await self._client.get_historical_cutoff()

        current_order_map, current_order_conflicts = _dedupe_orders(current_orders.records)
        historical_order_map, historical_order_conflicts = _dedupe_orders(historical_orders.records)
        orders, overlap_conflicts = _dedupe_orders(current_orders.records + historical_orders.records)
        current_fill_map = _dedupe_fills(current_fills.records)
        historical_fill_map = _dedupe_fills(historical_fills.records)
        fills = _merge_fills(current_fill_map, historical_fill_map)

        incomplete_reasons = [
            *current_order_conflicts,
            *historical_order_conflicts,
            *overlap_conflicts,
        ]
        if cutoff_before.orders_updated_at != cutoff_after.orders_updated_at:
            incomplete_reasons.append("cutoff_drift:orders")
        if cutoff_before.trades_created_at != cutoff_after.trades_created_at:
            incomplete_reasons.append("cutoff_drift:fills")
        orphan_order_ids = sorted({fill.order_id for fill in fills.values()} - set(orders))
        if orphan_order_ids:
            incomplete_reasons.append("orphan_fill:" + ",".join(orphan_order_ids))
        provider_user_hashes = {hashlib.sha256(order.user_id.encode()).hexdigest() for order in orders.values()}
        if len(provider_user_hashes) > 1:
            incomplete_reasons.append("provider_user_conflict")
        fills_by_order: dict[str, list[KalshiFill]] = {}
        for fill in fills.values():
            order = orders.get(fill.order_id)
            if order is None:
                continue
            fills_by_order.setdefault(fill.order_id, []).append(fill)
            if (
                fill.ticker != order.ticker
                or fill.market_ticker != order.ticker
                or fill.outcome_side != order.outcome_side
                or fill.book_side != order.book_side
                or fill.subaccount_number != order.subaccount_number
            ):
                incomplete_reasons.append(f"fill_order_mismatch:{fill.fill_id}")
        for order_id, order_fills in fills_by_order.items():
            if sum((Fraction(fill.count) for fill in order_fills), Fraction()) > Fraction(
                orders[order_id].initial_count
            ):
                incomplete_reasons.append(f"fill_count_mismatch:{order_id}")

        known_orders, known_fills = await self._known_local_evidence(principal_fingerprint, orders, fills)
        unknown_order_ids = tuple(sorted(set(orders) - known_orders))
        unknown_client_order_ids = tuple(sorted({orders[order_id].client_order_id for order_id in unknown_order_ids}))
        unknown_fill_ids = tuple(sorted(set(fills) - known_fills))

        order_payloads = {order_id: _order_payload(order) for order_id, order in orders.items()}
        fill_payloads = {fill_id: _fill_payload(fill) for fill_id, fill in fills.items()}
        order_identity_payloads = {
            order_id: {field: payload[field] for field in _ORDER_IMMUTABLE_FIELDS}
            for order_id, payload in order_payloads.items()
        }
        order_identity_hashes = {key: _evidence_hash(payload) for key, payload in order_identity_payloads.items()}
        order_hashes = {key: _evidence_hash(payload) for key, payload in order_payloads.items()}
        fill_hashes = {key: _evidence_hash(payload) for key, payload in fill_payloads.items()}
        # This hash identifies the selected modeled observations.  It is not proof
        # that independently repeated HTTP traversals form a reproducible snapshot.
        observed_evidence_hash = _evidence_hash(
            {
                "orders": [[key, order_hashes[key]] for key in sorted(order_hashes)],
                "fills": [[key, fill_hashes[key]] for key in sorted(fill_hashes)],
            }
        )
        page_counts = {
            "current_orders": current_orders.pages,
            "current_fills": current_fills.pages,
            "historical_orders": historical_orders.pages,
            "historical_fills": historical_fills.pages,
        }
        unique_counts = {
            "current_orders": len(current_order_map),
            "current_fills": len(current_fill_map),
            "historical_orders": len(historical_order_map),
            "historical_fills": len(historical_fill_map),
            "orders": len(orders),
            "fills": len(fills),
        }
        status: Literal["complete", "incomplete"] = "incomplete" if incomplete_reasons else "complete"
        reason = ";".join(sorted(set(incomplete_reasons))) if incomplete_reasons else "bounded_evidence_complete"
        result = KalshiPortfolioCoverageResult(
            principal_fingerprint=principal_fingerprint,
            coverage_id=coverage_id,
            observed_at=observed_at,
            status=status,
            reason=reason,
            page_counts=page_counts,
            unique_counts=unique_counts,
            observed_evidence_hash=observed_evidence_hash,
            unknown_order_ids=unknown_order_ids,
            unknown_client_order_ids=unknown_client_order_ids,
            unknown_fill_ids=unknown_fill_ids,
        )

        try:
            async with self._session_factory() as session, session.begin():
                for order_id in sorted(orders):
                    order = orders[order_id]
                    await session.execute(
                        pg_insert(KalshiPortfolioOrderIdentity)
                        .values(
                            principal_fingerprint=principal_fingerprint,
                            order_id=order_id,
                            identity_hash=order_identity_hashes[order_id],
                            identity_json=order_identity_payloads[order_id],
                            first_observed_at=observed_at,
                        )
                        .on_conflict_do_nothing(index_elements=["principal_fingerprint", "order_id"])
                    )
                    identity = await session.get(
                        KalshiPortfolioOrderIdentity,
                        {"principal_fingerprint": principal_fingerprint, "order_id": order_id},
                    )
                    if identity is None or identity.identity_hash != order_identity_hashes[order_id]:
                        raise KalshiPortfolioCoverageConflictError(
                            f"order_id {order_id!r} conflicts with durable immutable order identity"
                        )
                    await session.execute(
                        pg_insert(KalshiPortfolioOrderObservation)
                        .values(
                            principal_fingerprint=principal_fingerprint,
                            order_id=order_id,
                            evidence_hash=order_hashes[order_id],
                            payload_json=order_payloads[order_id],
                            provider_updated_at=_order_updated_at(order),
                            first_observed_at=observed_at,
                        )
                        .on_conflict_do_nothing(index_elements=["principal_fingerprint", "order_id", "evidence_hash"])
                    )
                    observation = await session.get(
                        KalshiPortfolioOrderObservation,
                        {
                            "principal_fingerprint": principal_fingerprint,
                            "order_id": order_id,
                            "evidence_hash": order_hashes[order_id],
                        },
                    )
                    if observation is None or observation.payload_json != order_payloads[order_id]:
                        raise KalshiPortfolioCoverageConflictError(
                            f"order_id {order_id!r} conflicts with durable order snapshot"
                        )
                for fill_id in sorted(fills):
                    fill = fills[fill_id]
                    await session.execute(
                        pg_insert(KalshiPortfolioFillObservation)
                        .values(
                            principal_fingerprint=principal_fingerprint,
                            fill_id=fill_id,
                            evidence_hash=fill_hashes[fill_id],
                            order_id=fill.order_id,
                            payload_json=fill_payloads[fill_id],
                            provider_created_at=_fill_created_at(fill),
                            first_observed_at=observed_at,
                        )
                        .on_conflict_do_nothing(index_elements=["principal_fingerprint", "fill_id"])
                    )
                    existing_fill = await session.get(
                        KalshiPortfolioFillObservation,
                        {"principal_fingerprint": principal_fingerprint, "fill_id": fill_id},
                    )
                    if existing_fill is None or existing_fill.evidence_hash != fill_hashes[fill_id]:
                        raise KalshiPortfolioCoverageConflictError(
                            f"fill_id {fill_id!r} conflicts with durable fill evidence"
                        )
                session.add(_checkpoint_from_result(result, cutoff_before, cutoff_after))
                await session.flush()
        except IntegrityError as exc:
            concurrent = await self._load_checkpoint(principal_fingerprint, coverage_id)
            if concurrent is not None and _result_from_checkpoint(concurrent) == result:
                return result
            raise KalshiPortfolioCoverageConflictError(
                "coverage_id or durable observation conflicted with concurrently committed evidence"
            ) from exc
        return result

    async def _load_checkpoint(
        self, principal_fingerprint: str, coverage_id: str
    ) -> KalshiPortfolioCoverageCheckpoint | None:
        async with self._session_factory() as session:
            return await session.get(
                KalshiPortfolioCoverageCheckpoint,
                {"principal_fingerprint": principal_fingerprint, "coverage_id": coverage_id},
            )

    async def _known_local_evidence(
        self,
        principal_fingerprint: str,
        orders: dict[str, KalshiOrder],
        fills: dict[str, KalshiFill],
    ) -> tuple[set[str], set[str]]:
        if not orders:
            return set(), set()
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(VenueOrderIntentRecord, VenueProviderAcknowledgementRecord)
                    .join(
                        VenueProviderAcknowledgementRecord,
                        VenueProviderAcknowledgementRecord.intent_id == VenueOrderIntentRecord.id,
                    )
                    .where(
                        VenueOrderIntentRecord.venue == "kalshi",
                        VenueOrderIntentRecord.authenticated_principal_fingerprint == principal_fingerprint,
                        VenueOrderIntentRecord.authenticated_principal_fingerprint.is_not(None),
                        VenueProviderAcknowledgementRecord.provider_order_id.in_(set(orders)),
                    )
                )
            ).all()

            known_order_owners: dict[str, str] = {}
            for intent, acknowledgement in rows:
                order = orders.get(acknowledgement.provider_order_id)
                if order is None or order.client_order_id != intent.client_order_id:
                    continue
                effective_price = order.yes_price if order.book_side == "bid" else order.no_price
                if (
                    order.ticker != intent.instrument_id
                    or order.book_side != intent.book_side
                    or order.order_type != "limit"
                    or order.initial_count != intent.quantity
                    or effective_price != intent.limit_price
                ):
                    continue
                known_order_owners[order.order_id] = intent.id

            if not known_order_owners or not fills:
                return set(known_order_owners), set()
            events = (
                await session.execute(
                    select(VenueExecutionEvent).where(
                        VenueExecutionEvent.venue == "kalshi",
                        VenueExecutionEvent.event_type == "fill_observed",
                        VenueExecutionEvent.intent_id.in_(set(known_order_owners.values())),
                        VenueExecutionEvent.provider_event_id.in_(set(fills)),
                    )
                )
            ).scalars()
            known_fills = {
                event.provider_event_id
                for event in events
                if event.provider_event_id is not None
                and event.provider_order_id in known_order_owners
                and known_order_owners[event.provider_order_id] == event.intent_id
                and fills[event.provider_event_id].order_id == event.provider_order_id
            }
            return set(known_order_owners), known_fills

    async def _traverse(self, source: str, getter, **fixed_kwargs: object) -> _Traversal:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        records: list[object] = []
        pages = 0
        while True:
            page = await getter(**fixed_kwargs, limit=1000, cursor=cursor)
            pages += 1
            page_records = page.orders if isinstance(page, KalshiOrdersPage) else page.fills
            records.extend(page_records)
            if not page.cursor:
                return _Traversal(records=tuple(records), pages=pages)
            if page.cursor in seen_cursors:
                raise KalshiPortfolioCoverageConflictError(f"Kalshi {source} pagination cursor repeated")
            seen_cursors.add(page.cursor)
            cursor = page.cursor


def _ceil_epoch(value: datetime) -> int:
    if value.tzinfo is None:
        raise KalshiPortfolioCoverageConflictError("Kalshi historical cutoff must be timezone-aware")
    return math.ceil(value.timestamp())


def _provider_datetime(value: str | None, field_name: str) -> datetime:
    if value is None:
        raise KalshiPortfolioCoverageConflictError(f"Kalshi {field_name} is required for durable evidence")
    match = _RFC3339_PATTERN.fullmatch(value)
    if match is None or (match.group("fraction") and len(match.group("fraction")) > 6):
        raise KalshiPortfolioCoverageConflictError(f"Kalshi {field_name} must be database-exact RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise KalshiPortfolioCoverageConflictError(f"Kalshi {field_name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise KalshiPortfolioCoverageConflictError(f"Kalshi {field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _order_updated_at(order: KalshiOrder) -> datetime:
    return _provider_datetime(order.last_update_time or order.created_time, "order update time")


def _fill_created_at(fill: KalshiFill) -> datetime:
    if fill.created_time is not None:
        return _provider_datetime(fill.created_time, "fill created_time")
    if fill.ts is None or fill.ts < 0:
        raise KalshiPortfolioCoverageConflictError("Kalshi fill requires created_time or non-negative ts")
    return datetime.fromtimestamp(fill.ts, tz=timezone.utc)


def _dedupe_orders(records: tuple[object, ...]) -> tuple[dict[str, KalshiOrder], list[str]]:
    grouped: dict[str, list[KalshiOrder]] = {}
    for item in records:
        if not isinstance(item, KalshiOrder):
            raise KalshiPortfolioCoverageConflictError("orders traversal returned a non-order record")
        grouped.setdefault(item.order_id, []).append(item)

    result: dict[str, KalshiOrder] = {}
    conflicts: list[str] = []
    for order_id, snapshots in grouped.items():
        payloads = [(snapshot, _order_payload(snapshot)) for snapshot in snapshots]
        immutable_identities = {tuple(payload[field] for field in _ORDER_IMMUTABLE_FIELDS) for _, payload in payloads}
        if len(immutable_identities) > 1:
            result[order_id] = min(
                snapshots,
                key=lambda snapshot: _evidence_hash(_order_payload(snapshot)),
            )
            conflicts.append(f"order_identity_conflict:{order_id}")
            continue

        snapshots_by_time: dict[datetime, list[KalshiOrder]] = {}
        for snapshot in snapshots:
            snapshots_by_time.setdefault(_order_updated_at(snapshot), []).append(snapshot)
        if any(
            len({_evidence_hash(_order_payload(snapshot)) for snapshot in same_time}) > 1
            for same_time in snapshots_by_time.values()
        ):
            conflicts.append(f"equal_timestamp_order_conflict:{order_id}")
        latest = snapshots_by_time[max(snapshots_by_time)]
        result[order_id] = min(
            latest,
            key=lambda snapshot: _evidence_hash(_order_payload(snapshot)),
        )
    return result, conflicts


def _dedupe_fills(records: tuple[object, ...]) -> dict[str, KalshiFill]:
    result: dict[str, KalshiFill] = {}
    for item in records:
        if not isinstance(item, KalshiFill):
            raise KalshiPortfolioCoverageConflictError("fills traversal returned a non-fill record")
        existing = result.get(item.fill_id)
        if existing is not None and _fill_payload(existing) != _fill_payload(item):
            raise KalshiPortfolioCoverageConflictError(f"fill_id {item.fill_id!r} returned divergent evidence")
        result[item.fill_id] = item
    return result


def _merge_fills(*sources: dict[str, KalshiFill]) -> dict[str, KalshiFill]:
    result: dict[str, KalshiFill] = {}
    for source in sources:
        for fill_id, fill in source.items():
            existing = result.get(fill_id)
            if existing is not None and _fill_payload(existing) != _fill_payload(fill):
                raise KalshiPortfolioCoverageConflictError(f"fill_id {fill_id!r} returned divergent evidence")
            result[fill_id] = fill
    return result


def _order_payload(order: KalshiOrder) -> dict[str, object]:
    return {
        "order_id": order.order_id,
        "provider_user_hash": hashlib.sha256(order.user_id.encode()).hexdigest(),
        "client_order_id": order.client_order_id,
        "ticker": order.ticker,
        "outcome_side": order.outcome_side,
        "book_side": order.book_side,
        "order_type": order.order_type,
        "status": order.status,
        "yes_price": _decimal_text(order.yes_price),
        "no_price": _decimal_text(order.no_price),
        "fill_count": _decimal_text(order.fill_count),
        "remaining_count": _decimal_text(order.remaining_count),
        "initial_count": _decimal_text(order.initial_count),
        "taker_fees": _decimal_text(order.taker_fees),
        "maker_fees": _decimal_text(order.maker_fees),
        "taker_fill_cost": _decimal_text(order.taker_fill_cost),
        "maker_fill_cost": _decimal_text(order.maker_fill_cost),
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
        "count": _decimal_text(fill.count),
        "yes_price": _decimal_text(fill.yes_price),
        "no_price": _decimal_text(fill.no_price),
        "is_taker": fill.is_taker,
        "fee_cost": _decimal_text(fill.fee_cost),
        "created_time": fill.created_time,
        "subaccount_number": fill.subaccount_number,
        "ts": fill.ts,
    }


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise KalshiPortfolioCoverageConflictError("non-finite Decimal in Kalshi evidence")
    return format(value, "f")


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise TypeError("naive datetime is not canonical")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    raise TypeError(f"unsupported canonical JSON value: {type(value)!r}")


def _evidence_hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _checkpoint_from_result(
    result: KalshiPortfolioCoverageResult,
    before: KalshiHistoricalCutoff,
    after: KalshiHistoricalCutoff,
) -> KalshiPortfolioCoverageCheckpoint:
    return KalshiPortfolioCoverageCheckpoint(
        principal_fingerprint=result.principal_fingerprint,
        coverage_id=result.coverage_id,
        observed_at=result.observed_at,
        orders_cutoff_before=before.orders_updated_at,
        orders_cutoff_after=after.orders_updated_at,
        fills_cutoff_before=before.trades_created_at,
        fills_cutoff_after=after.trades_created_at,
        current_orders_pages=result.page_counts["current_orders"],
        current_fills_pages=result.page_counts["current_fills"],
        historical_orders_pages=result.page_counts["historical_orders"],
        historical_fills_pages=result.page_counts["historical_fills"],
        current_orders_unique=result.unique_counts["current_orders"],
        current_fills_unique=result.unique_counts["current_fills"],
        historical_orders_unique=result.unique_counts["historical_orders"],
        historical_fills_unique=result.unique_counts["historical_fills"],
        orders_unique=result.unique_counts["orders"],
        fills_unique=result.unique_counts["fills"],
        observed_evidence_hash=result.observed_evidence_hash,
        unknown_order_ids_json=list(result.unknown_order_ids),
        unknown_client_order_ids_json=list(result.unknown_client_order_ids),
        unknown_fill_ids_json=list(result.unknown_fill_ids),
        status=result.status,
        reason=result.reason,
        retry_allowed=False,
    )


def _result_from_checkpoint(checkpoint: KalshiPortfolioCoverageCheckpoint) -> KalshiPortfolioCoverageResult:
    if checkpoint.retry_allowed is not False or checkpoint.status not in {"complete", "incomplete"}:
        raise KalshiPortfolioCoverageConflictError("persisted Kalshi coverage checkpoint is invalid")
    return KalshiPortfolioCoverageResult(
        principal_fingerprint=checkpoint.principal_fingerprint,
        coverage_id=checkpoint.coverage_id,
        observed_at=checkpoint.observed_at,
        status=checkpoint.status,
        reason=checkpoint.reason,
        page_counts={
            "current_orders": checkpoint.current_orders_pages,
            "current_fills": checkpoint.current_fills_pages,
            "historical_orders": checkpoint.historical_orders_pages,
            "historical_fills": checkpoint.historical_fills_pages,
        },
        unique_counts={
            "current_orders": checkpoint.current_orders_unique,
            "current_fills": checkpoint.current_fills_unique,
            "historical_orders": checkpoint.historical_orders_unique,
            "historical_fills": checkpoint.historical_fills_unique,
            "orders": checkpoint.orders_unique,
            "fills": checkpoint.fills_unique,
        },
        observed_evidence_hash=checkpoint.observed_evidence_hash,
        unknown_order_ids=tuple(checkpoint.unknown_order_ids_json),
        unknown_client_order_ids=tuple(checkpoint.unknown_client_order_ids_json),
        unknown_fill_ids=tuple(checkpoint.unknown_fill_ids_json),
    )


__all__ = [
    "KalshiPortfolioCoverageConflictError",
    "KalshiPortfolioCoverageResult",
    "KalshiPortfolioCoverageService",
]
