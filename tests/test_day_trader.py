"""Tests for day trader mode — config overrides, filters, time-based exits, prompt, and feasibility."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kalshi_bot.config import AppConfig, ClaudeConfig, ScannerConfig, StrategyConfig, RiskConfig
from kalshi_bot.models import Event, EventAnalysis, Market, MarketAnalysis, OrderAction, Position, Side
from kalshi_bot.scanner.filters import passes_max_time_to_expiry
from kalshi_bot.strategy.decision_engine import DecisionEngine
from kalshi_bot.analysis.engine import AnalysisEngine
from kalshi_bot.analysis.prompts import (
    DAY_TRADER_PROMPT_ADDENDUM,
    SYSTEM_PROMPT,
    build_feasibility_prompt,
)


# ── Config overrides ─────────────────────────────────────────────────────────


class TestDayTraderConfigOverrides:
    def test_day_trader_mode_applies_overrides(self):
        cfg = AppConfig(day_trader_mode=True)
        # Exit thresholds
        assert cfg.strategy.take_profit_percent == 4.0
        assert cfg.strategy.stop_loss_percent == 15.0
        assert cfg.strategy.re_eval_exit_edge == -0.01
        assert cfg.strategy.max_hold_minutes == 30
        # Speed
        assert cfg.scheduler.pipeline_interval_minutes == 5
        assert cfg.scheduler.portfolio_monitor_interval_minutes == 1
        assert cfg.claude.cache_ttl_minutes == 5
        assert cfg.claude.max_concurrent == 5
        assert cfg.execution.stale_order_hours == 1
        # Market selection
        assert cfg.scanner.max_spread_cents == 8
        assert cfg.scanner.min_hours_to_expiry == 0.5
        assert cfg.scanner.max_hours_to_expiry == 48
        assert cfg.scanner.min_volume == 500  # uses default (no override)
        assert cfg.scanner.min_open_interest == 200  # uses default (no override)
        assert cfg.scanner.max_markets_per_cycle == 100
        assert cfg.scanner.cooldown_hours == 1
        assert cfg.scanner.score_jitter_pct == 0.15
        # Sizing / filtering
        assert cfg.strategy.min_price_cents == 5
        assert cfg.strategy.min_edge_threshold == 0.02
        assert cfg.strategy.kelly_fraction == 0.5
        assert cfg.risk.min_order_cents == 500

    def test_day_trader_mode_off_keeps_defaults(self):
        cfg = AppConfig(day_trader_mode=False)
        assert cfg.strategy.take_profit_percent == 30.0
        assert cfg.strategy.stop_loss_percent == 50.0
        assert cfg.strategy.max_hold_minutes == 0
        assert cfg.scheduler.pipeline_interval_minutes == 30
        assert cfg.scanner.max_hours_to_expiry == 0

    def test_day_trader_mode_respects_explicit_config(self):
        cfg = AppConfig(
            day_trader_mode=True,
            strategy={"kelly_fraction": 0.1},
        )
        # Explicit value preserved
        assert cfg.strategy.kelly_fraction == 0.1
        # Other defaults still overridden
        assert cfg.strategy.take_profit_percent == 4.0
        assert cfg.strategy.stop_loss_percent == 15.0


# ── Max-hours-to-expiry filter ───────────────────────────────────────────────


def _make_market(**overrides) -> Market:
    defaults = {
        "ticker": "TST",
        "event_ticker": "EVT",
        "title": "Test",
        "status": "open",
        "yes_ask": 60,
        "yes_bid": 55,
        "volume": 500,
        "open_interest": 200,
        "close_time": datetime.now(timezone.utc) + timedelta(days=7),
    }
    defaults.update(overrides)
    return Market(**defaults)


class TestMaxHoursToExpiryFilter:
    def test_passes_within_limit(self):
        cfg = ScannerConfig(max_hours_to_expiry=24)
        m = _make_market(close_time=datetime.now(timezone.utc) + timedelta(hours=12))
        assert passes_max_time_to_expiry(m, cfg)

    def test_fails_beyond_limit(self):
        cfg = ScannerConfig(max_hours_to_expiry=24)
        m = _make_market(close_time=datetime.now(timezone.utc) + timedelta(hours=48))
        assert not passes_max_time_to_expiry(m, cfg)

    def test_disabled_when_zero(self):
        cfg = ScannerConfig(max_hours_to_expiry=0)
        m = _make_market(close_time=datetime.now(timezone.utc) + timedelta(days=365))
        assert passes_max_time_to_expiry(m, cfg)

    def test_no_close_time_fails_when_enabled(self):
        cfg = ScannerConfig(max_hours_to_expiry=24)
        m = _make_market(close_time=None)
        assert not passes_max_time_to_expiry(m, cfg)


# ── Time-based exit decisions ────────────────────────────────────────────────


class TestTimeBasedExit:
    def _make_engine(self, max_hold_minutes: float = 30) -> DecisionEngine:
        return DecisionEngine(
            strategy_config=StrategyConfig(
                max_hold_minutes=max_hold_minutes,
                take_profit_percent=30.0,
                stop_loss_percent=50.0,
            ),
            risk_config=RiskConfig(),
            bankroll_cents=50000,
        )

    def test_triggers_after_max_hold(self):
        engine = self._make_engine(max_hold_minutes=30)
        positions = [
            Position(
                ticker="MKT-OLD",
                side=Side.YES,
                quantity=10,
                average_price_cents=50,
                market_price_cents=52,  # small gain, within TP/SL
                entry_time=datetime.now(timezone.utc) - timedelta(minutes=45),
            )
        ]
        markets = {
            "MKT-OLD": Market(
                ticker="MKT-OLD",
                event_ticker="EVT",
                title="Test",
                status="open",
                yes_ask=54,
                yes_bid=50,
                last_price=52,
            )
        }
        decisions = engine.generate_exit_decisions(positions, markets)
        assert len(decisions) == 1
        assert decisions[0].action == OrderAction.SELL
        assert "max_hold_time" in decisions[0].reasoning

    def test_no_exit_before_max_hold(self):
        engine = self._make_engine(max_hold_minutes=30)
        positions = [
            Position(
                ticker="MKT-NEW",
                side=Side.YES,
                quantity=10,
                average_price_cents=50,
                market_price_cents=52,
                entry_time=datetime.now(timezone.utc) - timedelta(minutes=10),
            )
        ]
        markets = {
            "MKT-NEW": Market(
                ticker="MKT-NEW",
                event_ticker="EVT",
                title="Test",
                status="open",
                yes_ask=54,
                yes_bid=50,
                last_price=52,
            )
        }
        decisions = engine.generate_exit_decisions(positions, markets)
        assert len(decisions) == 0

    def test_disabled_when_zero(self):
        engine = self._make_engine(max_hold_minutes=0)
        positions = [
            Position(
                ticker="MKT-LONG",
                side=Side.YES,
                quantity=10,
                average_price_cents=50,
                market_price_cents=52,
                entry_time=datetime.now(timezone.utc) - timedelta(hours=24),
            )
        ]
        markets = {
            "MKT-LONG": Market(
                ticker="MKT-LONG",
                event_ticker="EVT",
                title="Test",
                status="open",
                yes_ask=54,
                yes_bid=50,
                last_price=52,
            )
        }
        decisions = engine.generate_exit_decisions(positions, markets)
        assert len(decisions) == 0

    def test_no_exit_without_entry_time(self):
        engine = self._make_engine(max_hold_minutes=30)
        positions = [
            Position(
                ticker="MKT-NOTIME",
                side=Side.YES,
                quantity=10,
                average_price_cents=50,
                market_price_cents=52,
                entry_time=None,
            )
        ]
        markets = {
            "MKT-NOTIME": Market(
                ticker="MKT-NOTIME",
                event_ticker="EVT",
                title="Test",
                status="open",
                yes_ask=54,
                yes_bid=50,
                last_price=52,
            )
        }
        decisions = engine.generate_exit_decisions(positions, markets)
        assert len(decisions) == 0


# ── Claude prompt ────────────────────────────────────────────────────────────


class TestDayTraderPrompt:
    def test_addendum_appended_when_on(self):
        prompt = SYSTEM_PROMPT + DAY_TRADER_PROMPT_ADDENDUM
        assert "DAY TRADER MODE" in prompt
        assert "imminent catalysts" in prompt.lower()

    def test_base_prompt_clean_when_off(self):
        assert "DAY TRADER MODE" not in SYSTEM_PROMPT


# ── Category filtering config ──────────────────────────────────────────────


class TestDayTraderCategories:
    def test_day_trader_sets_categories(self):
        cfg = AppConfig(day_trader_mode=True)
        assert cfg.scanner.categories == [
            "Economics", "Financials", "Climate and Weather"
        ]

    def test_normal_mode_no_categories(self):
        cfg = AppConfig(day_trader_mode=False)
        assert cfg.scanner.categories == []

    def test_day_trader_respects_explicit_categories(self):
        cfg = AppConfig(
            day_trader_mode=True,
            scanner={"categories": ["Politics"]},
        )
        assert cfg.scanner.categories == ["Politics"]

    def test_custom_day_trader_categories(self):
        cfg = AppConfig(
            day_trader_mode=True,
            scanner={"day_trader_categories": ["Sports"]},
        )
        assert cfg.scanner.categories == ["Sports"]


# ── Feasibility prompt ─────────────────────────────────────────────────────


class TestFeasibilityPrompt:
    def test_feasibility_prompt_includes_hours(self):
        markets = [
            {"ticker": "MKT-A", "title": "Will X happen?"},
            {"ticker": "MKT-B", "title": "Will Y happen?"},
        ]
        hours = {"MKT-A": 6.0, "MKT-B": 18.5}
        prompt = build_feasibility_prompt("Test Event", "Context", markets, hours)
        assert "~6h" in prompt
        assert "~18h" in prompt

    def test_feasibility_prompt_includes_all_markets(self):
        markets = [
            {"ticker": "AAA", "title": "Market A"},
            {"ticker": "BBB", "title": "Market B"},
            {"ticker": "CCC", "title": "Market C"},
        ]
        prompt = build_feasibility_prompt("Evt", "", markets, {})
        assert "AAA" in prompt
        assert "BBB" in prompt
        assert "CCC" in prompt

    def test_feasibility_prompt_without_subtitle(self):
        markets = [{"ticker": "X", "title": "Test?"}]
        prompt = build_feasibility_prompt("Evt", "", markets, {})
        assert "CONTEXT:" not in prompt


# ── Two-pass integration (mock Claude) ─────────────────────────────────────


def _make_feasibility_response(assessments: list[dict]) -> MagicMock:
    """Build a mock Claude response for a feasibility tool call."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = "submit_feasibility"
    tool_block.input = {"assessments": assessments}

    response = MagicMock()
    response.content = [tool_block]
    return response


def _make_analysis_response(event_ticker: str, tickers: list[str]) -> MagicMock:
    """Build a mock Claude response for a full analysis tool call."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = "submit_analysis"
    tool_block.input = {
        "event_ticker": event_ticker,
        "event_summary": "Test analysis",
        "market_analyses": [
            {
                "ticker": t,
                "predicted_probability": 0.7,
                "confidence": 0.8,
                "reasoning": "Test reasoning",
            }
            for t in tickers
        ],
    }

    response = MagicMock()
    response.content = [tool_block]
    return response


class TestTwoPassAnalysis:
    @pytest.mark.asyncio
    async def test_two_pass_filters_infeasible_markets(self):
        config = ClaudeConfig(api_key="test-key", cache_ttl_minutes=5)
        engine = AnalysisEngine(config, day_trader_mode=True)

        # Create event with 3 markets
        now = datetime.now(timezone.utc)
        markets = [
            Market(
                ticker="MKT-FEASIBLE-1",
                event_ticker="EVT",
                title="Will GDP grow?",
                status="open",
                volume=500,
                open_interest=200,
                close_time=now + timedelta(hours=6),
            ),
            Market(
                ticker="MKT-FEASIBLE-2",
                event_ticker="EVT",
                title="Will unemployment drop?",
                status="open",
                volume=500,
                open_interest=200,
                close_time=now + timedelta(hours=6),
            ),
            Market(
                ticker="MKT-INFEASIBLE",
                event_ticker="EVT",
                title="Will Congress pass bill?",
                status="open",
                volume=500,
                open_interest=200,
                close_time=now + timedelta(hours=6),
            ),
        ]
        event = Event(
            event_ticker="EVT",
            title="Economic Indicators",
            category="Economics",
            markets=markets,
        )

        # Mock: feasibility says 2 feasible, 1 not
        feasibility_resp = _make_feasibility_response([
            {"ticker": "MKT-FEASIBLE-1", "feasible": True, "reason": "Data release scheduled"},
            {"ticker": "MKT-FEASIBLE-2", "feasible": True, "reason": "Report due today"},
            {"ticker": "MKT-INFEASIBLE", "feasible": False, "reason": "Congress not in session"},
        ])
        # Mock: full analysis for 2 feasible markets
        analysis_resp = _make_analysis_response("EVT", ["MKT-FEASIBLE-1", "MKT-FEASIBLE-2"])

        engine.client = AsyncMock()
        engine.client.messages.create = AsyncMock(side_effect=[feasibility_resp, analysis_resp])

        analyses = await engine.analyze_events_two_pass([event])

        assert len(analyses) == 1
        analysis = analyses[0]

        # Should have 3 market analyses total (2 real + 1 stub)
        assert len(analysis.market_analyses) == 3

        # The feasible markets should have real analysis
        feasible_results = {
            ma.ticker: ma for ma in analysis.market_analyses if ma.confidence > 0
        }
        assert "MKT-FEASIBLE-1" in feasible_results
        assert "MKT-FEASIBLE-2" in feasible_results
        assert feasible_results["MKT-FEASIBLE-1"].predicted_probability == 0.7

        # The infeasible market should have a confidence=0 stub
        infeasible = next(
            ma for ma in analysis.market_analyses if ma.ticker == "MKT-INFEASIBLE"
        )
        assert infeasible.confidence == 0.0
        assert "infeasible" in infeasible.reasoning.lower() or "not feasible" in infeasible.reasoning.lower()

    @pytest.mark.asyncio
    async def test_two_pass_all_infeasible_skips_full_analysis(self):
        config = ClaudeConfig(api_key="test-key", cache_ttl_minutes=5)
        engine = AnalysisEngine(config, day_trader_mode=True)

        now = datetime.now(timezone.utc)
        event = Event(
            event_ticker="EVT2",
            title="Congress Funding",
            category="Politics",
            markets=[
                Market(
                    ticker="DHS-FUND",
                    event_ticker="EVT2",
                    title="Will Congress fund DHS by tomorrow?",
                    status="open",
                    volume=500,
                    open_interest=200,
                    close_time=now + timedelta(hours=12),
                )
            ],
        )

        feasibility_resp = _make_feasibility_response([
            {"ticker": "DHS-FUND", "feasible": False, "reason": "Congress not in session on weekend"},
        ])

        engine.client = AsyncMock()
        engine.client.messages.create = AsyncMock(return_value=feasibility_resp)

        analyses = await engine.analyze_events_two_pass([event])

        assert len(analyses) == 1
        # Full analysis should NOT have been called (only 1 API call for feasibility)
        assert engine.client.messages.create.call_count == 1
        assert analyses[0].market_analyses[0].confidence == 0.0

    @pytest.mark.asyncio
    async def test_two_pass_uses_cache(self):
        config = ClaudeConfig(api_key="test-key", cache_ttl_minutes=5)
        engine = AnalysisEngine(config, day_trader_mode=True)

        cached_analysis = EventAnalysis(
            event_ticker="CACHED",
            event_summary="Already analyzed",
            market_analyses=[
                MarketAnalysis(
                    ticker="MKT-C",
                    predicted_probability=0.6,
                    confidence=0.9,
                    reasoning="Cached",
                )
            ],
        )
        engine.cache.set("CACHED", cached_analysis)

        event = Event(
            event_ticker="CACHED",
            title="Cached Event",
            markets=[
                Market(
                    ticker="MKT-C",
                    event_ticker="CACHED",
                    title="Test?",
                    status="open",
                )
            ],
        )

        engine.client = AsyncMock()
        analyses = await engine.analyze_events_two_pass([event])

        assert len(analyses) == 1
        assert analyses[0].event_summary == "Already analyzed"
        # No API calls should have been made
        engine.client.messages.create.assert_not_called()


# ── Infeasible position exits ──────────────────────────────────────────────


class TestInfeasibleExitDecisions:
    def _make_engine(self) -> DecisionEngine:
        return DecisionEngine(
            strategy_config=StrategyConfig(),
            risk_config=RiskConfig(),
            bankroll_cents=50000,
        )

    def test_generates_sell_for_infeasible_position(self):
        engine = self._make_engine()
        positions = [
            Position(
                ticker="DHS-FUND",
                side=Side.YES,
                quantity=10,
                average_price_cents=60,
                market_price_cents=55,
            )
        ]
        markets = {
            "DHS-FUND": Market(
                ticker="DHS-FUND",
                event_ticker="EVT-DHS",
                title="Will Congress fund DHS?",
                status="open",
                yes_ask=57,
                yes_bid=53,
                last_price=55,
            )
        }
        feasibility = {"DHS-FUND": False}

        decisions = engine.generate_infeasible_exit_decisions(positions, markets, feasibility)
        assert len(decisions) == 1
        assert decisions[0].action == OrderAction.SELL
        assert decisions[0].ticker == "DHS-FUND"
        assert "infeasible" in decisions[0].reasoning.lower()

    def test_no_exit_for_feasible_position(self):
        engine = self._make_engine()
        positions = [
            Position(
                ticker="GDP-Q1",
                side=Side.YES,
                quantity=10,
                average_price_cents=50,
                market_price_cents=55,
            )
        ]
        markets = {
            "GDP-Q1": Market(
                ticker="GDP-Q1",
                event_ticker="EVT-GDP",
                title="Will GDP grow?",
                status="open",
                yes_ask=57,
                yes_bid=53,
                last_price=55,
            )
        }
        feasibility = {"GDP-Q1": True}

        decisions = engine.generate_infeasible_exit_decisions(positions, markets, feasibility)
        assert len(decisions) == 0

    def test_unknown_ticker_treated_as_feasible(self):
        engine = self._make_engine()
        positions = [
            Position(
                ticker="UNKNOWN",
                side=Side.NO,
                quantity=5,
                average_price_cents=40,
                market_price_cents=42,
            )
        ]
        markets = {
            "UNKNOWN": Market(
                ticker="UNKNOWN",
                event_ticker="EVT-UNK",
                title="Unknown market",
                status="open",
                yes_ask=60,
                yes_bid=56,
                no_ask=44,
                no_bid=40,
                last_price=58,
            )
        }
        # Ticker not in feasibility dict at all
        feasibility: dict[str, bool] = {}

        decisions = engine.generate_infeasible_exit_decisions(positions, markets, feasibility)
        assert len(decisions) == 0

    def test_mixed_feasible_and_infeasible(self):
        engine = self._make_engine()
        positions = [
            Position(
                ticker="GOOD",
                side=Side.YES,
                quantity=10,
                average_price_cents=50,
                market_price_cents=55,
            ),
            Position(
                ticker="BAD",
                side=Side.YES,
                quantity=10,
                average_price_cents=60,
                market_price_cents=20,
            ),
        ]
        markets = {
            "GOOD": Market(
                ticker="GOOD", event_ticker="E1", title="Good", status="open",
                yes_ask=57, yes_bid=53, last_price=55,
            ),
            "BAD": Market(
                ticker="BAD", event_ticker="E2", title="Bad", status="open",
                yes_ask=22, yes_bid=18, last_price=20,
            ),
        }
        feasibility = {"GOOD": True, "BAD": False}

        decisions = engine.generate_infeasible_exit_decisions(positions, markets, feasibility)
        assert len(decisions) == 1
        assert decisions[0].ticker == "BAD"


class TestPositionFeasibilityCheck:
    @pytest.mark.asyncio
    async def test_check_positions_feasibility(self):
        config = ClaudeConfig(api_key="test-key", cache_ttl_minutes=5)
        engine = AnalysisEngine(config, day_trader_mode=True)

        now = datetime.now(timezone.utc)
        event = Event(
            event_ticker="EVT-DHS",
            title="DHS Funding",
            markets=[
                Market(
                    ticker="DHS-FUND",
                    event_ticker="EVT-DHS",
                    title="Will Congress fund DHS by tomorrow?",
                    status="open",
                    close_time=now + timedelta(hours=12),
                )
            ],
        )

        feasibility_resp = _make_feasibility_response([
            {"ticker": "DHS-FUND", "feasible": False, "reason": "Congress not in session"},
        ])

        engine.client = AsyncMock()
        engine.client.messages.create = AsyncMock(return_value=feasibility_resp)

        result = await engine.check_positions_feasibility([event])

        assert result == {"DHS-FUND": False}

    @pytest.mark.asyncio
    async def test_check_positions_feasibility_uses_cache(self):
        config = ClaudeConfig(api_key="test-key", cache_ttl_minutes=5)
        engine = AnalysisEngine(config, day_trader_mode=True)

        # Pre-populate cache
        engine.cache.set("_feas_EVT-DHS", {"DHS-FUND": False})

        event = Event(
            event_ticker="EVT-DHS",
            title="DHS Funding",
            markets=[
                Market(
                    ticker="DHS-FUND",
                    event_ticker="EVT-DHS",
                    title="Will Congress fund DHS?",
                    status="open",
                )
            ],
        )

        engine.client = AsyncMock()
        result = await engine.check_positions_feasibility([event])

        assert result == {"DHS-FUND": False}
        engine.client.messages.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_positions_feasibility_error_assumes_feasible(self):
        config = ClaudeConfig(api_key="test-key", cache_ttl_minutes=5)
        engine = AnalysisEngine(config, day_trader_mode=True)

        event = Event(
            event_ticker="EVT-ERR",
            title="Error Event",
            markets=[
                Market(
                    ticker="ERR-MKT",
                    event_ticker="EVT-ERR",
                    title="Error market",
                    status="open",
                )
            ],
        )

        engine.client = AsyncMock()
        engine.client.messages.create = AsyncMock(side_effect=Exception("API down"))

        result = await engine.check_positions_feasibility([event])

        # On error, all positions assumed feasible (don't panic-sell)
        assert result == {"ERR-MKT": True}
