"""Claude analysis engine — sends events to Claude for probability estimation."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import anthropic
import structlog

from kalshi_bot.analysis.cache import TTLCache
from kalshi_bot.analysis.prompts import (
    DAY_TRADER_PROMPT_ADDENDUM,
    FEASIBILITY_SYSTEM_PROMPT,
    FEASIBILITY_TOOL_SCHEMA,
    SYSTEM_PROMPT,
    build_event_prompt,
    build_feasibility_prompt,
)
from kalshi_bot.config import ClaudeConfig
from kalshi_bot.models import Event, EventAnalysis, Market, MarketAnalysis

logger = structlog.get_logger(__name__)

# Explicit inline schema — avoids $defs/$ref issues and makes market_analyses required
_TOOL_SCHEMA = {
    "type": "object",
    "required": ["event_ticker", "event_summary", "market_analyses"],
    "properties": {
        "event_ticker": {"type": "string", "description": "The event ticker"},
        "event_summary": {"type": "string", "description": "Brief summary of the event context"},
        "market_analyses": {
            "type": "array",
            "description": "One analysis per market — you MUST include an entry for every market listed",
            "items": {
                "type": "object",
                "required": ["ticker", "predicted_probability", "confidence", "reasoning"],
                "properties": {
                    "ticker": {"type": "string", "description": "The market ticker"},
                    "predicted_probability": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "description": "Estimated probability the YES outcome occurs (0.0-1.0)",
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "description": "How confident you are in this estimate (0.0-1.0)",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "Brief explanation of the probability estimate",
                    },
                    "key_factors": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Key factors driving the estimate",
                    },
                    "suggested_side": {
                        "type": "string",
                        "enum": ["yes", "no"],
                        "description": "Which side looks favorable, if any",
                    },
                },
            },
        },
        "correlations": {
            "type": "string",
            "description": "Notable correlations between markets in this event",
        },
    },
}


class AnalysisEngine:
    def __init__(self, config: ClaudeConfig, day_trader_mode: bool = False) -> None:
        self.config = config
        self.day_trader_mode = day_trader_mode
        self.client = anthropic.AsyncAnthropic(api_key=config.api_key)
        self.cache = TTLCache(ttl_seconds=config.cache_ttl_minutes * 60)
        self._semaphore = asyncio.Semaphore(config.max_concurrent)

    async def analyze_events(self, events: list[Event]) -> list[EventAnalysis]:
        """Analyze multiple events concurrently, bounded by semaphore."""
        tasks = [self._analyze_event(event) for event in events]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        analyses: list[EventAnalysis] = []
        for event, result in zip(events, results):
            if isinstance(result, Exception):
                logger.error("analysis_failed", event_ticker=event.event_ticker, error=str(result))
            else:
                analyses.append(result)
        return analyses

    async def analyze_events_two_pass(self, events: list[Event]) -> list[EventAnalysis]:
        """Two-pass analysis: feasibility screen first, then full analysis on feasible markets only."""
        analyses: list[EventAnalysis] = []

        for event in events:
            # Check cache first
            cached = self.cache.get(event.event_ticker)
            if cached is not None:
                logger.debug("analysis_cache_hit", event_ticker=event.event_ticker)
                analyses.append(cached)
                continue

            # Pass 1: Feasibility check
            try:
                feasibility = await self._feasibility_check(event)
            except Exception as e:
                logger.error(
                    "feasibility_check_failed",
                    event_ticker=event.event_ticker,
                    error=str(e),
                )
                # On failure, treat all markets as feasible
                feasibility = {m.ticker: True for m in event.markets}

            feasible_markets = [m for m in event.markets if feasibility.get(m.ticker, True)]
            infeasible_tickers = [m.ticker for m in event.markets if not feasibility.get(m.ticker, True)]

            logger.info(
                "feasibility_check_complete",
                event_ticker=event.event_ticker,
                feasible=len(feasible_markets),
                infeasible=len(infeasible_tickers),
                infeasible_tickers=infeasible_tickers,
            )

            if not feasible_markets:
                # All markets infeasible — create stub analysis
                analysis = EventAnalysis(
                    event_ticker=event.event_ticker,
                    event_summary="All markets deemed infeasible for the timeframe",
                    market_analyses=[
                        MarketAnalysis(
                            ticker=m.ticker,
                            predicted_probability=0.5,
                            confidence=0.0,
                            reasoning=f"Infeasible: {feasibility.get(m.ticker, 'unknown')}",
                        )
                        for m in event.markets
                    ],
                )
                self.cache.set(event.event_ticker, analysis)
                analyses.append(analysis)
                continue

            # Pass 2: Full analysis on feasible markets only
            try:
                feasible_event = event.model_copy(update={"markets": feasible_markets})
                async with self._semaphore:
                    analysis = await self._call_claude(feasible_event)

                # Add stubs for infeasible markets
                for ticker in infeasible_tickers:
                    analysis.market_analyses.append(
                        MarketAnalysis(
                            ticker=ticker,
                            predicted_probability=0.5,
                            confidence=0.0,
                            reasoning="Skipped — outcome not feasible in timeframe",
                        )
                    )

                analysis.event_ticker = event.event_ticker
                self.cache.set(event.event_ticker, analysis)
                analyses.append(analysis)
            except Exception as e:
                logger.error("analysis_failed", event_ticker=event.event_ticker, error=str(e))

        return analyses

    async def _feasibility_check(self, event: Event) -> dict[str, bool]:
        """Quick feasibility screen — returns {ticker: feasible_bool} for each market."""
        now = datetime.now(timezone.utc)
        hours_to_close: dict[str, float] = {}
        market_descs: list[dict[str, str]] = []

        for m in event.markets:
            market_descs.append({"ticker": m.ticker, "title": m.title})
            if m.close_time is not None:
                close = m.close_time if m.close_time.tzinfo else m.close_time.replace(tzinfo=timezone.utc)
                hours_to_close[m.ticker] = max(0, (close - now).total_seconds() / 3600)

        user_prompt = build_feasibility_prompt(
            event.title, event.subtitle, market_descs, hours_to_close,
            current_utc=now,
        )

        logger.info(
            "feasibility_check_start",
            event_ticker=event.event_ticker,
            market_count=len(event.markets),
        )

        async with self._semaphore:
            response = await self.client.messages.create(
                model=self.config.model,
                max_tokens=1024,
                temperature=0.0,
                system=FEASIBILITY_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
                tools=[
                    {
                        "name": "submit_feasibility",
                        "description": "Submit feasibility assessments for each market.",
                        "input_schema": FEASIBILITY_TOOL_SCHEMA,
                    }
                ],
                tool_choice={"type": "tool", "name": "submit_feasibility"},
            )

        # Parse response
        result: dict[str, bool] = {}
        for block in response.content:
            if block.type == "tool_use" and block.name == "submit_feasibility":
                for assessment in block.input.get("assessments", []):
                    ticker = assessment.get("ticker", "")
                    feasible = assessment.get("feasible", True)
                    reason = assessment.get("reason", "")
                    result[ticker] = feasible
                    if not feasible:
                        logger.info(
                            "market_infeasible",
                            ticker=ticker,
                            reason=reason,
                        )
                return result

        # Fallback — treat all as feasible if parsing fails
        logger.warning("feasibility_no_tool_call", event_ticker=event.event_ticker)
        return {m.ticker: True for m in event.markets}

    async def check_positions_feasibility(
        self, events: list[Event],
    ) -> dict[str, bool]:
        """Run feasibility check on events built from held positions.

        Returns {ticker: feasible_bool} for every market across all events.
        Uses a short-lived cache key so we don't re-check the same positions
        every monitoring cycle.
        """
        result: dict[str, bool] = {}

        for event in events:
            cache_key = f"_feas_{event.event_ticker}"
            cached = self.cache.get(cache_key)
            if cached is not None:
                result.update(cached)
                continue

            try:
                feas = await self._feasibility_check(event)
            except Exception as e:
                logger.error(
                    "position_feasibility_check_failed",
                    event_ticker=event.event_ticker,
                    error=str(e),
                )
                # On error, assume feasible (don't panic-sell)
                feas = {m.ticker: True for m in event.markets}

            self.cache.set(cache_key, feas)
            result.update(feas)

            infeasible = [t for t, f in feas.items() if not f]
            if infeasible:
                logger.warning(
                    "positions_infeasible",
                    event_ticker=event.event_ticker,
                    infeasible_tickers=infeasible,
                )

        return result

    async def _analyze_event(self, event: Event) -> EventAnalysis:
        """Analyze a single event, using cache if available."""
        cached = self.cache.get(event.event_ticker)
        if cached is not None:
            logger.debug("analysis_cache_hit", event_ticker=event.event_ticker)
            return cached

        async with self._semaphore:
            analysis = await self._call_claude(event)
            self.cache.set(event.event_ticker, analysis)
            return analysis

    async def _call_claude(self, event: Event) -> EventAnalysis:
        """Send event to Claude and parse structured response."""
        # Build market descriptions WITHOUT prices (anti-anchoring)
        now = datetime.now(timezone.utc)
        market_descs = [
            {"ticker": m.ticker, "title": m.title}
            for m in event.markets
        ]
        user_prompt = build_event_prompt(event.title, event.subtitle, market_descs, current_utc=now)

        system_prompt = SYSTEM_PROMPT
        if self.day_trader_mode:
            system_prompt += DAY_TRADER_PROMPT_ADDENDUM

        logger.info(
            "claude_analysis_start",
            event_ticker=event.event_ticker,
            market_count=len(event.markets),
        )

        response = await self.client.messages.create(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            tools=[
                {
                    "name": "submit_analysis",
                    "description": (
                        "Submit your probability analysis. You MUST include one entry "
                        "in market_analyses for EVERY market listed in the prompt."
                    ),
                    "input_schema": _TOOL_SCHEMA,
                }
            ],
            tool_choice={"type": "tool", "name": "submit_analysis"},
        )

        # Extract the tool call result
        for block in response.content:
            if block.type == "tool_use" and block.name == "submit_analysis":
                raw_input = block.input
                logger.info(
                    "claude_raw_response",
                    event_ticker=event.event_ticker,
                    market_analyses_count=len(raw_input.get("market_analyses", [])) if isinstance(raw_input, dict) else 0,
                )
                analysis = EventAnalysis.model_validate(raw_input)
                analysis.event_ticker = event.event_ticker
                logger.info(
                    "claude_analysis_complete",
                    event_ticker=event.event_ticker,
                    markets_analyzed=len(analysis.market_analyses),
                )
                return analysis

        # Fallback: if no tool call, return empty analysis
        logger.warning("claude_no_tool_call", event_ticker=event.event_ticker)
        return EventAnalysis(
            event_ticker=event.event_ticker,
            event_summary="Analysis failed — no structured output returned",
            market_analyses=[
                MarketAnalysis(
                    ticker=m.ticker,
                    predicted_probability=0.5,
                    confidence=0.0,
                    reasoning="Failed to get structured analysis from Claude",
                )
                for m in event.markets
            ],
        )
