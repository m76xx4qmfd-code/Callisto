"""Current Kalshi Predictions API V2 signing and event-order transport.

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
from decimal import Decimal, InvalidOperation
from typing import Literal
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

BookSide = Literal["bid", "ask"]
TimeInForce = Literal["good_till_canceled", "immediate_or_cancel", "fill_or_kill"]


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
    quantized = value.quantize(quantum)
    if quantized != value:
        raise KalshiProtocolError(f"{field} has more precision than Kalshi V2 permits ({quantum})")
    return format(quantized, "f")


def _integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise KalshiProtocolError(f"{field} must be an integer")
    try:
        return int(value)
    except ValueError as exc:
        raise KalshiProtocolError(f"{field} must be an integer") from exc


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
            balance_dollars = _decimal(payload["balance_dollars"], field="balance_dollars")
            portfolio_value_cents = _integer(payload["portfolio_value"], field="portfolio_value")
            updated_ts = _integer(payload["updated_ts"], field="updated_ts")
        except KeyError as exc:
            raise KalshiProtocolError("invalid Kalshi balance response") from exc

        raw_breakdown = payload.get("balance_breakdown", [])
        if not isinstance(raw_breakdown, list):
            raise KalshiProtocolError("balance_breakdown must be a list")
        breakdown: list[KalshiSubaccountBalance] = []
        for entry in raw_breakdown:
            if not isinstance(entry, dict):
                raise KalshiProtocolError("invalid balance_breakdown entry")
            try:
                exchange_index = _integer(entry["exchange_index"], field="exchange_index")
                balance = _decimal(entry["balance"], field="breakdown balance")
            except KeyError as exc:
                raise KalshiProtocolError("invalid balance_breakdown entry") from exc
            if exchange_index < 0:
                raise KalshiProtocolError("exchange_index cannot be negative")
            breakdown.append(
                KalshiSubaccountBalance(
                    exchange_index=exchange_index,
                    balance=balance,
                )
            )

        if balance_cents < 0 or portfolio_value_cents < 0 or updated_ts <= 0:
            raise KalshiProtocolError("invalid values in Kalshi balance response")
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
    self_trade_prevention_type: str = "taker_at_cross"
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
        if not self.self_trade_prevention_type.strip():
            raise KalshiProtocolError("self_trade_prevention_type is required")
        if count <= 0:
            raise KalshiProtocolError("count must be greater than zero")
        if price <= 0 or price >= 1:
            raise KalshiProtocolError("price must be greater than 0 and less than 1")
        if self.subaccount < 0:
            raise KalshiProtocolError("subaccount cannot be negative")
        if self.exchange_index < 0:
            raise KalshiProtocolError("exchange_index cannot be negative")

        _fixed(count, quantum=Decimal("0.01"), field="count")
        _fixed(price, quantum=Decimal("0.0001"), field="price")
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
            "price": _fixed(self.price, quantum=Decimal("0.0001"), field="price"),
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
    def from_payload(cls, payload: Mapping[str, object]) -> KalshiOrderAcknowledgement:
        try:
            order_id = str(payload["order_id"]).strip()
            client_order_id = str(payload["client_order_id"]).strip()
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
    """Minimal current V2 event-order transport with fail-closed write arming.

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

    async def get_balance(self) -> KalshiBalanceSnapshot:
        """Return the authenticated account balance without enabling writes."""

        timestamp_ms = self._now_ms()
        headers = {
            **self._signer.headers(
                timestamp_ms=timestamp_ms,
                method="GET",
                path=_BALANCE_PATH,
            ),
            "Accept": "application/json",
        }
        response = await self._http.get(
            f"{self._origin}{_BALANCE_PATH}",
            headers=headers,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise KalshiProtocolError("Kalshi balance response was not JSON") from exc
        if not isinstance(payload, dict):
            raise KalshiProtocolError("Kalshi balance response must be an object")
        return KalshiBalanceSnapshot.from_payload(payload)

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

        if response.is_error:
            raise KalshiAPIError(
                status_code=response.status_code,
                detail=response.text[:1000],
                client_order_id=order.client_order_id,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise KalshiProtocolError("Kalshi V2 order acknowledgement was not JSON") from exc
        if not isinstance(payload, dict):
            raise KalshiProtocolError("Kalshi V2 order acknowledgement must be an object")
        acknowledgement = KalshiOrderAcknowledgement.from_payload(payload)
        if acknowledgement.client_order_id != order.client_order_id:
            raise KalshiProtocolError("Kalshi acknowledgement client_order_id did not match submission")
        return acknowledgement
