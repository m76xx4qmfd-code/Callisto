from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import Callable, Literal, Mapping, cast
from urllib.parse import quote, urlsplit

import httpx

KALSHI_PAPER_MARKET_DATA_ORIGIN = "https://external-api.kalshi.com"
KALSHI_API_PREFIX = "/trade-api/v2"
KALSHI_OPENAPI_SHA256 = "41d93050bf3f692cf3a898ba3a1a033f3e857fee56370ddcb18af6a4225f41cb"
KALSHI_OPENAPI_VERSION = "3.27.0"
MAX_SOURCE_AGE_SECONDS = Decimal("5")
_PRICE_SCALE = 6
_QUANTITY_SCALE = 2
_MONEY_SCALE = 18
_TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]*$")
_DECIMAL_PATTERN = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d+)?$")

Outcome = Literal["yes", "no"]
PaperFillStatus = Literal["filled", "partial", "no_fill"]


class KalshiPaperProtocolError(ValueError):
    pass


def _decimal_string(value: object, *, field: str, max_scale: int) -> Decimal:
    if not isinstance(value, str) or not _DECIMAL_PATTERN.fullmatch(value):
        raise KalshiPaperProtocolError(f"{field} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise KalshiPaperProtocolError(f"{field} must be a finite decimal string") from exc
    if not parsed.is_finite():
        raise KalshiPaperProtocolError(f"{field} must be a finite decimal string")
    exponent = parsed.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -max_scale:
        raise KalshiPaperProtocolError(f"{field} has more than {max_scale} decimal places")
    return parsed


def parse_price(value: object, *, field: str = "price") -> Decimal:
    parsed = _decimal_string(value, field=field, max_scale=_PRICE_SCALE)
    if parsed <= 0 or parsed >= 1:
        raise KalshiPaperProtocolError(f"{field} must be greater than 0 and less than 1")
    return parsed


def parse_quantity(value: object, *, field: str = "quantity") -> Decimal:
    parsed = _decimal_string(value, field=field, max_scale=_QUANTITY_SCALE)
    if parsed <= 0:
        raise KalshiPaperProtocolError(f"{field} must be greater than 0")
    return parsed


def parse_money(value: object, *, field: str) -> Decimal:
    parsed = _decimal_string(value, field=field, max_scale=_PRICE_SCALE)
    if parsed < 0:
        raise KalshiPaperProtocolError(f"{field} must be non-negative")
    return parsed


def decimal_string(value: Decimal, *, scale: int | None = None) -> str:
    if not value.is_finite():
        raise KalshiPaperProtocolError("financial value must be finite")
    if scale is None:
        return format(value, "f")
    scaled = _to_scaled_int(value, scale=scale, field="financial value")
    return format(_from_scaled_int(scaled, scale=scale), f".{scale}f")


def _to_scaled_int(value: Decimal, *, scale: int, field: str) -> int:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise KalshiPaperProtocolError(f"{field} must be a finite decimal")
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise KalshiPaperProtocolError(f"{field} must have a finite decimal exponent")
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    shift = exponent + scale
    if shift >= 0:
        coefficient *= 10**shift
    else:
        divisor = 10 ** (-shift)
        if coefficient % divisor:
            raise KalshiPaperProtocolError(f"{field} has more than {scale} decimal places")
        coefficient //= divisor
    return -coefficient if sign else coefficient


def _from_scaled_int(value: int, *, scale: int) -> Decimal:
    sign = 1 if value < 0 else 0
    digits_text = str(abs(value))
    digits = tuple(int(digit) for digit in digits_text)
    return Decimal((sign, digits, -scale))


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash_json(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _required_mapping(payload: Mapping[str, object], key: str, *, context: str) -> Mapping[str, object]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise KalshiPaperProtocolError(f"invalid Kalshi {context} response")
    return cast(Mapping[str, object], value)


def _required_text(payload: Mapping[str, object], key: str, *, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise KalshiPaperProtocolError(f"invalid Kalshi {context} {key}")
    return value.strip()


def _parse_rfc3339(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise KalshiPaperProtocolError(f"{field} is required")
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise KalshiPaperProtocolError(f"{field} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise KalshiPaperProtocolError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _parse_source_date(response: httpx.Response, *, now: datetime) -> datetime:
    raw = response.headers.get("date")
    if not raw:
        raise KalshiPaperProtocolError("Kalshi response is missing source Date")
    try:
        observed_at = parsedate_to_datetime(raw)
    except (TypeError, ValueError) as exc:
        raise KalshiPaperProtocolError("Kalshi response has invalid source Date") from exc
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    observed_at = observed_at.astimezone(timezone.utc)
    age = Decimal(str((now - observed_at).total_seconds()))
    if age < -MAX_SOURCE_AGE_SECONDS:
        raise KalshiPaperProtocolError("Kalshi response source Date is in the future")
    if age > MAX_SOURCE_AGE_SECONDS:
        raise KalshiPaperProtocolError("Kalshi response is stale")
    return observed_at


def _load_json(response: httpx.Response, *, context: str) -> Mapping[str, object]:
    try:
        payload = json.loads(response.content.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KalshiPaperProtocolError(f"invalid Kalshi {context} JSON") from exc
    if not isinstance(payload, Mapping):
        raise KalshiPaperProtocolError(f"invalid Kalshi {context} response")
    return cast(Mapping[str, object], payload)


@dataclass(frozen=True)
class PaperBookLevel:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True)
class PaperPriceRange:
    start: Decimal
    end: Decimal
    step: Decimal


@dataclass(frozen=True)
class PaperBook:
    ticker: str
    yes_bids: tuple[PaperBookLevel, ...]
    no_bids: tuple[PaperBookLevel, ...]
    source_origin: str
    observed_at: datetime
    fetched_at: datetime
    evidence_hash: str
    evidence_json: str


@dataclass(frozen=True)
class PaperMarket:
    ticker: str
    event_ticker: str
    notional_value: Decimal
    price_level_structure: str
    price_ranges: tuple[PaperPriceRange, ...]
    fee: Decimal
    fee_rule_version: str
    fee_provenance: Mapping[str, str]
    fee_waiver_expiration: datetime
    observed_at: datetime
    fetched_at: datetime
    evidence_hash: str
    evidence_json: str


@dataclass(frozen=True)
class PaperQuote:
    market: PaperMarket
    book: PaperBook


@dataclass(frozen=True)
class PaperFill:
    sequence: int
    quantity: Decimal
    price: Decimal
    notional: Decimal
    source_bid_price: Decimal
    source_side: Outcome


@dataclass(frozen=True)
class PaperFillResult:
    status: PaperFillStatus
    reason: str
    requested_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    average_fill_price: Decimal | None
    notional: Decimal
    fee: Decimal
    fills: tuple[PaperFill, ...]
    formula_version: str = "kalshi-complementary-depth-ioc-v1"


def _parse_price_ranges(value: object) -> tuple[PaperPriceRange, ...]:
    if not isinstance(value, list) or not value:
        raise KalshiPaperProtocolError("Kalshi market price_ranges must be a non-empty list")
    parsed: list[PaperPriceRange] = []
    previous_end: int | None = None
    for index, raw_range in enumerate(value):
        if not isinstance(raw_range, Mapping):
            raise KalshiPaperProtocolError(f"Kalshi market price_ranges[{index}] must be an object")
        start = _decimal_string(raw_range.get("start"), field=f"price_ranges[{index}].start", max_scale=_PRICE_SCALE)
        end = _decimal_string(raw_range.get("end"), field=f"price_ranges[{index}].end", max_scale=_PRICE_SCALE)
        step = _decimal_string(raw_range.get("step"), field=f"price_ranges[{index}].step", max_scale=_PRICE_SCALE)
        start_units = _to_scaled_int(start, scale=_PRICE_SCALE, field=f"price_ranges[{index}].start")
        end_units = _to_scaled_int(end, scale=_PRICE_SCALE, field=f"price_ranges[{index}].end")
        step_units = _to_scaled_int(step, scale=_PRICE_SCALE, field=f"price_ranges[{index}].step")
        if start_units < 0 or end_units > 10**_PRICE_SCALE or start_units >= end_units:
            raise KalshiPaperProtocolError(f"Kalshi market price_ranges[{index}] has invalid bounds")
        if step_units <= 0 or (end_units - start_units) % step_units:
            raise KalshiPaperProtocolError(f"Kalshi market price_ranges[{index}] has an invalid step")
        if previous_end is not None and start_units < previous_end:
            raise KalshiPaperProtocolError("Kalshi market price_ranges overlap or are out of order")
        parsed.append(PaperPriceRange(start=start, end=end, step=step))
        previous_end = end_units
    return tuple(parsed)


def _price_is_on_tick(price_units: int, price_ranges: tuple[PaperPriceRange, ...]) -> bool:
    for price_range in price_ranges:
        start_units = _to_scaled_int(price_range.start, scale=_PRICE_SCALE, field="price range start")
        end_units = _to_scaled_int(price_range.end, scale=_PRICE_SCALE, field="price range end")
        step_units = _to_scaled_int(price_range.step, scale=_PRICE_SCALE, field="price range step")
        if start_units <= price_units <= end_units and (price_units - start_units) % step_units == 0:
            return True
    return False


def simulate_buy_ioc(
    *,
    book: PaperBook,
    outcome: Outcome,
    quantity: Decimal,
    limit_price: Decimal,
    price_ranges: tuple[PaperPriceRange, ...],
) -> PaperFillResult:
    if outcome not in {"yes", "no"}:
        raise KalshiPaperProtocolError("outcome must be yes or no")
    quantity_units = _to_scaled_int(quantity, scale=_QUANTITY_SCALE, field="quantity")
    if quantity_units <= 0:
        raise KalshiPaperProtocolError("quantity must be greater than 0")
    limit_units = _to_scaled_int(limit_price, scale=_PRICE_SCALE, field="limit_price")
    if limit_units <= 0 or limit_units >= 10**_PRICE_SCALE:
        raise KalshiPaperProtocolError("limit_price must be greater than 0 and less than 1")
    if not _price_is_on_tick(limit_units, price_ranges):
        raise KalshiPaperProtocolError("limit_price is not valid for the market price ranges")

    source_levels = book.no_bids if outcome == "yes" else book.yes_bids
    validated_levels: list[tuple[int, int]] = []
    for level in source_levels:
        source_price_units = _to_scaled_int(level.price, scale=_PRICE_SCALE, field="book price")
        level_quantity_units = _to_scaled_int(level.quantity, scale=_QUANTITY_SCALE, field="book quantity")
        if source_price_units <= 0 or source_price_units >= 10**_PRICE_SCALE:
            raise KalshiPaperProtocolError("book price must be greater than 0 and less than 1")
        if not _price_is_on_tick(source_price_units, price_ranges):
            raise KalshiPaperProtocolError("book price is not valid for the market price ranges")
        if level_quantity_units <= 0:
            raise KalshiPaperProtocolError("book quantity must be greater than 0")
        validated_levels.append((source_price_units, level_quantity_units))

    remaining_units = quantity_units
    total_notional_units = 0
    fills: list[PaperFill] = []
    for source_price_units, available_units in sorted(validated_levels, reverse=True):
        execution_price_units = 10**_PRICE_SCALE - source_price_units
        if not _price_is_on_tick(execution_price_units, price_ranges):
            raise KalshiPaperProtocolError("complementary execution price is not valid for the market price ranges")
        if execution_price_units > limit_units:
            continue
        fill_units = min(remaining_units, available_units)
        if fill_units <= 0:
            continue
        fill_notional_units = execution_price_units * fill_units
        total_notional_units += fill_notional_units
        fills.append(
            PaperFill(
                sequence=len(fills) + 1,
                quantity=_from_scaled_int(fill_units, scale=_QUANTITY_SCALE),
                price=_from_scaled_int(execution_price_units, scale=_PRICE_SCALE),
                notional=_from_scaled_int(fill_notional_units, scale=_PRICE_SCALE + _QUANTITY_SCALE),
                source_bid_price=_from_scaled_int(source_price_units, scale=_PRICE_SCALE),
                source_side="no" if outcome == "yes" else "yes",
            )
        )
        remaining_units -= fill_units
        if remaining_units == 0:
            break

    filled_units = quantity_units - remaining_units
    status: PaperFillStatus
    reason: str
    if filled_units == 0:
        status = "no_fill"
        reason = "displayed_depth_empty" if not validated_levels else "limit_does_not_cross_displayed_depth"
    elif remaining_units == 0:
        status = "filled"
        reason = "displayed_depth_filled_ioc"
    else:
        status = "partial"
        reason = "displayed_depth_partially_filled_ioc"

    average_fill_price = None
    if filled_units:
        with localcontext() as context:
            context.prec = max(50, len(str(abs(total_notional_units))) + len(str(abs(filled_units))) + 20)
            context.rounding = ROUND_HALF_EVEN
            raw_average = Decimal(total_notional_units) / Decimal(filled_units) / Decimal(10**_PRICE_SCALE)
            average_fill_price = raw_average.quantize(Decimal("0.000000000000000001"))

    return PaperFillResult(
        status=status,
        reason=reason,
        requested_quantity=_from_scaled_int(quantity_units, scale=_QUANTITY_SCALE),
        filled_quantity=_from_scaled_int(filled_units, scale=_QUANTITY_SCALE),
        remaining_quantity=_from_scaled_int(remaining_units, scale=_QUANTITY_SCALE),
        average_fill_price=average_fill_price,
        notional=_from_scaled_int(total_notional_units, scale=_PRICE_SCALE + _QUANTITY_SCALE),
        fee=Decimal("0.000000000000000000"),
        fills=tuple(fills),
    )


class KalshiPaperMarketDataClient:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], datetime] | None = None,
        origin: str = KALSHI_PAPER_MARKET_DATA_ORIGIN,
    ) -> None:
        normalized = origin.rstrip("/")
        parsed = urlsplit(normalized)
        if normalized != KALSHI_PAPER_MARKET_DATA_ORIGIN or parsed.scheme != "https" or parsed.path:
            raise KalshiPaperProtocolError("paper market data origin must be the approved Kalshi production origin")
        self._origin = normalized
        self._transport = transport
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def _get(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        url = f"{self._origin}{path}"
        try:
            response = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise KalshiPaperProtocolError("Kalshi read-only market-data request failed") from exc
        if response.status_code != 200:
            raise KalshiPaperProtocolError(f"Kalshi read-only market-data request returned HTTP {response.status_code}")
        return response

    async def fetch_quote(self, ticker: str) -> PaperQuote:
        normalized_ticker = str(ticker or "").strip().upper()
        if not _TICKER_PATTERN.fullmatch(normalized_ticker):
            raise KalshiPaperProtocolError("ticker is invalid")
        encoded_ticker = quote(normalized_ticker, safe="")

        async with httpx.AsyncClient(
            transport=self._transport,
            headers={"Accept": "application/json"},
            timeout=5.0,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            market_response = await self._get(client, f"{KALSHI_API_PREFIX}/markets/{encoded_ticker}")
            market_fetched_at = self._now().astimezone(timezone.utc)
            market_observed_at = _parse_source_date(market_response, now=market_fetched_at)
            market_payload = _required_mapping(
                _load_json(market_response, context="market"),
                "market",
                context="market",
            )
            market = self._parse_market(
                requested_ticker=normalized_ticker,
                payload=market_payload,
                observed_at=market_observed_at,
                fetched_at=market_fetched_at,
            )

            book_response = await self._get(
                client,
                f"{KALSHI_API_PREFIX}/markets/{encoded_ticker}/orderbook",
                params={"depth": 100},
            )
            book_fetched_at = self._now().astimezone(timezone.utc)
            book_observed_at = _parse_source_date(book_response, now=book_fetched_at)
            orderbook_payload = _required_mapping(
                _load_json(book_response, context="orderbook"),
                "orderbook_fp",
                context="orderbook",
            )
            book = self._parse_book(
                ticker=normalized_ticker,
                payload=orderbook_payload,
                price_ranges=market.price_ranges,
                observed_at=book_observed_at,
                fetched_at=book_fetched_at,
            )
            refreshed_market_response = await self._get(
                client,
                f"{KALSHI_API_PREFIX}/markets/{encoded_ticker}",
            )
            refreshed_market_fetched_at = self._now().astimezone(timezone.utc)
            refreshed_market_observed_at = _parse_source_date(
                refreshed_market_response,
                now=refreshed_market_fetched_at,
            )
            refreshed_market_payload = _required_mapping(
                _load_json(refreshed_market_response, context="market"),
                "market",
                context="market",
            )
            refreshed_market = self._parse_market(
                requested_ticker=normalized_ticker,
                payload=refreshed_market_payload,
                observed_at=refreshed_market_observed_at,
                fetched_at=refreshed_market_fetched_at,
            )
        if (
            refreshed_market.event_ticker != market.event_ticker
            or refreshed_market.notional_value != market.notional_value
            or refreshed_market.price_level_structure != market.price_level_structure
            or refreshed_market.price_ranges != market.price_ranges
        ):
            raise KalshiPaperProtocolError("Kalshi market execution terms changed during quote collection")
        if refreshed_market.fee_waiver_expiration <= refreshed_market_fetched_at:
            raise KalshiPaperProtocolError("Kalshi market fee waiver is not active after orderbook observation")
        return PaperQuote(market=refreshed_market, book=book)

    def _parse_market(
        self,
        *,
        requested_ticker: str,
        payload: Mapping[str, object],
        observed_at: datetime,
        fetched_at: datetime,
    ) -> PaperMarket:
        ticker = _required_text(payload, "ticker", context="market").upper()
        if ticker != requested_ticker:
            raise KalshiPaperProtocolError("Kalshi market ticker identity mismatch")
        event_ticker = _required_text(payload, "event_ticker", context="market").upper()
        if _required_text(payload, "market_type", context="market") != "binary":
            raise KalshiPaperProtocolError("paper execution supports binary Kalshi markets only")
        if _required_text(payload, "status", context="market") != "active":
            raise KalshiPaperProtocolError("Kalshi market is not active")
        notional_value = parse_money(payload.get("notional_value_dollars"), field="notional_value_dollars")
        if notional_value != Decimal("1"):
            raise KalshiPaperProtocolError("paper execution supports one-dollar notional markets only")
        fee_waiver_expiration = _parse_rfc3339(
            payload.get("fee_waiver_expiration_time"),
            field="fee waiver expiration",
        )
        if fee_waiver_expiration <= observed_at:
            raise KalshiPaperProtocolError("Kalshi market fee waiver is not active")
        price_level_structure = _required_text(payload, "price_level_structure", context="market")
        price_ranges = _parse_price_ranges(payload.get("price_ranges"))

        evidence = {
            "ticker": ticker,
            "event_ticker": event_ticker,
            "market_type": "binary",
            "status": "active",
            "notional_value_dollars": decimal_string(notional_value, scale=_PRICE_SCALE),
            "fee_waiver_expiration_time": fee_waiver_expiration.isoformat(),
            "close_time": _required_text(payload, "close_time", context="market"),
            "latest_expiration_time": _required_text(payload, "latest_expiration_time", context="market"),
            "price_level_structure": price_level_structure,
            "price_ranges": [
                {
                    "start": decimal_string(price_range.start, scale=_PRICE_SCALE),
                    "end": decimal_string(price_range.end, scale=_PRICE_SCALE),
                    "step": decimal_string(price_range.step, scale=_PRICE_SCALE),
                }
                for price_range in price_ranges
            ],
        }
        evidence_json = _canonical_json(evidence)
        evidence_hash = _hash_json(evidence_json)
        fee_provenance = MappingProxyType(
            {
                "kind": "market_fee_waiver",
                "waiver_expiration_time": fee_waiver_expiration.isoformat(),
                "openapi_sha256": KALSHI_OPENAPI_SHA256,
                "market_snapshot_hash": evidence_hash,
                "observed_at": observed_at.isoformat(),
            }
        )
        return PaperMarket(
            ticker=ticker,
            event_ticker=event_ticker,
            notional_value=notional_value,
            price_level_structure=price_level_structure,
            price_ranges=price_ranges,
            fee=Decimal("0"),
            fee_rule_version="kalshi-market-fee-waiver-v1",
            fee_provenance=fee_provenance,
            fee_waiver_expiration=fee_waiver_expiration,
            observed_at=observed_at,
            fetched_at=fetched_at,
            evidence_hash=evidence_hash,
            evidence_json=evidence_json,
        )

    def _parse_book(
        self,
        *,
        ticker: str,
        payload: Mapping[str, object],
        price_ranges: tuple[PaperPriceRange, ...],
        observed_at: datetime,
        fetched_at: datetime,
    ) -> PaperBook:
        yes_bids = self._parse_levels(payload.get("yes_dollars"), side="yes")
        no_bids = self._parse_levels(payload.get("no_dollars"), side="no")
        if not yes_bids and not no_bids:
            raise KalshiPaperProtocolError("Kalshi orderbook displayed depth is empty")
        for level in (*yes_bids, *no_bids):
            price_units = _to_scaled_int(level.price, scale=_PRICE_SCALE, field="book price")
            if not _price_is_on_tick(price_units, price_ranges):
                raise KalshiPaperProtocolError("Kalshi orderbook contains an off-tick price")
        evidence = {
            "ticker": ticker,
            "yes_dollars": [
                [decimal_string(level.price, scale=_PRICE_SCALE), decimal_string(level.quantity, scale=_QUANTITY_SCALE)]
                for level in yes_bids
            ],
            "no_dollars": [
                [decimal_string(level.price, scale=_PRICE_SCALE), decimal_string(level.quantity, scale=_QUANTITY_SCALE)]
                for level in no_bids
            ],
            "source_origin": self._origin,
            "observed_at": observed_at.isoformat(),
        }
        evidence_json = _canonical_json(evidence)
        return PaperBook(
            ticker=ticker,
            yes_bids=yes_bids,
            no_bids=no_bids,
            source_origin=self._origin,
            observed_at=observed_at,
            fetched_at=fetched_at,
            evidence_hash=_hash_json(evidence_json),
            evidence_json=evidence_json,
        )

    @staticmethod
    def _parse_levels(value: object, *, side: Outcome) -> tuple[PaperBookLevel, ...]:
        if not isinstance(value, list):
            raise KalshiPaperProtocolError(f"Kalshi orderbook {side}_dollars must be an array")
        levels: list[PaperBookLevel] = []
        seen_prices: set[Decimal] = set()
        for raw_level in value:
            if not isinstance(raw_level, list) or len(raw_level) != 2:
                raise KalshiPaperProtocolError(f"Kalshi orderbook {side} level must contain price and quantity")
            price = parse_price(raw_level[0], field="price")
            quantity = parse_quantity(raw_level[1], field="quantity")
            if price in seen_prices:
                raise KalshiPaperProtocolError(f"Kalshi orderbook {side} contains a duplicate price")
            seen_prices.add(price)
            levels.append(PaperBookLevel(price=price, quantity=quantity))
        return tuple(levels)
