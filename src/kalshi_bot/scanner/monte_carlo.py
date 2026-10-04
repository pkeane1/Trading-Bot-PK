"""Monte Carlo simulation for market selection parameter optimization.

Generates synthetic markets, simulates outcomes, and tests thousands of
scoring/filter parameter combinations to find optimal settings.

Usage:
    python -m kalshi_bot.scanner.monte_carlo          # default 5000 sims
    python -m kalshi_bot.scanner.monte_carlo --sims 10000 --seed 42
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from dataclasses import dataclass, field
from typing import NamedTuple

# --Synthetic Market Generation -----------------------------------------------


@dataclass
class SyntheticMarket:
    """A fake market with known outcome for backtesting scoring/filters."""

    ticker: str
    event_ticker: str
    volume: int
    open_interest: int
    spread: int  # cents
    midpoint: int  # cents (YES price)
    hours_to_expiry: float
    true_probability: float  # hidden ground truth -- what actually resolves
    outcome: bool = False  # resolved after simulation


class ScoringWeights(NamedTuple):
    volume_w: float
    oi_w: float
    spread_w: float
    price_centrality_w: float  # new: bonus for prices near 50c


class FilterThresholds(NamedTuple):
    min_volume: int
    min_open_interest: int
    max_spread_cents: int
    min_hours_to_expiry: float


@dataclass
class SimResult:
    """Result of a single simulation run with a given parameter set."""

    weights: ScoringWeights
    filters: FilterThresholds
    total_trades: int = 0
    winning_trades: int = 0
    total_pnl_cents: int = 0
    max_drawdown_cents: int = 0
    markets_passed_filter: int = 0

    @property
    def win_rate(self) -> float:
        return self.winning_trades / self.total_trades if self.total_trades > 0 else 0.0

    @property
    def avg_pnl_cents(self) -> float:
        return self.total_pnl_cents / self.total_trades if self.total_trades > 0 else 0.0

    @property
    def sharpe_approx(self) -> float:
        """Rough Sharpe-like ratio: mean P&L / assumed volatility."""
        if self.total_trades < 2:
            return 0.0
        # Use win_rate and avg_pnl as a proxy
        avg = self.avg_pnl_cents
        # Estimate stddev from binary outcomes
        if self.win_rate <= 0 or self.win_rate >= 1:
            return avg / 100 if avg != 0 else 0.0
        stddev = math.sqrt(self.win_rate * (1 - self.win_rate)) * 100
        return avg / stddev if stddev > 0 else 0.0


@dataclass
class MonteCarloReport:
    """Aggregated results across all simulations."""

    best_by_pnl: SimResult | None = None
    best_by_sharpe: SimResult | None = None
    best_by_winrate: SimResult | None = None
    current_baseline: SimResult | None = None
    all_results: list[SimResult] = field(default_factory=list)
    total_sims: int = 0


# --Market Generation ---------------------------------------------------------


def generate_market_universe(n_markets: int = 200, rng: random.Random | None = None) -> list[SyntheticMarket]:
    """Generate a realistic universe of synthetic Kalshi markets.

    Distributions are modeled after typical Kalshi market characteristics:
    - Volume follows a power law (few high-volume, many low-volume)
    - Spreads are generally 1-20 cents
    - Prices cluster slightly toward extremes (many near 10c or 90c)
    - True probability has noise vs midpoint (market isn't perfectly efficient)
    """
    if rng is None:
        rng = random.Random()

    markets: list[SyntheticMarket] = []
    for i in range(n_markets):
        # Volume: power law distribution (lots of thin markets, few thick ones)
        volume = int(rng.paretovariate(1.5) * 100)
        volume = min(volume, 50000)

        # Open interest: correlated with volume but noisier
        oi = int(volume * rng.uniform(0.2, 1.5))
        oi = max(0, oi)

        # Spread: most are tight, some are wide
        spread = max(1, int(rng.expovariate(0.3)))
        spread = min(spread, 30)

        # Midpoint: slight U-shape -- markets often sit near extremes
        if rng.random() < 0.3:
            # ~30% near extremes
            midpoint = rng.choice([
                rng.randint(3, 15),
                rng.randint(85, 97),
            ])
        else:
            # ~70% roughly uniform
            midpoint = rng.randint(10, 90)

        # Hours to expiry
        hours_to_expiry = rng.expovariate(1 / 48)  # mean ~48 hours
        hours_to_expiry = max(0.5, min(hours_to_expiry, 720))

        # True probability: market midpoint + noise (market is ~80% efficient)
        noise = rng.gauss(0, 0.08)
        true_prob = (midpoint / 100.0) + noise
        true_prob = max(0.01, min(0.99, true_prob))

        markets.append(SyntheticMarket(
            ticker=f"SYN-{i:04d}",
            event_ticker=f"EVT-{i // 3:04d}",
            volume=volume,
            open_interest=oi,
            spread=spread,
            midpoint=midpoint,
            hours_to_expiry=hours_to_expiry,
            true_probability=true_prob,
        ))

    return markets


def resolve_outcomes(markets: list[SyntheticMarket], rng: random.Random | None = None) -> None:
    """Resolve each market's binary outcome based on true_probability."""
    if rng is None:
        rng = random.Random()
    for m in markets:
        m.outcome = rng.random() < m.true_probability


# --Scoring & Filtering ------------------------------------------------------


def score_market(market: SyntheticMarket, weights: ScoringWeights) -> float:
    """Score a market using the given weights."""
    spread_score = max(0, 20 - market.spread)
    # Price centrality: how close to 50c (0 at extremes, 1 at 50c)
    centrality = 1.0 - abs(market.midpoint - 50) / 50.0

    return (
        market.volume * weights.volume_w
        + market.open_interest * weights.oi_w
        + spread_score * weights.spread_w
        + centrality * weights.price_centrality_w
    )


def passes_filters(market: SyntheticMarket, filters: FilterThresholds) -> bool:
    return (
        market.volume >= filters.min_volume
        and market.open_interest >= filters.min_open_interest
        and market.spread <= filters.max_spread_cents
        and market.hours_to_expiry >= filters.min_hours_to_expiry
    )


# --Trade Simulation ---------------------------------------------------------


def simulate_trade(market: SyntheticMarket, kelly_fraction: float = 0.25) -> int:
    """Simulate a trade P&L on a resolved market.

    Assumes we buy the side that looks favorable (midpoint vs 50c heuristic),
    then the market resolves to its true outcome.

    Returns P&L in cents per contract.
    """
    # Simple edge heuristic: if midpoint < 50, market implies NO is more likely,
    # but if true_prob > midpoint/100, there's YES edge (and vice versa).
    market_prob = market.midpoint / 100.0

    # Simulate what a reasonable model would estimate -- true prob + noise
    # (model is better than random but not perfect)
    model_noise = random.gauss(0, 0.05)
    estimated_prob = market.true_probability + model_noise
    estimated_prob = max(0.01, min(0.99, estimated_prob))

    yes_edge = estimated_prob - market_prob
    no_edge = -yes_edge

    if yes_edge > no_edge and yes_edge > 0.02:
        # Buy YES
        cost = market.midpoint
        if market.outcome:
            return 100 - cost  # win
        else:
            return -cost  # lose
    elif no_edge > 0.02:
        # Buy NO
        cost = 100 - market.midpoint
        if not market.outcome:
            return 100 - cost  # win (NO paid off)
        else:
            return -cost  # lose
    else:
        # No edge -- skip
        return 0


# --Monte Carlo Engine --------------------------------------------------------


def _sample_weights(rng: random.Random) -> ScoringWeights:
    """Sample random scoring weights for one simulation."""
    return ScoringWeights(
        volume_w=rng.uniform(0.01, 2.0),
        oi_w=rng.uniform(0.01, 2.0),
        spread_w=rng.uniform(10, 500),
        price_centrality_w=rng.uniform(0, 2000),
    )


def _sample_filters(rng: random.Random) -> FilterThresholds:
    """Sample random filter thresholds for one simulation."""
    return FilterThresholds(
        min_volume=rng.choice([50, 100, 200, 300, 500, 750, 1000]),
        min_open_interest=rng.choice([25, 50, 100, 200, 300, 500]),
        max_spread_cents=rng.choice([5, 8, 10, 12, 15, 20]),
        min_hours_to_expiry=rng.choice([0.5, 1.0, 2.0, 4.0, 8.0]),
    )


# Current production defaults for baseline comparison
CURRENT_WEIGHTS = ScoringWeights(volume_w=0.5, oi_w=0.3, spread_w=100, price_centrality_w=0)
CURRENT_FILTERS = FilterThresholds(min_volume=500, min_open_interest=200, max_spread_cents=10, min_hours_to_expiry=2)


def run_single_sim(
    markets: list[SyntheticMarket],
    weights: ScoringWeights,
    filters: FilterThresholds,
    max_markets: int = 50,
) -> SimResult:
    """Run one simulation: filter -> score -> rank -> trade -> measure."""
    # Filter
    candidates = [m for m in markets if passes_filters(m, filters)]

    # Score and rank
    scored = [(score_market(m, weights), m) for m in candidates]
    scored.sort(key=lambda x: x[0], reverse=True)

    # Take top N
    selected = [m for _, m in scored[:max_markets]]

    # Trade and track P&L
    result = SimResult(
        weights=weights,
        filters=filters,
        markets_passed_filter=len(candidates),
    )

    running_pnl = 0
    peak_pnl = 0
    max_dd = 0

    for market in selected:
        pnl = simulate_trade(market)
        if pnl == 0:
            continue  # skipped (no edge)

        result.total_trades += 1
        result.total_pnl_cents += pnl
        if pnl > 0:
            result.winning_trades += 1

        running_pnl += pnl
        peak_pnl = max(peak_pnl, running_pnl)
        dd = peak_pnl - running_pnl
        max_dd = max(max_dd, dd)

    result.max_drawdown_cents = max_dd
    return result


def run_monte_carlo(
    n_sims: int = 5000,
    n_markets: int = 300,
    max_markets_per_cycle: int = 50,
    seed: int | None = None,
) -> MonteCarloReport:
    """Run the full Monte Carlo optimization.

    For each simulation:
    1. Generate a fresh market universe with random outcomes
    2. Sample random scoring weights and filter thresholds
    3. Run the selection pipeline and simulate trades
    4. Track results

    Then compare all parameter combos against the current production settings.
    """
    rng = random.Random(seed)
    report = MonteCarloReport(total_sims=n_sims)

    best_pnl = float("-inf")
    best_sharpe = float("-inf")
    best_winrate = float("-inf")

    for i in range(n_sims):
        # Fresh market universe each sim (different market conditions)
        markets = generate_market_universe(n_markets, rng)
        resolve_outcomes(markets, rng)

        # Random parameter set
        weights = _sample_weights(rng)
        filters = _sample_filters(rng)

        result = run_single_sim(markets, weights, filters, max_markets_per_cycle)
        report.all_results.append(result)

        if result.total_pnl_cents > best_pnl:
            best_pnl = result.total_pnl_cents
            report.best_by_pnl = result

        if result.sharpe_approx > best_sharpe and result.total_trades >= 5:
            best_sharpe = result.sharpe_approx
            report.best_by_sharpe = result

        if result.win_rate > best_winrate and result.total_trades >= 5:
            best_winrate = result.win_rate
            report.best_by_winrate = result

    # Run baseline with current production params (averaged over many universes)
    baseline_pnl = 0
    baseline_trades = 0
    baseline_wins = 0
    baseline_dd = 0
    n_baseline = min(500, n_sims)
    baseline_rng = random.Random(seed if seed else 12345)

    for _ in range(n_baseline):
        markets = generate_market_universe(n_markets, baseline_rng)
        resolve_outcomes(markets, baseline_rng)
        result = run_single_sim(markets, CURRENT_WEIGHTS, CURRENT_FILTERS, max_markets_per_cycle)
        baseline_pnl += result.total_pnl_cents
        baseline_trades += result.total_trades
        baseline_wins += result.winning_trades
        baseline_dd = max(baseline_dd, result.max_drawdown_cents)

    report.current_baseline = SimResult(
        weights=CURRENT_WEIGHTS,
        filters=CURRENT_FILTERS,
        total_trades=baseline_trades,
        winning_trades=baseline_wins,
        total_pnl_cents=baseline_pnl // n_baseline,
        max_drawdown_cents=baseline_dd,
    )

    return report


# --Reporting -----------------------------------------------------------------


def format_weights(w: ScoringWeights) -> str:
    return (
        f"volume={w.volume_w:.3f}, oi={w.oi_w:.3f}, "
        f"spread={w.spread_w:.0f}, centrality={w.price_centrality_w:.0f}"
    )


def format_filters(f: FilterThresholds) -> str:
    return (
        f"min_vol={f.min_volume}, min_oi={f.min_open_interest}, "
        f"max_spread={f.max_spread_cents}c, min_hours={f.min_hours_to_expiry}"
    )


def print_report(report: MonteCarloReport) -> None:
    print("\n" + "=" * 78)
    print(f"  MONTE CARLO MARKET SELECTION OPTIMIZATION -- {report.total_sims:,} simulations")
    print("=" * 78)

    # Current baseline
    b = report.current_baseline
    if b:
        print("\n--CURRENT PRODUCTION BASELINE (averaged over 500 universes)--")
        print(f"  Weights:  {format_weights(b.weights)}")
        print(f"  Filters:  {format_filters(b.filters)}")
        print(f"  Avg P&L:  {b.total_pnl_cents:+,}c  |  Win rate: {b.win_rate:.1%}")
        print(f"  Trades:   {b.total_trades}  |  Max DD: {b.max_drawdown_cents:,}c")
        print(f"  Sharpe:   {b.sharpe_approx:.3f}")

    # Best by total P&L
    r = report.best_by_pnl
    if r:
        print("\n--BEST BY TOTAL P&L--")
        print(f"  Weights:  {format_weights(r.weights)}")
        print(f"  Filters:  {format_filters(r.filters)}")
        print(f"  P&L:      {r.total_pnl_cents:+,}c  |  Win rate: {r.win_rate:.1%}")
        print(f"  Trades:   {r.total_trades}  |  Max DD: {r.max_drawdown_cents:,}c")
        if b:
            delta = r.total_pnl_cents - b.total_pnl_cents
            print(f"  vs base:  {delta:+,}c ({delta / max(1, abs(b.total_pnl_cents)) * 100:+.0f}%)")

    # Best by Sharpe
    r = report.best_by_sharpe
    if r:
        print("\n--BEST BY SHARPE RATIO--")
        print(f"  Weights:  {format_weights(r.weights)}")
        print(f"  Filters:  {format_filters(r.filters)}")
        print(f"  Sharpe:   {r.sharpe_approx:.3f}  |  Win rate: {r.win_rate:.1%}")
        print(f"  P&L:      {r.total_pnl_cents:+,}c  |  Trades: {r.total_trades}")

    # Best by win rate
    r = report.best_by_winrate
    if r:
        print("\n--BEST BY WIN RATE--")
        print(f"  Weights:  {format_weights(r.weights)}")
        print(f"  Filters:  {format_filters(r.filters)}")
        print(f"  Win rate: {r.win_rate:.1%}  |  P&L: {r.total_pnl_cents:+,}c")
        print(f"  Trades:   {r.total_trades}  |  Sharpe: {r.sharpe_approx:.3f}")

    # Distribution stats
    pnls = [r.total_pnl_cents for r in report.all_results if r.total_trades > 0]
    if pnls:
        pnls.sort()
        n = len(pnls)
        print("\n--P&L DISTRIBUTION ACROSS ALL SIMS--")
        print(f"  Sims with trades: {n:,} / {report.total_sims:,}")
        print(f"  Mean P&L:    {sum(pnls) / n:+,.0f}c")
        print(f"  Median P&L:  {pnls[n // 2]:+,}c")
        print(f"  P5 / P95:    {pnls[int(n * 0.05)]:+,}c / {pnls[int(n * 0.95)]:+,}c")
        print(f"  Min / Max:   {pnls[0]:+,}c / {pnls[-1]:+,}c")
        profitable = sum(1 for p in pnls if p > 0)
        print(f"  Profitable:  {profitable:,} ({profitable / n:.1%})")

    # Top-10 parameter insights (by Sharpe)
    valid = [r for r in report.all_results if r.total_trades >= 5]
    if valid:
        top10 = sorted(valid, key=lambda r: r.sharpe_approx, reverse=True)[:10]
        print("\n--TOP 10 PARAMETER SETS BY SHARPE--")
        print(f"  {'#':>3}  {'Sharpe':>7}  {'P&L':>8}  {'WR':>5}  {'Trades':>6}  Weights -> Filters")
        for i, r in enumerate(top10, 1):
            print(
                f"  {i:3d}  {r.sharpe_approx:7.3f}  {r.total_pnl_cents:+8,}c  "
                f"{r.win_rate:5.1%}  {r.total_trades:6d}  "
                f"v={r.weights.volume_w:.2f} oi={r.weights.oi_w:.2f} "
                f"sp={r.weights.spread_w:.0f} cen={r.weights.price_centrality_w:.0f} -> "
                f"vol>={r.filters.min_volume} oi>={r.filters.min_open_interest} "
                f"sp<={r.filters.max_spread_cents}"
            )

    # Key takeaways
    if valid:
        top50 = sorted(valid, key=lambda r: r.sharpe_approx, reverse=True)[:50]
        avg_vol_w = sum(r.weights.volume_w for r in top50) / len(top50)
        avg_oi_w = sum(r.weights.oi_w for r in top50) / len(top50)
        avg_sp_w = sum(r.weights.spread_w for r in top50) / len(top50)
        avg_cen_w = sum(r.weights.price_centrality_w for r in top50) / len(top50)
        avg_min_vol = sum(r.filters.min_volume for r in top50) / len(top50)
        avg_min_oi = sum(r.filters.min_open_interest for r in top50) / len(top50)
        avg_max_sp = sum(r.filters.max_spread_cents for r in top50) / len(top50)

        print("\n--KEY INSIGHTS (avg of top-50 by Sharpe)--")
        print(f"  Avg volume weight:     {avg_vol_w:.3f}  (current: {CURRENT_WEIGHTS.volume_w})")
        print(f"  Avg OI weight:         {avg_oi_w:.3f}  (current: {CURRENT_WEIGHTS.oi_w})")
        print(f"  Avg spread weight:     {avg_sp_w:.0f}  (current: {CURRENT_WEIGHTS.spread_w})")
        print(f"  Avg centrality weight: {avg_cen_w:.0f}  (current: {CURRENT_WEIGHTS.price_centrality_w})")
        print(f"  Avg min volume:        {avg_min_vol:.0f}  (current: {CURRENT_FILTERS.min_volume})")
        print(f"  Avg min OI:            {avg_min_oi:.0f}  (current: {CURRENT_FILTERS.min_open_interest})")
        print(f"  Avg max spread:        {avg_max_sp:.0f}c  (current: {CURRENT_FILTERS.max_spread_cents}c)")

    print("\n" + "=" * 78)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Monte Carlo market selection optimization")
    parser.add_argument("--sims", type=int, default=5000, help="Number of simulations")
    parser.add_argument("--markets", type=int, default=300, help="Markets per universe")
    parser.add_argument("--max-per-cycle", type=int, default=50, help="Max markets per cycle")
    parser.add_argument("--seed", type=int, default=None, help="Random seed")
    args = parser.parse_args()

    print(f"Running {args.sims:,} Monte Carlo simulations...")
    report = run_monte_carlo(
        n_sims=args.sims,
        n_markets=args.markets,
        max_markets_per_cycle=args.max_per_cycle,
        seed=args.seed,
    )
    print_report(report)


if __name__ == "__main__":
    main()
