# Day Trader Mode Guide

Day trader mode transforms the bot from a patient, swing-style trader into an aggressive short-term scalper. The core philosophy: **get in, get out.** Take profits fast, cut losses faster, and never let capital sit idle in a position that isn't moving.

---

## How to Turn It On

You have two options. Pick whichever is easiest for you.

### Option A: Environment Variable

Add this to your `.env` file:

```
DAY_TRADER_MODE=true
```

Or pass it inline when you launch the bot:

```
DAY_TRADER_MODE=true python -m kalshi_bot
```

### Option B: YAML Config

Edit `config/default.yaml` (or your environment overlay like `config/demo.yaml`):

```yaml
day_trader_mode: true
```

That's it. When the bot starts, you'll see this in the logs:

```
day_trader_mode_enabled  take_profit=10.0%  stop_loss=15.0%  max_hold_minutes=30
                         pipeline_interval=5m  monitor_interval=1m  max_hours_to_expiry=24
```

To turn it off, set it back to `false` (or remove the variable) and restart.

---

## What Changes

When you flip the switch, day trader mode automatically overrides **18 settings** across the bot. Every override is a sensible default — if you explicitly set any of these values yourself, your value is preserved. Day trader mode only touches settings that are still at their factory defaults.

### Exit Thresholds — The Key Changes

These are the most important differences. Normal mode is patient; day trader mode is not.

| Setting | Normal | Day Trader | What It Means |
|---|---|---|---|
| `take_profit_percent` | 30% | **10%** | Sell when you're up 10% instead of waiting for 30%. Lock in gains before they evaporate. |
| `stop_loss_percent` | 50% | **15%** | Cut a losing position at 15% down instead of riding it to 50%. Preserves capital for the next trade. |
| `re_eval_exit_edge` | -0.03 | **-0.01** | If Claude re-analyzes and your edge drops to just -1%, bail. In normal mode you'd tolerate -3%. |
| `max_hold_minutes` | 0 (off) | **30** | **New.** If a position hasn't hit TP or SL after 30 minutes, sell it anyway. Don't let capital sit idle. |

**`max_hold_minutes`** is the signature day trader feature. In normal mode it's disabled (0). When enabled, every position gets a countdown — if the price hasn't moved enough in your favor within 30 minutes, the bot exits regardless of P&L. This prevents the common trap of holding a stagnant position while better opportunities pass by.

The bot tracks when each position was first opened and checks the age every monitoring cycle. You can customize the hold time:

```yaml
strategy:
  max_hold_minutes: 45  # override the default 30 to give positions more room
```

### Speed — Scan and React Faster

Day trading requires faster reflexes. The bot tightens all its timing loops:

| Setting | Normal | Day Trader | What It Means |
|---|---|---|---|
| `pipeline_interval_minutes` | 30 | **5** | Full SCAN-ANALYZE-DECIDE-EXECUTE cycle runs every 5 min instead of 30. 6x more opportunities. |
| `portfolio_monitor_interval_minutes` | 5 | **1** | Checks TP/SL/time exits every 60 seconds instead of every 5 minutes. Catches exits faster. |
| `cache_ttl_minutes` | 30 | **5** | Claude's cached analysis expires after 5 min. Fast-moving markets need fresh estimates. |
| `max_concurrent` | 3 | **5** | Runs 5 Claude analyses in parallel instead of 3. Handles the higher scan throughput. |
| `stale_order_hours` | 2 | **1** | Unfilled orders are cancelled after 1 hour instead of 2. Don't leave stale bids sitting. |

The tighter monitoring interval is especially important — with a 15% stop loss, you want to catch the exit quickly, not discover 5 minutes later that you're already down 25%.

### Market Selection — Focus on What Moves

Day trader mode narrows the market filter to find liquid, near-expiry markets where prices actually move:

| Setting | Normal | Day Trader | What It Means |
|---|---|---|---|
| `max_spread_cents` | 15 | **8** | Only trade markets with tight spreads (8 cents max). Wide spreads eat your profit on quick trades. |
| `min_hours_to_expiry` | 2 | **0.5** | Allow markets expiring in as little as 30 min. This is where prices move fastest. |
| `max_hours_to_expiry` | 0 (off) | **24** | **New.** Only look at markets resolving within 24 hours. Ignore long-dated contracts. |
| `min_volume` | 100 | **200** | Require higher volume so your orders actually fill quickly. |
| `min_open_interest` | 50 | **100** | Require more counterparties. You need someone to sell to when you exit. |
| `max_markets_per_cycle` | 50 | **100** | Scan more markets per cycle since near-expiry filters will exclude many. |

**`max_hours_to_expiry`** is a new filter introduced with day trader mode. When set to 0 (normal mode), there's no upper limit — the bot considers markets regardless of when they resolve. When set to 24, only markets resolving within the next 24 hours pass the filter. Markets without a close time are excluded entirely when this filter is active.

You can customize both expiry bounds independently:

```yaml
scanner:
  min_hours_to_expiry: 1    # nothing expiring in less than 1 hour (too risky)
  max_hours_to_expiry: 12   # nothing more than 12 hours out (tighter focus)
```

### Position Sizing — Bigger Bets, Shorter Holds

| Setting | Normal | Day Trader | What It Means |
|---|---|---|---|
| `min_edge_threshold` | 5% | **3%** | Take trades with smaller edges. You make it up on volume and turnover. |
| `kelly_fraction` | 0.25 | **0.5** | Half-Kelly instead of quarter-Kelly. Larger position sizes since you're exiting quickly. |

The combination of lower edge threshold + higher Kelly fraction means more trades at larger sizes. This is the day trader tradeoff: any single trade has less margin of safety, but you're turning over capital much faster and catching more opportunities. The tight stop loss (15%) limits downside on each individual position.

### Claude Analysis — Short-Term Focus

When day trader mode is on, Claude receives an additional prompt addendum that shifts its analysis toward short-term factors:

- Imminent catalysts (scheduled announcements, data releases, deadlines happening today)
- Momentum and sentiment (how fast is opinion shifting right now?)
- Time decay (markets near expiry converge toward 0% or 100%)
- Information asymmetry (what does the crowd likely NOT know yet?)
- Decisiveness (small edges matter when holding for minutes, not days)

Claude is explicitly told **not** to over-weight long-term base rates for events resolving in hours. This is appropriate for near-expiry markets where the question isn't "what usually happens?" but "what's happening right now?"

---

## Full Override Reference

Here's every setting that day trader mode touches, all in one place:

| Section | Setting | Normal Default | Day Trader Value |
|---|---|---|---|
| `strategy` | `take_profit_percent` | 30.0 | 10.0 |
| `strategy` | `stop_loss_percent` | 50.0 | 15.0 |
| `strategy` | `re_eval_exit_edge` | -0.03 | -0.01 |
| `strategy` | `max_hold_minutes` | 0 | 30 |
| `strategy` | `min_edge_threshold` | 0.05 | 0.03 |
| `strategy` | `kelly_fraction` | 0.25 | 0.5 |
| `scheduler` | `pipeline_interval_minutes` | 30 | 5 |
| `scheduler` | `portfolio_monitor_interval_minutes` | 5 | 1 |
| `claude` | `cache_ttl_minutes` | 30 | 5 |
| `claude` | `max_concurrent` | 3 | 5 |
| `execution` | `stale_order_hours` | 2 | 1 |
| `scanner` | `max_spread_cents` | 15 | 8 |
| `scanner` | `min_hours_to_expiry` | 2.0 | 0.5 |
| `scanner` | `max_hours_to_expiry` | 0 | 24 |
| `scanner` | `min_volume` | 100 | 200 |
| `scanner` | `min_open_interest` | 50 | 100 |
| `scanner` | `max_markets_per_cycle` | 50 | 100 |

---

## Customizing Day Trader Mode

Day trader mode is a defaults layer, not a hard override. Any setting you explicitly configure takes priority. This means you can turn on day trader mode and then fine-tune individual values.

**Example: Day trader mode with a more conservative stop loss:**

```yaml
day_trader_mode: true

strategy:
  stop_loss_percent: 25.0   # your explicit value — day trader won't touch it
  # take_profit_percent: left at default, so day trader sets it to 10.0
```

**Example: Day trader mode with custom expiry window:**

```yaml
day_trader_mode: true

scanner:
  min_hours_to_expiry: 1.0   # your value — preserved
  max_hours_to_expiry: 6.0   # your value — preserved
  # min_volume: left at default, so day trader sets it to 200
```

**Example: Day trader mode via env var + YAML overrides:**

```
# .env
DAY_TRADER_MODE=true
```

```yaml
# config/demo.yaml
strategy:
  kelly_fraction: 0.3   # more conservative than the day trader default of 0.5
  max_hold_minutes: 60   # give positions a full hour instead of 30 min
```

---

## Risk Considerations

Day trader mode does **not** modify any risk limits. Your `max_position_size_cents`, `max_total_exposure_cents`, `max_daily_loss_cents`, `consecutive_loss_limit`, and all other circuit breaker settings remain exactly as configured. The risk module is your safety net regardless of trading style.

That said, the higher Kelly fraction (0.5 vs 0.25) means individual positions are larger. Combined with faster turnover, you could hit daily loss limits more quickly. Consider these adjustments for day trading:

```yaml
risk:
  max_daily_loss_cents: 5000    # tighter daily loss limit ($50 instead of $100)
  consecutive_loss_limit: 3     # trip circuit breaker after 3 losses instead of 5
  hourly_loss_limit_cents: 2000 # tighter hourly limit
```

---

## Verifying It Works

### 1. Check the startup logs

When you start the bot with day trader mode on, the first few log lines will confirm all overridden settings:

```
orchestrator_starting  environment=demo  dry_run=true  day_trader_mode=true
day_trader_mode_enabled  take_profit=10.0%  stop_loss=15.0%  max_hold_minutes=30
                         pipeline_interval=5m  monitor_interval=1m  max_hours_to_expiry=24
```

### 2. Check scanner filtering

Watch for the `scan_filtered` log line. With `max_hours_to_expiry=24`, you should see many long-dated markets being removed:

```
scan_fetched  total_markets=347
scan_filtered  passed=42  removed=305
```

### 3. Check exit decisions

The `exit_decision` log will show the reason. Look for `max_hold_time` exits alongside the usual `take_profit` and `stop_loss`:

```
exit_decision  ticker=MKT-ABC  reason=max_hold_time  pnl_ratio=+2.3%  quantity=15
exit_decision  ticker=MKT-XYZ  reason=take_profit    pnl_ratio=+11.5% quantity=8
exit_decision  ticker=MKT-123  reason=stop_loss      pnl_ratio=-14.8% quantity=10
```

### 4. Run the tests

All day trader behavior is covered by tests:

```
python -m pytest tests/test_day_trader.py -v
```

This runs 13 tests covering config overrides, expiry filters, time-based exits, and prompt behavior.

---

## When to Use Day Trader Mode

**Good times to run day trader mode:**
- During high-activity periods with many markets resolving that day
- When major news events create fast-moving markets
- When you want to actively scalp and don't mind higher Claude API costs (5-min cache + 5 concurrent calls)

**When to stick with normal mode:**
- Overnight or unattended operation (normal mode is more set-and-forget)
- When markets are slow and few near-expiry opportunities exist
- When you want to minimize API costs (30-min cache is much cheaper)

You can switch between modes freely — just change the flag and restart the bot. There's no state to migrate. Existing positions will immediately be subject to the new TP/SL/time thresholds on the next monitoring cycle.
