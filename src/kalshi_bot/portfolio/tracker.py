"""Portfolio tracking — positions, balance, and P&L from Kalshi API."""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from kalshi_bot.api.client import KalshiClient
from kalshi_bot.models import Position, PortfolioSnapshot, Side

logger = structlog.get_logger(__name__)


class PortfolioTracker:
    def __init__(self, client: KalshiClient) -> None:
        self.client = client
        self._last_snapshot: PortfolioSnapshot | None = None
        self._entry_times: dict[str, datetime] = {}

    async def take_snapshot(self) -> PortfolioSnapshot:
        """Fetch current portfolio state from Kalshi API."""
        try:
            balance = await self.client.get_balance()
            raw_positions = await self.client.get_positions()
        except Exception as e:
            logger.error("portfolio_snapshot_failed", error=str(e))
            if self._last_snapshot:
                return self._last_snapshot
            return PortfolioSnapshot(
                timestamp=datetime.now(timezone.utc),
                balance_cents=0,
            )

        positions: list[Position] = []
        for p in raw_positions:
            # "position" is the current net YES position (negative = holding NO).
            # Skip entries where position is 0 — those are fully closed.
            yes_qty = int(p.get("position", 0) or 0)
            if yes_qty == 0:
                continue

            side = Side.YES if yes_qty > 0 else Side.NO
            quantity = abs(yes_qty)

            positions.append(
                Position(
                    ticker=p.get("ticker", ""),
                    side=side,
                    quantity=quantity,
                    average_price_cents=int(p.get("average_price", 0) or 0),
                    market_price_cents=int(p.get("market_price", 0) or 0),
                )
            )

        # Filter out fully settled positions (both prices zero = Kalshi ghost entry)
        settled_count = 0
        active_positions: list[Position] = []
        for p in positions:
            if p.market_price_cents == 0 and p.average_price_cents == 0:
                settled_count += 1
            else:
                active_positions.append(p)
        if settled_count:
            logger.info("settled_positions_filtered", count=settled_count)
        positions = active_positions

        # Track entry times — record when positions first appear
        now = datetime.now(timezone.utc)
        current_tickers = {p.ticker for p in positions}
        for p in positions:
            if p.ticker not in self._entry_times:
                self._entry_times[p.ticker] = now
            p.entry_time = self._entry_times[p.ticker]
        # Clean up tickers no longer held
        stale = set(self._entry_times) - current_tickers
        for ticker in stale:
            del self._entry_times[ticker]

        total_exposure = sum(p.cost_cents for p in positions)
        unrealized_pnl = sum(p.unrealized_pnl_cents for p in positions)

        snapshot = PortfolioSnapshot(
            timestamp=datetime.now(timezone.utc),
            balance_cents=balance,
            positions=positions,
            total_exposure_cents=total_exposure,
            unrealized_pnl_cents=unrealized_pnl,
        )

        logger.info(
            "portfolio_snapshot",
            balance=f"${balance / 100:.2f}",
            positions=len(positions),
            exposure=f"${total_exposure / 100:.2f}",
            unrealized_pnl=f"${unrealized_pnl / 100:.2f}",
        )

        self._last_snapshot = snapshot
        return snapshot
