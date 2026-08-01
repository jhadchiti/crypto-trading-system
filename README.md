# Crypto Trading System — README

*The front door to this project. Read this first. Last updated: 2026-07-31.*

---

## What this is

An autonomous cryptocurrency trading system. It runs on this laptop every night at 00:05 UTC, checks the market, and — when specific pre-tested conditions are met — places real trades on Binance USD-M futures, protects them with exchange-side stop-losses, closes them when rules say so, and sends a Discord message explaining everything it did and why. Most nights it does nothing, on purpose. It currently manages ~$110 and is built to work identically at any capital size.

## The philosophy

~84% of retail crypto traders lose money in year one — by trading on emotion: chasing tops, panic-selling bottoms, overriding their own plans. This system removes the human from those decisions. Every rule was chosen by one method: **propose a hypothesis, test it on out-of-sample historical data, keep it only if statistically significant.** Ten strategy ideas were tested this way. Eight failed and are documented as failures (see SYSTEM_QA.md Q69-74). Two survived. The rejected ideas are as valuable as the kept ones — they are mistakes that will never be made with real money.

The edge harvested is **trend-following (momentum)** — the best-documented phenomenon in financial markets (Journal of Financial and Quantitative Analysis 2024: crypto trend factor, weekly alpha 2.62%, t-stat 4.22). Assets breaking to new highs tend to keep going because humans chase, herd, and underreact. Rare big winners pay for many small losses.

## Strategy 1 — Trend system (LIVE)

**Enter long when ALL of:**
1. Close > highest close of last 55 days (breakout)
2. BTC 200-day SMA is rising vs 20 days ago (macro regime ON — only trade with the tide)
3. Funding rate within ±20 bps/8h (trade not dangerously crowded)
4. Coin in top 20% of 30-day return vs BTC among the universe (buy leaders, not laggards — this filter raised expectancy +69% in walk-forward v7)

Shorts are the mirror. Universe: top ~30 liquid Binance perps (≥$50M daily volume, ≥180d history), refreshed weekly.

**Exit when ANY of:** close crosses the 20-day channel against the position · stop-loss at 2×ATR(20) from entry hits (lives ON the exchange, protects 24/7) · 90 days pass.

**Sizing:** 0.75% of total equity risked per trade (~$0.84 today). Max 4 concurrent positions (3% portfolio heat cap). Expect ~30% win rate, avg loss ~0.7R, avg win ~2.6R, occasional 30R+ monsters — the monsters ARE the strategy.

**Current state: DORMANT.** BTC macro has been OFF for 300+ days. The dashboard shows the SMA slope; when it turns positive, the system wakes automatically.

## Strategy 2 — Funding carry (PAPER until Sept 1, 2026 review)

When perp funding is sustained ≥10bps/8h (crowded longs paying shorts), hold spot + short the perp: price cancels, funding is income. Backtest 2019-2026: +9.3% APY on capital, 81% episode win rate, worst 30-day window −0.09%. Complements trend (earns during euphoria; trend earns during moves; Simple Earn yields always). Spec: FUNDING_CARRY_SPEC.md.

## The nightly machine (Task Scheduler → daily_check.py, 00:05 UTC)

| Step | Script | Job |
|---|---|---|
| 1 | signal_alerter.py | Compute signals, Discord alerts with full rationale |
| 2 | executor.py | THE TRADER: process exits, open passing entries, place exchange stops, record trades |
| 3 | dashboard.py | Regenerate dashboard.html (the full operational picture) |
| 4 | funding_carry_monitor.py | Carry paper-trading state machine |
| 5 | account_sync.py | Read real Binance balances (read-only), cross-check positions vs records, log equity history |

Everything logs to automation.log. All traffic runs through NordVPN (Binance geo-blocks any US/blocked-region IP with HTTP 451 — the VPN must be connected, auto-connect enabled).

## The money — two compartments by design

- **Futures wallet (~$50): the bot's ENTIRE reach.** Its API key can trade futures only — no withdrawals, no transfers. You control its ammunition via what you transfer in.
- **Everything else (~$60): beyond the bot's permissions.** Should sit in Binance Simple Earn USDT Flexible earning ~1.7-5% APY, instantly redeemable.

## Safety systems

- **Kill file:** create a file named `STOP_TRADING` in this folder → bot does nothing. `New-Item STOP_TRADING` halts; `Remove-Item STOP_TRADING` re-enables.
- **Circuit breaker:** if futures equity drops >5% between runs (net of transfers), the bot halts ITSELF and alerts. Proven working (fired correctly on day one).
- **Exchange-side stops:** every position gets a STOP_MARKET on Binance — protection does not depend on the laptop being on.
- **Hard rails (executor_config.json):** max 4 positions · $40 notional/trade cap · 3x isolated margin · price-sanity check (skip if mark deviates >2% from signal) · no signal ever executes twice.

## Honest expectations

Long-run: **+12-25%/year, 10-20% drawdowns, lumpy** — months of nothing, then windfall stretches when a big trend hits. Live results typically run 60-80% of backtest. At current capital that's ~$15-25/year; the goal now is proving the machine so it deserves more capital. Anyone promising 1%/day is running a scam (that compounds to 3,700%/year; the best fund in history did 66%). The real lever is monthly deposits compounding at honest rates for years.

## The operator's job (yours)

1. Laptop plugged in; sleep is fine (Task Scheduler wake timers enabled); VPN on auto-connect
2. Read Discord when it pings; execute nothing manually — the bot trades
3. **Never override.** No skipped signals, no discretionary trades, no panic in drawdowns
4. Scheduled reviews: **Sept 1, 2026** carry go/no-go · **after 20 live trades** live-vs-backtest calibration · **Aug 2027** pre-registered ANCHOR factor re-test (SYSTEM_QA Q74)
5. If anything breaks: paste the error to Claude, say "diagnose"

## Emergency procedures

| Situation | Action |
|---|---|
| Stop all trading NOW | `New-Item STOP_TRADING` in this folder |
| Bot halted itself | Read the Discord message + STOP_TRADING contents; investigate; delete file to resume |
| 451 errors return | Check VPN: `curl.exe https://ipinfo.io/country` must NOT be US/blocked region |
| Task not running | `Get-ScheduledTaskInfo -TaskName "CryptoDailyCheck"` — LastTaskResult should be 0 |
| Position on exchange not in records (or vice versa) | account_sync Discords a red warning; reconcile live_trades.csv against Binance manually |

## Document map

| File | Contents |
|---|---|
| SYSTEM_QA.md | 74 Q&As — every design decision, every rejected idea, the full research trail |
| STRATEGY_SPEC.md | Formal trend-strategy rules |
| FUNDING_CARRY_SPEC.md | Formal carry rules + decision gates |
| DEPLOYMENT_PLAYBOOK.md | Operations manual |
| STRATEGY_IMPROVEMENT_TREE.md | Enhancement decision tree |
| dashboard.html | Live operational picture (regenerated nightly) |
| automation.log | The machine's diary |
| live_trades.csv | The trade record (inception 2026-07-31, $111.76) |
| equity_history.csv | Daily real-equity snapshots |

## Key validation results (the evidence this is real)

- Walk-forward (8 folds, 2019-2026): OOS expectancy +2.19R/trade, bootstrap 95% CI lower bound +0.22R (statistically significant), OOS trade Sharpe 1.27
- Rel-strength filter (v7): +69% expectancy vs baseline at equal annualized return, 43% fewer trades
- Funding carry backtest: 181 episodes, 81% win, +9.3% APY, max 30d loss 0.09%
- Rejected with documentation: mean reversion, MTF weekly confirm, MAX/lottery factor, weekly reversal, betting-against-beta, 52-week-high anchor (re-test Aug 2027), day-of-week, hourly seasonality

---

**One sentence:** this is a machine that waits patiently for statistically-validated opportunities, bets small, cuts losses fast, lets winners run, and protects you from yourself — your only real job is to let it.
