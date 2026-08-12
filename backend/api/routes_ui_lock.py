from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from config import settings
from services.ui_lock import UILockRateLimited, UI_LOCK_SESSION_COOKIE, ui_lock_service

router = APIRouter(tags=["UI Lock"])

_PUBLIC_UI_LOCK_PATHS = {"/api/ui-lock/status", "/api/ui-lock/unlock"}


def requires_ui_auth(path: str) -> bool:
    normalized = str(path or "")
    if normalized in _PUBLIC_UI_LOCK_PATHS:
        return False
    return normalized.startswith(("/api", "/mcp", "/ws"))


def _client_key(request: Request) -> str:
    cloudflare_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cloudflare_ip:
        return cloudflare_ip
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        last_hop = forwarded.split(",")[-1].strip()
        if last_hop:
            return last_hop
    return request.client.host if request.client else "unknown"


class UILockStatusResponse(BaseModel):
    enabled: bool
    locked: bool
    idle_timeout_minutes: int
    has_password: bool


class UnlockRequest(BaseModel):
    password: str = Field(..., min_length=1, max_length=256)


@router.get("/ui-lock/status", response_model=UILockStatusResponse)
async def get_ui_lock_status(request: Request):
    token = request.cookies.get(UI_LOCK_SESSION_COOKIE)
    status = await ui_lock_service.status(token)
    return UILockStatusResponse(**status)


@router.post("/ui-lock/unlock")
async def unlock_ui(request: Request, payload: UnlockRequest):
    password = str(payload.password or "")
    try:
        success, token, message = await ui_lock_service.unlock(
            password=password,
            client_key=_client_key(request),
        )
    except UILockRateLimited as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "900"},
        ) from exc
    if not success:
        raise HTTPException(status_code=401, detail=message)

    response = JSONResponse(
        {
            "status": "success",
            "message": message,
            "locked": False,
        }
    )
    if token:
        response.set_cookie(
            key=UI_LOCK_SESSION_COOKIE,
            value=token,
            httponly=True,
            secure=True if settings.PUBLIC_AUTH_ENABLED else request.url.scheme == "https",
            samesite="strict" if settings.PUBLIC_AUTH_ENABLED else "lax",
            max_age=30 * 24 * 60 * 60,
            path="/",
        )
    return response


@router.post("/ui-lock/lock")
async def lock_ui(request: Request):
    token = request.cookies.get(UI_LOCK_SESSION_COOKIE)
    if token:
        await ui_lock_service.invalidate_session(token)
    else:
        await ui_lock_service.invalidate_all_sessions()

    response = JSONResponse({"status": "success", "message": "Locked"})
    response.delete_cookie(key=UI_LOCK_SESSION_COOKIE, path="/")
    return response


@router.post("/ui-lock/activity")
async def ui_lock_activity(request: Request):
    token = request.cookies.get(UI_LOCK_SESSION_COOKIE)
    accepted = await ui_lock_service.record_activity(token)
    if not accepted:
        raise HTTPException(status_code=423, detail="UI lock is active.")
    return {"status": "ok"}
