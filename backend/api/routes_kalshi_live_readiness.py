"""Read-only Kalshi live-readiness route."""

from __future__ import annotations

from fastapi import APIRouter

from services.kalshi_live_readiness import build_live_readiness


router = APIRouter()


@router.get("/kalshi/live-readiness")
async def get_kalshi_live_readiness() -> dict[str, object]:
    return build_live_readiness()


__all__ = ["get_kalshi_live_readiness", "router"]
