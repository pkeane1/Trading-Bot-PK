"""Tests for edge calculator and decision engine."""

from __future__ import annotations

import pytest

from kalshi_bot.strategy.edge_calculator import (
    calculate_edge,
    kelly_fraction_binary,
    kelly_size,
)
from kalshi_bot.config import RiskConfig, StrategyConfig
from kalshi_bot.models import EventAnalysis, Market, MarketAnalysis, OrderAction, Position, Side
from kalshi_bot.strategy.decision_engine import DecisionEngine


class TestCalculateEdge:
    def test_positive_yes_edge(self):
        # Claude thinks 70%, market says 60%
        edge = calculate_edge(0.70, 60, side_yes=True)
        assert abs(edge - 0.10) < 0.001

    def test_negative_yes_edge(self):
        # Claude thinks 40%, market says 60%
        edge = calculate_edge(0.40, 60, side_yes=True)
        assert abs(edge - (-0.20)) < 0.001

    def test_no_edge_is_opposite(self):
        yes_edge = calculate_edge(0.70, 60, side_yes=True)
        no_edge = calculate_edge(0.70, 60, side_yes=False)
        assert abs(yes_edge + no_edge) < 0.001

    def test_zero_edge(self):
        edge = calculate_edge(0.50, 50, side_yes=True)
        assert abs(edge) < 0.001


class TestKellyFraction:
    def test_positive_edge_gives_positive_fraction(self):
        # 70% prob at 60c => positive edge
        f = kelly_fraction_binary(0.70, 60, side_yes=True)
        assert f > 0

    def test_no_edge_gives_zero(self):
        f = kelly_fraction_binary(0.50, 50, side_yes=True)
        assert abs(f) < 0.001

    def test_negative_edge_gives_zero(self):
        f = kelly_fraction_binary(0.30, 60, side_yes=True)
        assert f == 0.0

    def test_extreme_edge(self):
        # 95% prob at 50c => strong edge
        f = kelly_fraction_binary(0.95, 50, side_yes=True)
        assert f > 0.5

    def test_boundary_prices(self):
        assert kelly_fraction_binary(0.5, 0, side_yes=True) == 0.0
        assert kelly_fraction_binary(0.5, 100, side_yes=True) == 0.0


class TestKellySize:
    def test_reasonable_sizing(self):
        contracts = kelly_size(
            prob=0.70,
            price_cents=60,
            side_yes=True,
            bankroll_cents=10000,
            fraction=0.25,
            max_order_cents=2000,
        )
        assert contracts > 0
        assert contracts * 60 <= 2000  # respects max order

    def test_no_edge_no_contracts(self):
        contracts = kelly_size(
            prob=0.50,
            price_cents=50,
            side_yes=True,
            bankroll_cents=10000,
        )
        assert contracts == 0

    def test_respects_max_order(self):
        contracts = kelly_size(
            prob=0.95,
            price_cents=10,
            side_yes=True,
            bankroll_cents=1000000,
            max_order_cents=100,
        )
        assert contracts * 10 <= 100


class TestDecisionEngine:
    def test_generates_decision_for_edge(self, strategy_config, risk_config):
        engine = DecisionEngine(
            strategy_config=strategy_config,
            risk_config=risk_config,
            bankroll_cents=50000,
        )

        analyses = [
            EventAnalysis(
                event_ticker="EVT",
                event_summary="Test event",
                market_analyses=[
                    MarketAnalysis(
                        ticker="MKT-1",
                        predicted_probability=0.75,
                        confidence=0.8,
                        reasoning="Strong historical pattern",
                    )
                ],
            )
        ]
        markets = {
            "MKT-1": Market(
                ticker="MKT-1",
                event_ticker="EVT",
                title="Test",
                status="open",
                yes_ask=60,
                yes_bid=55,
                last_price=57,
            )
        }

        decisions = engine.generate_decisions(analyses, markets)
        assert len(decisions) >= 1
        assert decisions[0].ticker == "MKT-1"
        assert decisions[0].edge > 0

    def test_no_decision_for_no_edge(self, strategy_config, risk_config):
        engine = DecisionEngine(
            strategy_config=strategy_config,
            risk_config=risk_config,
            bankroll_cents=50000,
        )

        analyses = [
            EventAnalysis(
                event_ticker="EVT",
                event_summary="Test",
                market_analyses=[
                    MarketAnalysis(
                        ticker="MKT-1",
                        predicted_probability=0.50,
                        confidence=0.8,
                        reasoning="No clear direction",
                    )
                ],
            )
        ]
        markets = {
            "MKT-1": Market(
                ticker="MKT-1",
                event_ticker="EVT",
                title="Test",
                status="open",
                yes_ask=52,
                yes_bid=48,
                last_price=50,
            )
        }

        decisions = engine.generate_decisions(analyses, markets)
        assert len(decisions) == 0

    def test_filters_low_confidence(self, strategy_config, risk_config):
        engine = DecisionEngine(
            strategy_config=strategy_config,
            risk_config=risk_config,
            bankroll_cents=50000,
        )

        analyses = [
            EventAnalysis(
                event_ticker="EVT",
                event_summary="Test",
                market_analyses=[
                    MarketAnalysis(
                        ticker="MKT-1",
                        predicted_probability=0.80,
                        confidence=0.2,  # Below min_confidence
                        reasoning="Very uncertain",
                    )
                ],
            )
        ]
        markets = {
            "MKT-1": Market(
                ticker="MKT-1",
                event_ticker="EVT",
                title="Test",
                status="open",
                yes_ask=55,
                yes_bid=50,
                last_price=52,
            )
        }

        decisions = engine.generate_decisions(analyses, markets)
        assert len(decisions) == 0


class TestExitDecisions:
    def _make_engine(self, strategy_config, risk_config):
        return DecisionEngine(
            strategy_config=strategy_config,
            risk_config=risk_config,
            bankroll_cents=50000,
        )

    def test_take_profit_generates_sell(self, strategy_config, risk_config):
        """Position with 40% gain should trigger a take-profit SELL (threshold is 30%)."""
        engine = self._make_engine(strategy_config, risk_config)

        # Bought YES at 50c, market now at 70c → unrealized P&L = 20c/contract
        # P&L ratio = (70-50)*10 / (50*10) = 200/500 = 40%
        positions = [
            Position(
                ticker="MKT-TP",
                side=Side.YES,
                quantity=10,
                average_price_cents=50,
                market_price_cents=70,
            )
        ]
        markets = {
            "MKT-TP": Market(
                ticker="MKT-TP",
                event_ticker="EVT",
                title="Test",
                status="open",
                yes_ask=72,
                yes_bid=68,
                last_price=70,
            )
        }

        decisions = engine.generate_exit_decisions(positions, markets)
        assert len(decisions) == 1
        assert decisions[0].action == OrderAction.SELL
        assert decisions[0].ticker == "MKT-TP"
        assert decisions[0].quantity == 10
        assert "take_profit" in decisions[0].reasoning

    def test_stop_loss_generates_sell(self, strategy_config, risk_config):
        """Position with 60% loss should trigger a stop-loss SELL (threshold is 50%)."""
        engine = self._make_engine(strategy_config, risk_config)

        # Bought YES at 50c, market now at 20c → unrealized P&L = -30c/contract
        # P&L ratio = (20-50)*10 / (50*10) = -300/500 = -60%
        positions = [
            Position(
                ticker="MKT-SL",
                side=Side.YES,
                quantity=10,
                average_price_cents=50,
                market_price_cents=20,
            )
        ]
        markets = {
            "MKT-SL": Market(
                ticker="MKT-SL",
                event_ticker="EVT",
                title="Test",
                status="open",
                yes_ask=22,
                yes_bid=18,
                last_price=20,
            )
        }

        decisions = engine.generate_exit_decisions(positions, markets)
        assert len(decisions) == 1
        assert decisions[0].action == OrderAction.SELL
        assert decisions[0].ticker == "MKT-SL"
        assert "stop_loss" in decisions[0].reasoning

    def test_no_exit_within_thresholds(self, strategy_config, risk_config):
        """Position within TP/SL thresholds should not generate a SELL."""
        engine = self._make_engine(strategy_config, risk_config)

        # Bought YES at 50c, market now at 55c → P&L ratio = +10%
        positions = [
            Position(
                ticker="MKT-OK",
                side=Side.YES,
                quantity=10,
                average_price_cents=50,
                market_price_cents=55,
            )
        ]
        markets = {
            "MKT-OK": Market(
                ticker="MKT-OK",
                event_ticker="EVT",
                title="Test",
                status="open",
                yes_ask=57,
                yes_bid=53,
                last_price=55,
            )
        }

        decisions = engine.generate_exit_decisions(positions, markets)
        assert len(decisions) == 0

    def test_reval_exit_on_flipped_edge(self, strategy_config, risk_config):
        """When Claude's new analysis shows edge has flipped negative, generate SELL."""
        engine = self._make_engine(strategy_config, risk_config)

        # We hold YES at 60c. Claude now thinks probability is 50%.
        # Market midpoint is 60c. Edge for YES side = 0.50 - 0.60 = -0.10
        # re_eval_exit_edge is -0.03, so -0.10 <= -0.03 → SELL
        positions = [
            Position(
                ticker="MKT-RE",
                side=Side.YES,
                quantity=5,
                average_price_cents=60,
                market_price_cents=60,
            )
        ]
        analyses = [
            EventAnalysis(
                event_ticker="EVT",
                event_summary="Re-eval test",
                market_analyses=[
                    MarketAnalysis(
                        ticker="MKT-RE",
                        predicted_probability=0.50,
                        confidence=0.8,
                        reasoning="Conditions changed",
                    )
                ],
            )
        ]
        markets = {
            "MKT-RE": Market(
                ticker="MKT-RE",
                event_ticker="EVT",
                title="Test",
                status="open",
                yes_ask=62,
                yes_bid=58,
                last_price=60,
            )
        }

        decisions = engine.generate_reval_exit_decisions(positions, analyses, markets)
        assert len(decisions) == 1
        assert decisions[0].action == OrderAction.SELL
        assert decisions[0].ticker == "MKT-RE"
        assert "reval" in decisions[0].reasoning

    def test_reval_no_exit_when_edge_positive(self, strategy_config, risk_config):
        """When Claude still agrees with the position, no exit."""
        engine = self._make_engine(strategy_config, risk_config)

        # We hold YES at 50c. Claude thinks 70%. Market at 55c.
        # Edge for YES = 0.70 - 0.55 = +0.15 → still positive, no exit
        positions = [
            Position(
                ticker="MKT-OK2",
                side=Side.YES,
                quantity=5,
                average_price_cents=50,
                market_price_cents=55,
            )
        ]
        analyses = [
            EventAnalysis(
                event_ticker="EVT",
                event_summary="Still good",
                market_analyses=[
                    MarketAnalysis(
                        ticker="MKT-OK2",
                        predicted_probability=0.70,
                        confidence=0.8,
                        reasoning="Still favorable",
                    )
                ],
            )
        ]
        markets = {
            "MKT-OK2": Market(
                ticker="MKT-OK2",
                event_ticker="EVT",
                title="Test",
                status="open",
                yes_ask=57,
                yes_bid=53,
                last_price=55,
            )
        }

        decisions = engine.generate_reval_exit_decisions(positions, analyses, markets)
        assert len(decisions) == 0
