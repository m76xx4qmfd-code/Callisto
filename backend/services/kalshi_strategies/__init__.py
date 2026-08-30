"""Dedicated paper-only Kalshi strategy engines.

This namespace is intentionally separate from the generic opportunity strategy
catalog because its engines consume retained venue evidence and have no live
exchange execution capability.
"""

from services.kalshi_strategies.confirmed_yes_reversal import (
    STRATEGY_ID,
    STRATEGY_VERSION,
    ConfirmedYesReversalCandle,
    ConfirmedYesReversalDecision,
    ConfirmedYesReversalMarket,
    ConfirmedYesReversalSchedule,
    ConfirmedYesReversalSettlement,
    ConfirmedYesReversalStrategy,
)

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "ConfirmedYesReversalCandle",
    "ConfirmedYesReversalDecision",
    "ConfirmedYesReversalMarket",
    "ConfirmedYesReversalSchedule",
    "ConfirmedYesReversalSettlement",
    "ConfirmedYesReversalStrategy",
]
