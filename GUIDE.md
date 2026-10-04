# Kalshi AI Trading Bot — Setup & Run Guide

A step-by-step guide to get the bot running from scratch.

---

## Prerequisites

- **Python 3.11+** installed and on your PATH
- **Git** (you already have this)
- A **Kalshi** account (demo or production)
- An **Anthropic** API key for Claude

---

## Step 1: Install Python Dependencies

Open a terminal in the project folder and run:

```
pip install -e ".[dev]"
```

This installs the bot and all its dependencies (httpx, anthropic, cryptography, etc.) in editable mode so you can modify the code without reinstalling.

If `pip` is not found, use the full path:
```
C:\Users\<you>\AppData\Local\Programs\Python\Python3XX\python.exe -m pip install -e ".[dev]"
```

---

## Step 2: Create a Kalshi Account & API Key

### 2a. Sign up for Kalshi Demo

1. Go to [https://demo.kalshi.co](https://demo.kalshi.co)
2. Create an account (the demo environment uses fake money)
3. Once logged in, you'll have a demo balance to test with

### 2b. Generate an API Key

1. On demo.kalshi.co, go to **Settings** > **API Keys**
2. Click **Create API Key**
3. Kalshi will give you two things:
   - **API Key ID** — a string like `abc123-def456-...`
   - **Private Key** — a `.pem` file download
4. Save the `.pem` file somewhere safe on your computer (e.g., `C:\Users\<you>\kalshi-key.pem`)
5. **Never commit this file to git** — it is already covered by `.gitignore`

---

## Step 3: Get an Anthropic API Key

1. Go to [https://console.anthropic.com](https://console.anthropic.com)
2. Sign up or log in
3. Go to **API Keys** and create a new key
4. Copy the key (starts with `sk-ant-...`)
5. You'll need to add credits to your account — the bot uses Claude Sonnet by default, which costs roughly $3 per 1M input tokens

---

## Step 4: Create Your .env File

Copy the example and fill in your credentials:

```
cp .env.example .env
```

Then edit `.env` with your actual values:

```
KALSHI_API_KEY=your-api-key-id-from-step-2
KALSHI_PRIVATE_KEY_PATH=C:\Users\<you>\kalshi-key.pem
ANTHROPIC_API_KEY=sk-ant-your-key-from-step-3
KALSHI_ENVIRONMENT=demo
DRY_RUN=true
```

**Important:**
- `KALSHI_ENVIRONMENT=demo` keeps you on the sandbox (fake money)
- `DRY_RUN=true` means the bot will analyze markets and log what it *would* trade, but won't place real orders

---

## Step 5: Verify the Setup

Run the test suite to make sure everything is installed correctly:

```
python -m pytest tests/ -v
```

You should see all 66 tests pass. This does NOT connect to Kalshi or use your API keys — the tests use mocked responses.

---

## Step 6: Run the Bot (Dry Run)

```
python -m kalshi_bot
```

The bot will:
1. Connect to Kalshi Demo API and verify your credentials
2. Fetch your demo balance
3. Run the first pipeline cycle immediately:
   - **SCAN**: Fetch all open markets, filter by volume/spread/liquidity
   - **ANALYZE**: Send events to Claude for probability estimation
   - **DECIDE**: Compare Claude's estimates to market prices, find edges
   - **EXECUTE**: Log the trades it would place (dry run = no real orders)
4. Schedule the pipeline to repeat every 30 minutes
5. Monitor your portfolio every 5 minutes

Press `Ctrl+C` to stop the bot gracefully.

---

## Step 7: Go Live on Demo

Once you're comfortable with the dry-run output and Claude's analysis quality, enable real order placement on the demo exchange:

Edit `.env`:
```
DRY_RUN=false
```

Or edit `config/demo.yaml`:
```yaml
execution:
  dry_run: false
```

Run the bot again:
```
python -m kalshi_bot
```

Now it will place real orders on the demo exchange (still fake money). Monitor the logs to see fills, P&L, and position tracking.

---

## Step 8: Transition to Production (When Ready)

**Only do this after you've validated the bot on demo.**

1. Create a production API key at [https://kalshi.com](https://kalshi.com) (Settings > API Keys)

2. Update `.env`:
   ```
   KALSHI_API_KEY=your-production-api-key
   KALSHI_PRIVATE_KEY_PATH=C:\Users\<you>\kalshi-prod-key.pem
   KALSHI_ENVIRONMENT=production
   DRY_RUN=true
   ```

3. Run a dry-run on production first to confirm auth works with real market data

4. When ready for real trading, set `DRY_RUN=false` — but note that `config/production.yaml` starts with conservative limits:
   - Max $5 per position
   - Max $50 total exposure
   - Max $5 per order

5. Gradually increase limits in `config/production.yaml` as you build confidence

---

## Configuration Reference

All tunable settings live in the `config/` YAML files. You can also override any setting via environment variables.

### Key Settings

| Setting | File | Default | What it does |
|---|---|---|---|
| `dry_run` | `config/demo.yaml` | `true` | Log trades without placing orders |
| `pipeline_interval_minutes` | `config/default.yaml` | `30` | How often the bot scans for opportunities |
| `min_edge_threshold` | `config/default.yaml` | `0.05` | Minimum 5% edge required to trade |
| `kelly_fraction` | `config/default.yaml` | `0.25` | Quarter-Kelly for conservative position sizing |
| `max_single_order_cents` | `config/default.yaml` | `2000` | $20 max per order |
| `max_total_exposure_cents` | `config/default.yaml` | `50000` | $500 max total portfolio exposure |
| `max_daily_loss_cents` | `config/default.yaml` | `10000` | $100 daily loss limit triggers circuit breaker |
| `model` | `config/default.yaml` | `claude-sonnet-4-5-20250929` | Which Claude model to use for analysis |
| `max_concurrent` | `config/default.yaml` | `3` | Max parallel Claude API calls |

### Config Hierarchy

Settings are merged in this order (later overrides earlier):

1. `config/default.yaml` — base defaults
2. `config/demo.yaml` or `config/production.yaml` — environment-specific overrides
3. `.env` file — environment variable overrides
4. Actual environment variables — final override

---

## Troubleshooting

### "Python was not found"
Python is not on your PATH. Either:
- Reinstall Python and check **"Add Python to PATH"** during setup
- Use the full path: `C:\Users\<you>\AppData\Local\Programs\Python\Python3XX\python.exe`

### "KALSHI-ACCESS-KEY authentication failed"
- Double-check your API key ID in `.env`
- Make sure the `.pem` file path is correct and the file exists
- Verify you're using the right environment (demo key for demo, production key for production)

### "Anthropic API key invalid"
- Verify your `ANTHROPIC_API_KEY` in `.env` starts with `sk-ant-`
- Check that your Anthropic account has credits

### Circuit breaker tripped
The bot automatically halts trading if it hits safety limits (e.g., 5 consecutive losses, hourly loss threshold). Check the logs for the reason. To resume:
- Stop the bot (`Ctrl+C`)
- Review what happened
- Restart the bot (the circuit breaker resets on startup)

### No trades being generated
This is normal and expected. The bot only trades when it finds markets where Claude's probability estimate differs from the market price by more than the `min_edge_threshold` (default 5%). If the market is efficient, there may be few opportunities.

---

## Useful Commands

| Command | What it does |
|---|---|
| `python -m kalshi_bot` | Start the bot |
| `python -m pytest tests/ -v` | Run all tests |
| `python -m pytest tests/test_strategy.py -v` | Run a specific test file |
| `Ctrl+C` | Gracefully stop the bot |
