"""Performance metrics computation — win rate, P&L, drawdown tracking."""

from __future__ import annotations

import structlog

from kalshi_bot.models import Fill, OrderAction, PerformanceMetrics

logger = structlog.get_logger(__name__)


class MetricsTracker:
    """Tracks and computes aggregate performance metrics from trade fills."""

    def __init__(self) -> None:
        self._metrics = PerformanceMetrics()
        self._peak_pnl_cents = 0
        self._trade_journal: list[dict[str, object]] = []

    @property
    def metrics(self) -> PerformanceMetrics:
        return self._metrics

    def record_fill(self, fill: Fill) -> None:
        """Record a fill and update metrics.

        For simplicity, we track sells as realizations: selling at fill price
        vs. the cost basis. Buy fills are recorded but don't count as P&L yet.
        """
        entry = {
            "ticker": fill.ticker,
            "side": fill.side.value,
            "action": fill.action.value,
            "count": fill.count,
            "price_cents": fill.price_cents,
        }
        self._trade_journal.append(entry)
        self._metrics.total_trades += 1

    def record_realized_pnl(self, pnl_cents: int) -> None:
        """Record a realized P&L from a closed position."""
        self._metrics.total_pnl_cents += pnl_cents

        if pnl_cents > 0:
            self._metrics.winning_trades += 1
            self._metrics.best_trade_pnl_cents = max(
                self._metrics.best_trade_pnl_cents, pnl_cents
            )
        elif pnl_cents < 0:
            self._metrics.losing_trades += 1
            self._metrics.worst_trade_pnl_cents = min(
                self._metrics.worst_trade_pnl_cents, pnl_cents
            )

        # Update drawdown
        self._peak_pnl_cents = max(self._peak_pnl_cents, self._metrics.total_pnl_cents)
        drawdown = self._peak_pnl_cents - self._metrics.total_pnl_cents
        self._metrics.max_drawdown_cents = max(self._metrics.max_drawdown_cents, drawdown)

    def log_summary(self) -> None:
        """Log a performance summary."""
        m = self._metrics
        logger.info(
            "performance_summary",
            total_trades=m.total_trades,
            win_rate=f"{m.win_rate:.1%}" if m.total_trades > 0 else "N/A",
            total_pnl=f"${m.total_pnl_cents / 100:.2f}",
            avg_pnl=f"${m.average_pnl_cents / 100:.2f}" if m.total_trades > 0 else "N/A",
            max_drawdown=f"${m.max_drawdown_cents / 100:.2f}",
            best_trade=f"${m.best_trade_pnl_cents / 100:.2f}",
            worst_trade=f"${m.worst_trade_pnl_cents / 100:.2f}",
        )
