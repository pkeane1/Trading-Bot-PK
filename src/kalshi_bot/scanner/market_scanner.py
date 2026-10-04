"""Market discovery pipeline — fetches, filters, groups, and ranks markets."""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import datetime, timezone

import structlog

from kalshi_bot.api.client import KalshiClient
from kalshi_bot.config import ScannerConfig
from kalshi_bot.models import Event, Market
from kalshi_bot.scanner.filters import passes_all_filters

logger = structlog.get_logger(__name__)


class MarketScanner:
    def __init__(self, client: KalshiClient, config: ScannerConfig) -> None:
        self.client = client
        self.config = config
        self._recent_events: dict[str, datetime] = {}

    async def scan(self) -> list[Event]:
        """Run the full scan pipeline: fetch → filter → group → rank → cap."""
        # 1. Fetch all open markets
        markets = await self.client.get_all_markets(status="open")
        logger.info("scan_fetched", total_markets=len(markets))

        # 2. Apply filters
        filtered = [m for m in markets if passes_all_filters(m, self.config)]
        logger.info("scan_filtered", passed=len(filtered), removed=len(markets) - len(filtered))

        # 3. Group by event
        events_map: dict[str, list[Market]] = defaultdict(list)
        for m in filtered:
            events_map[m.event_ticker].append(m)

        # 4. Build Event objects with metadata
        events: list[Event] = []
        for event_ticker, event_markets in events_map.items():
            try:
                event = await self.client.get_event(event_ticker)
                event.markets = event_markets
                events.append(event)
            except Exception:
                # Fall back to a minimal Event if the API call fails
                events.append(
                    Event(
                        event_ticker=event_ticker,
                        title=event_ticker,
                        markets=event_markets,
                    )
                )
                logger.warning("event_fetch_failed", event_ticker=event_ticker)

        # 4b. Filter events by category
        if self.config.categories:
            allowed = {c.lower() for c in self.config.categories}
            before = len(events)
            events = [e for e in events if e.category.lower() in allowed]
            logger.info(
                "scan_category_filtered",
                passed=len(events),
                removed=before - len(events),
                categories=self.config.categories,
            )

        # 5. Score, apply cooldown penalty + jitter, then rank
        now = datetime.now(timezone.utc)
        scored: list[tuple[float, Event]] = []
        for event in events:
            score = _event_score(event)
            score = _apply_cooldown(
                score, event.event_ticker, now,
                self._recent_events,
                self.config.cooldown_hours,
                self.config.cooldown_penalty,
            )
            score = _apply_jitter(score, self.config.score_jitter_pct)
            scored.append((score, event))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        events = [event for _, event in scored]

        # 6. Cap total markets
        capped = _cap_markets(events, self.config.max_markets_per_cycle)

        # 7. Update cooldown tracker for selected events
        for event in capped:
            self._recent_events[event.event_ticker] = now
        self._cleanup_cooldowns(now)

        logger.info("scan_complete", events=len(capped), total_markets=sum(len(e.markets) for e in capped))
        return capped

    def _cleanup_cooldowns(self, now: datetime) -> None:
        """Remove cooldown entries older than cooldown_hours."""
        cutoff_seconds = self.config.cooldown_hours * 3600
        expired = [
            ticker for ticker, ts in self._recent_events.items()
            if (now - ts).total_seconds() > cutoff_seconds
        ]
        for ticker in expired:
            del self._recent_events[ticker]


def _event_score(event: Event) -> float:
    """Score an event by attractiveness (higher is better).

    Weights derived from Monte Carlo optimization (10k sims):
    - volume_w=1.0, oi_w=1.0: both are strong liquidity signals, weighted equally
    - spread_w=260: tighter spreads matter ~2.5x more than previously weighted
    - centrality_w=800: markets near 50c have far more edge potential than
      extreme prices — this was the biggest missing signal in the original scorer
    """
    if not event.markets:
        return 0.0
    total_volume = sum(m.volume for m in event.markets)
    total_oi = sum(m.open_interest for m in event.markets)
    avg_spread = sum(m.spread for m in event.markets) / len(event.markets)
    spread_score = max(0, 20 - avg_spread)  # tighter spread = higher score

    # Price centrality: markets near 50c have more tradeable edge than extremes.
    # Score 0.0 at 0c/100c, 1.0 at 50c. Averaged across all markets in event.
    avg_centrality = sum(
        1.0 - abs(m.midpoint - 50) / 50.0 for m in event.markets
    ) / len(event.markets)

    return (
        total_volume * 1.0
        + total_oi * 1.0
        + spread_score * 260
        + avg_centrality * 800
    )


def _apply_cooldown(
    score: float,
    event_ticker: str,
    now: datetime,
    recent_events: dict[str, datetime],
    cooldown_hours: float,
    cooldown_penalty: float,
) -> float:
    """Reduce score for recently-selected events with linear decay."""
    if event_ticker not in recent_events:
        return score
    elapsed_hours = (now - recent_events[event_ticker]).total_seconds() / 3600
    if elapsed_hours >= cooldown_hours:
        return score
    # Linear decay: full penalty at elapsed=0, no penalty at elapsed=cooldown_hours
    decay = 1 - (elapsed_hours / cooldown_hours)  # 1.0 → 0.0
    penalty = 1 - (1 - cooldown_penalty) * decay   # penalty → 1.0
    return score * penalty


def _apply_jitter(score: float, jitter_pct: float) -> float:
    """Add random ± jitter_pct to a score."""
    return score * (1 + random.uniform(-jitter_pct, jitter_pct))


def _cap_markets(events: list[Event], max_markets: int) -> list[Event]:
    """Limit total markets across all events to max_markets."""
    result: list[Event] = []
    count = 0
    for event in events:
        if count >= max_markets:
            break
        remaining = max_markets - count
        if len(event.markets) <= remaining:
            result.append(event)
            count += len(event.markets)
        else:
            # Take a subset of markets from this event
            capped_event = event.model_copy(update={"markets": event.markets[:remaining]})
            result.append(capped_event)
            count += remaining
    return result
