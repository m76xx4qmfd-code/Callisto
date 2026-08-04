"""Current Kalshi Predictions API V2 signing, read models, and order transport.

This module is intentionally isolated from Homerun's legacy ``KalshiClient``
and from the Polymarket live executor. Importing it cannot place an order.
Writes are blocked unless the caller explicitly constructs ``KalshiV2Client``
with ``allow_writes=True``; Callisto does not wire that flag to any route or
runtime in this foundation increment.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Literal, cast
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from services.venues.contracts import VenueOrderIntent

KALSHI_API_PREFIX = "/trade-api/v2"
KALSHI_PRODUCTION_ORIGIN = "https://external-api.kalshi.com"
KALSHI_DEMO_ORIGIN = "https://external-api.demo.kalshi.co"
_KALSHI_APPROVED_ORIGINS = frozenset(
    {
        KALSHI_PRODUCTION_ORIGIN,
        "https://api.elections.kalshi.com",
        KALSHI_DEMO_ORIGIN,
        "https://demo-api.kalshi.co",
    }
)
_EVENT_ORDERS_PATH = f"{KALSHI_API_PREFIX}/portfolio/events/orders"
_BALANCE_PATH = f"{KALSHI_API_PREFIX}/portfolio/balance"
_ORDERS_PATH = f"{KALSHI_API_PREFIX}/portfolio/orders"
_FILLS_PATH = f"{KALSHI_API_PREFIX}/portfolio/fills"
_POSITIONS_PATH = f"{KALSHI_API_PREFIX}/portfolio/positions"
_SETTLEMENTS_PATH = f"{KALSHI_API_PREFIX}/portfolio/settlements"

BookSide = Literal["bid", "ask"]
TimeInForce = Literal["good_till_canceled", "immediate_or_cancel", "fill_or_kill"]
SelfTradePreventionType = Literal["taker_at_cross", "maker"]


class LiveTradingNotArmedError(RuntimeError):
    """Raised before transport when a caller has not explicitly armed writes."""


class KalshiProtocolError(ValueError):
    """Raised when local input or a Kalshi response violates the V2 contract."""


class KalshiAPIError(RuntimeError):
    """Raised for an explicit non-success HTTP response from Kalshi."""

    def __init__(self, *, status_code: int, detail: str, client_order_id: str) -> None:
        self.status_code = status_code
        self.detail = detail
        self.client_order_id = client_order_id
        super().__init__(f"Kalshi order rejected with HTTP {status_code}: {detail}")


class KalshiSubmissionUnknown(RuntimeError):
    """Transport failed after submission began; venue acceptance is unknown.

    The same order must not be submitted again until reconciliation by its
    persisted ``client_order_id`` establishes authoritative venue state.
    """

    def __init__(self, *, client_order_id: str, cause: Exception) -> None:
        self.client_order_id = client_order_id
        self.cause = cause
        super().__init__(
            f"Kalshi acknowledgement unknown for client_order_id={client_order_id}; reconcile before retrying"
        )


def _decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool):
        raise KalshiProtocolError(f"{field} must be a finite decimal")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise KalshiProtocolError(f"{field} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise KalshiProtocolError(f"{field} must be a finite decimal")
    return parsed


def _fixed(value: Decimal, *, quantum: Decimal, field: str) -> str:
    value_exponent = value.as_tuple().exponent
    quantum_exponent = quantum.as_tuple().exponent
    if not isinstance(value_exponent, int) or not isinstance(quantum_exponent, int):
        raise KalshiProtocolError(f"{field} must be a finite decimal")
    if value_exponent < quantum_exponent:
        raise KalshiProtocolError(f"{field} has more precision than Kalshi V2 permits ({quantum})")
    try:
        quantized = value.quantize(quantum)
    except InvalidOperation as exc:
        raise KalshiProtocolError(f"{field} is outside Kalshi's fixed-point range") from exc
    if quantized != value:
        raise KalshiProtocolError(f"{field} has more precision than Kalshi V2 permits ({quantum})")
    return format(quantized, "f")


def _integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise KalshiProtocolError(f"{field} must be an integer")
    return value


def _required_text(payload: Mapping[str, object], key: str, *, context: str) -> str:
    try:
        value = payload[key]
    except KeyError as exc:
        raise KalshiProtocolError(f"invalid Kalshi {context} response") from exc
    if not isinstance(value, str) or not value.strip():
        raise KalshiProtocolError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_text(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise KalshiProtocolError(f"{key} must be a non-empty string when present")
    return value.strip()


def _required_decimal(payload: Mapping[str, object], key: str, *, context: str) -> Decimal:
    try:
        return _decimal(payload[key], field=key)
    except KeyError as exc:
        raise KalshiProtocolError(f"invalid Kalshi {context} response") from exc


def _required_fixed(
    payload: Mapping[str, object],
    key: str,
    *,
    context: str,
    quantum: Decimal,
) -> Decimal:
    try:
        raw_value = payload[key]
    except KeyError as exc:
        raise KalshiProtocolError(f"invalid Kalshi {context} response") from exc
    if not isinstance(raw_value, str):
        raise KalshiProtocolError(f"{key} must be a fixed-point string")
    value = _required_decimal(payload, key, context=context)
    _fixed(value, quantum=quantum, field=key)
    return value


def _optional_integer(payload: Mapping[str, object], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    return _integer(value, field=key)


def _strict_bool(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise KalshiProtocolError(f"{field} must be a boolean")
    return value


def _validate_page_query(
    *,
    limit: int,
    cursor: str | None,
    subaccount: int | None,
    min_ts: int | None = None,
    max_ts: int | None = None,
) -> dict[str, str | int]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise KalshiProtocolError("limit must be an integer between 1 and 1000")
    params: dict[str, str | int] = {"limit": limit}
    if cursor is not None:
        cursor = cursor.strip()
        if not cursor:
            raise KalshiProtocolError("cursor cannot be empty when provided")
        params["cursor"] = cursor
    if subaccount is not None:
        if isinstance(subaccount, bool) or not isinstance(subaccount, int) or not 0 <= subaccount <= 63:
            raise KalshiProtocolError("subaccount must be an integer between 0 and 63")
        params["subaccount"] = subaccount
    for name, value in (("min_ts", min_ts), ("max_ts", max_ts)):
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise KalshiProtocolError(f"{name} must be a non-negative integer")
            params[name] = value
    if min_ts is not None and max_ts is not None and min_ts > max_ts:
        raise KalshiProtocolError("min_ts cannot be greater than max_ts")
    return params


def _add_text_query(params: dict[str, str | int], name: str, value: str | None) -> None:
    if value is None:
        return
    value = value.strip()
    if not value:
        raise KalshiProtocolError(f"{name} cannot be empty when provided")
    params[name] = value


@dataclass(frozen=True)
class KalshiOrder:
    order_id: str
    user_id: str
    client_order_id: str
    ticker: str
    outcome_side: Literal["yes", "no"]
    book_side: BookSide
    order_type: Literal["limit", "market"]
    status: Literal["resting", "canceled", "executed"]
    yes_price: Decimal
    no_price: Decimal
    fill_count: Decimal
    remaining_count: Decimal
    initial_count: Decimal
    taker_fees: Decimal
    maker_fees: Decimal
    taker_fill_cost: Decimal
    maker_fill_cost: Decimal
    created_time: str | None
    last_update_time: str | None
    expiration_time: str | None
    subaccount_number: int | None
    exchange_index: int | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> KalshiOrder:
        outcome_side = _required_text(payload, "outcome_side", context="order")
        book_side = _required_text(payload, "book_side", context="order")
        order_type = _required_text(payload, "type", context="order")
        status = _required_text(payload, "status", context="order")
        if outcome_side not in {"yes", "no"}:
            raise KalshiProtocolError("outcome_side must be 'yes' or 'no'")
        if book_side not in {"bid", "ask"}:
            raise KalshiProtocolError("book_side must be 'bid' or 'ask'")
        if (outcome_side == "yes") != (book_side == "bid"):
            raise KalshiProtocolError("outcome_side and book_side are inconsistent")
        if order_type not in {"limit", "market"}:
            raise KalshiProtocolError("type must be 'limit' or 'market'")
        if status not in {"resting", "canceled", "executed"}:
            raise KalshiProtocolError("invalid order status")
        values = {
            name: _required_fixed(
                payload,
                wire_name,
                context="order",
                quantum=(Decimal("0.01") if "count" in wire_name else Decimal("0.000001")),
            )
            for name, wire_name in {
                "yes_price": "yes_price_dollars",
                "no_price": "no_price_dollars",
                "fill_count": "fill_count_fp",
                "remaining_count": "remaining_count_fp",
                "initial_count": "initial_count_fp",
                "taker_fees": "taker_fees_dollars",
                "maker_fees": "maker_fees_dollars",
                "taker_fill_cost": "taker_fill_cost_dollars",
                "maker_fill_cost": "maker_fill_cost_dollars",
            }.items()
        }
        if not 0 <= values["yes_price"] <= 1 or not 0 <= values["no_price"] <= 1:
            raise KalshiProtocolError("order prices must be between 0 and 1")
        if values["yes_price"] + values["no_price"] != 1:
            raise KalshiProtocolError("order yes and no prices must sum to 1")
        if any(value < 0 for name, value in values.items() if name not in {"yes_price", "no_price"}):
            raise KalshiProtocolError("order counts, fees, and costs cannot be negative")
        subaccount = _optional_integer(payload, "subaccount_number")
        exchange_index = _optional_integer(payload, "exchange_index")
        if subaccount is not None and not 0 <= subaccount <= 63:
            raise KalshiProtocolError("subaccount_number must be between 0 and 63")
        if exchange_index is not None and exchange_index < -1:
            raise KalshiProtocolError("exchange_index cannot be less than -1")
        return cls(
            order_id=_required_text(payload, "order_id", context="order"),
            user_id=_required_text(payload, "user_id", context="order"),
            client_order_id=_required_text(payload, "client_order_id", context="order"),
            ticker=_required_text(payload, "ticker", context="order"),
            outcome_side=cast(Literal["yes", "no"], outcome_side),
            book_side=cast(BookSide, book_side),
            order_type=cast(Literal["limit", "market"], order_type),
            status=cast(Literal["resting", "canceled", "executed"], status),
            created_time=_optional_text(payload, "created_time"),
            last_update_time=_optional_text(payload, "last_update_time"),
            expiration_time=_optional_text(payload, "expiration_time"),
            subaccount_number=subaccount,
            exchange_index=exchange_index,
            **values,
        )


@dataclass(frozen=True)
class KalshiOrdersPage:
    orders: tuple[KalshiOrder, ...]
    cursor: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> KalshiOrdersPage:
        orders = payload.get("orders")
        if not isinstance(orders, list):
            raise KalshiProtocolError("invalid Kalshi orders response")
        cursor = payload.get("cursor")
        if not isinstance(cursor, str):
            raise KalshiProtocolError("orders cursor must be a string")
        if not all(isinstance(item, dict) for item in orders):
            raise KalshiProtocolError("invalid Kalshi order entry")
        return cls(orders=tuple(KalshiOrder.from_payload(item) for item in orders), cursor=cursor)


@dataclass(frozen=True)
class KalshiFill:
    fill_id: str
    trade_id: str
    order_id: str
    ticker: str
    market_ticker: str
    outcome_side: Literal["yes", "no"]
    book_side: BookSide
    count: Decimal
    yes_price: Decimal
    no_price: Decimal
    is_taker: bool
    fee_cost: Decimal
    created_time: str | None
    subaccount_number: int | None
    ts: int | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> KalshiFill:
        outcome_side = _required_text(payload, "outcome_side", context="fill")
        book_side = _required_text(payload, "book_side", context="fill")
        if outcome_side not in {"yes", "no"}:
            raise KalshiProtocolError("outcome_side must be 'yes' or 'no'")
        if book_side not in {"bid", "ask"}:
            raise KalshiProtocolError("book_side must be 'bid' or 'ask'")
        if (outcome_side == "yes") != (book_side == "bid"):
            raise KalshiProtocolError("outcome_side and book_side are inconsistent")
        count = _required_fixed(payload, "count_fp", context="fill", quantum=Decimal("0.01"))
        yes_price = _required_fixed(payload, "yes_price_dollars", context="fill", quantum=Decimal("0.000001"))
        no_price = _required_fixed(payload, "no_price_dollars", context="fill", quantum=Decimal("0.000001"))
        fee_cost = _required_fixed(payload, "fee_cost", context="fill", quantum=Decimal("0.000001"))
        if count < 0 or fee_cost < 0 or not 0 <= yes_price <= 1 or not 0 <= no_price <= 1:
            raise KalshiProtocolError("invalid numeric values in Kalshi fill")
        if yes_price + no_price != 1:
            raise KalshiProtocolError("fill yes and no prices must sum to 1")
        try:
            is_taker = _strict_bool(payload["is_taker"], field="is_taker")
        except KeyError as exc:
            raise KalshiProtocolError("invalid Kalshi fill response") from exc
        subaccount = _optional_integer(payload, "subaccount_number")
        if subaccount is not None and not 0 <= subaccount <= 63:
            raise KalshiProtocolError("subaccount_number must be between 0 and 63")
        fill_id = _required_text(payload, "fill_id", context="fill")
        trade_id = _required_text(payload, "trade_id", context="fill")
        ticker = _required_text(payload, "ticker", context="fill")
        market_ticker = _required_text(payload, "market_ticker", context="fill")
        if fill_id != trade_id:
            raise KalshiProtocolError("fill_id and trade_id must match")
        if ticker != market_ticker:
            raise KalshiProtocolError("ticker and market_ticker must match")
        return cls(
            fill_id=fill_id,
            trade_id=trade_id,
            order_id=_required_text(payload, "order_id", context="fill"),
            ticker=ticker,
            market_ticker=market_ticker,
            outcome_side=cast(Literal["yes", "no"], outcome_side),
            book_side=cast(BookSide, book_side),
            count=count,
            yes_price=yes_price,
            no_price=no_price,
            is_taker=is_taker,
            fee_cost=fee_cost,
            created_time=_optional_text(payload, "created_time"),
            subaccount_number=subaccount,
            ts=_optional_integer(payload, "ts"),
        )


@dataclass(frozen=True)
class KalshiFillsPage:
    fills: tuple[KalshiFill, ...]
    cursor: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> KalshiFillsPage:
        fills = payload.get("fills")
        if not isinstance(fills, list):
            raise KalshiProtocolError("invalid Kalshi fills response")
        cursor = payload.get("cursor")
        if not isinstance(cursor, str):
            raise KalshiProtocolError("fills cursor must be a string")
        if not all(isinstance(item, dict) for item in fills):
            raise KalshiProtocolError("invalid Kalshi fill entry")
        return cls(fills=tuple(KalshiFill.from_payload(item) for item in fills), cursor=cursor)


@dataclass(frozen=True)
class KalshiMarketPosition:
    ticker: str
    total_traded: Decimal
    position: Decimal
    market_exposure: Decimal
    realized_pnl: Decimal
    fees_paid: Decimal
    last_updated_ts: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> KalshiMarketPosition:
        try:
            return cls(
                ticker=_required_text(payload, "ticker", context="market position"),
                total_traded=_required_fixed(
                    payload, "total_traded_dollars", context="market position", quantum=Decimal("0.000001")
                ),
                position=_required_fixed(payload, "position_fp", context="market position", quantum=Decimal("0.01")),
                market_exposure=_required_fixed(
                    payload, "market_exposure_dollars", context="market position", quantum=Decimal("0.000001")
                ),
                realized_pnl=_required_fixed(
                    payload, "realized_pnl_dollars", context="market position", quantum=Decimal("0.000001")
                ),
                fees_paid=_required_fixed(
                    payload, "fees_paid_dollars", context="market position", quantum=Decimal("0.000001")
                ),
                last_updated_ts=_required_text(payload, "last_updated_ts", context="market position"),
            )
        except KalshiProtocolError as exc:
            if "market position" in str(exc):
                raise
            raise KalshiProtocolError(f"invalid Kalshi market position response: {exc}") from exc


@dataclass(frozen=True)
class KalshiEventPosition:
    event_ticker: str
    total_cost: Decimal
    total_cost_shares: Decimal
    event_exposure: Decimal
    realized_pnl: Decimal
    fees_paid: Decimal

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> KalshiEventPosition:
        return cls(
            event_ticker=_required_text(payload, "event_ticker", context="event position"),
            total_cost=_required_fixed(
                payload, "total_cost_dollars", context="event position", quantum=Decimal("0.000001")
            ),
            total_cost_shares=_required_fixed(
                payload, "total_cost_shares_fp", context="event position", quantum=Decimal("0.01")
            ),
            event_exposure=_required_fixed(
                payload, "event_exposure_dollars", context="event position", quantum=Decimal("0.000001")
            ),
            realized_pnl=_required_fixed(
                payload, "realized_pnl_dollars", context="event position", quantum=Decimal("0.000001")
            ),
            fees_paid=_required_fixed(
                payload, "fees_paid_dollars", context="event position", quantum=Decimal("0.000001")
            ),
        )


@dataclass(frozen=True)
class KalshiPositionsPage:
    market_positions: tuple[KalshiMarketPosition, ...]
    event_positions: tuple[KalshiEventPosition, ...]
    cursor: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> KalshiPositionsPage:
        markets = payload.get("market_positions")
        events = payload.get("event_positions")
        cursor = payload.get("cursor", "")
        if not isinstance(markets, list) or not all(isinstance(item, dict) for item in markets):
            raise KalshiProtocolError("invalid Kalshi market positions response")
        if not isinstance(events, list) or not all(isinstance(item, dict) for item in events):
            raise KalshiProtocolError("invalid Kalshi event positions response")
        if not isinstance(cursor, str):
            raise KalshiProtocolError("positions cursor must be a string")
        return cls(
            market_positions=tuple(KalshiMarketPosition.from_payload(item) for item in markets),
            event_positions=tuple(KalshiEventPosition.from_payload(item) for item in events),
            cursor=cursor,
        )


@dataclass(frozen=True)
class KalshiSettlement:
    ticker: str
    event_ticker: str
    market_result: Literal["yes", "no", "scalar"]
    yes_count: Decimal
    yes_total_cost: Decimal
    no_count: Decimal
    no_total_cost: Decimal
    revenue_cents: int
    settled_time: str
    fee_cost: Decimal
    settlement_value_cents: int | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> KalshiSettlement:
        market_result = _required_text(payload, "market_result", context="settlement")
        if market_result not in {"yes", "no", "scalar"}:
            raise KalshiProtocolError("market_result must be yes, no, or scalar")
        try:
            revenue = _integer(payload["revenue"], field="revenue")
        except KeyError as exc:
            raise KalshiProtocolError("invalid Kalshi settlement response") from exc
        values = {
            name: _required_fixed(
                payload,
                wire_name,
                context="settlement",
                quantum=(Decimal("0.01") if wire_name.endswith("_fp") else Decimal("0.000001")),
            )
            for name, wire_name in {
                "yes_count": "yes_count_fp",
                "yes_total_cost": "yes_total_cost_dollars",
                "no_count": "no_count_fp",
                "no_total_cost": "no_total_cost_dollars",
                "fee_cost": "fee_cost",
            }.items()
        }
        if revenue < 0 or any(value < 0 for value in values.values()):
            raise KalshiProtocolError("settlement counts, costs, fees, and revenue cannot be negative")
        settlement_value = _optional_integer(payload, "value")
        if settlement_value is not None and not 0 <= settlement_value <= 100:
            raise KalshiProtocolError("settlement value must be between 0 and 100 cents")
        return cls(
            ticker=_required_text(payload, "ticker", context="settlement"),
            event_ticker=_required_text(payload, "event_ticker", context="settlement"),
            market_result=cast(Literal["yes", "no", "scalar"], market_result),
            revenue_cents=revenue,
            settled_time=_required_text(payload, "settled_time", context="settlement"),
            settlement_value_cents=settlement_value,
            **values,
        )


@dataclass(frozen=True)
class KalshiSettlementsPage:
    settlements: tuple[KalshiSettlement, ...]
    cursor: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> KalshiSettlementsPage:
        settlements = payload.get("settlements")
        cursor = payload.get("cursor", "")
        if not isinstance(settlements, list) or not all(isinstance(item, dict) for item in settlements):
            raise KalshiProtocolError("invalid Kalshi settlements response")
        if not isinstance(cursor, str):
            raise KalshiProtocolError("settlements cursor must be a string")
        return cls(
            settlements=tuple(KalshiSettlement.from_payload(item) for item in settlements),
            cursor=cursor,
        )


@dataclass(frozen=True)
class KalshiSubaccountBalance:
    exchange_index: int
    balance: Decimal


@dataclass(frozen=True)
class KalshiBalanceSnapshot:
    balance_cents: int
    balance_dollars: Decimal
    portfolio_value_cents: int
    updated_ts: int
    balance_breakdown: tuple[KalshiSubaccountBalance, ...]

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> KalshiBalanceSnapshot:
        try:
            balance_cents = _integer(payload["balance"], field="balance")
            portfolio_value_cents = _integer(payload["portfolio_value"], field="portfolio_value")
            updated_ts = _integer(payload["updated_ts"], field="updated_ts")
        except KeyError as exc:
            raise KalshiProtocolError("invalid Kalshi balance response") from exc
        balance_dollars = _required_fixed(
            payload,
            "balance_dollars",
            context="balance",
            quantum=Decimal("0.000001"),
        )

        raw_breakdown = payload.get("balance_breakdown", [])
        if not isinstance(raw_breakdown, list):
            raise KalshiProtocolError("balance_breakdown must be a list")
        breakdown: list[KalshiSubaccountBalance] = []
        for entry in raw_breakdown:
            if not isinstance(entry, dict):
                raise KalshiProtocolError("invalid balance_breakdown entry")
            try:
                exchange_index = _integer(entry["exchange_index"], field="exchange_index")
            except KeyError as exc:
                raise KalshiProtocolError("invalid balance_breakdown entry") from exc
            balance = _required_fixed(
                entry,
                "balance",
                context="balance_breakdown entry",
                quantum=Decimal("0.000001"),
            )
            if exchange_index < 0:
                raise KalshiProtocolError("exchange_index cannot be negative")
            if balance < 0:
                raise KalshiProtocolError("breakdown balance cannot be negative")
            breakdown.append(
                KalshiSubaccountBalance(
                    exchange_index=exchange_index,
                    balance=balance,
                )
            )

        if balance_cents < 0 or portfolio_value_cents < 0 or updated_ts <= 0:
            raise KalshiProtocolError("invalid values in Kalshi balance response")
        if balance_dollars < 0:
            raise KalshiProtocolError("balance_dollars cannot be negative")
        balance_dollars_in_cents = (balance_dollars * 100).to_integral_value(rounding=ROUND_DOWN)
        if balance_dollars_in_cents != balance_cents:
            raise KalshiProtocolError("balance_dollars does not match balance cents")
        return cls(
            balance_cents=balance_cents,
            balance_dollars=balance_dollars,
            portfolio_value_cents=portfolio_value_cents,
            updated_ts=updated_ts,
            balance_breakdown=tuple(breakdown),
        )


@dataclass(frozen=True)
class KalshiEventOrderRequest:
    """Exact request shape for ``POST /portfolio/events/orders``."""

    ticker: str
    client_order_id: str
    side: BookSide
    count: Decimal
    price: Decimal
    time_in_force: TimeInForce = "good_till_canceled"
    self_trade_prevention_type: SelfTradePreventionType = "taker_at_cross"
    post_only: bool = False
    cancel_order_on_pause: bool = False
    reduce_only: bool = False
    subaccount: int = 0
    exchange_index: int = 0

    def __post_init__(self) -> None:
        ticker = self.ticker.strip()
        client_order_id = self.client_order_id.strip()
        count = _decimal(self.count, field="count")
        price = _decimal(self.price, field="price")

        if not ticker:
            raise KalshiProtocolError("ticker is required")
        if not client_order_id:
            raise KalshiProtocolError("client_order_id is required")
        if self.side not in {"bid", "ask"}:
            raise KalshiProtocolError("side must be 'bid' or 'ask'")
        if self.time_in_force not in {
            "good_till_canceled",
            "immediate_or_cancel",
            "fill_or_kill",
        }:
            raise KalshiProtocolError("unsupported time_in_force")
        if self.self_trade_prevention_type not in {"taker_at_cross", "maker"}:
            raise KalshiProtocolError("self_trade_prevention_type must be 'taker_at_cross' or 'maker'")
        if count <= 0:
            raise KalshiProtocolError("count must be greater than zero")
        if price <= 0 or price >= 1:
            raise KalshiProtocolError("price must be greater than 0 and less than 1")
        if isinstance(self.subaccount, bool) or not isinstance(self.subaccount, int):
            raise KalshiProtocolError("subaccount must be an integer")
        if self.subaccount < 0:
            raise KalshiProtocolError("subaccount cannot be negative")
        if isinstance(self.exchange_index, bool) or not isinstance(self.exchange_index, int):
            raise KalshiProtocolError("exchange_index must be an integer")
        if self.exchange_index < -1:
            raise KalshiProtocolError("exchange_index cannot be less than -1")

        _fixed(count, quantum=Decimal("0.01"), field="count")
        _fixed(price, quantum=Decimal("0.000001"), field="price")
        object.__setattr__(self, "ticker", ticker)
        object.__setattr__(self, "client_order_id", client_order_id)
        object.__setattr__(self, "count", count)
        object.__setattr__(self, "price", price)

    def to_payload(self) -> dict[str, object]:
        return {
            "ticker": self.ticker,
            "client_order_id": self.client_order_id,
            "side": self.side,
            "count": _fixed(self.count, quantum=Decimal("0.01"), field="count"),
            "price": _fixed(self.price, quantum=Decimal("0.000001"), field="price"),
            "time_in_force": self.time_in_force,
            "self_trade_prevention_type": self.self_trade_prevention_type,
            "post_only": self.post_only,
            "cancel_order_on_pause": self.cancel_order_on_pause,
            "reduce_only": self.reduce_only,
            "subaccount": self.subaccount,
            "exchange_index": self.exchange_index,
        }


@dataclass(frozen=True)
class KalshiOrderAcknowledgement:
    order_id: str
    client_order_id: str
    fill_count: Decimal
    remaining_count: Decimal
    ts_ms: int

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
        *,
        submitted_client_order_id: str,
    ) -> KalshiOrderAcknowledgement:
        try:
            order_id = str(payload["order_id"]).strip()
            raw_client_order_id = payload.get("client_order_id")
            client_order_id = (
                submitted_client_order_id if raw_client_order_id is None else str(raw_client_order_id).strip()
            )
            fill_count = _decimal(payload["fill_count"], field="fill_count")
            remaining_count = _decimal(payload["remaining_count"], field="remaining_count")
            raw_ts_ms = payload["ts_ms"]
            if isinstance(raw_ts_ms, bool) or not isinstance(raw_ts_ms, (int, str)):
                raise TypeError("ts_ms must be an integer")
            ts_ms = int(raw_ts_ms)
        except (KeyError, TypeError, ValueError) as exc:
            raise KalshiProtocolError("invalid Kalshi V2 order acknowledgement") from exc

        if not order_id or not client_order_id:
            raise KalshiProtocolError("order acknowledgement IDs cannot be empty")
        if fill_count < 0 or remaining_count < 0 or ts_ms <= 0:
            raise KalshiProtocolError("invalid values in order acknowledgement")
        return cls(
            order_id=order_id,
            client_order_id=client_order_id,
            fill_count=fill_count,
            remaining_count=remaining_count,
            ts_ms=ts_ms,
        )


def event_order_from_intent(
    intent: VenueOrderIntent,
    *,
    cancel_order_on_pause: bool = False,
    reduce_only: bool = False,
    subaccount: int = 0,
    exchange_index: int = 0,
) -> KalshiEventOrderRequest:
    """Translate a venue-neutral order intent to the current Kalshi wire model."""

    if intent.venue != "kalshi":
        raise ValueError("Kalshi translator requires a Kalshi venue intent")
    return KalshiEventOrderRequest(
        ticker=intent.instrument_id,
        client_order_id=intent.client_order_id,
        side=intent.book_side,
        count=intent.quantity,
        price=intent.limit_price,
        time_in_force=intent.time_in_force,
        post_only=intent.post_only,
        cancel_order_on_pause=cancel_order_on_pause,
        reduce_only=reduce_only,
        subaccount=subaccount,
        exchange_index=exchange_index,
    )


class KalshiRequestSigner:
    """RSA-PSS/SHA-256 signer for current Kalshi authenticated requests."""

    def __init__(self, *, key_id: str, private_key_pem: str) -> None:
        self.key_id = key_id.strip()
        if not self.key_id:
            raise KalshiProtocolError("Kalshi key_id is required")
        try:
            key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
        except (TypeError, ValueError) as exc:
            raise KalshiProtocolError("invalid Kalshi RSA private key") from exc
        if not isinstance(key, rsa.RSAPrivateKey):
            raise KalshiProtocolError("Kalshi private key must be RSA")
        self._private_key = key

    def headers(self, *, timestamp_ms: int, method: str, path: str) -> dict[str, str]:
        if timestamp_ms <= 0:
            raise KalshiProtocolError("timestamp_ms must be positive")
        method_upper = method.strip().upper()
        if not method_upper:
            raise KalshiProtocolError("HTTP method is required")
        path_without_query = urlsplit(path).path
        if not path_without_query.startswith(KALSHI_API_PREFIX + "/"):
            raise KalshiProtocolError(f"signed path must start with {KALSHI_API_PREFIX}/")
        timestamp = str(timestamp_ms)
        message = f"{timestamp}{method_upper}{path_without_query}".encode()
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256().digest_size,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("ascii"),
        }


class KalshiV2Client:
    """Authenticated V2 portfolio reads and event orders with fail-closed writes.

    This class performs no automatic POST retry. A transport failure becomes
    ``KalshiSubmissionUnknown`` so callers must reconcile by
    ``client_order_id`` before deciding whether another submit is safe.
    """

    def __init__(
        self,
        *,
        key_id: str,
        private_key_pem: str,
        http_client: httpx.AsyncClient | None = None,
        origin: str = KALSHI_PRODUCTION_ORIGIN,
        allow_writes: bool = False,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        origin = origin.strip().rstrip("/")
        parsed_origin = urlsplit(origin)
        if parsed_origin.scheme != "https" or not parsed_origin.netloc:
            raise KalshiProtocolError("Kalshi origin must be an HTTPS origin")
        if origin not in _KALSHI_APPROVED_ORIGINS:
            raise KalshiProtocolError("credentials may only be sent to an approved Kalshi origin")
        self._origin = origin
        self._signer = KalshiRequestSigner(key_id=key_id, private_key_pem=private_key_pem)
        self._http = http_client or httpx.AsyncClient(timeout=30.0)
        self._owns_http = http_client is None
        self._allow_writes = allow_writes
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)

    async def close(self) -> None:
        if self._owns_http and not self._http.is_closed:
            await self._http.aclose()

    async def _signed_get_json(
        self,
        *,
        path: str,
        params: Mapping[str, str | int] | None = None,
        response_name: str,
    ) -> Mapping[str, object]:
        timestamp_ms = self._now_ms()
        headers = {
            **self._signer.headers(
                timestamp_ms=timestamp_ms,
                method="GET",
                path=path,
            ),
            "Accept": "application/json",
        }
        response = await self._http.get(
            f"{self._origin}{path}",
            headers=headers,
            params=params,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise KalshiProtocolError(f"Kalshi {response_name} response was not JSON") from exc
        if not isinstance(payload, dict):
            raise KalshiProtocolError(f"Kalshi {response_name} response must be an object")
        return payload

    async def get_balance(self) -> KalshiBalanceSnapshot:
        """Return the authenticated account balance without enabling writes."""

        payload = await self._signed_get_json(
            path=_BALANCE_PATH,
            response_name="balance",
        )
        return KalshiBalanceSnapshot.from_payload(payload)

    async def get_orders(
        self,
        *,
        ticker: str | None = None,
        event_tickers: tuple[str, ...] = (),
        status: Literal["resting", "canceled", "executed"] | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
        limit: int = 100,
        cursor: str | None = None,
        subaccount: int | None = None,
    ) -> KalshiOrdersPage:
        """Return one authoritative page of current or historical orders."""

        params = _validate_page_query(
            limit=limit,
            cursor=cursor,
            subaccount=subaccount,
            min_ts=min_ts,
            max_ts=max_ts,
        )
        _add_text_query(params, "ticker", ticker)
        if len(event_tickers) > 10:
            raise KalshiProtocolError("event_tickers cannot contain more than 10 values")
        if event_tickers:
            normalized = tuple(value.strip() for value in event_tickers)
            if any(not value for value in normalized):
                raise KalshiProtocolError("event_tickers cannot contain empty values")
            params["event_ticker"] = ",".join(normalized)
        if status is not None:
            if status not in {"resting", "canceled", "executed"}:
                raise KalshiProtocolError("status must be resting, canceled, or executed")
            params["status"] = status
        payload = await self._signed_get_json(
            path=_ORDERS_PATH,
            params=params,
            response_name="orders",
        )
        return KalshiOrdersPage.from_payload(payload)

    async def get_fills(
        self,
        *,
        ticker: str | None = None,
        order_id: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
        limit: int = 100,
        cursor: str | None = None,
        subaccount: int | None = None,
    ) -> KalshiFillsPage:
        """Return one authoritative page of user fills."""

        params = _validate_page_query(
            limit=limit,
            cursor=cursor,
            subaccount=subaccount,
            min_ts=min_ts,
            max_ts=max_ts,
        )
        _add_text_query(params, "ticker", ticker)
        _add_text_query(params, "order_id", order_id)
        payload = await self._signed_get_json(
            path=_FILLS_PATH,
            params=params,
            response_name="fills",
        )
        return KalshiFillsPage.from_payload(payload)

    async def get_positions(
        self,
        *,
        cursor: str | None = None,
        limit: int = 100,
        count_filter: tuple[Literal["position", "total_traded"], ...] = (),
        ticker: str | None = None,
        event_ticker: str | None = None,
        subaccount: int = 0,
    ) -> KalshiPositionsPage:
        """Return one page of market and event positions for a subaccount."""

        params = _validate_page_query(
            limit=limit,
            cursor=cursor,
            subaccount=subaccount,
        )
        _add_text_query(params, "ticker", ticker)
        _add_text_query(params, "event_ticker", event_ticker)
        if count_filter:
            if any(value not in {"position", "total_traded"} for value in count_filter):
                raise KalshiProtocolError("count_filter values must be position or total_traded")
            if len(set(count_filter)) != len(count_filter):
                raise KalshiProtocolError("count_filter values cannot be duplicated")
            params["count_filter"] = ",".join(count_filter)
        payload = await self._signed_get_json(
            path=_POSITIONS_PATH,
            params=params,
            response_name="positions",
        )
        return KalshiPositionsPage.from_payload(payload)

    async def get_settlements(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        ticker: str | None = None,
        event_ticker: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
        subaccount: int | None = None,
    ) -> KalshiSettlementsPage:
        """Return one authoritative page of historical settlements."""

        params = _validate_page_query(
            limit=limit,
            cursor=cursor,
            subaccount=subaccount,
            min_ts=min_ts,
            max_ts=max_ts,
        )
        _add_text_query(params, "ticker", ticker)
        _add_text_query(params, "event_ticker", event_ticker)
        payload = await self._signed_get_json(
            path=_SETTLEMENTS_PATH,
            params=params,
            response_name="settlements",
        )
        return KalshiSettlementsPage.from_payload(payload)

    async def create_order(self, order: KalshiEventOrderRequest) -> KalshiOrderAcknowledgement:
        if not self._allow_writes:
            raise LiveTradingNotArmedError("Kalshi writes are disabled; no request was sent")

        timestamp_ms = self._now_ms()
        headers = {
            **self._signer.headers(
                timestamp_ms=timestamp_ms,
                method="POST",
                path=_EVENT_ORDERS_PATH,
            ),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            response = await self._http.post(
                f"{self._origin}{_EVENT_ORDERS_PATH}",
                json=order.to_payload(),
                headers=headers,
            )
        except httpx.TransportError as exc:
            raise KalshiSubmissionUnknown(client_order_id=order.client_order_id, cause=exc) from exc

        if response.status_code >= 500:
            raise KalshiSubmissionUnknown(
                client_order_id=order.client_order_id,
                cause=KalshiAPIError(
                    status_code=response.status_code,
                    detail=response.text[:1000],
                    client_order_id=order.client_order_id,
                ),
            )
        if response.is_error:
            raise KalshiAPIError(
                status_code=response.status_code,
                detail=response.text[:1000],
                client_order_id=order.client_order_id,
            )
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise KalshiProtocolError("Kalshi V2 order acknowledgement must be an object")
            acknowledgement = KalshiOrderAcknowledgement.from_payload(
                payload,
                submitted_client_order_id=order.client_order_id,
            )
            if acknowledgement.client_order_id != order.client_order_id:
                raise KalshiProtocolError("Kalshi acknowledgement client_order_id did not match submission")
            return acknowledgement
        except (ValueError, TypeError) as exc:
            raise KalshiSubmissionUnknown(
                client_order_id=order.client_order_id,
                cause=exc,
            ) from exc
