"""Prompt templates for Claude analysis — deliberately omits market prices to prevent anchoring."""

from __future__ import annotations

from datetime import datetime


SYSTEM_PROMPT = """\
You are an expert calibrated forecaster specializing in prediction markets. Your job is to \
estimate the probability of real-world outcomes based on available information.

Guidelines:
- Use reference class forecasting: identify similar historical events and their base rates.
- Consider multiple perspectives and potential surprises.
- Be well-calibrated: when you say 70%, outcomes should happen ~70% of the time.
- Express genuine uncertainty — don't anchor to round numbers or extremes without justification.
- Probabilities near 0% or 100% require extraordinary confidence.
- Think about what information would change your estimate.
- Rate your confidence in your estimate (how much relevant information you have, how \
predictable this type of event is).

IMPORTANT: You are estimating probabilities based ONLY on the event descriptions and your \
knowledge. You do NOT have access to current market prices, and you should NOT try to guess \
what the market thinks. Form your own independent estimate.\
"""


DAY_TRADER_PROMPT_ADDENDUM = """\

ADDITIONAL CONTEXT — DAY TRADER MODE:
You are analyzing markets that will resolve SOON (within hours). Prioritize:
- Imminent catalysts: scheduled announcements, data releases, deadlines happening today.
- Momentum and sentiment: how fast is opinion shifting right now?
- Time decay: markets near expiry tend to converge to extremes (near 0% or 100%).
- Information asymmetry: what does the crowd likely NOT know yet?
- Be decisive: small edges matter when the holding period is short.
Do NOT over-weight long-term base rates for events resolving in hours.\
"""


FEASIBILITY_SYSTEM_PROMPT = """\
You are a quick feasibility screener for prediction markets. Your ONLY job is to determine \
whether each market's outcome is realistically achievable within the given timeframe.

Rules:
- If the outcome requires actions that CANNOT physically happen in the time remaining \
(e.g., legislation passing overnight, multi-week processes completing in hours), mark it NOT feasible.
- If the outcome depends on a scheduled event (data release, game, vote) happening within \
the timeframe, mark it feasible.
- If the outcome is about a measurable threshold (temperature, price level, score) that \
could be reached in the timeframe, mark it feasible.
- When in doubt, mark it feasible — better to analyze an extra market than miss an opportunity.
Be fast and decisive. Do not over-explain.\
"""

FEASIBILITY_TOOL_SCHEMA = {
    "type": "object",
    "required": ["assessments"],
    "properties": {
        "assessments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["ticker", "feasible", "reason"],
                "properties": {
                    "ticker": {"type": "string"},
                    "feasible": {
                        "type": "boolean",
                        "description": "True if the outcome could realistically occur in the timeframe",
                    },
                    "reason": {"type": "string", "description": "One sentence explanation"},
                },
            },
        }
    },
}


def build_feasibility_prompt(
    event_title: str,
    event_subtitle: str,
    markets: list[dict[str, str]],
    hours_to_close: dict[str, float],
    current_utc: datetime | None = None,
) -> str:
    """Build prompt for feasibility screening."""
    markets_text = "\n".join(
        f"  {i+1}. [{m['ticker']}] {m['title']} "
        f"(resolves in ~{hours_to_close.get(m['ticker'], 0):.0f}h)"
        for i, m in enumerate(markets)
    )
    time_line = ""
    if current_utc is not None:
        time_line = f"\nCURRENT TIME (UTC): {current_utc.strftime('%Y-%m-%d %H:%M')}\n"
    return f"""\
For each market below, determine: is this outcome realistically achievable in the time remaining?
{time_line}
EVENT: {event_title}
{f"CONTEXT: {event_subtitle}" if event_subtitle else ""}

MARKETS:
{markets_text}

For each market, respond with whether it is feasible to trade on (the outcome could realistically \
happen or not happen in the remaining time) and a brief reason.\
"""


def build_event_prompt(
    event_title: str,
    event_subtitle: str,
    markets: list[dict[str, str]],
    current_utc: datetime | None = None,
) -> str:
    """Build the user prompt for analyzing an event with its markets.

    Args:
        event_title: The event title.
        event_subtitle: Additional event context.
        markets: List of dicts with 'ticker' and 'title' keys (NO prices).
        current_utc: Current UTC timestamp to ground temporal reasoning.
    """
    markets_text = "\n".join(
        f"  {i+1}. [{m['ticker']}] {m['title']}" for i, m in enumerate(markets)
    )

    time_line = ""
    if current_utc is not None:
        time_line = f"\nCURRENT TIME (UTC): {current_utc.strftime('%Y-%m-%d %H:%M')}\n"

    return f"""\
Analyze the following prediction market event and estimate probabilities for each market.
{time_line}
EVENT: {event_title}
{f"CONTEXT: {event_subtitle}" if event_subtitle else ""}

MARKETS:
{markets_text}

For each market, provide:
1. Your estimated probability that the YES outcome occurs (0.0 to 1.0)
2. Your confidence in this estimate (0.0 to 1.0)
3. Brief reasoning explaining your estimate
4. Key factors that drive your prediction
5. Which side (yes/no) looks more favorable, if any

Think step by step. Consider base rates, recent developments, and potential surprises.\
"""
