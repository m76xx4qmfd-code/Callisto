from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_macos_and_windows_default_to_numpy_similarity_without_faiss() -> None:
    from services.news.semantic_matcher import _ENABLE_FAISS as semantic_faiss_enabled
    from services.news.semantic_matcher import _faiss_enabled_by_default
    from services.news.market_watcher_index import _ENABLE_FAISS as watcher_faiss_enabled
    from services.news.market_watcher_index import (
        _faiss_enabled_by_default as watcher_faiss_enabled_by_default,
    )

    assert _faiss_enabled_by_default("darwin") is False
    assert _faiss_enabled_by_default("win32") is False
    assert _faiss_enabled_by_default("linux") is True
    assert watcher_faiss_enabled_by_default("darwin") is False
    assert watcher_faiss_enabled_by_default("win32") is False
    assert watcher_faiss_enabled_by_default("linux") is True
    if sys.platform in {"darwin", "win32"}:
        assert semantic_faiss_enabled is False
        assert watcher_faiss_enabled is False


def test_macos_setup_excludes_and_removes_stale_faiss() -> None:
    requirements = (BACKEND_ROOT / "requirements.txt").read_text(encoding="utf-8")
    setup = (BACKEND_ROOT.parent / "scripts" / "infra" / "setup.sh").read_text(
        encoding="utf-8"
    )

    assert 'faiss-cpu>=1.7.0; sys_platform != "darwin"' in requirements
    assert "pip uninstall -q -y faiss-cpu" in setup


def test_api_lifespan_never_initializes_legacy_live_execution() -> None:
    source = (BACKEND_ROOT / "main.py").read_text(encoding="utf-8")
    lifespan = source[source.index("async def lifespan"):source.index("app = FastAPI")]

    assert "live_execution_service.initialize" not in lifespan
    assert "semantic_matcher.initialize" not in lifespan


def test_api_loader_warmup_uses_loader_owned_sessions() -> None:
    source = (BACKEND_ROOT / "main.py").read_text(encoding="utf-8")
    lifespan = source[source.index("async def lifespan"):source.index("app = FastAPI")]

    assert "strategy_loader.refresh_all_from_db(session=" not in lifespan
    assert "data_source_loader.refresh_all_from_db(session=" not in lifespan
    strategy_loader_source = (
        BACKEND_ROOT / "services" / "strategy_loader.py"
    ).read_text(encoding="utf-8")
    assert "async def refresh_from_db(" not in strategy_loader_source


def test_api_readiness_does_not_require_external_worker_planes() -> None:
    source = (BACKEND_ROOT / "main.py").read_text(encoding="utf-8")
    readiness = source[source.index("async def readiness_check"):source.index("_gui_health_cache")]

    assert 'checks[k] for k in ("database",)' in readiness
    assert '"execution": "disabled"' in readiness


def test_local_frontend_default_avoids_port_3000_collision() -> None:
    vite = (BACKEND_ROOT.parent / "frontend" / "vite.config.ts").read_text(encoding="utf-8")

    assert "process.env.VITE_PORT || 5173" in vite


def test_desktop_launcher_requires_current_session_worker_selection() -> None:
    launcher = (BACKEND_ROOT.parent / "gui.py").read_text(encoding="utf-8")

    assert 'self._worker_supervision_enabled = "--workers" in sys.argv[1:]' in launcher
    assert "if not self._worker_supervision_enabled:" in launcher


def test_legacy_polymarket_service_is_hard_disabled_before_credentials() -> None:
    from services.live_execution_service import LiveExecutionService

    service = LiveExecutionService()
    credential_lookup = AsyncMock(side_effect=AssertionError("credentials must not be read"))
    service._resolve_polymarket_credentials = credential_lookup

    assert asyncio.run(service.initialize()) is False
    assert service.get_last_init_error() == "legacy_polymarket_execution_disabled_in_callisto"
    credential_lookup.assert_not_awaited()
