from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from workers import kalshi_paper_test_trade_worker as worker

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent


@pytest.mark.asyncio
async def test_worker_recovers_only_explicit_starting_and_ticks_monitoring_runs() -> None:
    service = AsyncMock()
    starting = {"run": {
        "status": "starting", "run_id": "start-1", "account_id": "account",
        "opportunity_id": "opportunity", "opportunity_revision": "a" * 64,
        "quantity": "2.00", "entry_limit_price": "0.600000",
        "take_profit_price": "0.700000", "stop_loss_price": "0.400000",
        "stop_loss_minimum_price": "0.300000",
    }, "events": []}
    await worker.process_run(service, starting)
    service.start_run.assert_awaited_once_with(
        run_id="start-1", account_id="account", opportunity_id="opportunity",
        opportunity_revision="a" * 64, quantity="2.00", entry_limit_price="0.600000",
        take_profit_price="0.700000", stop_loss_price="0.400000",
        stop_loss_minimum_price="0.300000",
    )
    service.tick_run.assert_not_awaited()

    await worker.process_run(service, {"run": {"status": "monitoring", "run_id": "monitor-1"}, "events": []})
    service.tick_run.assert_awaited_once_with("monitor-1")
    await worker.process_run(service, {"run": {"status": "paused", "run_id": "paused-1"}, "events": []})
    assert service.start_run.await_count == 1
    assert service.tick_run.await_count == 1


def test_paper_only_host_has_minimal_transitive_imports_and_no_mutation_surface() -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(BACKEND_ROOT)
    probe = """import sys
import workers.kalshi_paper_test_trade_host
forbidden = ('services.venues.kalshi_v2', 'services.live_execution_service',
             'services.simulation', 'services.shadow_execution',
             'services.trader_orchestrator', 'workers.host')
found = [name for name in forbidden if name in sys.modules]
raise SystemExit(','.join(found) if found else 0)
"""
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=BACKEND_ROOT, env=environment,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert not any(
        hasattr(worker.KalshiPaperMarketDataClient, name)
        for name in ("create_order", "submit_order", "cancel_order", "amend_order")
    )


def test_paper_only_worker_is_reachable_with_credential_free_topology_parity() -> None:
    gui_text = (PROJECT_ROOT / "gui.py").read_text(encoding="utf-8")
    compose_text = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    host_text = (BACKEND_ROOT / "workers" / "host.py").read_text(encoding="utf-8")
    assert '("KALSHI PAPER TEST", "kalshi_paper_test")' in gui_text
    assert '"kalshi_paper_test": "workers.kalshi_paper_test_trade_host"' in gui_text
    assert '"kalshi_paper_test_trades": "kalshi_paper_test"' in gui_text
    assert "_strip_paper_worker_credentials" in gui_text
    assert "worker-kalshi-paper-test:" in compose_text
    paper_service = compose_text.split("worker-kalshi-paper-test:", 1)[1].split("\n  frontend:", 1)[0]
    assert "workers.kalshi_paper_test_trade_host" in paper_service
    assert "*backend-env" not in paper_service
    assert "POLYMARKET_" not in paper_service
    assert "KALSHI_PORTFOLIO_CREDENTIAL_MANIFEST" not in paper_service
    assert "kalshi_paper_test_trade_worker" not in host_text

    tree = ast.parse(gui_text)
    selected = []
    names = {"_PAPER_WORKER_CREDENTIAL_PREFIXES", "_PAPER_WORKER_CREDENTIAL_NAMES"}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in names for target in node.targets
        ):
            selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "_strip_paper_worker_credentials":
            selected.append(node)
    namespace: dict[str, object] = {}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "gui.py", "exec"), namespace)
    strip_credentials = namespace["_strip_paper_worker_credentials"]
    clean = strip_credentials({
        "DATABASE_URL": "postgresql://local", "LOG_LEVEL": "INFO",
        "KALSHI_API_KEY": "secret", "KALSHI_PORTFOLIO_CREDENTIAL_MANIFEST": "/secret",
        "POLYMARKET_PRIVATE_KEY": "secret", "APP_SECRETS_KEY": "secret",
    })
    assert clean == {"DATABASE_URL": "postgresql://local", "LOG_LEVEL": "INFO"}