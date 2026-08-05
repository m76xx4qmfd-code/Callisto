from unittest.mock import MagicMock

import pytest

from services.live_execution_service import LiveExecutionService, OrderSide, OrderStatus, OrderType


def _disabled_service() -> tuple[LiveExecutionService, MagicMock]:
    service = LiveExecutionService()
    client = MagicMock()
    service._initialized = True
    service._client = client
    return service, client


def test_legacy_polymarket_executor_is_never_ready_even_with_seeded_client() -> None:
    service, client = _disabled_service()

    assert service.is_ready() is False
    assert service._client is client


@pytest.mark.asyncio
async def test_legacy_polymarket_initialization_is_hard_disabled_before_client_access() -> None:
    service, client = _disabled_service()

    assert await service.initialize(force=True) is False
    assert service.get_last_init_error() == "legacy_polymarket_execution_disabled_in_callisto"
    client.assert_not_called()
    assert client.mock_calls == []


@pytest.mark.asyncio
async def test_legacy_polymarket_balance_read_returns_disabled_shape_without_client_access() -> None:
    service, client = _disabled_service()

    assert await service.get_balance(force_probe_all=True) == {"error": "Polymarket credentials not configured"}
    assert client.mock_calls == []


@pytest.mark.asyncio
async def test_legacy_polymarket_order_submission_fails_without_provider_access() -> None:
    service, client = _disabled_service()

    order = await service.place_order(
        token_id="legacy-token",
        side=OrderSide.BUY,
        price=0.5,
        size=2.0,
        order_type=OrderType.GTC,
    )

    assert order.status is OrderStatus.FAILED
    assert order.error_message == "Trading service not initialized"
    assert order.clob_order_id is None
    assert client.mock_calls == []


@pytest.mark.asyncio
async def test_legacy_polymarket_cancel_fails_without_provider_access() -> None:
    service, client = _disabled_service()

    assert await service.cancel_order("legacy-order") is False
    assert client.mock_calls == []


@pytest.mark.asyncio
async def test_legacy_polymarket_position_sync_returns_only_local_state_without_provider_access() -> None:
    service, client = _disabled_service()

    assert await service.sync_positions() == []
    assert client.mock_calls == []
