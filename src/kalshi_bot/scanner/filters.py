"""Filter predicates for market selection."""

from __future__ import annotations

from datetime import datetime, timezone

from kalshi_bot.config import ScannerConfig
from kalshi_bot.models import Market


def passes_volume(market: Market, config: ScannerConfig) -> bool:
    return market.volume >= config.min_volume


def passes_open_interest(market: Market, config: ScannerConfig) -> bool:
    return market.open_interest >= config.min_open_interest


def passes_spread(market: Market, config: ScannerConfig) -> bool:
    if market.spread == 0:
        return True  # No spread data available, don't filter out
    return market.spread <= config.max_spread_cents


def passes_time_to_expiry(market: Market, config: ScannerConfig) -> bool:
    if market.close_time is None:
        return True
    now = datetime.now(timezone.utc)
    close = market.close_time if market.close_time.tzinfo else market.close_time.replace(
        tzinfo=timezone.utc
    )
    hours_remaining = (close - now).total_seconds() / 3600
    return hours_remaining >= config.min_hours_to_expiry


def passes_max_time_to_expiry(market: Market, config: ScannerConfig) -> bool:
    if config.max_hours_to_expiry <= 0:
        return True  # 0 = disabled
    if market.close_time is None:
        return False  # No close time = can't verify, skip in day trader mode
    now = datetime.now(timezone.utc)
    close = market.close_time if market.close_time.tzinfo else market.close_time.replace(
        tzinfo=timezone.utc
    )
    hours_remaining = (close - now).total_seconds() / 3600
    return hours_remaining <= config.max_hours_to_expiry


def passes_category(market: Market, config: ScannerConfig) -> bool:
    if not config.categories:
        return True  # Empty = all categories
    return market.category.lower() in [c.lower() for c in config.categories]


def passes_all_filters(market: Market, config: ScannerConfig) -> bool:
    """Run all filter predicates on a market.

    Note: category filtering is intentionally NOT done here — Kalshi only sets
    category on Events, not Markets, so market.category is always "".
    Category filtering happens at the event level in MarketScanner.scan().
    """
    return (
        passes_volume(market, config)
        and passes_open_interest(market, config)
        and passes_spread(market, config)
        and passes_time_to_expiry(market, config)
        and passes_max_time_to_expiry(market, config)
    )
