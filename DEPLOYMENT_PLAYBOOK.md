# Deployment Playbook — Donchian + BTC-Macro v1.0

*Print this. Put it next to your trading station. When you're tempted to deviate, read it first.*

---

## 1. What This Is

Long/short Donchian channel breakouts on 8 crypto USD-M perpetuals, gated by a BTC macro trend filter and a funding-crowdedness filter. Daily bars, Daily decisions. Validated on 2020-09 through 2026-05 with statistically significant out-of-sample expectancy (+0.6R per trade, 95% CI excludes zero, +27% annualized at 0.75% risk/trade).

The edge is regime-conditional: this strategy works in trending markets and bleeds in chop. You will have 6-9 month losing periods. Know this before you start.

## 2. Universe

| Symbol | Venue | Instrument |
|---|---|---|
| BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT | Binance | USD-M perpetual |
| AVAXUSDT, LINKUSDT, DOGEUSDT, XRPUSDT | Binance | USD-M perpetual |

Do not deviate from this universe without re-running the walk-forward harness.

## 3. Entry Rules

**LONG entry** fires when ALL of these are true at Daily close (UTC 00:00):

1. Daily close > rolling 55-day high (excluding today's bar)
2. BTC SMA(200) today > BTC SMA(200) twenty days ago *(macro regime ON)*
3. Most recent 8h funding rate ≤ +20 bps *(not crowded long)*
4. No existing position in this symbol

**SHORT entry** fires when ALL of:

1. Daily close < rolling 55-day low (excluding today)
2. BTC macro ON (same as above)
3. Most recent 8h funding rate ≥ −20 bps *(not crowded short)*
4. No existing position in this symbol

The dashboard's verdict column shows exactly this logic. If it says `LONG_ENTRY` you enter long, period. If it says anything else you do not.

## 4. Exit Rules

Close any open position at Daily close when ANY of these is true:

| Trigger | Condition |
|---|---|
| `channel_exit` | LONG: close < 20-day low. SHORT: close > 20-day high. |
| `atr_stop` | Price reaches the initial stop at 2 × ATR(20) from entry. |
| `time_stop` | Position has been open ≥ 90 days. |

Stops are placed at order entry and **never moved against you**. They may trail favorably after +1R unrealized — see code. Never widen a stop. Never average down. Never "give it more room."

## 5. Position Sizing — Worked Example

```
equity                  = $100,000
risk_per_trade          = 0.75% × equity         = $750
ATR(20) for BTC         = $1,500
stop_distance           = 2 × ATR                = $3,000
position_size_BTC       = $750 / $3,000          = 0.25 BTC
notional_exposure       = 0.25 × $60,000         = $15,000  (15% of equity)
leverage                = 15,000 / 100,000       = 0.15×    (essentially unlevered)
```

If the vol-cap math (position annualized vol ≤ 15% of equity) gives a smaller size than the risk math, use the smaller number. Never the larger.

**Portfolio heat cap:** sum of (risk_per_trade × n_open_positions) must stay ≤ 3% of equity. With 0.75% per trade, the cap is 4 simultaneous open positions. New signals beyond this are skipped.

## 6. Daily Checklist (5 minutes, every day)

At your chosen UTC time after the daily candle closes:

- [ ] Run `python dashboard.py`
- [ ] Open `dashboard.html` in browser, hit refresh
- [ ] **Check regime bar.** BTC macro ON? Heat under cap? System OK?
- [ ] **Review signals table.** Act on every `LONG_ENTRY` / `SHORT_ENTRY` row, in order. Skip every `NONE` / `BLOCKED_*` row.
- [ ] **Place orders** on the exchange. Use limit-or-better at the day's close price. Mark the stop.
- [ ] **Add the new trade** to `live_trades.csv` with `entry_date`, `entry_price`, `size`, `stop`, `risk_dollars`. Leave exit fields empty.
- [ ] **Review open positions.** Any that need to exit per rules? Close them.
- [ ] For any closed trade today, fill `exit_date`, `exit_price`, `pnl_net`, `r_multiple`, `exit_reason`, `bars_held` in the CSV.

If the dashboard shows zero `LONG_ENTRY`/`SHORT_ENTRY` rows and no exits needed, you are done. **Doing nothing is the correct action most days.**

## 7. Decision Tree for Unusual Events

| Event | Action |
|---|---|
| Exchange outage during a signal | Skip the signal. Wait for next daily close. |
| Funding rate spikes > 100 bps mid-trade | Keep position. Funding is a known cost. |
| BTC macro flips OFF while you have open positions | Keep all open positions (manage to normal exits). Take no new entries. |
| 30+ consecutive days of all-NONE signals | Continue waiting. This is normal in macro-OFF or low-vol regimes. |
| Drawdown reaches 15% | Pause new entries for 7 days, review trade log for rule deviations, then resume. Do not size down. |
| Drawdown reaches 20% | Stop. Re-validate the strategy by re-running `walk_forward_v3.py` on data through today. If OOS expectancy is still positive, resume at 50% size. If not, halt deployment. |
| Single trade slippage > 50 bps | Log it. If it happens 3+ times in a quarter, switch venues. |
| Bug in `dashboard.py` or signals look wrong | Halt new entries until verified. Manage existing positions per documented rules. |

## 8. Drawdown Expectations

These are what you should expect, not what scares you. Calibrated to 0.75% risk/trade:

| Scenario | Drawdown range |
|---|---|
| Normal chop period (no edge that month) | 3-6% |
| Bad regime (e.g. 2022-23 style chop) | 10-15% |
| Tail event (correlated cascade) | 18-22% |
| Strategy broken (re-validate) | > 25% |

If you cannot psychologically hold through 15%, size down to 0.5% risk/trade. **It is better to deploy at smaller size you can hold through than full size you cannot.**

## 9. Performance Milestones — What's Normal

| Time horizon | Expectation |
|---|---|
| Month 1 | 0-5 trades, frequently net negative. Pure noise. |
| Months 2-3 | 5-15 trades. Statistical signal still drowning in variance. Do not draw conclusions. |
| Months 6-12 | 30-50 trades. Edge starts to emerge. Realistic to be ±10% on equity. |
| Year 1 (full) | 50-80 trades. Expect 10-25% annualized return at default sizing if regime cooperative. Could be −5 to −10% in a chop year. |
| Year 2+ | Sample size starts to mean something. If you're meaningfully negative after 200+ trades, strategy is dead — see §11. |

Do not size up until you have at least 100 closed trades **and** at least one realized drawdown ≥ 10% that you held through.

## 10. Override Protocol

### NEVER override on:
- A single losing trade
- News, FUD, market commentary, X/Twitter posts, YouTube videos
- A "gut feeling"
- Pundit predictions, on-chain "signals" you don't have tested edge for
- Fear during drawdown
- Greed during winning streaks (do not size up beyond schedule)
- The fact that you've held the position for too long and want it to be over

### YOU MAY override when:
- Exchange technical issue makes order placement unreliable
- Wallet/account compromise — close everything immediately
- 60+ days of BTC macro ON AND every trade losing AND your win rate < 15% — *consider* pausing 30 days. This is the only judgement call in the system.
- Position size error in execution (e.g. fat finger 10× the intended size) — close to correct size immediately

Every override must be logged with date, reason, and what was done. If you override more than 2x per quarter, you are no longer running this strategy — you are discretionary trading. Stop and reassess.

## 11. When to Pull the Plug

The strategy is officially dead and you should stop trading it when:

1. You have ≥ 150 closed trades AND
2. OOS bootstrap on those 150 trades shows CI95 lower bound ≤ 0 AND
3. You have re-run `walk_forward_v3.py` on data through today AND the aggregate OOS expectancy is no longer starred.

If only (3) becomes true but (1) and (2) haven't been reached, that's just a bad regime — keep going.

If (1) and (2) become true, it means the edge has decayed. Either the market structure has changed (more bots, more funding, less retail), or your execution is worse than backtested. Investigate, do not just keep trading.

## 12. Logging Discipline

For every trade, in addition to the CSV columns, keep a separate note recording:

- **Time signal appeared** (the dashboard generation timestamp)
- **Time order was placed** on the exchange
- **Difference between signal price and fill price** in bps (slippage)
- **Did you actually follow the rules** (Y/N — if N, why not)
- **Mental state at time of trade** (calm / fearful / greedy / bored)

After 90 days, review the log. If "fearful" and "greedy" entries cluster around your worst trades, sizing is too aggressive for your psychology — drop to 0.5% per trade.

## 13. Quarterly Review

Every 90 days, regardless of P&L:

- [ ] Run `python bootstrap.py --file live_trades.csv` and compare to backtest aggregate
- [ ] Run `python walk_forward_v3.py` with current data and verify the system still passes deployment criteria
- [ ] Count overrides. If > 2, write a one-page note on why and what to fix
- [ ] Review your psychological log
- [ ] Decide: continue at current size, change size, pause, or stop

## 14. Strategy Parameters Reference

For when you forget what the numbers are:

```
N_ENTRY            = 55       # entry channel lookback
N_EXIT             = 20       # exit channel lookback
ATR_PERIOD         = 20
ATR_STOP_MULT      = 2.0      # initial stop = 2 × ATR
TIME_STOP_BARS     = 90       # forced exit after N days

BTC_SMA_PERIOD     = 200      # macro filter
BTC_SLOPE_LOOKBACK = 20

FUNDING_LIMIT_BPS  = 20.0     # block entries when funding crowded

RISK_PER_TRADE     = 0.0075   # 0.75% of equity at risk per trade
VOL_TARGET_ANNUAL  = 0.15     # 15% annualized per-position vol cap
PORTFOLIO_HEAT_CAP = 0.03     # max 3% total open risk

TAKER_FEE_BPS      = 4.0      # per side
SLIPPAGE_ASSUMED   = 5.0      # per side
```

## 15. The Most Important Page

If you read nothing else, read this:

> **The strategy works in aggregate, not in any single trade.** Every individual entry has ~35% chance of being a winner. Most of your money comes from a small number of large winners. You will have losing streaks of 5-8 in a row. They are not the strategy breaking. They are the strategy working as designed.
>
> Your job is not to be right on every trade. Your job is to follow the rules. The rules are right on average. You are not.

---

*Generated for the validated Donchian + BTC-macro variant. If you change the strategy, update this playbook. If you find yourself deviating from this playbook, stop and ask why — that's the most important question in the whole system.*
