from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services.kalshi_strategies.confirmed_yes_reversal import (
    STRATEGY_ID,
    STRATEGY_VERSION,
    ConfirmedYesReversalCandle,
    ConfirmedYesReversalMarket,
    ConfirmedYesReversalSchedule,
    ConfirmedYesReversalSettlement,
    ConfirmedYesReversalStrategy,
)


CALL = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)


def candle(hours_before: int, bid: str | None, ask: str | None, volume: str | None = "1.00"):
    return ConfirmedYesReversalCandle(
        end_time=CALL - timedelta(hours=hours_before),
        yes_bid=bid,
        yes_ask=ask,
        volume=volume,
    )


def schedule(*, observed_at: datetime | None = None) -> ConfirmedYesReversalSchedule:
    return ConfirmedYesReversalSchedule(
        call_start=CALL,
        published_at=CALL - timedelta(days=30),
        observed_at=observed_at or CALL - timedelta(days=20),
        source_url="https://investor.example.test/call",
        source_content_sha256="a" * 64,
        supersedes_source_content_sha256=None,
    )


def market(candles, *, result: str | None = "yes") -> ConfirmedYesReversalMarket:
    settlement = (
        ConfirmedYesReversalSettlement(
            result=result,
            observed_at=CALL + timedelta(hours=3),
            source_url="https://external-api.kalshi.com/trade-api/v2/markets/test",
            evidence_sha256="b" * 64,
            final=True,
        )
        if result is not None
        else None
    )
    return ConfirmedYesReversalMarket(
        ticker="KXEARNINGSMENTIONTEST-26SEP01-WORD",
        market_open=CALL - timedelta(days=40),
        market_close=CALL + timedelta(hours=2),
        candles=tuple(candles),
        settlement=settlement,
    )


def test_normal_quote_exit_uses_delayed_entry_ask_exit_bid_and_exact_fees() -> None:
    strategy = ConfirmedYesReversalStrategy()
    decision = strategy.evaluate_market(
        schedule=schedule(),
        market=market(
            [
                candle(60, "0.490000", "0.510000"),
                candle(48, "0.240000", "0.260000", "5.00"),
                candle(47, "0.260000", "0.280000", "3.00"),
                candle(1, "0.400000", "0.420000", "2.00"),
            ]
        ),
    )

    assert decision.strategy_id == STRATEGY_ID
    assert decision.strategy_version == STRATEGY_VERSION == 1
    assert decision.decision == "selected"
    assert decision.exit_mode == "pre_call_quote"
    assert decision.signal_time == CALL - timedelta(hours=48)
    assert decision.entry_time == CALL - timedelta(hours=47)
    assert decision.exit_time == CALL - timedelta(hours=1)
    assert decision.entry_price == "0.280000"
    assert decision.exit_price == "0.400000"
    assert decision.entry_fee == "0.0142"
    assert decision.exit_fee == "0.0168"
    assert decision.opening_cost == "0.294200"
    assert decision.net_proceeds == "0.383200"
    assert decision.pnl == "0.089000"
    assert Decimal(decision.roi) == Decimal("0.3025152957171991842284160435")


def test_latest_invalid_reference_blocks_fallback_to_older_favorable_quote() -> None:
    strategy = ConfirmedYesReversalStrategy()
    decision = strategy.evaluate_market(
        schedule=schedule(),
        market=market(
            [
                candle(61, "0.490000", "0.510000"),
                candle(60, "0", "0"),
                candle(48, "0.240000", "0.260000", "5.00"),
                candle(47, "0.260000", "0.280000", "3.00"),
                candle(1, "0.400000", "0.420000"),
            ]
        ),
    )
    assert decision.decision == "abstain"
    assert decision.reason == "no_qualifying_signal"


def test_failed_delayed_entry_does_not_block_a_later_valid_signal() -> None:
    strategy = ConfirmedYesReversalStrategy()
    decision = strategy.evaluate_market(
        schedule=schedule(),
        market=market(
            [
                candle(90, "0.490000", "0.510000"),
                candle(78, "0.240000", "0.260000", "2.00"),
                candle(77, "0.100000", "0.300000", "2.00"),
                candle(60, "0.490000", "0.510000"),
                candle(48, "0.240000", "0.260000", "5.00"),
                candle(47, "0.260000", "0.280000", "3.00"),
                candle(1, "0.400000", "0.420000"),
            ]
        ),
    )
    assert decision.decision == "selected"
    assert decision.signal_time == CALL - timedelta(hours=48)


def test_schedule_observed_after_signal_forces_abstention() -> None:
    strategy = ConfirmedYesReversalStrategy()
    decision = strategy.evaluate_market(
        schedule=schedule(observed_at=CALL - timedelta(hours=47)),
        market=market(
            [
                candle(60, "0.490000", "0.510000"),
                candle(48, "0.240000", "0.260000", "5.00"),
                candle(47, "0.260000", "0.280000", "3.00"),
                candle(1, "0.400000", "0.420000"),
            ]
        ),
    )
    assert decision.decision == "abstain"
    assert decision.reason == "schedule_not_observed_before_signal"


def test_missing_exit_uses_authoritative_yes_settlement_without_exit_fee() -> None:
    strategy = ConfirmedYesReversalStrategy()
    decision = strategy.evaluate_market(
        schedule=schedule(),
        market=market(
            [
                candle(60, "0.490000", "0.510000"),
                candle(48, "0.240000", "0.260000", "5.00"),
                candle(47, "0.260000", "0.280000", "3.00"),
            ],
            result="yes",
        ),
    )
    assert decision.decision == "selected"
    assert decision.exit_mode == "settlement_fallback"
    assert decision.exit_fee == "0.0000"
    assert decision.net_proceeds == "1.000000"
    assert decision.pnl == "0.705800"


def test_missing_exit_and_unresolved_market_remain_settlement_pending() -> None:
    strategy = ConfirmedYesReversalStrategy()
    decision = strategy.evaluate_market(
        schedule=schedule(),
        market=market(
            [
                candle(60, "0.490000", "0.510000"),
                candle(48, "0.240000", "0.260000", "5.00"),
                candle(47, "0.260000", "0.280000", "3.00"),
            ],
            result=None,
        ),
    )
    assert decision.decision == "selected"
    assert decision.exit_mode == "settlement_pending"
    assert decision.net_proceeds is None
    assert decision.pnl is None
    assert decision.roi is None


def test_frozen_specification_is_paper_only_and_not_configurable() -> None:
    spec = ConfirmedYesReversalStrategy.specification()
    assert spec["strategy_id"] == "strategy_1_max_roi_confirmed_yes_reversal"
    assert spec["version"] == 1
    assert spec["position_side"] == "yes"
    assert spec["quantity"] == "1.00"
    assert spec["execution_authority"] == "paper_only"
    assert spec["live_exchange_writes"] == "prohibited"
    assert spec["signal_search_start_hours_before_call"] == 168
    assert spec["signal_search_end_hours_before_call"] == 6
    assert spec["reference_lookback_hours"] == 12
    assert spec["minimum_drop"] == "0.20"
    assert spec["maximum_signal_midpoint"] == "0.25"
    assert spec["exit_hours_before_call"] == 1
    assert spec["source_spec_sha256"] == "a467de43aaa661a4d1097a5e333c6541e86c28910c02412494cb397d0bb7e1ca"


@pytest.mark.parametrize("signal_hours", [168, 6])
def test_signal_search_boundaries_are_inclusive(signal_hours: int) -> None:
    decision = ConfirmedYesReversalStrategy().evaluate_market(
        schedule=schedule(),
        market=market(
            [
                candle(signal_hours + 12, "0.490000", "0.510000"),
                candle(signal_hours, "0.240000", "0.260000", "1.00"),
                candle(signal_hours - 1, "0.250000", "0.270000", "1.00"),
                candle(1, "0.400000", "0.420000"),
            ]
        ),
    )
    assert decision.decision == "selected"


def test_signal_after_six_hour_boundary_is_rejected() -> None:
    decision = ConfirmedYesReversalStrategy().evaluate_market(
        schedule=schedule(),
        market=market(
            [
                candle(17, "0.490000", "0.510000"),
                candle(5, "0.240000", "0.260000", "1.00"),
                candle(4, "0.250000", "0.270000", "1.00"),
                candle(1, "0.400000", "0.420000"),
            ]
        ),
    )
    assert decision.decision == "abstain"


def test_reference_older_than_six_hours_is_rejected() -> None:
    decision = ConfirmedYesReversalStrategy().evaluate_market(
        schedule=schedule(),
        market=market(
            [
                candle(67, "0.490000", "0.510000"),
                candle(48, "0.240000", "0.260000", "1.00"),
                candle(47, "0.250000", "0.270000", "1.00"),
                candle(1, "0.400000", "0.420000"),
            ]
        ),
    )
    assert decision.decision == "abstain"


@pytest.mark.parametrize(
    ("bid", "ask", "volume"),
    [("0.190000", "0.310001", "1.00"), ("0.240000", "0.260000", "0")],
)
def test_signal_spread_and_volume_gates_are_strict(bid: str, ask: str, volume: str) -> None:
    decision = ConfirmedYesReversalStrategy().evaluate_market(
        schedule=schedule(),
        market=market(
            [
                candle(60, "0.490000", "0.510000"),
                candle(48, bid, ask, volume),
                candle(47, "0.250000", "0.270000", "1.00"),
                candle(1, "0.400000", "0.420000"),
            ]
        ),
    )
    assert decision.decision == "abstain"


def test_next_retained_entry_more_than_two_hours_later_is_rejected() -> None:
    decision = ConfirmedYesReversalStrategy().evaluate_market(
        schedule=schedule(),
        market=market(
            [
                candle(60, "0.490000", "0.510000"),
                candle(48, "0.240000", "0.260000", "1.00"),
                candle(45, "0.250000", "0.270000", "1.00"),
                candle(1, "0.400000", "0.420000"),
            ]
        ),
    )
    assert decision.decision == "abstain"


def test_latest_invalid_exit_blocks_older_quote_and_uses_settlement() -> None:
    decision = ConfirmedYesReversalStrategy().evaluate_market(
        schedule=schedule(),
        market=market(
            [
                candle(60, "0.490000", "0.510000"),
                candle(48, "0.240000", "0.260000", "1.00"),
                candle(47, "0.250000", "0.270000", "1.00"),
                candle(2, "0.400000", "0.420000"),
                candle(1, "0", "0"),
            ],
            result="no",
        ),
    )
    assert decision.exit_mode == "settlement_fallback"
    assert decision.net_proceeds == "0.000000"
    assert decision.pnl == "-0.283800"


def test_market_close_at_entry_boundary_forces_abstention() -> None:
    candles = (
        candle(60, "0.490000", "0.510000"),
        candle(48, "0.240000", "0.260000", "1.00"),
        candle(47, "0.250000", "0.270000", "1.00"),
    )
    closed_market = ConfirmedYesReversalMarket(
        ticker="KXEARNINGSMENTIONTEST-26SEP01-CLOSED",
        market_open=CALL - timedelta(days=40),
        market_close=CALL - timedelta(hours=47),
        candles=candles,
        settlement=ConfirmedYesReversalSettlement(
            result="no",
            observed_at=CALL + timedelta(hours=3),
            source_url="https://external-api.kalshi.com/trade-api/v2/markets/test",
            evidence_sha256="b" * 64,
            final=True,
        ),
    )
    decision = ConfirmedYesReversalStrategy().evaluate_market(
        schedule=schedule(), market=closed_market
    )
    assert decision.decision == "abstain"


def test_duplicate_candle_times_are_rejected_at_input_boundary() -> None:
    duplicate = candle(48, "0.240000", "0.260000", "1.00")
    with pytest.raises(ValueError, match="unique"):
        market([duplicate, duplicate])


def test_nonfinal_or_pre_call_settlement_evidence_is_rejected() -> None:
    with pytest.raises(ValueError, match="final"):
        ConfirmedYesReversalSettlement(
            result="yes",
            observed_at=CALL + timedelta(hours=1),
            source_url="https://external-api.kalshi.com/trade-api/v2/markets/test",
            evidence_sha256="b" * 64,
            final=False,
        )
