"""Pre-trade risk checks — enforces position, exposure, and loss limits."""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from kalshi_bot.config import RiskConfig
from kalshi_bot.models import OrderAction, Position, Side, TradeDecision

logger = structlog.get_logger(__name__)


@dataclass
class RiskCheckResult:
    approved: bool
    reason: str = ""
    original_quantity: int = 0
    approved_quantity: int = 0


class RiskManager:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config
        self.daily_loss_cents: int = 0
        self.available_balance_cents: int = 0
        self._positions: dict[str, Position] = {}

    def update_positions(self, positions: list[Position]) -> None:
        """Refresh the current position state."""
        self._positions = {p.ticker: p for p in positions}

    def record_loss(self, loss_cents: int) -> None:
        """Record a realized loss (positive value = loss)."""
        self.daily_loss_cents += loss_cents

    def reset_daily(self) -> None:
        """Reset daily counters (called at midnight)."""
        self.daily_loss_cents = 0
        logger.info("risk_daily_reset")

    def check_trade(self, decision: TradeDecision) -> RiskCheckResult:
        """Run all pre-trade risk checks. Returns approved quantity (may be reduced)."""
        if decision.action == OrderAction.SELL:
            return self.check_exit_trade(decision)

        quantity = decision.quantity
        cost_per_contract = (
            decision.limit_price_cents
            if decision.side == Side.YES
            else 100 - decision.limit_price_cents
        )
        order_cost = quantity * cost_per_contract

        # Check 1: Single order size
        if order_cost > self.config.max_single_order_cents:
            max_qty = self.config.max_single_order_cents // cost_per_contract
            if max_qty <= 0:
                return RiskCheckResult(
                    approved=False,
                    reason=f"Order cost {order_cost}c exceeds max single order {self.config.max_single_order_cents}c",
                    original_quantity=quantity,
                )
            quantity = max_qty
            order_cost = quantity * cost_per_contract

        # Check 2: Per-ticker position size
        existing = self._positions.get(decision.ticker)
        existing_cost = existing.cost_cents if existing else 0
        if existing_cost + order_cost > self.config.max_position_size_cents:
            room = self.config.max_position_size_cents - existing_cost
            max_qty = room // cost_per_contract
            if max_qty <= 0:
                return RiskCheckResult(
                    approved=False,
                    reason=f"Position in {decision.ticker} would exceed max {self.config.max_position_size_cents}c",
                    original_quantity=decision.quantity,
                )
            quantity = min(quantity, max_qty)
            order_cost = quantity * cost_per_contract

        # Check 3: Total portfolio exposure
        total_exposure = sum(p.cost_cents for p in self._positions.values())
        if total_exposure + order_cost > self.config.max_total_exposure_cents:
            room = self.config.max_total_exposure_cents - total_exposure
            max_qty = room // cost_per_contract
            if max_qty <= 0:
                return RiskCheckResult(
                    approved=False,
                    reason=f"Total exposure would exceed max {self.config.max_total_exposure_cents}c",
                    original_quantity=decision.quantity,
                )
            quantity = min(quantity, max_qty)

        # Check 4: Position count
        if decision.ticker not in self._positions and len(self._positions) >= self.config.max_positions:
            return RiskCheckResult(
                approved=False,
                reason=f"Already at max positions ({self.config.max_positions})",
                original_quantity=decision.quantity,
            )

        # Check 5: Daily loss limit
        if self.daily_loss_cents >= self.config.max_daily_loss_cents:
            return RiskCheckResult(
                approved=False,
                reason=f"Daily loss limit reached ({self.daily_loss_cents}c / {self.config.max_daily_loss_cents}c)",
                original_quantity=decision.quantity,
            )

        # Check 6: Sufficient balance
        order_cost = quantity * cost_per_contract
        if self.available_balance_cents > 0 and order_cost > self.available_balance_cents:
            max_qty = self.available_balance_cents // cost_per_contract
            if max_qty <= 0:
                return RiskCheckResult(
                    approved=False,
                    reason=f"Insufficient balance ({self.available_balance_cents}c) for order cost ({order_cost}c)",
                    original_quantity=decision.quantity,
                )
            quantity = min(quantity, max_qty)

        # Check 7: Minimum order size — reject if final cost is too small
        final_cost = quantity * cost_per_contract
        if final_cost < self.config.min_order_cents:
            return RiskCheckResult(
                approved=False,
                reason=f"Order cost {final_cost}c below minimum {self.config.min_order_cents}c (${self.config.min_order_cents / 100:.2f})",
                original_quantity=decision.quantity,
            )

        # Deduct from available balance so subsequent orders in same cycle see reduced balance
        if self.available_balance_cents > 0:
            self.available_balance_cents -= final_cost

        return RiskCheckResult(
            approved=True,
            original_quantity=decision.quantity,
            approved_quantity=quantity,
        )

    def check_exit_trade(self, decision: TradeDecision) -> RiskCheckResult:
        """Lighter risk checks for SELL orders (exits reduce exposure)."""
        quantity = decision.quantity

        # Check 1: Can't sell more than you own
        existing = self._positions.get(decision.ticker)
        if existing is None:
            return RiskCheckResult(
                approved=False,
                reason=f"No position in {decision.ticker} to sell",
                original_quantity=quantity,
            )

        if quantity > existing.quantity:
            quantity = existing.quantity

        if quantity <= 0:
            return RiskCheckResult(
                approved=False,
                reason=f"No quantity to sell in {decision.ticker}",
                original_quantity=decision.quantity,
            )

        # Check 2: Daily loss limit (stop-loss sells count against it)
        if self.daily_loss_cents >= self.config.max_daily_loss_cents:
            return RiskCheckResult(
                approved=False,
                reason=f"Daily loss limit reached ({self.daily_loss_cents}c / {self.config.max_daily_loss_cents}c)",
                original_quantity=decision.quantity,
            )

        return RiskCheckResult(
            approved=True,
            original_quantity=decision.quantity,
            approved_quantity=quantity,
        )
