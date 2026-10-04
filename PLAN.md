# Kalshi AI Trading Bot — Implementation Plan

## Status: COMPLETE

All 13 implementation steps finished. 24 source files created. 66/66 unit tests passing.

---

## Context

Build a fully-automated Python trading bot for [Kalshi](https://kalshi.com), a CFTC-regulated prediction market exchange. The bot uses Claude (Anthropic) to analyze real-world events, estimate outcome probabilities, and trade contracts when it identifies an edge vs. market prices. Targets all market categories (politics, economics, sports, etc.). Starts on Kalshi's demo/sandbox environment for safe testing before transitioning to production.

---

## Architecture

Pipeline-based design executing on a configurable schedule:

```
SCAN ──> ANALYZE ──> DECIDE ──> EXECUTE ──> MONITOR
  │          │          │          │           │
  ▼          ▼          ▼          ▼           ▼
Market    Claude    Strategy    Order      Portfolio
Scanner   Engine    Engine     Manager     Tracker
```

Key design decisions:
- **Custom Kalshi API client** (not the auto-generated `kalshi-python` SDK) for async support, rate limiting, retries, and WebSocket
- **RSA-PSS signed auth** per Kalshi's spec (3 headers: key, timestamp, signature)
- **Anchoring bias prevention** — Claude sees event descriptions but NOT current market prices, forming independent probability estimates
- **Cents-based math** — all internal monetary values are integer cents to avoid float issues
- **Event-level analysis** — Claude analyzes correlated markets together (e.g., all markets under "Fed June Meeting")

---

## Project Structure

```
Trading-Bot-PK/
├── pyproject.toml
├── .env.example
├── PLAN.md
├── GUIDE.md                         # Step-by-step setup & run guide
├── config/
│   ├── default.yaml                 # Default config values
│   ├── demo.yaml                    # Demo environment overrides
│   └── production.yaml              # Production overrides
├── src/kalshi_bot/
│   ├── __init__.py
│   ├── __main__.py                  # Entry point: python -m kalshi_bot
│   ├── config.py                    # Pydantic-settings config loading
│   ├── models.py                    # Shared Pydantic data models
│   ├── logging_config.py            # structlog setup
│   ├── api/
│   │   ├── auth.py                  # RSA-PSS request signing
│   │   ├── client.py                # Async Kalshi REST client
│   │   ├── websocket.py             # Real-time WebSocket client
│   │   ├── rate_limiter.py          # Token-bucket rate limiter
│   │   └── exceptions.py            # API error types
│   ├── scanner/
│   │   ├── market_scanner.py        # Market discovery + filtering
│   │   └── filters.py              # Filter predicates (volume, spread, etc.)
│   ├── analysis/
│   │   ├── engine.py                # Claude analysis engine
│   │   ├── prompts.py               # Prompt templates (no price anchoring)
│   │   └── cache.py                 # TTL cache for analysis results
│   ├── strategy/
│   │   ├── edge_calculator.py       # Edge computation + Kelly criterion
│   │   └── decision_engine.py       # Analysis → TradeDecision conversion
│   ├── risk/
│   │   ├── manager.py               # Position/exposure/loss limits
│   │   └── circuit_breaker.py       # Emergency stop conditions
│   ├── execution/
│   │   ├── order_manager.py         # Order placement + lifecycle
│   │   └── fill_tracker.py          # Fill monitoring
│   ├── portfolio/
│   │   ├── tracker.py               # Position + P&L tracking
│   │   └── metrics.py               # Win rate, drawdown, performance
│   └── scheduler/
│       └── orchestrator.py          # Main loop + pipeline scheduling
└── tests/
    ├── conftest.py
    ├── test_auth.py
    ├── test_client.py
    ├── test_scanner.py
    ├── test_analysis.py
    ├── test_strategy.py
    ├── test_risk.py
    └── test_execution.py
```

---

## Dependencies

| Package | Purpose |
|---|---|
| `httpx` | Async HTTP client for Kalshi REST API |
| `websockets` | Real-time WebSocket connection |
| `cryptography` | RSA-PSS request signing |
| `anthropic` | Claude API SDK with structured outputs |
| `pydantic` + `pydantic-settings` | Data models, validation, config |
| `pyyaml` | YAML config files |
| `apscheduler` | Cron-like job scheduling |
| `structlog` | Structured JSON logging |
| `tenacity` | Retry logic with exponential backoff |
| `python-dotenv` | .env file loading |
| Dev: `pytest`, `pytest-asyncio`, `respx`, `ruff`, `mypy` | |

---

## Module Details

### 1. Config + Models (`config.py`, `models.py`)
- `AppConfig` root with nested: `KalshiConfig`, `ClaudeConfig`, `RiskConfig`, `ScannerConfig`, `SchedulerConfig`
- Auto-derives base URLs from `environment` ("demo" vs "production")
- `dry_run: bool = True` safety default
- Shared Pydantic models: `Market`, `Event`, `OrderBook`, `MarketAnalysis`, `EventAnalysis`, `TradeDecision`, `Position`, `PortfolioSnapshot`
- `MarketAnalysis` doubles as Claude's structured output schema (predicted_probability, confidence, reasoning, suggested_side, etc.)

### 2. API Auth (`api/auth.py`)
- Loads RSA private key from PEM file
- Signs: `"{timestamp_ms}{METHOD}{path_without_query}"` with RSA-PSS + SHA-256
- Returns 3 headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`

### 3. API Client (`api/client.py`)
- Async context manager wrapping `httpx.AsyncClient`
- Auth header injection on every request
- Dual token-bucket rate limiter (separate read/write budgets matching Kalshi tiers)
- Retry via `tenacity` for 429s and transport errors (5 attempts, exponential backoff + jitter)
- Key methods: `get_markets()`, `get_market()`, `get_orderbook()`, `get_events()`, `create_order()`, `batch_create_orders()`, `cancel_order()`, `get_positions()`, `get_balance()`, `get_fills()`
- Pagination helper: `get_all_markets()` auto-follows cursors

### 4. Market Scanner (`scanner/`)
- Fetches all open markets, applies filter chain: min volume, max spread, min open interest, min time-to-expiry, category
- Groups markets by event, fetches event metadata
- Scores events by attractiveness (volume + spread tightness + open interest)
- Caps output at `max_markets_per_cycle` (default 50)

### 5. Claude Analysis Engine (`analysis/`)
- Sends event context to Claude via tool-use with `EventAnalysis` Pydantic schema as structured output
- System prompt instructs Claude to be a calibrated forecaster using reference class forecasting
- **Critically: prompts omit current market prices** to prevent anchoring — Claude forms independent estimates
- Concurrent analysis bounded by semaphore (default 3)
- 30-minute TTL cache prevents redundant re-analysis

### 6. Strategy Engine (`strategy/`)
- `edge_calculator.py`: `calculate_edge(predicted_prob, market_price)` and `kelly_fraction_binary(prob, price, side_yes)`
- `decision_engine.py`: For each (market, analysis) pair:
  1. Calculate YES edge and NO edge
  2. Pick side with better edge
  3. Filter by `min_edge_threshold` (adjusted by confidence)
  4. Size via quarter-Kelly criterion
  5. Build `TradeDecision` with limit price, quantity, reasoning

### 7. Risk Management (`risk/`)
- Pre-trade checks: single order size, per-ticker position size, total exposure, position count, daily loss limit
- Circuit breaker triggers: consecutive losses, hourly loss rate, API error rate
- Tripped breaker → cancel all orders, halt new trades, require manual reset
- Daily reset at midnight

### 8. Order Execution (`execution/`)
- Risk check → place order via API (or log in dry-run mode)
- Idempotent via `client_order_id` (UUID-based)
- Stale order cleanup (cancel orders resting > 2 hours)
- Emergency `cancel_all_orders()` on shutdown

### 9. Portfolio Tracking (`portfolio/`)
- Fetches balance, positions, fills from Kalshi API
- Computes unrealized P&L, total exposure
- `PerformanceMetrics`: win rate, total P&L, average P&L per trade, max drawdown, trade journal

### 10. Orchestrator (`scheduler/orchestrator.py`)
- Initializes all components, verifies Kalshi connectivity
- Schedules via APScheduler: main pipeline (30 min), portfolio monitor (5 min), daily risk reset
- Graceful shutdown: stop scheduler → cancel orders → close connections → log performance
- Windows-compatible (uses `KeyboardInterrupt` instead of POSIX signals)

---

## Kalshi API Reference

- **Production**: `https://api.elections.kalshi.com/trade-api/v2`
- **Demo**: `https://demo-api.kalshi.co/trade-api/v2`
- **Auth**: RSA-PSS signed headers
- **Rate limits**: Tiered (basic: 20 reads/s, 10 writes/s)
- **Orders**: `POST /portfolio/orders` — fields: ticker, side, action, count, yes_price, type
- **Batch**: Up to 20 orders via `POST /portfolio/orders/batched`
- **WebSocket**: Real-time price, orderbook, and fill streaming

---

## Default Risk Parameters

| Parameter | Default | Description |
|---|---|---|
| Max position size | $50 | Per-ticker exposure cap |
| Max total exposure | $500 | Portfolio-wide cap |
| Max daily loss | $100 | Triggers circuit breaker |
| Max positions | 20 | Concurrent open positions |
| Min edge threshold | 5% | Minimum predicted vs. market gap |
| Kelly fraction | 0.25 | Quarter-Kelly for conservative sizing |
| Max single order | $20 | Per-order cap |

---

## Implementation Order

| Step | Files | Status |
|---|---|---|
| 1 | `config.py`, `models.py`, `logging_config.py` | DONE |
| 2 | `api/auth.py`, `api/exceptions.py`, `api/rate_limiter.py` | DONE |
| 3 | `api/client.py` | DONE |
| 4 | `scanner/filters.py`, `scanner/market_scanner.py` | DONE |
| 5 | `analysis/prompts.py`, `analysis/cache.py`, `analysis/engine.py` | DONE |
| 6 | `strategy/edge_calculator.py`, `strategy/decision_engine.py` | DONE |
| 7 | `risk/manager.py`, `risk/circuit_breaker.py` | DONE |
| 8 | `execution/order_manager.py`, `execution/fill_tracker.py` | DONE |
| 9 | `portfolio/tracker.py`, `portfolio/metrics.py` | DONE |
| 10 | `scheduler/orchestrator.py`, `__main__.py` | DONE |
| 11 | `api/websocket.py` | DONE |
| 12 | Config files, `.env.example`, `pyproject.toml` | DONE |
| 13 | Test suite (66 tests) | DONE |

---

## Demo → Production Transition

1. **Demo + dry_run=true** — Validate pipeline runs end-to-end, check Claude analysis quality
2. **Demo + dry_run=false** — Place real orders on demo exchange, validate fill handling
3. **Production + dry_run=true** — Confirm production auth works with real market data
4. **Production + dry_run=false, low limits** — Trade with $5 max position, $50 max exposure for 1-2 weeks
5. **Production + normal limits** — Gradually increase based on demonstrated profitability

---

## Verification

1. **Auth**: Generate API key on demo.kalshi.com, run `GET /portfolio/balance` to verify signing works
2. **Scanner**: Run scan, confirm markets are fetched and filtered correctly, check category coverage
3. **Analysis**: Send a few events to Claude, inspect structured outputs for reasonable probabilities
4. **Strategy**: Feed known market prices + Claude outputs through decision engine, verify edge calculation and Kelly sizing math
5. **Risk**: Test with positions near limits, verify trades are correctly rejected
6. **End-to-end dry run**: Full pipeline cycle on demo, verify logs show SCAN → ANALYZE → DECIDE → EXECUTE flow
7. **End-to-end live on demo**: Place real orders on demo exchange, monitor fills and P&L tracking
