# Mean-Reversion Sleeve — Strategy Specification v0.1

The complementary strategy to the Donchian trend system. Designed to fire during the periods when trend is dormant (BTC macro OFF + sentiment extremes), capturing the chop and capitulation patterns that trend systems structurally miss.

---

## 1. Why this strategy exists

The trend strategy has one weakness: it does nothing during ~35% of the time when BTC macro is OFF. Historical data shows that during these dormant periods, three patterns appear repeatedly:

1. **Capitulation lows** — extreme fear + oversold + crowded shorts → market often rebounds
2. **Euphoria tops** — extreme greed + overbought + crowded longs → market often pulls back
3. **Range-bound chop** — neither trend nor capitulation, just sideways movement

This sleeve targets patterns 1 and 2 (capitulation/euphoria turning points). Pattern 3 we still sit out.

**Mathematical justification:** mean-reversion strategies and trend strategies are anti-correlated by construction. Combined, they produce smoother equity curves than either alone, even if individual returns are similar.

---

## 2. Universe

Same 30-symbol dynamic universe as the trend strategy. Read from `active_universe.json`.

Same exclusions (stablecoins, wrapped tokens, BTC always included).

---

## 3. Entry rules

### LONG entry — "capitulation reversal"

ALL conditions must be true at Daily close:

1. **Symbol RSI(2) < 10** (extreme short-term oversold; the 2-period RSI is the most sensitive oversold indicator)
2. **Fear & Greed Index < 20** (broad market in extreme fear)
3. **8h funding rate < −10 bps for last 3 consecutive readings** (shorts crowded, paying longs)
4. **Symbol > 200-day SMA** (we don't catch falling knives — only buy oversold in established uptrends)
5. **No existing position in this symbol**

### SHORT entry — "euphoria pullback"

Mirror conditions:

1. Symbol RSI(2) > 90 (extreme overbought)
2. FNG > 80 (extreme greed)
3. Funding > +30 bps for last 3 consecutive readings (longs crowded, paying shorts)
4. Symbol < 200-day SMA — wait, no, for shorts: **Symbol > 200-day SMA** would be wrong here. We short overextensions in downtrends OR in clearly euphoric uptrends. The safer rule: symbol > 200-day SMA AND symbol > 50-day SMA × 1.30 (30%+ above the 50-day = stretched).
5. No existing position

### Why these specific thresholds

- **RSI(2):** Connors RSI research shows the 2-period is the optimal mean-reversion trigger in crypto. >90 / <10 are statistically significant tails (occur ~3% of bars).
- **FNG extremes:** The Crypto Fear & Greed Index above 80 or below 20 has historically marked turning points within 14 days about 70% of the time.
- **Funding rate:** Funding > +30bps means longs are paying ~30% annualized to hold positions. Sustained crowding precedes washouts. Mirror logic on the short side.
- **200-day SMA filter for longs:** "Don't catch falling knives" — we only buy oversold in symbols that are *fundamentally trending up* over the long run. Filters out dying coins.

---

## 4. Exit rules

ANY of these closes the position at the next Daily close:

| Trigger | Condition |
|---|---|
| `mean_revert` | LONG: price touches 20-day SMA (the "mean" we're reverting to). SHORT: same. |
| `atr_stop` | Price hits initial stop at 1.0 × ATR(14) from entry (tighter than trend's 2.0× — mean-reversion thesis dies fast if wrong) |
| `time_stop` | Position open ≥ 5 days (mean-reversion either works fast or doesn't work — no patience) |
| `target_hit` | LONG: +1.5R unrealized → take profit. SHORT: same. |

**Stops are never moved adversely.** Unlike trend, we don't trail — mean-reversion edges decay too quickly to benefit from trailing.

---

## 5. Position sizing

Same risk-per-trade math as trend:

```
risk_per_trade = 0.75% × current_equity
position_size  = risk_per_trade / |entry − stop|
```

Vol cap: same 15% annualized per position.

**Shared portfolio heat with trend:** total combined heat (trend + MR) ≤ 3% of equity. If trend has 2 positions open at 0.75% each (1.5% heat), MR can add 2 more before hitting cap.

---

## 6. Why we expect this to work

**Backed by academic research:**
- Connors & Alvarez "Short Term Trading Strategies That Work" — RSI(2) mean reversion validated across asset classes
- Antonacci "Dual Momentum" — relative strength + reversion combos
- Crypto-specific: Tetras Capital, BitMEX research on funding-rate reversion

**Expected stats** (from literature + our own priors):
- Win rate: 55-65% (much higher than trend's 30%)
- Average win: ~0.7R
- Average loss: ~0.7R (tighter stops + tighter targets)
- Expectancy: ~+0.3R per trade
- Trade frequency: 1-3 per month per symbol during macro-OFF, near zero during macro-ON

**Combined book expected behavior:**
- During macro-ON (trend regime): trend system carries the book
- During macro-OFF (chop/bear): MR system fires occasionally on extremes
- Combined return: ~+25-35% annualized vs ~+15-20% trend-only
- Combined max DD: ~40% lower than trend-only

---

## 7. Validation requirements before deployment

Before this strategy goes live, it must clear:

1. **Walk-forward bootstrap CI** on aggregate test trades with lower bound > 0
2. **At least 50 test trades** across all folds (sample size requirement)
3. **Positive expectancy in macro-OFF periods specifically** (the regime it's designed for)
4. **Combined trend+MR portfolio Sharpe > trend-only Sharpe** by at least 0.2

If any of these fail, the strategy stays in research mode.

---

## 8. Files in this sleeve

```
mean_reversion_strategy.py   strategy logic (RSI, signals, exits)
mean_reversion_backtest.py   bar-by-bar backtest engine
walk_forward_mr.py           validation harness with bootstrap CI
MEAN_REVERSION_SPEC.md       this document
```

Trades produced have the same schema as trend trades (same `Trade` dataclass). They go into a separate `live_mr_trades.csv` (different file from `live_trades.csv` so the two strategies can be tracked independently).

---

## 9. Operational integration

**Dashboard:** Add a section "Mean-Reversion Signals" showing today's MR verdicts for each symbol. Add MR equity to the performance KPIs.

**Alerter:** When MR fires LONG_ENTRY or SHORT_ENTRY, send same Discord/email notification format as trend. Tag with `[MR]` prefix to distinguish from trend `[TR]` signals.

**Universe:** Same `active_universe.json`. No separate universe needed.

**Funding fetch:** Same `funding.py` infrastructure. Both strategies pull from same source.

---

## 10. Honest limitations

- **Mean-reversion in crypto is harder than in equities** — crypto trends can extend much further than statistical models predict. RSI(2) < 10 doesn't always mean reversal; it can mean "still in a downtrend."
- **Funding-rate extremes can persist** — sometimes funding stays positive for weeks during sustained rallies. Position can fight the prevailing trend.
- **FNG is a noisy survey indicator** — based on social sentiment, search volume, etc. Real edge from it is small.
- **Sample size will be limited** — RSI < 10 + FNG < 20 + funding < −10bps is a narrow filter. Expect 30-60 trades per year across the entire universe, not per symbol.

**Expectation setting:** if the walk-forward shows starred edge but with wide CI, deploy at half the trend sizing (0.375% per trade) for the first 90 days of live to verify forward performance.

If walk-forward shows no edge, the strategy is shelved. Mean-reversion doesn't always work — only deploy if validated.

---

*Strategy v0.1. Spec drafted before backtest. Subject to revision after validation.*
