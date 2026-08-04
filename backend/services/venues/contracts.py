"""Venue-neutral order intent contract.

Strategies and orchestrators should describe an intended order without knowing a
venue's HTTP path, authentication headers, or wire field names. Venue adapters
translate this immutable intent at the true external boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

VenueName = Literal["kalshi", "polymarket"]
BookSide = Literal["bid", "ask"]
TimeInForce = Literal["good_till_canceled", "immediate_or_cancel", "fill_or_kill"]


@dataclass(frozen=True)
class VenueOrderIntent:
    venue: VenueName
    instrument_id: str
    client_order_id: str
    book_side: BookSide
    quantity: Decimal
    limit_price: Decimal
    time_in_force: TimeInForce = "good_till_canceled"
    post_only: bool = False

    def __post_init__(self) -> None:
        instrument_id = self.instrument_id.strip()
        client_order_id = self.client_order_id.strip()
        try:
            quantity = self.quantity if isinstance(self.quantity, Decimal) else Decimal(str(self.quantity))
            limit_price = self.limit_price if isinstance(self.limit_price, Decimal) else Decimal(str(self.limit_price))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("quantity and limit_price must be finite decimals") from exc

        if self.venue not in {"kalshi", "polymarket"}:
            raise ValueError("unsupported venue")
        if not instrument_id:
            raise ValueError("instrument_id is required")
        if not client_order_id:
            raise ValueError("client_order_id is required")
        if self.book_side not in {"bid", "ask"}:
            raise ValueError("book_side must be 'bid' or 'ask'")
        if self.time_in_force not in {
            "good_till_canceled",
            "immediate_or_cancel",
            "fill_or_kill",
        }:
            raise ValueError("unsupported time_in_force")
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("quantity must be a positive finite decimal")
        if not limit_price.is_finite() or limit_price <= 0 or limit_price >= 1:
            raise ValueError("limit_price must be greater than 0 and less than 1")

        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "client_order_id", client_order_id)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "limit_price", limit_price)


__all__ = [
    "BookSide",
    "TimeInForce",
    "VenueName",
    "VenueOrderIntent",
]
