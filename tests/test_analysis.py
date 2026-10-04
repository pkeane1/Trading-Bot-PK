"""Tests for the analysis engine and prompts."""

from __future__ import annotations

import pytest

from kalshi_bot.analysis.cache import TTLCache
from kalshi_bot.analysis.prompts import SYSTEM_PROMPT, build_event_prompt
from kalshi_bot.models import EventAnalysis, MarketAnalysis


class TestPrompts:
    def test_system_prompt_no_price_mention(self):
        # The system prompt should explicitly tell Claude NOT to use market prices
        assert "NOT" in SYSTEM_PROMPT
        assert "market prices" in SYSTEM_PROMPT.lower() or "market" in SYSTEM_PROMPT.lower()

    def test_build_event_prompt_excludes_prices(self):
        markets = [
            {"ticker": "MKT-1", "title": "Will X happen?"},
            {"ticker": "MKT-2", "title": "Will Y happen?"},
        ]
        prompt = build_event_prompt("Test Event", "Some context", markets)

        # Prompt should contain market titles but no price information
        assert "MKT-1" in prompt
        assert "Will X happen?" in prompt
        assert "Test Event" in prompt
        assert "Some context" in prompt

    def test_build_event_prompt_without_subtitle(self):
        markets = [{"ticker": "MKT-1", "title": "Test?"}]
        prompt = build_event_prompt("Event", "", markets)
        assert "CONTEXT:" not in prompt


class TestTTLCache:
    def test_set_and_get(self):
        cache = TTLCache(ttl_seconds=60)
        cache.set("key1", "value1")
        assert cache.get("key1") == "value1"

    def test_expired_entry_returns_none(self):
        cache = TTLCache(ttl_seconds=0)  # Immediate expiry
        cache.set("key1", "value1")
        # With TTL=0 the entry expires immediately
        import time
        time.sleep(0.01)
        assert cache.get("key1") is None

    def test_invalidate(self):
        cache = TTLCache(ttl_seconds=60)
        cache.set("key1", "value1")
        cache.invalidate("key1")
        assert cache.get("key1") is None

    def test_clear(self):
        cache = TTLCache(ttl_seconds=60)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.clear()
        assert cache.get("a") is None
        assert cache.get("b") is None

    def test_cleanup_removes_expired(self):
        cache = TTLCache(ttl_seconds=0)
        cache.set("a", 1)
        cache.set("b", 2)
        import time
        time.sleep(0.01)
        removed = cache.cleanup()
        assert removed == 2
