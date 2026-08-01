# Crypto Trend System — Q&A Reference

A practical FAQ for the Donchian + BTC-macro systematic trading system. Read this when you forget how something works, when you need to explain it to someone, or when you're tempted to override the rules.

---

## 1. What is this system, in one paragraph?

A daily-timeframe systematic trend-following strategy that trades Binance USD-M perpetual futures on a dynamic universe of ~30 liquid crypto assets. Long/short Donchian channel breakouts (55-day entry, 20-day exit) gated by a BTC macro regime filter and a funding-rate filter. Validated on 2019–2026 data with statistically significant out-of-sample edge. The system is fully automated for monitoring; order placement remains manual during paper trading and early live deployment.

---

## 2. Why this strategy specifically?

Three reasons:

- **Validated empirically.** Walk-forward bootstrap CI on aggregate trades has lower bound +0.24R per trade, OOS lower bound +0.51R. Donchian-style trend-following is one of the few systematic edges that has survived decades in commodities and now in crypto.
- **Asymmetric payoff structure.** Roughly 30% win rate with avg win 2.6R vs avg loss 0.66R. Designed to catch the rare 30-50R monster trends that compound the portfolio.
- **Simple enough to actually run.** All rules are deterministic. No discretion. No overrides. Operational discipline is the hard part, not the strategy logic.

---

## 3. What does this strategy do that I couldn't do just holding BTC?

Honestly, on absolute returns 2019–2026, BTC buy-and-hold won. It went from ~$7k to ~$70-100k. The strategy made +5-25% annualized depending on sizing.

What the strategy *does* offer:
- **Low correlation to BTC** (−0.07 daily-return correlation). Useful as a diversifier in a multi-asset portfolio.
- **Smaller drawdowns.** Backtest max DD 2.5% (mark-to-close) vs BTC's 75%+ drawdowns in bear cycles.
- **Forward asymmetric upside.** Trends in new alts (the next SOL/TIA/SUI) get caught by a dynamic universe; buy-and-hold misses these unless you guess right.
- **Discipline.** Removes emotional decisions during volatility.

Honest framing: this is a *defensive sleeve in a multi-strategy book*, not a "make me rich on crypto" strategy.

---

## 4. What exactly triggers a LONG entry?

ALL of these must be true at Daily close (UTC 00:00):

1. Daily close > rolling 55-day high (excluding today's bar)
2. BTC SMA(200) today > BTC SMA(200) twenty days ago (macro regime ON)
3. Most recent 8h funding rate ≤ +20 bps (not crowded long)
4. No existing position in this symbol

A SHORT entry is the mirror: close < 55-day low, macro ON, funding ≥ −20bps.

---

## 5. What triggers an exit?

ANY of these closes the position at the next Daily close:

| Trigger | Condition |
|---|---|
| `channel_exit` | LONG: close < 20-day low. SHORT: close > 20-day high. |
| `atr_stop` | Price hits initial stop at 2 × ATR(20) from entry. |
| `time_stop` | Position open ≥ 90 days. |

Stops are placed at entry and **never moved adversely**. They trail favorably after +1R and +2R (see playbook).

---

## 6. Does the system trade both long and short?

Yes, mechanically. In practice, ~95% of trades are long because the BTC macro filter is only ON during BTC uptrends, when alt prices generally rise with BTC. Shorts fire occasionally during macro-on chop. The strategy structurally misses bear-market shorts because macro is off during downtrends — that's an accepted trade-off for risk-averse simplicity.

---

## 7. How is the universe selected?

Three rules, applied pre-trade with no lookback on performance:

1. **Active USDT perp on Binance** with status TRADING
2. **Listed ≥ 180 days** so SMA200 + Donchian channels have data
3. **24h notional volume ≥ $50M** for liquidity

Then ranked by volume and the top 30 are kept. BTCUSDT is always included (needed for macro regime). Stablecoins and wrapped tokens are excluded.

Refreshed weekly via `python dynamic_universe.py`. Written to `active_universe.json`.

---

## 8. Why a dynamic universe instead of a fixed list?

Empirically validated in walk_forward_v6. Static top-30 (rebalanced periodically) produced **+47% annualized vs +21% for the fixed 8 symbols** — more than 2× improvement with starred bootstrap CI. The original 8 (BTC, ETH, SOL, BNB, AVAX, LINK, DOGE, XRP) were above-median performers but missing several starred winners (PENDLE, FET, ALGO, EGLD, ADA).

Universe expansion is the win. Continuous rebalancing rotation doesn't add further value (we tested that — see v6).

---

## 9. How much does it trade per month?

Roughly **6 trades per month** when BTC macro is ON. When macro is OFF, zero trades. Historically macro has been on ~65% of the time, so expect ~50 trades per year on average.

Currently BTC macro has been OFF for 300+ days. The system is dormant by design. The next signal will come when BTC's 200-day SMA starts rising again.

---

## 10. What returns should I expect?

| Scenario | Annualized return (at 0.75% risk/trade) |
|---|---|
| Median year | +15-20% |
| Good year (one or two monster trends caught) | +30-50% |
| Bad year (chop regime, no edge captured) | −5 to −10% |
| Long-run average | ~+22% |

These are backtested numbers. Live returns will be 60-80% of backtest due to slippage, missed fills, and the inevitable underperformance from execution friction. Plan for ~+12-18% annualized live.

---

## 11. What's the worst drawdown I should expect?

| Scenario | Drawdown |
|---|---|
| Normal chop period | 3-6% |
| Bad regime (2022-23 style) | 10-15% |
| Tail event (correlated cascade) | 18-22% |
| "Strategy is broken — re-validate" | > 25% |

If you can't psychologically hold through a 15% drawdown without panicking, size down to 0.5% per trade. It is better to deploy at a size you can hold through than full size you cannot.

---

## 12. Why is the win rate only ~30%?

Because it's a trend-following strategy. **Trend systems have low win rates by mathematical necessity.** The edge comes from asymmetry: when you win, you win big (avg 2.6R). When you lose, you lose small (avg 0.66R).

For reference, the largest trend funds in history have similar profiles:
- Winton Group: ~37% win rate, ~12% annual return over 20 years
- Man AHL: ~35% win rate
- Renaissance RIEF (the public-facing Rentec fund): ~38% win rate

If you saw 70% win rate on a trend backtest, that would be the warning sign, not the comfort.

---

## 13. How are positions sized?

Risk-per-trade math, capped by volatility target:

```
risk_per_trade        = 0.75% × current_equity
position_size         = risk_per_trade / |entry_price − stop_price|
position_notional     = position_size × entry_price
```

If position annualized vol > 15% of equity, size down to that cap. Never up.

Portfolio heat cap: sum of (risk per trade × open positions) ≤ 3% of equity. With 0.75% per trade, max 4 simultaneous open positions.

---

## 14. What's my daily routine supposed to look like?

**Most days (99%):** Nothing. The automation runs. No notification fires. You don't open the dashboard.

**Days when something happens:**
1. Discord/email notification: "LONG_ENTRY on SOLUSDT at $142.50, stop $138.20, risk $7.50"
2. Open Binance app, place the order manually
3. Add one line to `live_trades.csv` with entry details
4. Done. 5 minutes total.

**Weekly (Sunday):** `dynamic_universe.py` auto-refreshes the universe. No action needed.

**Monthly:** Run `python strategy_report.py --file live_trades.csv` to see how you're tracking.

**Quarterly:** Re-run `walk_forward_v3.py` to confirm the edge still holds. Review your psychological log.

---

## 15. What files do I actually maintain by hand?

Just one: `live_trades.csv`. Add a row when you enter a trade, fill in the exit fields when you close. That's it.

Columns to fill at entry: `symbol`, `side` (+1 or −1), `entry_date`, `entry_price`, `size`, `stop`, `initial_stop`, `risk_dollars`.

Columns to fill at exit: `exit_date`, `exit_price`, `pnl_gross`, `pnl_net`, `r_multiple`, `exit_reason`, `bars_held`.

Everything else (the universe, dashboard, alerter logs) is automated.

---

## 16. What's automated right now and what's not?

**Automated (Windows Task Scheduler):**
- Daily at 00:05: `signal_alerter.py` → fires Discord/email if a signal or exit is triggered
- Daily at 00:05: `dashboard.py` → refreshes `dashboard.html`
- Weekly Sunday 00:00: `dynamic_universe.py` → refreshes the active universe

**Manual (intentionally):**
- Recording trades in `live_trades.csv`
- Placing orders on Binance
- Quarterly performance reports
- Walk-forward re-validation

---

## 17. What's the deployment stage progression?

| Stage | What it is | Duration |
|---|---|---|
| Stage 1: Paper | Record trades in CSV as if you placed them. No real money. | 60-90 days |
| Stage 2: Read-only API | Connect Binance read-only. Reconcile account state with CSV. Still no auto-trading. | 30-60 days |
| Stage 3: Assisted execution | Trade key with permissions. Script proposes orders, you type CONFIRM. Tiny size ($50-200/trade). | 60-90 days |
| Stage 4: Full automation | Scheduled task places orders automatically. Heavy monitoring + kill switch. | Ongoing |

**Skipping stages is the #1 way retail systematic trading fails.** Do not skip.

---

## 18. What stage am I in now?

Stage 1 (paper trading). The system fires signals and you record paper trades. No real money. No API integration with order placement. The dashboard's "Deployment" card tracks day count.

---

## 19. How do I know when I'm ready to advance?

To advance from Stage 1 → 2: at least 60 days of paper trading, at least 20 paper trades recorded, you actually followed every signal (no missed entries), and reviewing your psychological log shows minimal urge to override.

To advance from Stage 2 → 3: read-only integration has been running 30+ days, your CSV matches broker state perfectly, you've handled at least one regime transition (macro flip) without confusion.

To advance from Stage 3 → 4: 60+ days of assisted trading at small size with no execution bugs, you've held through at least one realized 10%+ drawdown without panic.

---

## 20. What happens when BTC macro flips back to ON?

The alerter fires a `REGIME FLIP` notification: "BTC macro flipped ON. Strategy now enabled."

Over the following weeks, expect:
- 0-3 LONG_ENTRY signals across the universe in the first week
- 5-15 total trades in the first 60 days
- Most will be on mid-cap alts that broke out as BTC turned

This is when the strategy actually starts trading after a long dormancy.

---

## 21. What happens when BTC macro flips back to OFF?

The alerter fires another `REGIME FLIP` notification: "BTC macro flipped OFF. No new entries; manage open positions normally."

Existing positions are *not* force-closed — they manage to their normal channel/ATR/time exits. But no new entries fire until macro flips back on.

This typically means a multi-month dormancy.

---

## 22. What if I think I should override a signal?

Read the playbook. The override protocol is explicit:

**NEVER override on:** single losing trade, news/FUD, gut feelings, pundit predictions, fear during drawdown, greed during winning streaks.

**MAY override when:** exchange technical issue, account compromise, 60+ days of macro-ON with every trade losing AND your win rate < 15%, position-size error in execution.

Every override must be logged with date, reason, and what was done. More than 2 overrides per quarter and you've stopped running a systematic strategy.

---

## 23. What's the "kill switch"?

A `panic_close.py` script (not yet built — you'd write it before stage 3) that closes all open positions at market in one command. Keep it on your desktop. You'll likely never need it, but having it removes the worst failure mode (frozen during emergency).

---

## 24. The dashboard shows BTC macro OFF for 300+ days. Why is the strategy doing nothing?

By design. The macro filter detects whether BTC's 200-day SMA is rising over the last 20 days. Right now it isn't, so the system sits out. This is the strategy's "discipline mode."

In backtest, ~35% of days have macro OFF. Sometimes these stretches are 30 days, sometimes 300+. We don't try to predict when it'll flip.

What this period costs: the opportunity to trade alts during a sideways/down BTC. What it saves: losing money in regime where this specific edge doesn't work.

---

## 25. What if I see a great-looking setup but the verdict says BLOCKED?

Trust the verdict. The filters were validated empirically and removing any of them historically *hurt* performance even when individual blocked trades looked promising. Specifically:

- BLOCKED_BTC_MACRO: backtest expectancy during macro-off periods is statistically significantly *negative*
- BLOCKED_FUNDING: crowded-long entries (funding > +20bps) have lower expectancy and higher drawdown in backtest

The verdict column is the rule. Anything else is discretion creeping back in.

---

## 26. How do I check that everything is working?

Run:

```powershell
Get-Content automation.log -Tail 50
```

You should see daily entries like:
```
[2026-06-04 22:18:33 UTC] daily_check starting
[2026-06-04 22:19:38 UTC]   alerter:   no actions  (BTC macro OFF; 0 open positions)
[2026-06-04 22:19:38 UTC] OK alerter
[2026-06-04 22:19:38 UTC] OK dashboard
[2026-06-04 22:19:38 UTC] daily_check complete (alerter=OK, dashboard=OK)
```

If you see FAIL anywhere, investigate. The most common failures: network blips (transient, ignore), Binance API rate limit (rare with our polite sleeps), or an unhandled symbol issue.

---

## 27. What does the strategy actually look like as code?

The decision-making is just this, in `dashboard.compute_signals`:

```python
is_long_break  = close > entry_high   # 55-day high break
is_short_break = close < entry_low    # 55-day low break

if not (is_long_break or is_short_break):
    verdict = "NONE"
elif USE_BTC_MACRO and not btc_macro_on:
    verdict = "BLOCKED_BTC_MACRO"
elif USE_FUNDING and is_long and funding > FUNDING_LIMIT_BPS:
    verdict = "BLOCKED_FUNDING"
elif USE_FUNDING and is_short and funding < -FUNDING_LIMIT_BPS:
    verdict = "BLOCKED_FUNDING"
else:
    verdict = "LONG_ENTRY" if is_long else "SHORT_ENTRY"
```

That's it. No machine learning. No optimization on live results. Just a hand-coded breakout rule with two filters.

---

## 28. What's the BTC macro filter actually doing?

```python
sma200 = btc_close.rolling(200).mean()
is_macro_on = sma200 > sma200.shift(20)   # is the 200-day SMA rising over last 20 days?
```

Plain English: "Has BTC's 200-day moving average been climbing over the past 20 days?" If yes, the strategy is on. If no, it sits out.

This is one indicator. Two parameters (200, 20). Both are well-established trend-following defaults.

---

## 29. Why this specific entry/exit (55/20)?

Classic Donchian setup tuned for Daily crypto:
- 55-day entry: long enough to filter noise breakouts, short enough to catch meaningful trends
- 20-day exit: ~4 trading weeks; matches typical trend pullback durations in crypto

Tested in walk_forward_v3 against (20/10), (40/15), (100/30), (200/50). Sharpe was 0.85-1.04 across all five — robust across parameter choice, which is a hallmark of real edge. We froze at (55/20) as the middle ground.

---

## 30. Why use Donchian instead of moving averages, RSI, MACD, etc?

Three reasons:

1. **Cleanest entry definition.** A breakout is unambiguous: close > N-day high. No "crossover" interpretation.
2. **No look-ahead bias risk.** The high we compare against is already in the past.
3. **Historical track record.** The Turtle Traders famously used Donchian channels and produced legendary returns. CTA literature shows Donchian as one of the most robust trend signals across asset classes.

Moving-average crossovers and oscillator-based signals were tested in the structural framework we falsified earlier in this project. They didn't produce starred edge. Donchian did.

---

## 31. Why these specific filters (BTC macro + funding)?

Filters were tested individually in walk_forward_v3 and v4. The winners:

- **BTC macro** improved Sharpe from 0.40 (no filter) to 0.84 (with filter). Single biggest improvement.
- **Funding filter** was a small but consistent improvement, mostly during crowded-long periods (avoided 2021-style mania entries).

Filters that *didn't* work:
- ADX > 25: marginal improvement, not worth the complexity
- Fear & Greed extremes: barely detectable improvement
- BTC-relative rotation: helps alt selection but hurts on BTC trades

So we kept the two that pay for themselves and skipped the rest.

---

## 32. What if Binance is down?

The fetcher will fail with a network error. The alerter and dashboard will report `FAIL`. Nothing bad happens — no entries fire because no data is available. When Binance comes back, the next scheduled run picks up normally.

If you have open positions when Binance goes down: nothing you can do. Stops are managed by your local CSV state, but actual order execution depends on Binance being live. This is a real risk in crypto. Mitigation: don't hold positions across known maintenance windows.

---

## 33. What if I miss a day?

For paper trading: doesn't matter. The signal would have fired the next day too if the conditions persisted.

For live trading: if you miss a day where a `LONG_ENTRY` fired, you've missed the trade. The strategy doesn't chase missed entries. Don't enter a day later — the conditions have changed.

If you miss recording an exit: figure out the exit price from history and record it accurately. Don't fabricate.

---

## 34. What if a position should have exited yesterday but I didn't see the signal?

Record it at yesterday's close price (the actual exit signal). Mark it as exited. Don't extend the position. The strategy assumes you exit on signal, so any delay is an override.

---

## 35. The dashboard shows 0 positions but my Binance account has positions. What do I do?

The dashboard reads from `live_trades.csv`, not from your Binance account. If your CSV is out of sync with your broker, that's your bug to fix.

In stage 2+ (read-only API), we add reconciliation. For stage 1 (paper), it's purely on you to keep the CSV accurate.

---

## 36. What's the difference between `dashboard.py` and `strategy_report.py`?

| File | Purpose | Frequency |
|---|---|---|
| `dashboard.py` | Daily operational check. What to do today. Current state. | Daily (automated) |
| `strategy_report.py` | Deep analytical tearsheet. CAGR, Calmar, Sortino, regime breakdown. | Quarterly (manual) |

Dashboard is for *acting*. Report is for *evaluating*.

---

## 37. What's the difference between the alerter and the dashboard?

| File | Purpose | Output |
|---|---|---|
| `signal_alerter.py` | Notification engine. Fires Discord/email when there's something to do. | Silent unless action needed |
| `dashboard.py` | Situational awareness. Shows full state of the system. | Always produces HTML |

Alerter says "do this NOW." Dashboard says "this is the full picture."

---

## 38. How do I scale up if the strategy is working?

After 6+ months of successful live trading, the deployment progression is:

1. Increase per-trade risk from 0.75% → 1.0% (modest)
2. Then to 1.5% after another 3 months (still conservative)
3. Then to 2.0% only after one full regime cycle (chop → trend → chop) successfully held through

Never increase risk during a winning streak. Only after a held-through drawdown.

---

## 39. What's the second strategy I keep hearing about?

A planned mean-reversion strategy (not yet built) that would fire when this one is dormant (macro-OFF periods + extreme sentiment). The two strategies together would smooth the portfolio equity curve much more than tweaking this one further.

We deferred building it to keep complexity manageable. It's the next major project once paper trading proves out.

---

## 40. What does the universe scan tell me?

```powershell
python universe_scan.py
```

Produces `universe_scan_report.html` showing how the strategy performs on every symbol with enough history. Three possible outcomes:

- **>40% starred symbols:** edge is universal across crypto. Our universe is fine.
- **20-40% starred:** partial edge, worth investigating what differentiates winners.
- **<20% starred:** edge is fragile, methodology needs rethinking.

Last run showed 77% positive expectancy, 7% individually starred (small per-symbol samples). Healthy.

---

## 41. What's the IS/OOS split and why does it matter?

The walk-forward harness splits history into 8 folds: 2-year training windows followed by 6-month disjoint test windows. Each test fold's parameters were committed *before* seeing that data. This is the gold standard for systematic validation.

Without walk-forward, in-sample backtest results overstate forward edge by 30-70% on average. With walk-forward, our results are the closest we can get to forward expectations without actual forward data.

---

## 42. The OOS expectancy was +1.36R. What does that mean in dollars?

```
expectancy per trade × risk per trade = average $ per trade

+1.36R × $7.50 (at $1000 starting equity, 0.75% risk) = +$10.20 per trade
60 trades/year × $10.20 = +$612/year = +61% on $1k starting equity

At larger scale:
+1.36R × $750 (at $100k, 0.75%) = +$1,020 per trade
60 trades × $1,020 = +$61,200/year = +61% on $100k
```

That's backtest. Live, expect 50-70% of that due to slippage and missed fills.

---

## 43. Why is starting equity $1,000 and not $100,000?

Your choice (you set it). It's appropriate for early stage trading where bugs and slippage surprises cost almost nothing while strategy mechanics still operate proportionally. The math is the same — just scale up the dollar amounts later.

---

## 44. What's `active_universe.json`?

The current list of 30 symbols the strategy is allowed to trade, refreshed weekly. Format:

```json
{
  "generated_at": "2026-06-04T00:00:00Z",
  "universe": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "..."],
  "criteria": {"max_size": 30, "min_volume_usd": 50000000},
  "stats": {"total_perps": 528, "passed_filters": 45, "final_size": 30}
}
```

If you delete it, the system falls back to a hardcoded 8-symbol list. Regenerate with `python dynamic_universe.py`.

---

## 45. What's `alerts.log`?

Append-only timeline of every alert ever sent. Format:

```
[2026-06-05 00:05 UTC]
[Crypto Alerter] 1 action + regime flip
REGIME: BTC macro flipped ON. Strategy now enabled.
ACTIONS:
  - LONG_ENTRY on ETHUSDT at 2850.00 (...)
```

Read with `Get-Content alerts.log -Tail 50`. Useful for quarterly review.

---

## 46. What's `automation.log`?

The Windows scheduled-task output log. Shows whether the daily check succeeded or failed. Read with `Get-Content automation.log -Tail 30`. Different from `alerts.log` (which is just user-facing alerts).

---

## 47. What's `last_dashboard_state.json`?

Auto-managed. Stores prior state so the dashboard's "What Changed" section can detect deltas. Don't touch.

---

## 48. What if I want to change the strategy parameters?

Don't. We tested. Don't.

Seriously: the parameters were chosen through systematic walk-forward and bootstrap testing. Any change you make based on a hunch will probably be a regression. The path to "the parameters could be better" goes through *another* walk-forward, not gut feeling.

If you have a specific hypothesis ("would 40/15 work better?"), edit `donchian_baseline.DCFG`, run `walk_forward_v3.py` and `walk_forward_v6.py`, compare bootstrap CIs against current params. Only deploy a change if it improves OOS expectancy *and* CI lower bound *and* doesn't reduce trade count below 200 in the test set.

---

## 49. How do I know when to stop trading this strategy entirely?

Three criteria, all must be met:

1. You have ≥ 150 live trades AND
2. The bootstrap CI lower bound on those trades ≤ 0 AND
3. Re-running `walk_forward_v3.py` with current data shows no starred OOS expectancy

If just (3) is true but (1) and (2) aren't, you're in a normal bad regime. Keep going.

If (1) and (2) hold, the edge has decayed. Stop, investigate. Either the market has changed (likely — crypto evolves) or your execution is materially worse than backtest.

---

## 50. What are the honest limitations of this whole project?

In order of severity:

1. **Survivorship bias.** All backtests run on currently-listed Binance symbols. LUNA, FTT, AGIX-peak, and other delisted coins are absent. The historical universe overstates returns. Forward expectation: ~70% of backtest numbers.

2. **Regime dependency.** Strategy works in trending regimes, bleeds in chop. Pre-2024 in-sample showed statistically significant negative edge. We can't predict regime transitions in advance.

3. **Single-strategy concentration.** If this strategy's regime is unfavorable, you have no other source of returns. The fix is building a second uncorrelated strategy. Not yet done.

4. **Sample size.** 178-275 walk-forward trades is enough for significance but not for "definitely will persist 5 years." Crypto markets evolve fast — what worked 2019-2026 may not work 2027-2030.

5. **Execution friction.** Backtest assumes 4bps fee + 5bps slippage per side. Live could be 2-3× worse on alt mega-trends. Real CAGR will be 60-80% of backtest.

6. **BTC underperformance.** Strategy made +5.7% CAGR vs BTC's +39.3% CAGR over 2019-2026. As a *crypto wealth-builder*, it failed. As a *defensive sleeve*, it's reasonable.

7. **No automated execution yet.** Manual order placement adds latency and human error. Risk: you don't place an order, or you place wrong size, or you miss the day.

8. **Discretionary override risk.** The single biggest cause of retail systematic trading failure. The strategy can't fail you; only you can fail the strategy.

---

## 51. What would I tell a friend before they deploy this?

In order:

- Don't expect to beat BTC buy-and-hold in absolute terms.
- Plan for at least one 6-month period where the strategy loses money.
- Don't override even when individual trades feel wrong.
- Maintain `live_trades.csv` religiously. The whole evaluation depends on it.
- After 6 months, run the strategy report and bootstrap. If CI lower bound is ≤ 0, the edge isn't there for you.
- Build a second uncorrelated strategy before risking serious capital.

---

## 52. What's the one paragraph that matters most?

> The strategy works in aggregate, not in any single trade. Every individual entry has ~30-37% chance of being a winner. Most of your money comes from a small number of large winners. You will have losing streaks of 5-8 in a row. They are not the strategy breaking. They are the strategy working as designed. Your job is not to be right on every trade. Your job is to follow the rules. The rules are right on average. You are not.

---

*Generated for the validated Donchian + BTC-macro variant. Last updated alongside the v3 dashboard and v6 walk-forward results.*

---

# Yield on Idle Capital — Addendum

The trading strategy spends 30-40% of its time dormant (BTC macro OFF). During those periods, cash earning 0% is an opportunity cost. This section covers how to deploy that idle capital without compromising the trading system.

---

## 53. Why does idle capital matter? It's only a few percent.

Compounding math. Over a 6-month dormant period at $1k portfolio size:
- 0% yield: $0 earned (current state)
- 5% yield: ~$25 earned
- 8% yield: ~$40 earned

On a $1k base that's modest. Scaled to $50k portfolio at the same rates:
- 0%: $0
- 5%: $1,250
- 8%: $2,000

The percentage matters most at scale. But even at $1k, recovering a few percent annually is a free Sharpe improvement — you take on essentially no new risk while your existing risk capital earns yield in the background.

---

## 54. What's the simplest yield deployment for my $1k portfolio?

**Binance Simple Earn — USDT Flexible.** This is the no-friction default.

- Current APY: ~5-10% blended (most of your $1k sits inside the bonus-tier window which is capped at first 500 USDT and pays significantly more than the base)
- Same account — no withdrawal or bridging
- Instant redemption when trade signals fire
- Optional: Multi-Asset Mode + LDUSDT lets the same balance earn yield AND serve as futures margin

For a $1k portfolio this captures ~80% of the available yield at ~5% of the operational friction. Anything more complex is over-engineering at this scale.

---

## 55. How do I set up Binance Simple Earn?

Five clicks:

1. Open Binance app (or website)
2. **Earn → Simple Earn**
3. Search **USDT** → **Flexible** → **Subscribe**
4. Enter amount (minimum 0.10 USDT — your full balance is fine)
5. Toggle **Auto-Subscribe** ON before confirming

Auto-Subscribe sweeps any idle USDT in your Spot wallet into Flexible twice daily (02:00 and 16:00 UTC). After a trade closes and the proceeds return to your Spot wallet, they automatically move back into yield within ~16 hours. No manual action ever required.

---

## 56. What's LDUSDT and should I use it?

LDUSDT is Binance's "reward-bearing margin asset" — it's a wrapper around Simple Earn USDT that lets the same balance simultaneously:
- Earn the Real-Time APR from Simple Earn
- Serve as collateral/margin for USD-M futures positions

To enable: Futures → Settings → enable **Multi-Asset Mode**. Convert your Flexible USDT to LDUSDT in the Earn interface.

**Recommended yes**, with one caveat: in Multi-Asset Mode, profit and loss across multiple positions is netted against the combined balance. Slightly different risk profile than Single-Asset Mode. For a strategy that holds 1-4 positions max at any time, this is a non-issue.

---

## 57. Will the trading system still work if my cash is in Simple Earn?

Yes, no changes needed.

- `dashboard.py` doesn't query Binance for balance — it computes equity from `live_trades.csv`. So whether your USDT sits in Spot or Simple Earn or LDUSDT is invisible to the dashboard.
- `signal_alerter.py` doesn't care either.
- When a signal fires and you place an order, Binance auto-redeems Simple Earn to Spot for trade margin (or LDUSDT serves directly as margin in Multi-Asset Mode).

The yield layer is operationally transparent to the trading system.

---

## 58. When should I move from Binance Simple Earn to DeFi?

Three triggers:

1. **Portfolio passes ~$10k.** At that size, the ~2% APY spread between DeFi (~7-9%) and Binance Earn (~5-7%) becomes meaningful: $200+/year vs $50/year on the smaller portfolio.
2. **Binance Simple Earn APY drops below 4%.** Then DeFi's 6-8% baseline wins on absolute return even at small size.
3. **You want to diversify off-exchange counterparty.** Splitting cash across Binance + DeFi reduces concentration if you're holding meaningful sums.

For your current $1k, none of these triggers apply. Stay on Binance Simple Earn.

---

## 59. What about DeFi if I do want to go there?

Top recommendation when you graduate: **Morpho's Max Yield USDC vault on Base.**

- APY: ~7-9% net
- Underlying: USDC supply to whitelisted lending markets, curated by Gauntlet/Steakhouse
- Why Base: cheapest L2 gas (~$0.50 round trip even at $1k), Coinbase uses these rails for their consumer USDC lending product
- Operational complexity: medium (one-time wallet + bridge setup, ~15 min)

Bridge path: convert USDT → USDC on Binance (free), withdraw USDC to Base network from Binance (~$0.10), deposit into Morpho. Reverse to exit.

Auditor coverage: Spearbit, OpenZeppelin, ChainSecurity, Cantina.

---

## 60. What about auto-compounding aggregators (Beefy, Yearn)?

Skip them at $1k. They make sense at $10k+ where the manual restake effort exceeds the 4.5-10% performance fee.

If you do use one later: Beefy's Aave-USDC vault on Base is the cleanest — single-asset, no impermanent loss, instant withdrawal, 4.5% performance fee baked into displayed APY.

The aggregator adds a second layer of smart contract risk on top of the underlying lending protocol. Worth it only when the auto-compounding saves real money.

---

## 61. What's the marginal risk of adding Binance Simple Earn?

Essentially zero relative to your existing setup.

- **Counterparty risk:** Funds are lent to Binance's internal Margin/Loan desk. Not segregated, not insured. But you already accept Binance counterparty risk by holding USDT on Binance and trading perps there. Adding Simple Earn doesn't materially increase that.
- **Smart contract risk:** None. It's a CeFi product.
- **Liquidity risk:** Daily redemption caps exist for extreme volatility / mass-redemption events. Caps are typically very large relative to $1k, so practical risk is low but not zero.

If you're paranoid about Binance counterparty: keep half in Simple Earn, half in Spot. That's the only mitigation that actually changes risk.

---

## 62. When should I pull cash back out of yield?

Three triggers:

1. **A trade signal fires** that requires more margin than your Spot balance — Binance handles this automatically via auto-redeem when you place the order. No manual action needed.
2. **You're scaling up size** and want to redeploy half the portfolio into a second strategy (mean-reversion sleeve, when it gets built).
3. **You're winding down** the entire system. Then redeem everything to Spot, then withdraw to your bank.

For day-to-day operation, you never manually move cash. The system handles it.

---

## 63. How does adding yield change my expected returns?

Concretely, for your $1k portfolio at 0.75% risk per trade:

| Component | Annualized contribution |
|---|---|
| Trading strategy (when active) | +15-20% in good years, −5 to −10% in bad years |
| Yield on idle capital (~35% of year) | +1.5-3.5% (5-10% APY × 35% of year) |
| **Total** | **~+17-23% in good years, −2 to −7% in bad years** |

Net effect: bad years become less bad. Good years compound on a slightly higher base. The yield layer is the closest thing to free Sharpe improvement in the whole system.

At $50k portfolio: same percentages, ~10× the absolute dollar amounts.

---

## 64. Does adding yield change the dashboard tracking math?

No. The dashboard computes equity from `live_trades.csv` (starting equity + cumulative `pnl_net`). Whether USDT lives in Spot, Simple Earn, or LDUSDT is invisible to the dashboard.

If you want to track yield earnings separately, that's a manual exercise (Binance shows total interest earned in the Earn dashboard). Don't add yield interest to `live_trades.csv` — it would pollute the strategy performance metrics.

The cleanest mental model: trading P&L and yield P&L are two separate streams that combine in your actual brokerage balance but are tracked separately in the system.

---

## 65. What's the one-line summary?

> **Enable Binance Simple Earn USDT Flexible + Auto-Subscribe today. Five clicks. Zero change to the trading system. Captures ~5-10% APY on idle capital that's currently earning 0%.**

Anything beyond that is optimization for a later, larger portfolio.

---

## 66. What's the BTC-relative strength filter?

Added 2026-07-31 after `walk_forward_v7.py` validated it.

**Rule:** at each entry candidate date, rank all symbols in the universe by their 30-day return minus BTC's 30-day return. Only take entries in symbols that are in the top 20% (top-quintile) of relative strength.

**Motivation:** filter out "beta breakouts" — coins going up only because BTC is going up. These trades have poor forward expectancy because they're just leveraged BTC exposure with worse slippage. Keeping only genuine leaders concentrates risk in coins showing real strength.

**Validation (walk_forward_v7, 7 folds, 2022-2026 OOS):**

| Metric | Baseline | With rel-strength |
|---|---|---|
| Trades | 157 | **90 (−43%)** |
| Exp_R | +0.77 | **+1.31 (+69%)** |
| Trade Sharpe | +0.65 | **+0.86 (+31%)** |
| OOS ann_ret | +49.95% | +45.17% |
| OOS bootstrap LB | +0.207 (★) | +0.211 (★) |

Same annualized return with 43% fewer trades = less capital-at-risk-time and higher per-trade quality.

---

## 67. What does BLOCKED_REL_STRENGTH mean in the dashboard?

You'll see this verdict when a symbol has broken out (close > 55-day high) BUT its 30-day return does not rank in the top 20% of the active universe vs BTC.

Example: SOL breaks out today, but BTC is up +18% in the last 30 days while SOL is up only +12%. SOL is a beta laggard. `BLOCKED_REL_STRENGTH` — no entry.

If you see this verdict often but the aggregate universe is showing many breakouts, that's the filter doing its job. You want the rare, disciplined breakouts of true leaders, not the many.

BTCUSDT is exempt from this filter (it's the benchmark; can't be ranked against itself).

---

## 68. Should I override the rel-strength filter?

No. The whole point of walk-forward validation is to constrain your discretion. If you find yourself wanting to override, that's the psychological failure mode the system was built to prevent.

If you have a legitimate reason to believe the filter is broken (e.g., structural regime change, a symbol that shouldn't be in the universe), **fix the code, re-validate, then ship** — don't override the live rules.

---

## 69. What did we test that DIDN'T ship?

`walk_forward_v7.py` also tested:
- **Volatility-scaled sizing** (scale up in calm regimes, down in chaos)
- **Early-listing bias** (weight coins <2 years old higher)

Both produced *identical* R-multiple distributions to rs_only. That's not a bug — R-multiple is size-invariant by construction (R = pnl / risk_dollars). These features affect the equity curve (drawdown, dollar returns) but not per-trade R.

**Verdict:** we can't measure their real impact through the current bootstrap harness. Not shipped. Revisit only if we build an equity-curve-based comparison that can detect the drawdown difference.

---

## 70. What's the next candidate improvement to test?

Ranked by expected impact × feasibility:

1. **On-chain smart-money flow gating** — use exchange net-flow, whale accumulation. Add as another entry gate ("only long when smart money is accumulating"). Real edge, not yet crowded. Requires paid Glassnode/CryptoQuant subscription for multi-year historical validation.
2. **Delta-neutral funding-rate carry** — capture perp funding when macro is OFF. Deploys currently idle capital. Separate strategy, needs its own careful build.
3. **Funding-rate z-score signal** — extend binary funding filter to continuous z-score. Uses data we already have.

Do NOT re-run walk-forward with more parameter sweeps on Donchian itself. That's curve-fitting territory.

---

## 71. Why was multi-timeframe confirmation (MTF) tested and rejected?

Tested 2026-07-31 in `walk_forward_v8.py`. The variant `rs_mtf_confirm` added a weekly Donchian confirmation gate on top of `rs_only`.

**Rule tested:** entry fires only if daily 55-day breakout AND last completed weekly close > weekly 20-week high.

**Results:**

| Metric | rs_only (baseline) | rs_mtf_confirm |
|---|---|---|
| Trades | 90 | 44 (−51%) |
| Exp_R | +1.31 | +1.45 (+11%) |
| OOS trade Sharpe | +1.27 | +1.75 (+38%) |
| **OOS ann_ret** | **+45.17%** | **+24.77% (−45%)** |
| OOS bootstrap LB | ★ +0.224 | ★ +0.215 |

**Verdict: REJECT.** Rule (a) of the pre-committed decision rule fails — ann_ret dropped 45% while trade Sharpe improved 38%. This is a classic risk-off overlay: cuts both losses and wins in equal proportion, smoother equity curve, less absolute return.

**When to revisit:**
- If portfolio grows past ~$50k where Sharpe matters more than absolute return
- If we ever start leveraging (higher Sharpe supports higher leverage safely)
- If BTC vol regime shifts to less directional / more chop

**Do not stack:** MTF confirm was tested on top of rs_only. Do not add MTF back without re-testing the full stack. Don't compose validated improvements without validating the composition.

The code (`mtf_confirm.py`, `walk_forward_v8.py`) is kept in the repo for future revisit — do NOT delete.

---

## 72. What did the factor lab test and reject?

Tested 2026-07-31 in `factor_lab.py`: four documented crypto anomalies, run against OUR universe, OUR $50M liquidity floor, and OUR cost model (18bps round trip on turnover). Pre-committed rule: promote only if OOS starred AND net ann spread > 10%.

| Factor | Literature claim | Our result | Verdict |
|---|---|---|---|
| MAX (lottery) | High max-daily-return coins outperform ~3%/wk | OOS +15.2%, NOT starred | REJECT — effect lives in microcaps below our liquidity floor |
| Weekly reversal | Past-week losers beat winners | −89.5% ann, significantly NEGATIVE | REJECT — and the negative sign = weekly momentum confirmation |
| Day-of-week | Monday/weekend effects | Best t=2.49 (Sat), fails multiple-comparison bar (~2.7 for 7 tests) | REJECT — noise |
| BTC hourly | 21:00-23:00 UTC positive drift | REPLICATED (t=3.36/2.63; 22:00 holds OOS) but ~8bps/day vs 18bps costs | REJECT — real but unharvestable at retail costs |

**Key takeaways:**
1. The strongly negative reversal spread is independent confirmation that cross-sectional momentum (what our trend + RS system harvests) is the live factor at weekly horizons. Do NOT flip the sign and trade weekly momentum long-short — that's post-hoc snooping, and it would be highly correlated with the trend sleeve anyway.
2. The hourly anomaly's 23:00 negative hour means our 00:00 UTC daily-close execution already buys after the intraday run-up/dip cycle. No change needed.
3. Published anomaly alphas quoted in papers should be discounted 50-100% at real liquidity floors and costs. Four for four failed here.

Weekly return CSVs and `factor_lab_results.csv` preserved for reference.

---

## 73. What's the strategy roadmap? (as of 2026-07-31)

**Running:** (1) Donchian trend + macro + funding + RS filter — live. (2) Delta-neutral funding carry — paper until ~2026-09-01 review.

**Rejected with documentation** (see Q69, 71, 72): mean reversion, MTF confirm, MAX, weekly reversal, day-of-week, hourly seasonality. Do not re-test without new data or a changed regime.

**Deferred — each unlocks on a concrete condition, not on impatience:**

| Strategy | Unlock condition |
|---|---|
| Token unlock event shorts | Operator commits to ~1hr/month manual calendar research |
| Vol-scaled sizing (equity-curve test) | Whenever a 2-3 day build slot is worth it; low priority |
| On-chain smart-money gating | Paid data subscription (Glassnode/CryptoQuant, $30-100/mo) |
| Deribit vol selling | Portfolio > $25k + options experience |
| MTF confirm revisit | Portfolio > $50k or leverage deployment |

**Standing rule:** no new strategy research until either (a) one of the unlock conditions above is met, or (b) both live sleeves have produced enough data for their scheduled reviews (20 trend trades; 30-day carry review). Research during dormancy is how validated systems get overfit into dead ones.

---

## 74. What did the second factor-lab run (ANCHOR, BAB) find?

Tested 2026-07-31, same engine, same rule (OOS starred + >10% ann).

| Factor | Source | Full | OOS | Verdict |
|---|---|---|---|---|
| BAB (beta-neutral, Frazzini-Pedersen) | Crypto factor literature | +35.9% | **−4.2%** | REJECT — in-sample artifact, decayed OOS |
| ANCHOR (52-week-high nearness) | J. Banking & Finance 2025 | +26.2% | +29.7%, Sharpe 0.47, NOT starred | REJECT — near-miss |

**ANCHOR near-miss protocol (PRE-REGISTERED 2026-07-31):**
- Positive both samples, OOS ≥ full (no overfit signature), near-zero correlation with other factors — but CI spans zero at ~300 weeks.
- ONE follow-up permitted: re-run `python factor_lab.py` with IDENTICAL parameters around **August 2027** (~50 more weeks of data). If OOS stars then, promote to deeper testing INCLUDING a correlation check against the trend sleeve (ANCHOR is conceptually adjacent to Donchian 55d-high entries).
- NO parameter changes, NO quantile changes, NO lookback tweaks before then. If it fails the 2027 re-run, permanent tombstone.

Running tally across all factor research: 10 hypotheses tested, 1 shipped (RS filter), 1 in paper (carry), 8 rejected. The pipeline is empty again by design.

---

## 75. Why doesn't the system short during bear markets? (tested and rejected 2026-08-01)

Tested in `walk_forward_v9.py`: a dedicated macro-OFF short sleeve — short 55d-low breakdowns in BOTTOM-quintile relative-strength coins (mirror of the validated long rules), funding-gated, BTC excluded.

**Results (8 folds):**

| Variant | n | exp_R | ann_ret |
|---|---|---|---|
| macro_on (live system, control) | 89 | +1.51 | **+26.96%** |
| bear_shorts (the sleeve alone) | 27 | **−0.32** | **−1.70%** |
| combined | 116 | +1.09 | +25.26% (worse than macro_on alone) |

**Rejected 0-for-3 on the pre-committed rule.** Decisive detail: fold F1 (Sept 2022–Mar 2023, the deepest bear window in the sample — the sleeve's ideal habitat) still LOST −3.2%. Every fold was flat or negative. Bootstrap OOS CI [−0.71, +0.26], point estimate negative.

**Interpretation:** weak-coin breakdowns in bear markets do not trend cleanly — they chop and squeeze; stop-outs and costs eat the edge. Simple Earn (~1.7-5% APY) strictly dominates bear-market shorting at our costs and timeframe. The 300+ day dormancy is not a missed opportunity; it is the correct allocation, now directly evidenced rather than assumed.

**Do not revisit** without a structurally different short methodology (e.g., options-based, or intraday) — the daily-Donchian short family is exhausted. Fourth independent reproduction of the live system's edge (+1.5R) recorded in the same run.

Final research tally: **11 hypotheses, 1 shipped, 1 in paper, 9 tombstoned.**

---

## 76. Why not pairs trading / statistical arbitrage? (tested and rejected 2026-08-02)

Tested in `pairs_lab.py`: all 190 pairs from the top-20 liquid universe, rolling 90d formation (correlation >= 0.90, mean-reverting spread by AR(1)), z-score +/-2 entries, convergence/blowout/30d exits, full 4-leg costs (36bps).

**Result: statistically significant NEGATIVE edge.** 2,607 episodes, mean net −1.14%, FULL CI [−1.81, −0.47] entirely below zero; OOS [−1.49, −0.09] likewise. Blowouts (1,452) outnumbered convergences (731) two-to-one: crypto spreads that open are usually structural repricings, not temporary dislocations. Even BTC/ETH — the most cointegrated pair available — netted +0.08%/episode: zero after costs.

**The unifying lesson (third independent confirmation):** every mean-reversion structure tested in this project has failed — RSI reversion (0 valid), weekly cross-sectional reversal (significantly negative), and now pairs spread reversion (significantly negative). Crypto is a momentum market at every measurable level. Continuation wins; snap-back loses. Do not test a fourth mean-reversion variant without fundamentally different data (e.g., intraday microstructure).

**On the fear of permanent dormancy** (the motivation for this test): macro regime history since 2019 — ON 64% of days; longest OFF streak ever 332 days (2022), current streak ~250 days (second longest); every OFF streak has resolved into ON periods of 130-562 days. Dormancy is seasonal, not terminal.

Final research tally: **12 hypotheses, 1 shipped, 1 in paper, 10 tombstoned.**
