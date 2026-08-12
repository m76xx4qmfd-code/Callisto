import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Import the focused route module without executing api/__init__.py, whose
# aggregate router eagerly imports optional ML dependencies unrelated to auth.
import types

if "api" not in sys.modules:
    api_package = types.ModuleType("api")
    api_package.__path__ = [str(BACKEND_ROOT / "api")]
    sys.modules["api"] = api_package

from api.routes_ui_lock import requires_ui_auth, router as ui_lock_router
from config import settings
from services.ui_lock import (
    FAILED_UNLOCK_LIMIT,
    UILockRateLimited,
    UILockService,
    UI_LOCK_SESSION_COOKIE,
)
from utils.passwords import hash_password, is_supported_password_hash

ORIGIN = "https://callistoterminal.ai"
PASSWORD = "test-only-password"


@pytest.fixture
def public_auth(monkeypatch):
    encoded = hash_password(PASSWORD)
    monkeypatch.setattr(settings, "PUBLIC_AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "PUBLIC_AUTH_PASSWORD_HASH", encoded)
    monkeypatch.setattr(settings, "PUBLIC_AUTH_IDLE_TIMEOUT_MINUTES", 60)
    monkeypatch.setattr(settings, "PUBLIC_APP_ORIGIN", ORIGIN)
    return encoded


@pytest.mark.asyncio
async def test_forced_public_auth_overrides_database_settings(public_auth, monkeypatch):
    service = UILockService()

    async def fail_if_called():
        raise AssertionError("forced auth must not read DB UI-lock settings")

    monkeypatch.setattr(service, "_load_settings_from_db", fail_if_called)
    snapshot = await service.get_settings()
    assert snapshot.enabled is True
    assert snapshot.password_hash == public_auth
    assert snapshot.idle_timeout_minutes == 60


@pytest.mark.asyncio
async def test_forced_public_auth_rejects_missing_hash(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "PUBLIC_AUTH_PASSWORD_HASH", None)
    with pytest.raises(RuntimeError, match="required"):
        await UILockService().get_settings()


@pytest.mark.asyncio
@pytest.mark.parametrize("encoded", ["not-a-hash", "pbkdf2_sha256$1$bad$bad"])
async def test_forced_public_auth_rejects_malformed_or_weakened_hash(monkeypatch, encoded):
    monkeypatch.setattr(settings, "PUBLIC_AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "PUBLIC_AUTH_PASSWORD_HASH", encoded)
    assert is_supported_password_hash(encoded) is False
    with pytest.raises(RuntimeError, match="malformed|unsupported"):
        await UILockService().get_settings()


def test_requires_ui_auth_covers_api_mcp_and_websocket_only_with_two_exceptions():
    assert requires_ui_auth("/api/opportunities") is True
    assert requires_ui_auth("/api/ui-lock/activity") is True
    assert requires_ui_auth("/api/ui-lock/lock") is True
    assert requires_ui_auth("/mcp") is True
    assert requires_ui_auth("/mcp/messages") is True
    assert requires_ui_auth("/ws") is True
    assert requires_ui_auth("/api/ui-lock/status") is False
    assert requires_ui_auth("/api/ui-lock/unlock") is False
    assert requires_ui_auth("/health") is False
    assert requires_ui_auth("/") is False


def _make_test_app() -> FastAPI:
    app = FastAPI()
    service = UILockService()

    # Patch the module singleton used by route handlers for complete isolation.
    import api.routes_ui_lock as routes_ui_lock

    routes_ui_lock.ui_lock_service = service

    @app.middleware("http")
    async def public_guard(request: Request, call_next):
        if settings.PUBLIC_AUTH_ENABLED and request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") != settings.PUBLIC_APP_ORIGIN:
                return JSONResponse(status_code=403, content={"detail": "Cross-origin request denied."})
        if requires_ui_auth(request.url.path):
            token = request.cookies.get(UI_LOCK_SESSION_COOKIE)
            if not await service.is_token_unlocked(token):
                return JSONResponse(status_code=423, content={"code": "auth-required"})
        return await call_next(request)

    app.include_router(ui_lock_router, prefix="/api")

    @app.get("/api/protected")
    async def protected():
        return {"status": "ok"}

    @app.api_route("/mcp", methods=["GET", "POST"])
    async def mcp():
        return {"status": "ok"}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


def test_forced_cookie_is_secure_http_only_strict_behind_http_proxy(public_auth):
    with TestClient(_make_test_app(), base_url="http://backend.internal") as client:
        response = client.post(
            "/api/ui-lock/unlock",
            headers={"origin": ORIGIN, "cf-connecting-ip": "203.0.113.10"},
            json={"password": PASSWORD},
        )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie


@pytest.mark.asyncio
async def test_five_failed_attempts_throttle_one_ip_but_not_an_independent_ip(public_auth):
    service = UILockService()
    for _ in range(FAILED_UNLOCK_LIMIT):
        success, _, _ = await service.unlock("wrong", client_key="198.51.100.1")
        assert success is False
    with pytest.raises(UILockRateLimited):
        await service.unlock(PASSWORD, client_key="198.51.100.1")

    success, token, _ = await service.unlock(PASSWORD, client_key="198.51.100.2")
    assert success is True
    assert token


def test_assembled_public_app_health_auth_origin_and_activity(public_auth):
    with TestClient(_make_test_app(), base_url="http://backend.internal") as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/protected").status_code == 423
        assert client.get("/mcp").status_code == 423
        assert client.post("/api/ui-lock/unlock", json={"password": PASSWORD}).status_code == 403

        login = client.post(
            "/api/ui-lock/unlock",
            headers={"origin": ORIGIN, "x-forwarded-for": "10.0.0.1, 203.0.113.20"},
            json={"password": PASSWORD},
        )
        assert login.status_code == 200
        # TestClient respects Secure cookies only over HTTPS; forward it explicitly
        # to emulate the TLS-terminating frontend proxy on this HTTP test transport.
        token = login.cookies.get(UI_LOCK_SESSION_COOKIE)
        assert token
        activity = client.post(
            "/api/ui-lock/activity",
            headers={"origin": ORIGIN},
            cookies={UI_LOCK_SESSION_COOKIE: token},
        )
        assert activity.status_code == 200


@pytest.mark.asyncio
async def test_real_assembled_application_middleware_guards_api_mcp_and_origin(public_auth):
    import importlib

    main_module = importlib.import_module("main")
    service = UILockService()
    monkeypatch_target = main_module.ui_lock_service
    original_routes_service = sys.modules["api.routes_ui_lock"].ui_lock_service
    main_module.ui_lock_service = service
    sys.modules["api.routes_ui_lock"].ui_lock_service = service
    try:
        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://backend.internal") as client:
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/api/settings")).status_code == 423
            assert (await client.get("/mcp")).status_code == 423
            assert (
                await client.post("/api/ui-lock/unlock", json={"password": PASSWORD})
            ).status_code == 403

            login = await client.post(
                "/api/ui-lock/unlock",
                headers={"origin": ORIGIN, "cf-connecting-ip": "203.0.113.30"},
                json={"password": PASSWORD},
            )
            assert login.status_code == 200
            token = login.cookies.get(UI_LOCK_SESSION_COOKIE)
            assert token
            activity = await client.post(
                "/api/ui-lock/activity",
                headers={"origin": ORIGIN},
                cookies={UI_LOCK_SESSION_COOKIE: token},
            )
            assert activity.status_code == 200
    finally:
        main_module.ui_lock_service = monkeypatch_target
        sys.modules["api.routes_ui_lock"].ui_lock_service = original_routes_service
