"""Edge computation and Kelly criterion position sizing for binary contracts."""

from __future__ import annotations

import math


def calculate_edge(predicted_prob: float, market_price_cents: int, side_yes: bool) -> float:
    """Calculate expected edge for a side of a binary contract.

    Edge = predicted_probability - implied_market_probability for YES,
    or (1 - predicted_probability) - (1 - implied_market_probability) for NO.

    Since NO implied prob = 1 - YES implied prob, the NO edge simplifies to
    -(predicted_prob - market_prob), i.e., the negative of YES edge.

    Args:
        predicted_prob: Claude's estimated probability of YES outcome (0-1).
        market_price_cents: Current market price in cents (1-99) for YES.
        side_yes: True if calculating edge for YES side.

    Returns:
        Edge as a fraction (e.g., 0.08 means 8% edge). Positive = favorable.
    """
    market_prob = market_price_cents / 100.0
    yes_edge = predicted_prob - market_prob
    return yes_edge if side_yes else -yes_edge


def kelly_fraction_binary(prob: float, price_cents: int, side_yes: bool) -> float:
    """Calculate full Kelly fraction for a binary contract.

    For a binary contract at price p with estimated probability q:
    - YES side: Kelly = (q * (100-p) - (1-q) * p) / (100-p) * p ... simplified:
      Kelly = (q - p/100) / (1 - p/100)  when edge is positive
    - NO side: Kelly = ((1-q) - (1-p/100)) / (p/100) when edge is positive

    Simplified: Kelly = edge / odds_against

    Args:
        prob: Estimated probability of YES outcome.
        price_cents: Market YES price in cents.
        side_yes: Whether betting YES.

    Returns:
        Kelly fraction (0-1). Returns 0 if edge is non-positive.
    """
    if price_cents <= 0 or price_cents >= 100:
        return 0.0

    if side_yes:
        # Buying YES at price p: risk p, gain (100-p) if YES
        win_prob = prob
        payout_ratio = (100 - price_cents) / price_cents  # reward-to-risk
    else:
        # Buying NO at price (100-p): risk (100-p), gain p if NO
        win_prob = 1.0 - prob
        no_price = 100 - price_cents
        payout_ratio = price_cents / no_price  # reward-to-risk

    # Kelly formula: f = (p * b - q) / b = p - q/b
    # where p = win probability, q = loss probability, b = payout ratio
    if payout_ratio <= 0:
        return 0.0

    kelly = (win_prob * payout_ratio - (1 - win_prob)) / payout_ratio
    return max(0.0, kelly)


def kelly_size(
    prob: float,
    price_cents: int,
    side_yes: bool,
    bankroll_cents: int,
    fraction: float = 0.25,
    max_order_cents: int = 2000,
) -> int:
    """Calculate position size using fractional Kelly.

    Args:
        prob: Estimated probability of YES outcome.
        price_cents: Market YES price in cents.
        side_yes: Whether betting YES.
        bankroll_cents: Available capital in cents.
        fraction: Kelly fraction to use (0.25 = quarter-Kelly).
        max_order_cents: Maximum order size in cents.

    Returns:
        Number of contracts to buy.
    """
    f = kelly_fraction_binary(prob, price_cents, side_yes)
    if f <= 0:
        return 0

    # Apply fractional Kelly
    bet_fraction = f * fraction
    bet_cents = int(bankroll_cents * bet_fraction)

    # Determine cost per contract
    cost_per_contract = price_cents if side_yes else (100 - price_cents)
    if cost_per_contract <= 0:
        return 0

    # Cap at max order size
    bet_cents = min(bet_cents, max_order_cents)

    contracts = bet_cents // cost_per_contract
    return max(0, contracts)
