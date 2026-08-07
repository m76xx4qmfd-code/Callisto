from __future__ import annotations

from collections.abc import Callable
from typing import Any

from models.database import KalshiPaperPosition
from services.kalshi_paper_execution import (
    KalshiPaperProtocolError,
    KalshiPublicResultClient,
    PaperMarketResultObservation,
    decimal_string,
)
from services.kalshi_paper_service import PaperPositionNotFound


class KalshiPaperResultService:
    """Read a held position ticker and observe its public production result."""

    def __init__(self, *, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory
        self._result_client = KalshiPublicResultClient()

    async def observe_position_result(
        self, *, account_id: str, position_id: str
    ) -> dict[str, object]:
        normalized_account_id = str(account_id or "").strip()
        normalized_position_id = str(position_id or "").strip()
        if not normalized_account_id or not normalized_position_id:
            raise ValueError("account_id and position_id are required")

        async with self._session_factory() as session:
            position = await session.get(
                KalshiPaperPosition,
                (normalized_account_id, normalized_position_id),
            )
        if position is None:
            raise PaperPositionNotFound("paper position not found")

        observation = await self._result_client.fetch_market_result(position.ticker)
        if observation.ticker != position.ticker:
            raise KalshiPaperProtocolError(
                "public result does not match the authoritative position ticker"
            )
        return self._serialize(
            account_id=normalized_account_id,
            position_id=normalized_position_id,
            observation=observation,
        )

    @staticmethod
    def _serialize(
        *,
        account_id: str,
        position_id: str,
        observation: PaperMarketResultObservation,
    ) -> dict[str, object]:
        return {
            "schema_version": "kalshi-paper-final-result/v1",
            "account_id": account_id,
            "position_id": position_id,
            "ticker": observation.ticker,
            "event_ticker": observation.event_ticker,
            "market_type": "binary",
            "status": observation.status,
            "result": observation.result,
            "state": observation.state,
            "final": observation.final,
            "notional_value_dollars": decimal_string(observation.notional_value, scale=6),
            "expiration_value": observation.expiration_value,
            "settlement_ts_present": observation.settlement_ts_present,
            "settlement_ts": (
                observation.settlement_ts.isoformat()
                if observation.settlement_ts is not None
                else None
            ),
            "settlement_value_dollars_present": observation.settlement_value_present,
            "settlement_value_dollars": (
                decimal_string(observation.settlement_value, scale=6)
                if observation.settlement_value is not None
                else None
            ),
            "source_origin": observation.source_origin,
            "source_path": observation.source_path,
            "observed_at": observation.observed_at.isoformat(),
            "fetched_at": observation.fetched_at.isoformat(),
            "resolution_openapi_version": observation.resolution_openapi_version,
            "resolution_openapi_sha256": observation.resolution_openapi_sha256,
            "evidence_json": observation.evidence_json,
            "evidence_hash": observation.evidence_hash,
        }
