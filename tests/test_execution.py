"""Tests for order execution in dry-run mode."""

from __future__ import annotations

import pytest

from kalshi_bot.config import ExecutionConfig, RiskConfig
from kalshi_bot.models import OrderAction, OrderStatus, Side, TradeDecision
from kalshi_bot.risk.circuit_breaker import CircuitBreaker
from kalshi_bot.risk.manager import RiskManager
from kalshi_bot.execution.order_manager import OrderManager


class FakeClient:
    """Minimal fake Kalshi client for testing."""

    async def create_order(self, **kwargs):
        pass

    async def get_orders(self, **kwargs):
        return []

    async def cancel_order(self, order_id):
        pass


def _make_decision(**overrides) -> TradeDecision:
    defaults = dict(
        ticker="TEST-MKT",
        side=Side.YES,
        action=OrderAction.BUY,
        quantity=25,
        limit_price_cents=50,
        edge=0.10,
        predicted_probability=0.60,
        market_price_cents=50,
        confidence=0.8,
        reasoning="Test trade",
    )
    defaults.update(overrides)
    return TradeDecision(**defaults)


class TestOrderManagerDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_creates_order_without_api(self):
        risk_config = RiskConfig()
        rm = RiskManager(risk_config)
        cb = CircuitBreaker(risk_config)
        config = ExecutionConfig(dry_run=True)

        om = OrderManager(
            client=FakeClient(),  # type: ignore[arg-type]
            config=config,
            risk_manager=rm,
            circuit_breaker=cb,
        )

        decisions = [_make_decision()]
        orders = await om.execute_decisions(decisions)
        assert len(orders) == 1
        assert orders[0].order_id.startswith("dry-")
        assert orders[0].ticker == "TEST-MKT"

    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks_orders(self):
        risk_config = RiskConfig()
        rm = RiskManager(risk_config)
        cb = CircuitBreaker(risk_config)
        cb.trip("Test trip")

        config = ExecutionConfig(dry_run=True)
        om = OrderManager(
            client=FakeClient(),  # type: ignore[arg-type]
            config=config,
            risk_manager=rm,
            circuit_breaker=cb,
        )

        decisions = [_make_decision()]
        orders = await om.execute_decisions(decisions)
        assert len(orders) == 0

    @pytest.mark.asyncio
    async def test_risk_rejection_blocks_order(self):
        risk_config = RiskConfig(max_positions=0)
        rm = RiskManager(risk_config)
        cb = CircuitBreaker(risk_config)

        config = ExecutionConfig(dry_run=True)
        om = OrderManager(
            client=FakeClient(),  # type: ignore[arg-type]
            config=config,
            risk_manager=rm,
            circuit_breaker=cb,
        )

        decisions = [_make_decision()]
        orders = await om.execute_decisions(decisions)
        assert len(orders) == 0
