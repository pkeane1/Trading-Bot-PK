"""Main orchestrator — initializes components, schedules pipeline, handles shutdown."""

from __future__ import annotations

import asyncio

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from kalshi_bot.analysis.engine import AnalysisEngine
from kalshi_bot.api.auth import KalshiAuth
from kalshi_bot.api.client import KalshiClient
from kalshi_bot.api.rate_limiter import DualRateLimiter
from kalshi_bot.config import AppConfig
from kalshi_bot.execution.fill_tracker import FillTracker
from kalshi_bot.execution.order_manager import OrderManager
from kalshi_bot.models import Event, Market, Position
from kalshi_bot.portfolio.metrics import MetricsTracker
from kalshi_bot.portfolio.tracker import PortfolioTracker
from kalshi_bot.risk.circuit_breaker import CircuitBreaker
from kalshi_bot.risk.manager import RiskManager
from kalshi_bot.scanner.market_scanner import MarketScanner
from kalshi_bot.strategy.decision_engine import DecisionEngine

logger = structlog.get_logger(__name__)


class Orchestrator:
    """Wires all components together and runs the trading pipeline on a schedule."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._scheduler: AsyncIOScheduler | None = None
        self._client: KalshiClient | None = None
        self._running = False

        # Components (initialized in start())
        self.scanner: MarketScanner | None = None
        self.analysis_engine: AnalysisEngine | None = None
        self.decision_engine: DecisionEngine | None = None
        self.risk_manager: RiskManager | None = None
        self.circuit_breaker: CircuitBreaker | None = None
        self.order_manager: OrderManager | None = None
        self.fill_tracker: FillTracker | None = None
        self.portfolio_tracker: PortfolioTracker | None = None
        self.metrics_tracker: MetricsTracker | None = None

    async def start(self) -> None:
        """Initialize all components and start the scheduled pipeline."""
        logger.info(
            "orchestrator_starting",
            environment=self.config.kalshi.environment,
            dry_run=self.config.execution.dry_run,
            day_trader_mode=self.config.day_trader_mode,
        )
        if self.config.day_trader_mode:
            logger.info(
                "day_trader_mode_enabled",
                take_profit=f"{self.config.strategy.take_profit_percent}%",
                stop_loss=f"{self.config.strategy.stop_loss_percent}%",
                max_hold_minutes=self.config.strategy.max_hold_minutes,
                pipeline_interval=f"{self.config.scheduler.pipeline_interval_minutes}m",
                monitor_interval=f"{self.config.scheduler.portfolio_monitor_interval_minutes}m",
                max_hours_to_expiry=self.config.scanner.max_hours_to_expiry,
            )

        # Initialize API client
        auth = KalshiAuth(
            api_key=self.config.kalshi.api_key,
            private_key_path=self.config.kalshi.private_key_path,
        )
        rate_limiter = DualRateLimiter()
        self._client = KalshiClient(
            base_url=self.config.kalshi.base_url,
            auth=auth,
            rate_limiter=rate_limiter,
        )
        await self._client.__aenter__()

        # Verify connectivity (non-fatal — service may be temporarily down)
        balance = 0
        try:
            balance = await self._client.get_balance()
            logger.info("kalshi_connected", balance=f"${balance / 100:.2f}")
        except Exception as e:
            body = getattr(e, "response_body", "")
            logger.warning(
                "kalshi_connection_check_failed",
                error=str(e),
                response_body=body,
                note="Will retry on first pipeline cycle — Kalshi service may be temporarily down",
            )

        # Initialize components
        self.scanner = MarketScanner(self._client, self.config.scanner)
        self.analysis_engine = AnalysisEngine(
            self.config.claude,
            day_trader_mode=self.config.day_trader_mode,
        )
        self.risk_manager = RiskManager(self.config.risk)
        self.circuit_breaker = CircuitBreaker(self.config.risk)
        self.decision_engine = DecisionEngine(
            strategy_config=self.config.strategy,
            risk_config=self.config.risk,
            bankroll_cents=balance,
        )
        self.order_manager = OrderManager(
            client=self._client,
            config=self.config.execution,
            risk_manager=self.risk_manager,
            circuit_breaker=self.circuit_breaker,
        )
        self.fill_tracker = FillTracker(self._client)
        self.portfolio_tracker = PortfolioTracker(self._client)
        self.metrics_tracker = MetricsTracker()

        # Schedule jobs
        self._scheduler = AsyncIOScheduler()
        self._scheduler.add_job(
            self.run_pipeline,
            "interval",
            minutes=self.config.scheduler.pipeline_interval_minutes,
            id="pipeline",
            max_instances=1,
        )
        self._scheduler.add_job(
            self.monitor_portfolio,
            "interval",
            minutes=self.config.scheduler.portfolio_monitor_interval_minutes,
            id="portfolio_monitor",
            max_instances=1,
        )
        self._scheduler.add_job(
            self.risk_manager.reset_daily,
            "cron",
            hour=0,
            minute=0,
            id="daily_risk_reset",
        )
        self._scheduler.start()
        self._running = True

        # Run first pipeline immediately
        logger.info("orchestrator_started", pipeline_interval=f"{self.config.scheduler.pipeline_interval_minutes}m")
        await self.run_pipeline()

    async def run_pipeline(self) -> None:
        """Execute one full SCAN → ANALYZE → DECIDE → EXECUTE cycle."""
        if not self._running:
            return

        logger.info("pipeline_cycle_start")

        try:
            # SCAN
            events = await self.scanner.scan()  # type: ignore[union-attr]
            if not events:
                logger.info("pipeline_no_events")
                return

            # ANALYZE
            if self.config.day_trader_mode:
                analyses = await self.analysis_engine.analyze_events_two_pass(events)  # type: ignore[union-attr]
            else:
                analyses = await self.analysis_engine.analyze_events(events)  # type: ignore[union-attr]
            if not analyses:
                logger.info("pipeline_no_analyses")
                return

            # Build market lookup (this is where prices enter the picture)
            markets_by_ticker: dict[str, Market] = {}
            for event in events:
                for market in event.markets:
                    markets_by_ticker[market.ticker] = market

            # Update bankroll for sizing + balance for risk checks
            balance = await self._client.get_balance()  # type: ignore[union-attr]
            self.decision_engine.bankroll_cents = balance  # type: ignore[union-attr]
            self.risk_manager.available_balance_cents = balance  # type: ignore[union-attr]

            # Snapshot BEFORE decisions so risk manager knows current positions
            snapshot = await self.portfolio_tracker.take_snapshot()  # type: ignore[union-attr]
            self.risk_manager.update_positions(snapshot.positions)  # type: ignore[union-attr]
            held_tickers = {p.ticker for p in snapshot.positions if p.market_price_cents > 0}

            # DECIDE — new buy opportunities (skip markets we already hold)
            decisions = self.decision_engine.generate_decisions(  # type: ignore[union-attr]
                analyses, markets_by_ticker, held_tickers=held_tickers,
            )
            reval_exits = self.decision_engine.generate_reval_exit_decisions(  # type: ignore[union-attr]
                snapshot.positions, analyses, markets_by_ticker,
            )
            if reval_exits:
                logger.info("reval_exit_decisions", count=len(reval_exits))

            all_decisions = reval_exits + decisions
            if not all_decisions:
                logger.info("pipeline_no_decisions")
                return

            # EXECUTE
            orders = await self.order_manager.execute_decisions(all_decisions)  # type: ignore[union-attr]
            logger.info("pipeline_cycle_complete", decisions=len(all_decisions), orders_placed=len(orders))

            # Cleanup stale orders
            await self.order_manager.cleanup_stale_orders()  # type: ignore[union-attr]

        except Exception as e:
            logger.error("pipeline_cycle_error", error=str(e), exc_info=True)

    async def monitor_portfolio(self) -> None:
        """Periodic portfolio monitoring — snapshots, fills, metrics, and exit checks."""
        if not self._running:
            return

        try:
            # Take snapshot
            snapshot = await self.portfolio_tracker.take_snapshot()  # type: ignore[union-attr]

            # Update risk manager with current positions
            self.risk_manager.update_positions(snapshot.positions)  # type: ignore[union-attr]

            # Check for take-profit / stop-loss exits
            if snapshot.positions:
                markets_by_ticker = await self._fetch_markets_for_positions(snapshot.positions)
                exit_decisions = self.decision_engine.generate_exit_decisions(  # type: ignore[union-attr]
                    snapshot.positions, markets_by_ticker,
                )
                if exit_decisions:
                    logger.info("exit_decisions_generated", count=len(exit_decisions))
                    await self.order_manager.execute_decisions(exit_decisions)  # type: ignore[union-attr]

                # Day trader mode: check if held positions are in infeasible markets
                if self.config.day_trader_mode:
                    infeasible_exits = await self._check_position_feasibility(
                        snapshot.positions, markets_by_ticker,
                    )
                    if infeasible_exits:
                        logger.info("infeasible_exit_decisions", count=len(infeasible_exits))
                        await self.order_manager.execute_decisions(infeasible_exits)  # type: ignore[union-attr]

            # Check for new fills
            new_fills = await self.fill_tracker.check_new_fills()  # type: ignore[union-attr]
            for fill in new_fills:
                self.metrics_tracker.record_fill(fill)  # type: ignore[union-attr]

            # Log metrics periodically
            self.metrics_tracker.log_summary()  # type: ignore[union-attr]

        except Exception as e:
            logger.error("portfolio_monitor_error", error=str(e))

    async def _fetch_markets_for_positions(
        self, positions: list[Position],
    ) -> dict[str, Market]:
        """Fetch current market data for tickers we hold positions in.

        Skips tickers already known to be closed and filters out markets
        whose status is not 'open' to avoid wasting API calls on exit
        attempts that will 409.
        """
        closed_tickers = self.order_manager.closed_tickers if self.order_manager else set()  # type: ignore[union-attr]
        markets: dict[str, Market] = {}
        for position in positions:
            if position.ticker in closed_tickers:
                logger.debug("skip_closed_market_fetch", ticker=position.ticker)
                continue
            try:
                market = await self._client.get_market(position.ticker)  # type: ignore[union-attr]
                if market is None:
                    continue
                if market.status not in ("open", "active"):
                    logger.info(
                        "position_market_not_open",
                        ticker=position.ticker,
                        status=market.status,
                    )
                    closed_tickers.add(position.ticker)
                    continue
                markets[position.ticker] = market
            except Exception as e:
                logger.warning("fetch_market_failed", ticker=position.ticker, error=str(e))
        return markets

    async def _check_position_feasibility(
        self,
        positions: list[Position],
        markets_by_ticker: dict[str, Market],
    ) -> list:
        """Build events from held positions, run feasibility check, generate exits."""
        from collections import defaultdict
        from kalshi_bot.models import TradeDecision

        # Group positions by event_ticker
        positions_by_event: dict[str, list[Position]] = defaultdict(list)
        for pos in positions:
            market = markets_by_ticker.get(pos.ticker)
            if market is not None:
                positions_by_event[market.event_ticker].append(pos)

        # Build lightweight Event objects with market data for feasibility check
        events: list[Event] = []
        for event_ticker, event_positions in positions_by_event.items():
            event_markets = [
                markets_by_ticker[p.ticker]
                for p in event_positions
                if p.ticker in markets_by_ticker
            ]
            if not event_markets:
                continue

            # Fetch event metadata for title/context
            try:
                event = await self._client.get_event(event_ticker)  # type: ignore[union-attr]
                event.markets = event_markets
            except Exception:
                event = Event(
                    event_ticker=event_ticker,
                    title=event_ticker,
                    markets=event_markets,
                )
            events.append(event)

        if not events:
            return []

        # Run feasibility check
        feasibility = await self.analysis_engine.check_positions_feasibility(events)  # type: ignore[union-attr]

        # Generate exit decisions for infeasible positions
        return self.decision_engine.generate_infeasible_exit_decisions(  # type: ignore[union-attr]
            positions, markets_by_ticker, feasibility,
        )

    async def shutdown(self) -> None:
        """Graceful shutdown: stop scheduler → cancel orders → close connections."""
        logger.info("orchestrator_shutting_down")
        self._running = False

        # Stop scheduler
        if self._scheduler:
            self._scheduler.shutdown(wait=False)

        # Cancel all resting orders
        if self.order_manager and not self.config.execution.dry_run:
            await self.order_manager.cancel_all_orders()

        # Log final performance
        if self.metrics_tracker:
            self.metrics_tracker.log_summary()

        # Close API client
        if self._client:
            await self._client.__aexit__(None, None, None)

        logger.info("orchestrator_shutdown_complete")
