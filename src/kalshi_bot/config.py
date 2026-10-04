"""Application configuration using pydantic-settings with YAML file support."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"

KALSHI_BASE_URLS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "production": "https://api.elections.kalshi.com/trade-api/v2",
}

KALSHI_WS_URLS = {
    "demo": "wss://demo-api.kalshi.co/trade-api/ws/v2",
    "production": "wss://api.elections.kalshi.com/trade-api/ws/v2",
}


# ── Nested config sections ─────────────────────────────────────────────────────


class KalshiConfig(BaseSettings):
    api_key: str = ""
    private_key_path: str = ""
    environment: str = "demo"

    @property
    def base_url(self) -> str:
        return KALSHI_BASE_URLS[self.environment]

    @property
    def ws_url(self) -> str:
        return KALSHI_WS_URLS[self.environment]


class ClaudeConfig(BaseSettings):
    api_key: str = ""
    model: str = "claude-sonnet-4-5-20250929"
    max_concurrent: int = 3
    cache_ttl_minutes: int = 30
    max_tokens: int = 2048
    temperature: float = 0.3


class ScannerConfig(BaseSettings):
    min_volume: int = 500
    min_open_interest: int = 200
    max_spread_cents: int = 10
    min_hours_to_expiry: float = 2
    max_hours_to_expiry: float = 0  # 0 = no upper limit (day trader sets 24)
    max_markets_per_cycle: int = 50
    cooldown_hours: float = 4
    cooldown_penalty: float = 0.5
    score_jitter_pct: float = 0.1
    categories: list[str] = Field(default_factory=list)
    day_trader_categories: list[str] = Field(
        default_factory=lambda: ["Economics", "Financials", "Climate and Weather"]
    )


class StrategyConfig(BaseSettings):
    min_edge_threshold: float = 0.05
    kelly_fraction: float = 0.25
    min_confidence: float = 0.5
    min_price_cents: int = 1  # price floor — reject extreme long-shots (day trader sets 5)
    take_profit_percent: float = 30.0
    stop_loss_percent: float = 50.0
    re_eval_exit_edge: float = -0.03
    max_hold_minutes: float = 0  # 0 = disabled (day trader sets 30)


class RiskConfig(BaseSettings):
    max_position_size_cents: int = 5000
    max_total_exposure_cents: int = 50000
    max_daily_loss_cents: int = 10000
    max_positions: int = 20
    max_single_order_cents: int = 2000
    min_order_cents: int = 1000  # minimum $10 per trade
    consecutive_loss_limit: int = 5
    hourly_loss_limit_cents: int = 3000
    api_error_rate_limit: float = 0.5


class ExecutionConfig(BaseSettings):
    stale_order_hours: int = 2
    dry_run: bool = True


class SchedulerConfig(BaseSettings):
    pipeline_interval_minutes: int = 30
    portfolio_monitor_interval_minutes: int = 5


# ── Root config ─────────────────────────────────────────────────────────────────


class AppConfig(BaseSettings):
    kalshi: KalshiConfig = Field(default_factory=KalshiConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    log_level: str = "INFO"
    json_logs: bool = False
    day_trader_mode: bool = False

    @model_validator(mode="after")
    def _apply_day_trader_overrides(self) -> "AppConfig":
        """When day_trader_mode is on, override defaults with aggressive settings.

        Only overrides values that are still at their defaults — explicit user
        config always wins.
        """
        if not self.day_trader_mode:
            return self

        _DAY_TRADER_OVERRIDES: list[tuple[BaseSettings, str, Any, Any]] = [
            # (section, field, default_value, day_trader_value)
            # Exit thresholds
            (self.strategy, "take_profit_percent", 30.0, 4.0),
            (self.strategy, "stop_loss_percent", 50.0, 15.0),
            (self.strategy, "re_eval_exit_edge", -0.03, -0.01),
            (self.strategy, "max_hold_minutes", 0, 30),
            # Speed
            (self.scheduler, "pipeline_interval_minutes", 30, 5),
            (self.scheduler, "portfolio_monitor_interval_minutes", 5, 1),
            (self.claude, "cache_ttl_minutes", 30, 5),
            (self.claude, "max_concurrent", 3, 5),
            (self.execution, "stale_order_hours", 2, 1),
            # Market selection
            (self.scanner, "max_spread_cents", 10, 8),
            (self.scanner, "min_hours_to_expiry", 2, 0.5),
            (self.scanner, "max_hours_to_expiry", 0, 48),
            (self.scanner, "max_markets_per_cycle", 50, 100),
            (self.scanner, "cooldown_hours", 4, 1),
            (self.scanner, "score_jitter_pct", 0.1, 0.15),
            # Sizing / filtering
            (self.strategy, "min_price_cents", 1, 5),
            (self.strategy, "min_edge_threshold", 0.05, 0.02),
            (self.strategy, "kelly_fraction", 0.25, 0.5),
            (self.risk, "min_order_cents", 1000, 500),
        ]

        for section, field, default_val, day_trader_val in _DAY_TRADER_OVERRIDES:
            if getattr(section, field) == default_val:
                object.__setattr__(section, field, day_trader_val)

        # Category restriction — only if user hasn't explicitly set categories
        if not self.scanner.categories:
            object.__setattr__(
                self.scanner, "categories", list(self.scanner.day_trader_categories)
            )

        return self

    @model_validator(mode="before")
    @classmethod
    def _load_yaml_config(cls, values: dict[str, Any]) -> dict[str, Any]:
        """Merge YAML config files into settings before validation."""
        # Load default config
        merged = _load_yaml(_CONFIG_DIR / "default.yaml")

        # Determine environment and load overlay
        env = (
            values.get("kalshi", {}).get("environment")
            or merged.get("kalshi", {}).get("environment")
            or "demo"
        )
        overlay_path = _CONFIG_DIR / f"{env}.yaml"
        if overlay_path.exists():
            overlay = _load_yaml(overlay_path)
            merged = _deep_merge(merged, overlay)

        # Programmatic values (from env vars or direct init) take precedence
        merged = _deep_merge(merged, values)
        return merged


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into base, with override winning on conflicts."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(**overrides: Any) -> AppConfig:
    """Load config from YAML files + environment variables + overrides."""
    # Map standard env vars to nested config structure
    import os

    values: dict[str, Any] = {}
    if api_key := os.environ.get("KALSHI_API_KEY"):
        values.setdefault("kalshi", {})["api_key"] = api_key
    if pk_path := os.environ.get("KALSHI_PRIVATE_KEY_PATH"):
        values.setdefault("kalshi", {})["private_key_path"] = pk_path
    if env := os.environ.get("KALSHI_ENVIRONMENT"):
        values.setdefault("kalshi", {})["environment"] = env
    if claude_key := os.environ.get("ANTHROPIC_API_KEY"):
        values.setdefault("claude", {})["api_key"] = claude_key
    if dry_run := os.environ.get("DRY_RUN"):
        values.setdefault("execution", {})["dry_run"] = dry_run.lower() in ("true", "1", "yes")
    if day_trader := os.environ.get("DAY_TRADER_MODE"):
        values["day_trader_mode"] = day_trader.lower() in ("true", "1", "yes")

    values.update(overrides)
    return AppConfig(**values)
