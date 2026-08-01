# Multi-Timeframe Structural Continuation — Codified Spec v0.1

A deterministic translation of the discretionary framework into rules that any operator (or machine) can implement identically. This is a starting point for backtesting — not a tuned production strategy.

## 1. Universe and Data

- **Assets:** BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT (Binance USD-M perpetuals).
- **Bars:** Daily (primary structural timeframe) and 4H (execution timeframe).
- **Period:** 2019-09-01 → present. Earlier than this Binance perp history is thin.
- **Price space:** all trendline math is done in **log price** (ln(close)). This normalizes structural slopes across price regimes.

## 2. Trendline Construction (zero-discretion)

For each asset and each bar, two structural lines are maintained.

### 2.1 Upper line (bullish continuation reference, "support")
- **Anchor A1:** the highest Daily close in a trailing 180-bar window.
- **Anchor A2:** the Daily close N=3 bars after A1.
- **Line:** linear in (bar_index, ln(close)) space, projected forward.
- **Refresh rule:** if a new 180-day high close prints, both anchors reset and a new line is drawn.

### 2.2 Lower line (bearish continuation reference, "resistance")
- **Anchor B1:** the lowest Daily close in a trailing 180-bar window.
- **Anchor B2:** the Daily close N=3 bars after B1.
- **Line:** linear in (bar_index, ln(close)) space, projected forward.
- **Refresh rule:** if a new 180-day low close prints, both anchors reset.

### 2.3 Sanity guards
- If A2 ≥ A1 (line slopes upward through ATH), discard — wait for next anchor.
- If B2 ≤ B1 (line slopes downward through ATL), discard.
- Minimum bars between anchor reset: 5 (prevents thrash).

## 3. Signal Logic

All signals are evaluated on **Daily close** only. Intraday wicks are ignored.

### 3.1 Long entry (bullish breakout)
- Daily close > lower-line value at that bar + k × ATR(14)
- AND the previous bar's close was ≤ lower-line value
- AND higher-timeframe filter: 50-day SMA of close is rising over last 10 bars (cheap macro-trend proxy)
- AND optional funding filter (see §6)

### 3.2 Short entry (bearish breakdown)
- Daily close < upper-line value at that bar − k × ATR(14)
- AND previous bar's close was ≥ upper-line value
- AND 50-day SMA is falling over last 10 bars
- AND optional funding filter

### 3.3 Parameters
- `k = 0.25` (ATR multiplier for break threshold)
- `ATR_period = 14`
- `sma_period = 50`, `sma_slope_lookback = 10`

## 4. Exits

### 4.1 Stop loss (initial)
- Long: entry_close − 1.5 × ATR(14)
- Short: entry_close + 1.5 × ATR(14)

### 4.2 Trailing stop (after +1R favorable move)
- Move stop to breakeven once unrealized PnL ≥ 1R.
- After +2R, trail at 2 × ATR below highest close since entry (longs) or above lowest close (shorts).

### 4.3 Time stop
- Close any position open > 60 Daily bars regardless of PnL. Forces sample independence.

### 4.4 Structural invalidation
- Long position closed if Daily close < upper-line value (the bullish-continuation reference is violated).
- Short position closed if Daily close > lower-line value.

## 5. Position Sizing

### 5.1 Per-trade risk
- Risk per trade = 0.75% of current equity.
- Position notional = (risk_per_trade × equity) / |entry_price − stop_price|.

### 5.2 Volatility cap
- Annualized notional vol target: 15% of equity per position.
- If the vol-implied size is smaller than the risk-implied size, use the smaller. Never the larger.

### 5.3 Portfolio heat cap
- Sum of open per-trade risks ≤ 3% of equity.
- New entries blocked when this cap is hit.

### 5.4 No pyramiding in v0.1
- One position per asset at a time. Pyramiding can be layered in v0.2 after baseline edge is established.

## 6. Crypto-Native Filters (optional, v0.1.1)

These get bolted on after the baseline backtest. They are listed here so the data pipeline accounts for them up front.

- **Funding rate filter:** skip longs when 8h funding > +20 bps for 3 consecutive readings (crowded long, mean-reversion risk). Mirror for shorts.
- **BTC dominance regime:** for alts, require BTC.D direction to be flat or favorable to the alt trade.
- **Liquidation proximity:** avoid entries within 1 × ATR of dense liquidation clusters (requires Coinglass or similar; out of scope for v0.1).

## 7. Costs and Frictions Applied in Backtest

- **Taker fee:** 4 bps per side (8 bps round trip). Conservative for Binance VIP-0.
- **Slippage:** 5 bps per side (assumes retail-to-small-pro size).
- **Funding carry:** historical 8h funding accrued against position direction for the holding period.
- **No borrow cost** assumed (perps).

## 8. Metrics Reported

Per asset and aggregated:
- Trade count
- Hit rate, average win (R), average loss (R), expectancy (R)
- CAGR, annualized vol, Sharpe (after costs)
- Max drawdown, drawdown duration
- Calmar ratio
- Profit factor
- Distribution of trade durations
- Equity curve

## 9. Validation Protocol

1. **In-sample:** 2019-09-01 → 2023-12-31. Use only this period for any parameter sanity checks.
2. **Out-of-sample:** 2024-01-01 → present. Untouched until in-sample is frozen.
3. **Bootstrap CI:** resample trade outcomes 10,000× to compute 95% CI on Sharpe and expectancy.
4. **Shuffled-bars control:** rerun on a randomly shuffled bar series. Expectancy should collapse to ~0; if it doesn't, the rules have lookahead.
5. **Cost sensitivity:** rerun with 2× costs. If edge disappears, the strategy is not viable at retail.

## 10. Decision Gates

| Out-of-sample result | Action |
| --- | --- |
| Sharpe < 0.3 after costs | Discard. No edge. |
| Sharpe 0.3–0.7 | Iterate — add funding/dominance filters, tune k and N. |
| Sharpe 0.7–1.2 | Paper-trade live for 90 days. |
| Sharpe > 1.2, DD < 25% | Begin live deployment at 25% of intended size. |

## 11. Known Limitations

- Trendlines from extrema are inherently lagging — by construction, a new ATH must print before the bullish line redraws. Expect late entries in the first leg of a new cycle.
- The 180-day anchor window is arbitrary. Walk-forward tuning needed.
- Daily bars miss intraweek liquidation cascades that 4H would catch. v0.2 should add a 4H confirmation layer.
- Survivorship bias on the asset universe — these are the survivors of 2018–2022. Backtest results overstate the true forward-looking edge for any "top N coins" strategy.
