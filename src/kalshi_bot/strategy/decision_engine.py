"""Converts Claude analyses + market prices into trade decisions."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

import structlog

from kalshi_bot.config import StrategyConfig, RiskConfig
from kalshi_bot.models import (
    EventAnalysis,
    Market,
    MarketAnalysis,
    OrderAction,
    Position,
    Side,
    TradeDecision,
)
from kalshi_bot.strategy.edge_calculator import calculate_edge, kelly_size

logger = structlog.get_logger(__name__)


class DecisionEngine:
    def __init__(
        self,
        strategy_config: StrategyConfig,
        risk_config: RiskConfig,
        bankroll_cents: int = 0,
    ) -> None:
        self.config = strategy_config
        self.risk_config = risk_config
        self.bankroll_cents = bankroll_cents

    def generate_decisions(
        self,
        analyses: list[EventAnalysis],
        markets_by_ticker: dict[str, Market],
        held_tickers: set[str] | None = None,
    ) -> list[TradeDecision]:
        """Generate trade decisions from analyses and current market data.

        This is where Claude's independent estimates meet market prices for the first time.
        Skips markets where we already hold a position (no double-buying).
        """
        decisions: list[TradeDecision] = []
        held_skipped = 0
        rejections: Counter[str] = Counter()
        total_evaluated = 0

        for event_analysis in analyses:
            for market_analysis in event_analysis.market_analyses:
                # Skip markets we already hold — don't pile into the same position
                if held_tickers and market_analysis.ticker in held_tickers:
                    held_skipped += 1
                    continue

                market = markets_by_ticker.get(market_analysis.ticker)
                if market is None:
                    continue

                total_evaluated += 1
                decision, reject_reason = self._evaluate_market(market_analysis, market)
                if decision is not None:
                    decisions.append(decision)
                elif reject_reason:
                    rejections[reject_reason] += 1

        # Sort by edge (highest first)
        decisions.sort(key=lambda d: abs(d.edge), reverse=True)
        logger.info(
            "decisions_generated",
            count=len(decisions),
            evaluated=total_evaluated,
            held_skipped=held_skipped,
            **{f"rejected_{k}": v for k, v in rejections.most_common()},
        )
        return decisions

    def _evaluate_market(
        self, analysis: MarketAnalysis, market: Market
    ) -> tuple[TradeDecision | None, str]:
        """Evaluate a single market for trading opportunity.

        Returns (decision, rejection_reason). rejection_reason is empty on success.
        """
        market_price = market.midpoint
        floor = self.config.min_price_cents
        if market_price < floor or market_price > (100 - floor):
            logger.debug("market_rejected_extreme_price", ticker=market.ticker, price=market_price, floor=floor)
            return None, "extreme_price"

        # Skip low-confidence analyses
        if analysis.confidence < self.config.min_confidence:
            logger.debug(
                "market_rejected_low_confidence",
                ticker=market.ticker,
                confidence=f"{analysis.confidence:.2f}",
                threshold=f"{self.config.min_confidence:.2f}",
            )
            return None, "low_confidence"

        # Calculate edge for both sides
        yes_edge = calculate_edge(analysis.predicted_probability, market_price, side_yes=True)
        no_edge = calculate_edge(analysis.predicted_probability, market_price, side_yes=False)

        # Pick the side with better edge
        if yes_edge > no_edge:
            edge = yes_edge
            side = Side.YES
        else:
            edge = no_edge
            side = Side.NO

        # Adjust minimum edge by confidence — lower confidence requires higher edge
        adjusted_min_edge = self.config.min_edge_threshold / analysis.confidence
        if edge < adjusted_min_edge:
            logger.debug(
                "market_rejected_insufficient_edge",
                ticker=market.ticker,
                edge=f"{edge:.3%}",
                adjusted_threshold=f"{adjusted_min_edge:.3%}",
                confidence=f"{analysis.confidence:.2f}",
                predicted_prob=f"{analysis.predicted_probability:.1%}",
                market_price=market_price,
            )
            return None, "insufficient_edge"

        # Size position via Kelly
        quantity = kelly_size(
            prob=analysis.predicted_probability,
            price_cents=market_price,
            side_yes=(side == Side.YES),
            bankroll_cents=self.bankroll_cents,
            fraction=self.config.kelly_fraction,
            max_order_cents=self.risk_config.max_single_order_cents,
        )
        if quantity <= 0:
            logger.debug("market_rejected_kelly_zero", ticker=market.ticker, edge=f"{edge:.3%}")
            return None, "kelly_zero"

        # Enforce minimum order size
        cost_per_contract = market_price if side == Side.YES else (100 - market_price)
        order_cost = quantity * cost_per_contract
        if order_cost < self.risk_config.min_order_cents:
            logger.debug(
                "market_rejected_below_min_order",
                ticker=market.ticker,
                order_cost_cents=order_cost,
                min_order_cents=self.risk_config.min_order_cents,
                quantity=quantity,
            )
            return None, "below_min_order"

        # Determine limit price — bid slightly inside the spread for better fills
        if side == Side.YES:
            limit_price = min(market.yes_ask, market_price + 1) if market.yes_ask > 0 else market_price
        else:
            limit_price = min(market.no_ask, (100 - market_price) + 1) if market.no_ask > 0 else 100 - market_price

        # Clamp to valid range
        limit_price = max(1, min(99, limit_price))

        decision = TradeDecision(
            ticker=market.ticker,
            side=side,
            action=OrderAction.BUY,
            quantity=quantity,
            limit_price_cents=limit_price,
            edge=edge,
            predicted_probability=analysis.predicted_probability,
            market_price_cents=market_price,
            confidence=analysis.confidence,
            reasoning=analysis.reasoning,
        )

        logger.info(
            "trade_opportunity",
            ticker=market.ticker,
            side=side.value,
            edge=f"{edge:.1%}",
            quantity=quantity,
            limit_price=limit_price,
            predicted_prob=f"{analysis.predicted_probability:.1%}",
            market_price=market_price,
        )
        return decision, ""

    def generate_exit_decisions(
        self,
        positions: list[Position],
        markets_by_ticker: dict[str, Market],
    ) -> list[TradeDecision]:
        """Generate SELL decisions for positions hitting take-profit or stop-loss thresholds."""
        decisions: list[TradeDecision] = []

        for position in positions:
            market = markets_by_ticker.get(position.ticker)
            if market is None:
                continue

            cost = position.cost_cents
            if cost <= 0:
                continue

            pnl_ratio = position.unrealized_pnl_cents / cost

            reason = ""
            if pnl_ratio >= self.config.take_profit_percent / 100:
                reason = "take_profit"
            elif pnl_ratio <= -(self.config.stop_loss_percent / 100):
                reason = "stop_loss"
            elif (
                self.config.max_hold_minutes > 0
                and position.entry_time is not None
            ):
                held_minutes = (
                    datetime.now(timezone.utc) - position.entry_time
                ).total_seconds() / 60
                if held_minutes >= self.config.max_hold_minutes:
                    reason = "max_hold_time"

            if not reason:
                continue

            # Use current bid for fast fill
            if position.side == Side.YES:
                limit_price = market.yes_bid if market.yes_bid > 0 else market.midpoint
            else:
                limit_price = market.no_bid if market.no_bid > 0 else 100 - market.midpoint

            limit_price = max(1, min(99, limit_price))

            decision = TradeDecision(
                ticker=position.ticker,
                side=position.side,
                action=OrderAction.SELL,
                quantity=position.quantity,
                limit_price_cents=limit_price,
                edge=pnl_ratio,
                predicted_probability=0.0,
                market_price_cents=market.midpoint,
                confidence=1.0,
                reasoning=f"Exit: {reason} (P&L {pnl_ratio:+.1%})",
            )
            decisions.append(decision)
            logger.info(
                "exit_decision",
                ticker=position.ticker,
                reason=reason,
                pnl_ratio=f"{pnl_ratio:+.1%}",
                quantity=position.quantity,
            )

        return decisions

    def generate_infeasible_exit_decisions(
        self,
        positions: list[Position],
        markets_by_ticker: dict[str, Market],
        feasibility: dict[str, bool],
    ) -> list[TradeDecision]:
        """Generate SELL decisions for positions in markets deemed infeasible.

        When a market's outcome can't realistically happen in the timeframe,
        exit immediately at the best available bid to cut losses.
        """
        decisions: list[TradeDecision] = []

        for position in positions:
            if feasibility.get(position.ticker, True):
                continue  # Feasible or unknown — keep holding

            market = markets_by_ticker.get(position.ticker)
            if market is None:
                continue

            # Use current bid for fast fill
            if position.side == Side.YES:
                limit_price = market.yes_bid if market.yes_bid > 0 else market.midpoint
            else:
                limit_price = market.no_bid if market.no_bid > 0 else 100 - market.midpoint

            limit_price = max(1, min(99, limit_price))

            cost = position.cost_cents
            pnl_ratio = position.unrealized_pnl_cents / cost if cost > 0 else 0.0

            decision = TradeDecision(
                ticker=position.ticker,
                side=position.side,
                action=OrderAction.SELL,
                quantity=position.quantity,
                limit_price_cents=limit_price,
                edge=pnl_ratio,
                predicted_probability=0.0,
                market_price_cents=market.midpoint,
                confidence=1.0,
                reasoning=f"Exit: infeasible — outcome cannot realistically occur in timeframe (P&L {pnl_ratio:+.1%})",
            )
            decisions.append(decision)
            logger.info(
                "infeasible_exit_decision",
                ticker=position.ticker,
                side=position.side.value,
                pnl_ratio=f"{pnl_ratio:+.1%}",
                quantity=position.quantity,
            )

        return decisions

    def generate_reval_exit_decisions(
        self,
        positions: list[Position],
        analyses: list[EventAnalysis],
        markets_by_ticker: dict[str, Market],
    ) -> list[TradeDecision]:
        """Generate SELL decisions when Claude's re-evaluation shows the edge has flipped."""
        # Build lookup: ticker -> MarketAnalysis
        analysis_by_ticker: dict[str, MarketAnalysis] = {}
        for event_analysis in analyses:
            for ma in event_analysis.market_analyses:
                analysis_by_ticker[ma.ticker] = ma

        decisions: list[TradeDecision] = []
        for position in positions:
            ma = analysis_by_ticker.get(position.ticker)
            market = markets_by_ticker.get(position.ticker)
            if ma is None or market is None:
                continue

            market_price = market.midpoint
            if market_price <= 0 or market_price >= 100:
                continue

            # Calculate edge from position's side
            edge = calculate_edge(
                ma.predicted_probability,
                market_price,
                side_yes=(position.side == Side.YES),
            )

            if edge <= self.config.re_eval_exit_edge:
                if position.side == Side.YES:
                    limit_price = market.yes_bid if market.yes_bid > 0 else market_price
                else:
                    limit_price = market.no_bid if market.no_bid > 0 else 100 - market_price

                limit_price = max(1, min(99, limit_price))

                decision = TradeDecision(
                    ticker=position.ticker,
                    side=position.side,
                    action=OrderAction.SELL,
                    quantity=position.quantity,
                    limit_price_cents=limit_price,
                    edge=edge,
                    predicted_probability=ma.predicted_probability,
                    market_price_cents=market_price,
                    confidence=ma.confidence,
                    reasoning=f"Exit: reval edge flipped ({edge:+.1%})",
                )
                decisions.append(decision)
                logger.info(
                    "reval_exit_decision",
                    ticker=position.ticker,
                    new_edge=f"{edge:+.1%}",
                    new_prob=f"{ma.predicted_probability:.1%}",
                    market_price=market_price,
                )

        return decisions
