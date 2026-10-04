"""Fill monitoring — tracks order fills from the Kalshi API."""

from __future__ import annotations

import structlog

from kalshi_bot.api.client import KalshiClient
from kalshi_bot.models import Fill

logger = structlog.get_logger(__name__)


class FillTracker:
    """Monitors and records trade fills."""

    def __init__(self, client: KalshiClient) -> None:
        self.client = client
        self._seen_trade_ids: set[str] = set()
        self.recent_fills: list[Fill] = []

    async def check_new_fills(self) -> list[Fill]:
        """Fetch fills and return only new ones since last check."""
        try:
            fills, _ = await self.client.get_fills(limit=100)
        except Exception as e:
            logger.error("fill_check_failed", error=str(e))
            return []

        new_fills: list[Fill] = []
        for fill in fills:
            if fill.trade_id and fill.trade_id not in self._seen_trade_ids:
                self._seen_trade_ids.add(fill.trade_id)
                new_fills.append(fill)

        if new_fills:
            logger.info("new_fills_detected", count=len(new_fills))
            self.recent_fills.extend(new_fills)

        return new_fills
