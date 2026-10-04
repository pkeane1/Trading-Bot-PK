"""Tests for market scanner and filters."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from kalshi_bot.config import ScannerConfig
from kalshi_bot.models import Event, Market
from kalshi_bot.scanner.filters import (
    passes_all_filters,
    passes_category,
    passes_open_interest,
    passes_spread,
    passes_time_to_expiry,
    passes_volume,
)
from kalshi_bot.scanner.market_scanner import (
    _apply_cooldown,
    _apply_jitter,
    _event_score,
)


@pytest.fixture
def config() -> ScannerConfig:
    return ScannerConfig(
        min_volume=500,
        min_open_interest=200,
        max_spread_cents=10,
        min_hours_to_expiry=2,
    )


def _make_market(**overrides) -> Market:
    defaults = {
        "ticker": "TST",
        "event_ticker": "EVT",
        "title": "Test",
        "status": "open",
        "yes_ask": 60,
        "yes_bid": 55,
        "volume": 500,
        "open_interest": 200,
        "close_time": datetime.now(timezone.utc) + timedelta(days=7),
    }
    defaults.update(overrides)
    return Market(**defaults)


class TestVolumeFilter:
    def test_passes_above_minimum(self, config):
        assert passes_volume(_make_market(volume=1000), config)

    def test_fails_below_minimum(self, config):
        assert not passes_volume(_make_market(volume=100), config)

    def test_passes_at_exactly_minimum(self, config):
        assert passes_volume(_make_market(volume=500), config)


class TestOpenInterestFilter:
    def test_passes_above_minimum(self, config):
        assert passes_open_interest(_make_market(open_interest=200), config)

    def test_fails_below_minimum(self, config):
        assert not passes_open_interest(_make_market(open_interest=10), config)


class TestSpreadFilter:
    def test_passes_tight_spread(self, config):
        assert passes_spread(_make_market(yes_ask=55, yes_bid=50), config)

    def test_fails_wide_spread(self, config):
        assert not passes_spread(_make_market(yes_ask=65, yes_bid=50), config)

    def test_passes_no_spread_data(self, config):
        assert passes_spread(_make_market(yes_ask=0, yes_bid=0), config)


class TestTimeToExpiryFilter:
    def test_passes_far_expiry(self, config):
        m = _make_market(close_time=datetime.now(timezone.utc) + timedelta(days=7))
        assert passes_time_to_expiry(m, config)

    def test_fails_imminent_expiry(self, config):
        m = _make_market(close_time=datetime.now(timezone.utc) + timedelta(minutes=30))
        assert not passes_time_to_expiry(m, config)

    def test_passes_no_expiry(self, config):
        m = _make_market(close_time=None)
        assert passes_time_to_expiry(m, config)


class TestCategoryFilter:
    def test_passes_when_no_categories_set(self, config):
        assert passes_category(_make_market(category="sports"), config)

    def test_passes_matching_category(self):
        cfg = ScannerConfig(categories=["sports", "politics"])
        assert passes_category(_make_market(category="sports"), cfg)

    def test_fails_non_matching_category(self):
        cfg = ScannerConfig(categories=["sports"])
        assert not passes_category(_make_market(category="economics"), cfg)


class TestAllFilters:
    def test_good_market_passes(self, config):
        m = _make_market(volume=1000, open_interest=500, yes_ask=55, yes_bid=50)
        assert passes_all_filters(m, config)

    def test_bad_market_fails(self, config):
        m = _make_market(volume=5, open_interest=5, yes_ask=90, yes_bid=10)
        assert not passes_all_filters(m, config)


# ── Cooldown penalty tests ────────────────────────────────────────────────────


class TestCooldownPenalty:
    def test_no_penalty_when_not_recently_selected(self):
        now = datetime.now(timezone.utc)
        score = _apply_cooldown(100.0, "EVT-A", now, {}, cooldown_hours=4, cooldown_penalty=0.5)
        assert score == 100.0

    def test_full_penalty_just_selected(self):
        now = datetime.now(timezone.utc)
        recent = {"EVT-A": now}
        score = _apply_cooldown(100.0, "EVT-A", now, recent, cooldown_hours=4, cooldown_penalty=0.5)
        assert score == pytest.approx(50.0)

    def test_no_penalty_after_cooldown_expires(self):
        now = datetime.now(timezone.utc)
        recent = {"EVT-A": now - timedelta(hours=5)}
        score = _apply_cooldown(100.0, "EVT-A", now, recent, cooldown_hours=4, cooldown_penalty=0.5)
        assert score == 100.0

    def test_partial_penalty_midway(self):
        now = datetime.now(timezone.utc)
        recent = {"EVT-A": now - timedelta(hours=2)}
        score = _apply_cooldown(100.0, "EVT-A", now, recent, cooldown_hours=4, cooldown_penalty=0.5)
        # 2 hours into a 4-hour window: decay = 0.5, penalty = 1 - 0.5*0.5 = 0.75
        assert score == pytest.approx(75.0)

    def test_penalty_decays_linearly(self):
        now = datetime.now(timezone.utc)
        recent = {"EVT-A": now - timedelta(hours=3)}
        score = _apply_cooldown(100.0, "EVT-A", now, recent, cooldown_hours=4, cooldown_penalty=0.5)
        # 3 hours into a 4-hour window: decay = 0.25, penalty = 1 - 0.5*0.25 = 0.875
        assert score == pytest.approx(87.5)


# ── Score jitter tests ────────────────────────────────────────────────────────


class TestScoreJitter:
    def test_jitter_within_expected_range(self):
        base_score = 100.0
        jitter_pct = 0.1
        for _ in range(200):
            jittered = _apply_jitter(base_score, jitter_pct)
            assert 90.0 <= jittered <= 110.0

    def test_zero_jitter_returns_exact_score(self):
        assert _apply_jitter(100.0, 0.0) == 100.0

    def test_jitter_produces_variation(self):
        scores = {_apply_jitter(100.0, 0.1) for _ in range(50)}
        assert len(scores) > 1
