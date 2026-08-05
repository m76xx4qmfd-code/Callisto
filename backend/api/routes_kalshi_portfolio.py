"""DB-only authoritative Kalshi portfolio snapshot route."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from models.database import AsyncSessionLocal
from services.kalshi_portfolio_projection import (
    KalshiPortfolioPrincipalAmbiguityError,
    KalshiPortfolioPrincipalNotFoundError,
    KalshiPortfolioProjectionReader,
)

router = APIRouter()


def get_projection_reader() -> KalshiPortfolioProjectionReader:
    return KalshiPortfolioProjectionReader(AsyncSessionLocal, stale_after=timedelta(seconds=30))


@router.get("/kalshi/portfolio/snapshot")
async def get_kalshi_portfolio_snapshot(
    reader: Annotated[KalshiPortfolioProjectionReader, Depends(get_projection_reader)],
    principal_fingerprint: Annotated[str | None, Query(pattern=r"^[0-9a-f]{64}$")] = None,
) -> dict[str, object]:
    """Read one principal projection exclusively from durable database evidence."""
    try:
        return await reader.read(principal_fingerprint)
    except KalshiPortfolioPrincipalAmbiguityError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "principal_ambiguous",
                "message": "Multiple Kalshi principals exist; principal_fingerprint is required",
                "principal_fingerprints": list(exc.principal_fingerprints),
            },
        ) from exc
    except KalshiPortfolioPrincipalNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Kalshi principal has no durable projection history") from exc


__all__ = ["get_projection_reader", "router"]
