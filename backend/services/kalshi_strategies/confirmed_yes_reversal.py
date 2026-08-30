from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Literal
from urllib.parse import urlsplit

STRATEGY_ID = "strategy_1_max_roi_confirmed_yes_reversal"
STRATEGY_VERSION = 1
SOURCE_SPEC_SHA256 = "a467de43aaa661a4d1097a5e333c6541e86c28910c02412494cb397d0bb7e1ca"

_SEARCH_START = timedelta(hours=168)
_SEARCH_END = timedelta(hours=6)
_REFERENCE_LOOKBACK = timedelta(hours=12)
_REFERENCE_MAX_AGE = timedelta(hours=6)
_MAX_SPREAD = Decimal("0.10")
_MINIMUM_DROP = Decimal("0.20")
_MAXIMUM_SIGNAL_MIDPOINT = Decimal("0.25")
_ENTRY_MAX_DELAY = timedelta(hours=2)
_EXIT_LEAD = timedelta(hours=1)
_EXIT_MAX_AGE = timedelta(hours=1)
_FEE_RATE = Decimal("0.07")
_FEE_QUANTUM = Decimal("0.0001")
_PRICE_QUANTUM = Decimal("0.000001")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")

SettlementResult = Literal["yes", "no"]
DecisionKind = Literal["selected", "abstain"]
ExitMode = Literal["pre_call_quote", "settlement_fallback", "settlement_pending"]


def _utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _decimal(value: str | None, *, field: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _DECIMAL_PATTERN.fullmatch(value):
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    if not parsed.is_finite():
        return None
    exponent = parsed.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -6:
        return None
    return parsed


def _money6(value: Decimal) -> str:
    return format(value.quantize(_PRICE_QUANTUM), ".6f")


def _fee4(value: Decimal) -> str:
    return format(value.quantize(_FEE_QUANTUM), ".4f")


def _fee(price: Decimal) -> Decimal:
    raw = _FEE_RATE * price * (Decimal("1") - price)
    return raw.quantize(_FEE_QUANTUM, rounding=ROUND_CEILING)


@dataclass(frozen=True)
class ConfirmedYesReversalCandle:
    end_time: datetime
    yes_bid: str | None
    yes_ask: str | None
    volume: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "end_time", _utc(self.end_time, field="candle.end_time"))


@dataclass(frozen=True)
class ConfirmedYesReversalSchedule:
    call_start: datetime
    published_at: datetime
    observed_at: datetime
    source_url: str
    source_content_sha256: str
    supersedes_source_content_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "call_start", _utc(self.call_start, field="schedule.call_start"))
        object.__setattr__(self, "published_at", _utc(self.published_at, field="schedule.published_at"))
        object.__setattr__(self, "observed_at", _utc(self.observed_at, field="schedule.observed_at"))
        parsed = urlsplit(self.source_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("schedule.source_url must be an HTTP(S) URL")
        digest = str(self.source_content_sha256 or "").lower()
        if not _HASH_PATTERN.fullmatch(digest):
            raise ValueError("schedule.source_content_sha256 must be a SHA-256 digest")
        object.__setattr__(self, "source_content_sha256", digest)
        if self.supersedes_source_content_sha256 is not None:
            supersedes = str(self.supersedes_source_content_sha256).lower()
            if not _HASH_PATTERN.fullmatch(supersedes):
                raise ValueError("schedule.supersedes_source_content_sha256 must be a SHA-256 digest")
            object.__setattr__(self, "supersedes_source_content_sha256", supersedes)
        if self.published_at > self.observed_at:
            raise ValueError("schedule cannot be observed before it was published")
        if self.observed_at >= self.call_start:
            raise ValueError("schedule must be observed before the scheduled call")


@dataclass(frozen=True)
class ConfirmedYesReversalSettlement:
    result: SettlementResult
    observed_at: datetime
    source_url: str
    evidence_sha256: str
    final: bool

    def __post_init__(self) -> None:
        if self.result not in {"yes", "no"}:
            raise ValueError("settlement.result must be yes or no")
        object.__setattr__(self, "observed_at", _utc(self.observed_at, field="settlement.observed_at"))
        parsed = urlsplit(self.source_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("settlement.source_url must be an HTTP(S) URL")
        digest = str(self.evidence_sha256 or "").lower()
        if not _HASH_PATTERN.fullmatch(digest):
            raise ValueError("settlement.evidence_sha256 must be a SHA-256 digest")
        object.__setattr__(self, "evidence_sha256", digest)
        if self.final is not True:
            raise ValueError("settlement evidence must be final")


@dataclass(frozen=True)
class ConfirmedYesReversalMarket:
    ticker: str
    market_open: datetime
    market_close: datetime
    candles: tuple[ConfirmedYesReversalCandle, ...]
    settlement: ConfirmedYesReversalSettlement | None = None

    def __post_init__(self) -> None:
        ticker = str(self.ticker or "").strip()
        if not ticker or not ticker.isascii():
            raise ValueError("market.ticker must be non-empty ASCII")
        object.__setattr__(self, "ticker", ticker)
        object.__setattr__(self, "market_open", _utc(self.market_open, field="market.market_open"))
        object.__setattr__(self, "market_close", _utc(self.market_close, field="market.market_close"))
        if self.market_open >= self.market_close:
            raise ValueError("market_open must precede market_close")
        ordered = tuple(sorted(self.candles, key=lambda candle: candle.end_time))
        if len({candle.end_time for candle in ordered}) != len(ordered):
            raise ValueError("market candles must have unique end_time values")
        object.__setattr__(self, "candles", ordered)


@dataclass(frozen=True)
class ConfirmedYesReversalDecision:
    strategy_id: str
    strategy_version: int
    ticker: str
    decision: DecisionKind
    reason: str
    execution_authority: str = "paper_only"
    live_exchange_writes: str = "prohibited"
    quantity: str = "1.00"
    schedule_source_url: str | None = None
    schedule_source_content_sha256: str | None = None
    schedule_observed_at: datetime | None = None
    signal_time: datetime | None = None
    reference_time: datetime | None = None
    entry_time: datetime | None = None
    exit_time: datetime | None = None
    exit_mode: ExitMode | None = None
    reference_midpoint: str | None = None
    reference_bid: str | None = None
    reference_ask: str | None = None
    signal_midpoint: str | None = None
    signal_bid: str | None = None
    signal_ask: str | None = None
    signal_volume: str | None = None
    signal_drop: str | None = None
    entry_price: str | None = None
    entry_bid: str | None = None
    entry_volume: str | None = None
    exit_price: str | None = None
    exit_ask: str | None = None
    entry_fee: str | None = None
    exit_fee: str | None = None
    opening_cost: str | None = None
    net_proceeds: str | None = None
    pnl: str | None = None
    roi: str | None = None
    settlement_result: str | None = None
    settlement_observed_at: datetime | None = None
    settlement_source_url: str | None = None
    settlement_evidence_sha256: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        for key in (
            "schedule_observed_at",
            "signal_time",
            "reference_time",
            "entry_time",
            "exit_time",
            "settlement_observed_at",
        ):
            value = payload[key]
            if isinstance(value, datetime):
                payload[key] = value.isoformat()
        return payload


@dataclass(frozen=True)
class _Quote:
    bid: Decimal
    ask: Decimal

    @property
    def midpoint(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


class ConfirmedYesReversalStrategy:
    """Frozen Version 1 paper evaluator.

    The engine is deliberately pure: it accepts retained evidence and returns a
    paper decision. It has no network, credential, order, or persistence
    capability.
    """

    @staticmethod
    def specification() -> dict[str, object]:
        return {
            "strategy_id": STRATEGY_ID,
            "version": STRATEGY_VERSION,
            "source_spec_sha256": SOURCE_SPEC_SHA256,
            "objective_label": "Maximum observed historical ROI candidate",
            "position_side": "yes",
            "quantity": "1.00",
            "primary_candle_granularity": "hourly",
            "normal_exit": "pre_call_quote",
            "residual_exit": "authoritative_settlement_fallback",
            "execution_authority": "paper_only",
            "live_exchange_writes": "prohibited",
            "historical_status": "retrospectively_selected_no_untouched_holdout",
            "signal_search_start_hours_before_call": 168,
            "signal_search_end_hours_before_call": 6,
            "reference_lookback_hours": 12,
            "maximum_reference_staleness_hours": 6,
            "minimum_drop": "0.20",
            "maximum_signal_midpoint": "0.25",
            "maximum_spread": "0.10",
            "entry_delay_candles": 1,
            "maximum_entry_delay_hours": 2,
            "exit_hours_before_call": 1,
            "maximum_exit_staleness_hours": 1,
            "fee_rate": "0.07",
            "fee_multiplier": "1",
            "fee_rounding": "ceil_0.0001_each_leg",
            "take_profit": None,
            "stop_loss": None,
        }

    @staticmethod
    def _quote(candle: ConfirmedYesReversalCandle) -> _Quote | None:
        bid = _decimal(candle.yes_bid, field="yes_bid")
        ask = _decimal(candle.yes_ask, field="yes_ask")
        if bid is None or ask is None or bid <= 0 or ask >= 1 or bid > ask:
            return None
        return _Quote(bid=bid, ask=ask)

    @staticmethod
    def _positive_volume(candle: ConfirmedYesReversalCandle) -> bool:
        volume = _decimal(candle.volume, field="volume")
        return volume is not None and volume > 0

    @staticmethod
    def _latest_index_at_or_before(
        candles: tuple[ConfirmedYesReversalCandle, ...], target: datetime
    ) -> int | None:
        latest: int | None = None
        for index, candle in enumerate(candles):
            if candle.end_time > target:
                break
            latest = index
        return latest

    def evaluate_market(
        self,
        *,
        schedule: ConfirmedYesReversalSchedule,
        market: ConfirmedYesReversalMarket,
    ) -> ConfirmedYesReversalDecision:
        call_start = schedule.call_start
        search_start = call_start - _SEARCH_START
        search_end = call_start - _SEARCH_END
        exit_target = call_start - _EXIT_LEAD
        schedule_late = False

        for signal_index, signal_candle in enumerate(market.candles):
            signal_time = signal_candle.end_time
            if signal_time < search_start:
                continue
            if signal_time > search_end:
                break
            signal_quote = self._quote(signal_candle)
            if signal_quote is None:
                continue
            if signal_quote.spread > _MAX_SPREAD or not self._positive_volume(signal_candle):
                continue
            if signal_quote.midpoint <= 0 or signal_quote.midpoint > _MAXIMUM_SIGNAL_MIDPOINT:
                continue

            reference_target = signal_time - _REFERENCE_LOOKBACK
            reference_index = self._latest_index_at_or_before(market.candles, reference_target)
            if reference_index is None:
                continue
            reference_candle = market.candles[reference_index]
            if reference_target - reference_candle.end_time > _REFERENCE_MAX_AGE:
                continue
            reference_quote = self._quote(reference_candle)
            if reference_quote is None or reference_quote.spread > _MAX_SPREAD:
                continue
            drop = reference_quote.midpoint - signal_quote.midpoint
            if drop < _MINIMUM_DROP:
                continue

            if schedule.published_at > signal_time or schedule.observed_at > signal_time:
                schedule_late = True
                continue
            entry_index = signal_index + 1
            if entry_index >= len(market.candles):
                continue
            entry_candle = market.candles[entry_index]
            elapsed = entry_candle.end_time - signal_time
            if elapsed <= timedelta(0) or elapsed > _ENTRY_MAX_DELAY:
                continue
            entry_quote = self._quote(entry_candle)
            if entry_quote is None:
                continue
            if entry_quote.spread > _MAX_SPREAD or not self._positive_volume(entry_candle):
                continue
            if entry_candle.end_time < market.market_open:
                continue
            if entry_candle.end_time >= min(market.market_close, call_start, exit_target):
                continue

            return self._selected_decision(
                schedule=schedule,
                market=market,
                reference_candle=reference_candle,
                reference_quote=reference_quote,
                signal_candle=signal_candle,
                signal_quote=signal_quote,
                signal_drop=drop,
                entry_candle=entry_candle,
                entry_quote=entry_quote,
            )

        return ConfirmedYesReversalDecision(
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            ticker=market.ticker,
            decision="abstain",
            reason=("schedule_not_observed_before_signal" if schedule_late else "no_qualifying_signal"),
            schedule_source_url=schedule.source_url,
            schedule_source_content_sha256=schedule.source_content_sha256,
            schedule_observed_at=schedule.observed_at,
        )

    def evaluate_markets(
        self,
        *,
        schedule: ConfirmedYesReversalSchedule,
        markets: tuple[ConfirmedYesReversalMarket, ...],
    ) -> tuple[ConfirmedYesReversalDecision, ...]:
        return tuple(self.evaluate_market(schedule=schedule, market=market) for market in markets)

    def _selected_decision(
        self,
        *,
        schedule: ConfirmedYesReversalSchedule,
        market: ConfirmedYesReversalMarket,
        reference_candle: ConfirmedYesReversalCandle,
        reference_quote: _Quote,
        signal_candle: ConfirmedYesReversalCandle,
        signal_quote: _Quote,
        signal_drop: Decimal,
        entry_candle: ConfirmedYesReversalCandle,
        entry_quote: _Quote,
    ) -> ConfirmedYesReversalDecision:
        entry_fee = _fee(entry_quote.ask)
        opening_cost = entry_quote.ask + entry_fee
        exit_target = schedule.call_start - _EXIT_LEAD
        exit_index = self._latest_index_at_or_before(market.candles, exit_target)
        exit_candle = market.candles[exit_index] if exit_index is not None else None
        exit_quote = self._quote(exit_candle) if exit_candle is not None else None
        executable_exit = (
            exit_candle is not None
            and exit_quote is not None
            and exit_target - exit_candle.end_time <= _EXIT_MAX_AGE
            and entry_candle.end_time < exit_candle.end_time
            and exit_candle.end_time < min(market.market_close, schedule.call_start)
        )

        exit_time: datetime | None = None
        exit_price: Decimal | None = None
        exit_fee = Decimal("0")
        proceeds: Decimal | None
        exit_mode: ExitMode
        if executable_exit:
            assert exit_candle is not None and exit_quote is not None
            exit_time = exit_candle.end_time
            exit_price = exit_quote.bid
            exit_fee = _fee(exit_price)
            proceeds = exit_price - exit_fee
            exit_mode = "pre_call_quote"
        elif market.settlement is None:
            proceeds = None
            exit_mode = "settlement_pending"
        else:
            if market.settlement.observed_at < schedule.call_start:
                raise ValueError("settlement evidence cannot predate the scheduled call")
            proceeds = Decimal("1") if market.settlement.result == "yes" else Decimal("0")
            exit_mode = "settlement_fallback"

        pnl = proceeds - opening_cost if proceeds is not None else None
        roi = pnl / opening_cost if pnl is not None else None
        return ConfirmedYesReversalDecision(
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            ticker=market.ticker,
            decision="selected",
            reason="confirmed_yes_reversal",
            schedule_source_url=schedule.source_url,
            schedule_source_content_sha256=schedule.source_content_sha256,
            schedule_observed_at=schedule.observed_at,
            signal_time=signal_candle.end_time,
            reference_time=reference_candle.end_time,
            entry_time=entry_candle.end_time,
            exit_time=exit_time,
            exit_mode=exit_mode,
            reference_midpoint=_money6(reference_quote.midpoint),
            reference_bid=_money6(reference_quote.bid),
            reference_ask=_money6(reference_quote.ask),
            signal_midpoint=_money6(signal_quote.midpoint),
            signal_bid=_money6(signal_quote.bid),
            signal_ask=_money6(signal_quote.ask),
            signal_volume=_money6(_decimal(signal_candle.volume, field="volume") or Decimal("0")),
            signal_drop=_money6(signal_drop),
            entry_price=_money6(entry_quote.ask),
            entry_bid=_money6(entry_quote.bid),
            entry_volume=_money6(_decimal(entry_candle.volume, field="volume") or Decimal("0")),
            exit_price=_money6(exit_price) if exit_price is not None else None,
            exit_ask=(
                _money6(exit_quote.ask)
                if executable_exit and exit_quote is not None
                else None
            ),
            entry_fee=_fee4(entry_fee),
            exit_fee=_fee4(exit_fee),
            opening_cost=_money6(opening_cost),
            net_proceeds=_money6(proceeds) if proceeds is not None else None,
            pnl=_money6(pnl) if pnl is not None else None,
            roi=format(roi, "f") if roi is not None else None,
            settlement_result=(
                market.settlement.result
                if exit_mode == "settlement_fallback" and market.settlement is not None
                else None
            ),
            settlement_observed_at=(
                market.settlement.observed_at
                if exit_mode == "settlement_fallback" and market.settlement is not None
                else None
            ),
            settlement_source_url=(
                market.settlement.source_url
                if exit_mode == "settlement_fallback" and market.settlement is not None
                else None
            ),
            settlement_evidence_sha256=(
                market.settlement.evidence_sha256
                if exit_mode == "settlement_fallback" and market.settlement is not None
                else None
            ),
        )
