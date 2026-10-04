"""Tests for risk management and circuit breaker."""

from __future__ import annotations

import pytest

from kalshi_bot.config import RiskConfig
from kalshi_bot.models import OrderAction, Position, Side, TradeDecision
from kalshi_bot.risk.circuit_breaker import CircuitBreaker
from kalshi_bot.risk.manager import RiskManager


def _make_decision(**overrides) -> TradeDecision:
    defaults = dict(
        ticker="TEST-MKT",
        side=Side.YES,
        action=OrderAction.BUY,
        quantity=10,
        limit_price_cents=50,
        edge=0.10,
        predicted_probability=0.60,
        market_price_cents=50,
        confidence=0.8,
        reasoning="Test",
    )
    defaults.update(overrides)
    return TradeDecision(**defaults)


class TestRiskManager:
    def test_approve_normal_trade(self, risk_config):
        rm = RiskManager(risk_config)
        decision = _make_decision(quantity=25, limit_price_cents=50)
        result = rm.check_trade(decision)
        assert result.approved
        assert result.approved_quantity == 25

    def test_reject_exceeds_single_order(self, risk_config):
        rm = RiskManager(risk_config)
        # 100 contracts * 50c = 5000c > max_single_order_cents (2000c)
        decision = _make_decision(quantity=100, limit_price_cents=50)
        result = rm.check_trade(decision)
        # Should reduce quantity, not reject entirely
        assert result.approved
        assert result.approved_quantity < 100
        assert result.approved_quantity * 50 <= risk_config.max_single_order_cents

    def test_reject_exceeds_position_size(self, risk_config):
        rm = RiskManager(risk_config)
        rm.update_positions([
            Position(
                ticker="TEST-MKT",
                side=Side.YES,
                quantity=50,
                average_price_cents=50,
            )
        ])
        # Existing position is 50*50=2500c. Max position is 5000c.
        # Requesting 80*50=4000c would exceed. Should reduce to fit.
        decision = _make_decision(quantity=80, limit_price_cents=50)
        result = rm.check_trade(decision)
        assert result.approved
        assert result.approved_quantity * 50 + 2500 <= risk_config.max_position_size_cents
        assert result.approved_quantity * 50 >= risk_config.min_order_cents

    def test_reject_max_positions(self, risk_config):
        rm = RiskManager(RiskConfig(max_positions=2))
        rm.update_positions([
            Position(ticker="A", side=Side.YES, quantity=1, average_price_cents=50),
            Position(ticker="B", side=Side.YES, quantity=1, average_price_cents=50),
        ])
        decision = _make_decision(ticker="C", quantity=1, limit_price_cents=50)
        result = rm.check_trade(decision)
        assert not result.approved
        assert "max positions" in result.reason.lower()

    def test_reject_daily_loss_limit(self, risk_config):
        rm = RiskManager(risk_config)
        rm.daily_loss_cents = risk_config.max_daily_loss_cents  # Already at limit
        decision = _make_decision(quantity=1, limit_price_cents=50)
        result = rm.check_trade(decision)
        assert not result.approved
        assert "daily loss" in result.reason.lower()

    def test_reset_daily(self, risk_config):
        rm = RiskManager(risk_config)
        rm.daily_loss_cents = 5000
        rm.reset_daily()
        assert rm.daily_loss_cents == 0


class TestCircuitBreaker:
    def test_not_tripped_by_default(self, risk_config):
        cb = CircuitBreaker(risk_config)
        assert not cb.is_tripped

    def test_trips_on_consecutive_losses(self, risk_config):
        cb = CircuitBreaker(risk_config)
        for _ in range(risk_config.consecutive_loss_limit):
            cb.record_trade_result(-100)
        assert cb.is_tripped
        assert "consecutive" in cb.trip_reason.lower()

    def test_resets_on_win(self, risk_config):
        cb = CircuitBreaker(risk_config)
        cb.record_trade_result(-100)
        cb.record_trade_result(-100)
        cb.record_trade_result(50)  # Win resets counter
        cb.record_trade_result(-100)
        cb.record_trade_result(-100)
        assert not cb.is_tripped

    def test_manual_trip_and_reset(self, risk_config):
        cb = CircuitBreaker(risk_config)
        cb.trip("Test emergency")
        assert cb.is_tripped
        assert cb.trip_reason == "Test emergency"
        cb.reset()
        assert not cb.is_tripped

    def test_api_error_rate(self, risk_config):
        cb = CircuitBreaker(RiskConfig(api_error_rate_limit=0.5))
        # Record 10 errors out of 10 calls (100% error rate)
        for _ in range(10):
            cb.record_api_call(was_error=True)
        assert cb.is_tripped
        assert "api error rate" in cb.trip_reason.lower()
