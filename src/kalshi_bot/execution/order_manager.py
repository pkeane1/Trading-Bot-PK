"""Order placement and lifecycle management."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import structlog

from kalshi_bot.api.client import KalshiClient
from kalshi_bot.api.exceptions import KalshiMarketClosedError
from kalshi_bot.config import ExecutionConfig
from kalshi_bot.models import Order, OrderStatus, TradeDecision
from kalshi_bot.risk.circuit_breaker import CircuitBreaker
from kalshi_bot.risk.manager import RiskManager

logger = structlog.get_logger(__name__)


class OrderManager:
    def __init__(
        self,
        client: KalshiClient,
        config: ExecutionConfig,
        risk_manager: RiskManager,
        circuit_breaker: CircuitBreaker,
    ) -> None:
        self.client = client
        self.config = config
        self.risk_manager = risk_manager
        self.circuit_breaker = circuit_breaker
        self._active_orders: dict[str, Order] = {}
        self.closed_tickers: set[str] = set()

    async def execute_decisions(self, decisions: list[TradeDecision]) -> list[Order]:
        """Execute a list of trade decisions through risk checks and order placement."""
        if self.circuit_breaker.is_tripped:
            logger.warning(
                "orders_blocked_circuit_breaker",
                reason=self.circuit_breaker.trip_reason,
            )
            return []

        placed_orders: list[Order] = []
        for decision in decisions:
            order = await self._execute_single(decision)
            if order is not None:
                placed_orders.append(order)

        return placed_orders

    async def _execute_single(self, decision: TradeDecision) -> Order | None:
        """Risk check and place a single order."""
        # Skip tickers we already know are closed
        if decision.ticker in self.closed_tickers:
            logger.debug("order_skipped_market_closed", ticker=decision.ticker)
            return None

        # Pre-trade risk check
        check = self.risk_manager.check_trade(decision)
        if not check.approved:
            logger.info("order_rejected_risk", ticker=decision.ticker, reason=check.reason)
            return None

        # Use approved quantity (may be reduced by risk manager)
        quantity = check.approved_quantity

        client_order_id = str(uuid.uuid4())

        if self.config.dry_run:
            logger.info(
                "dry_run_order",
                ticker=decision.ticker,
                side=decision.side.value,
                quantity=quantity,
                limit_price=decision.limit_price_cents,
                edge=f"{decision.edge:.1%}",
                client_order_id=client_order_id,
            )
            return Order(
                order_id=f"dry-{client_order_id}",
                client_order_id=client_order_id,
                ticker=decision.ticker,
                side=decision.side,
                action=decision.action,
                quantity=quantity,
                price_cents=decision.limit_price_cents,
                status=OrderStatus.PENDING,
            )

        try:
            order = await self.client.create_order(
                ticker=decision.ticker,
                side=decision.side,
                action=decision.action,
                count=quantity,
                price_cents=decision.limit_price_cents,
                client_order_id=client_order_id,
            )
            self._active_orders[order.order_id] = order
            logger.info(
                "order_placed",
                order_id=order.order_id,
                ticker=decision.ticker,
                side=decision.side.value,
                quantity=quantity,
                price=decision.limit_price_cents,
                edge=f"{decision.edge:.1%}",
            )
            self.circuit_breaker.record_api_call(was_error=False)
            return order
        except KalshiMarketClosedError:
            self.closed_tickers.add(decision.ticker)
            logger.warning(
                "order_market_closed",
                ticker=decision.ticker,
                note="Ticker added to closed set — will skip on future cycles",
            )
            return None
        except Exception as e:
            logger.error("order_placement_failed", ticker=decision.ticker, error=str(e))
            self.circuit_breaker.record_api_call(was_error=True)
            return None

    async def cleanup_stale_orders(self) -> int:
        """Cancel orders that have been resting too long."""
        try:
            resting = await self.client.get_orders(status="resting")
        except Exception as e:
            logger.error("stale_order_check_failed", error=str(e))
            return 0

        cancelled = 0
        cutoff_hours = self.config.stale_order_hours
        now = datetime.now(timezone.utc)

        for order in resting:
            if order.created_time is None:
                continue
            age_hours = (now - order.created_time).total_seconds() / 3600
            if age_hours > cutoff_hours:
                try:
                    await self.client.cancel_order(order.order_id)
                    cancelled += 1
                except Exception as e:
                    logger.error("stale_order_cancel_failed", order_id=order.order_id, error=str(e))

        if cancelled:
            logger.info("stale_orders_cancelled", count=cancelled)
        return cancelled

    async def cancel_all_orders(self) -> int:
        """Emergency: cancel all resting orders."""
        try:
            resting = await self.client.get_orders(status="resting")
        except Exception:
            logger.error("cancel_all_fetch_failed")
            return 0

        cancelled = 0
        for order in resting:
            try:
                await self.client.cancel_order(order.order_id)
                cancelled += 1
            except Exception:
                pass

        logger.info("emergency_cancel_all", cancelled=cancelled, total=len(resting))
        return cancelled
