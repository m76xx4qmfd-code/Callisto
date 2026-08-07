#!/usr/bin/env python3
"""Validate one Kalshi public market-result response without credentials."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import httpx

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.kalshi_paper_execution import (
    KALSHI_API_PREFIX,
    KalshiPaperProtocolError,
    parse_market_result_response,
)

OriginName = Literal["production", "demo"]
ORIGINS: dict[OriginName, str] = {
    "production": "https://external-api.kalshi.com",
    "demo": "https://demo-api.kalshi.co",
}


async def validate_public_result(
    *,
    origin_name: OriginName,
    ticker: str,
    transport: httpx.AsyncBaseTransport | None = None,
    now: Callable[[], datetime] | None = None,
) -> dict[str, str]:
    """Perform one unauthenticated GET and return nonsecret protocol state."""
    if origin_name not in ORIGINS:
        raise KalshiPaperProtocolError("origin must be production or demo")
    normalized_ticker = str(ticker or "").strip().upper()
    if not normalized_ticker:
        raise KalshiPaperProtocolError("ticker is required")
    source_path = f"{KALSHI_API_PREFIX}/markets/{quote(normalized_ticker, safe='')}"
    origin = ORIGINS[origin_name]
    async with httpx.AsyncClient(
        transport=transport,
        headers={"Accept": "application/json"},
        timeout=5.0,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        try:
            response = await client.get(f"{origin}{source_path}")
        except httpx.HTTPError as exc:
            raise KalshiPaperProtocolError("Kalshi public market result request failed") from exc
    fetched_at = (now or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    observation = parse_market_result_response(
        response,
        requested_ticker=normalized_ticker,
        source_origin=origin,
        source_path=source_path,
        fetched_at=fetched_at,
    )
    return {
        "schema_version": "kalshi-public-result-validation/v1",
        "origin": origin_name,
        "ticker": observation.ticker,
        "status": observation.status,
        "state": observation.state,
        "result": observation.result,
        "evidence_hash": observation.evidence_hash,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate one unauthenticated Kalshi public market result.")
    parser.add_argument("--origin", required=True, choices=tuple(ORIGINS))
    parser.add_argument("--ticker", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        result = asyncio.run(validate_public_result(origin_name=args.origin, ticker=args.ticker))
    except KalshiPaperProtocolError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
