"""Shared test fixtures for the kalshi_bot test suite."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from kalshi_bot.config import (
    AppConfig,
    ClaudeConfig,
    ExecutionConfig,
    KalshiConfig,
    RiskConfig,
    ScannerConfig,
    StrategyConfig,
)
from kalshi_bot.models import Event, Market, Side, OrderAction


@pytest.fixture
def risk_config() -> RiskConfig:
    return RiskConfig(
        max_position_size_cents=5000,
        max_total_exposure_cents=50000,
        max_daily_loss_cents=10000,
        max_positions=20,
        max_single_order_cents=2000,
        consecutive_loss_limit=5,
        hourly_loss_limit_cents=3000,
        api_error_rate_limit=0.5,
    )


@pytest.fixture
def strategy_config() -> StrategyConfig:
    return StrategyConfig(
        min_edge_threshold=0.05,
        kelly_fraction=0.25,
        min_confidence=0.5,
        take_profit_percent=30.0,
        stop_loss_percent=50.0,
        re_eval_exit_edge=-0.03,
    )


@pytest.fixture
def scanner_config() -> ScannerConfig:
    return ScannerConfig(
        min_volume=100,
        min_open_interest=50,
        max_spread_cents=15,
        min_hours_to_expiry=2,
        max_markets_per_cycle=50,
    )


@pytest.fixture
def sample_market() -> Market:
    return Market(
        ticker="TEST-MARKET-YES",
        event_ticker="TEST-EVENT",
        title="Will test thing happen?",
        status="open",
        yes_ask=55,
        yes_bid=50,
        no_ask=50,
        no_bid=45,
        last_price=52,
        volume=500,
        open_interest=200,
        close_time=datetime.now(timezone.utc) + timedelta(days=7),
        category="test",
    )


@pytest.fixture
def sample_event(sample_market: Market) -> Event:
    return Event(
        event_ticker="TEST-EVENT",
        title="Test Event",
        subtitle="A test event for unit tests",
        category="test",
        markets=[sample_market],
    )
