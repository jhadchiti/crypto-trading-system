# Delta-Neutral Funding Carry — Strategy Specification v1.0

Status: **DESIGN — not validated, not live.** Backtest first (`funding_carry_backtest.py`), then paper, then live.

Purpose: deploy capital that sits idle while BTC macro is OFF (300+ days as of 2026-07-31). Structurally uncorrelated with the Donchian trend strategy.

---

## 1. Economic rationale

Perpetual futures have no expiry; the funding mechanism tethers perp price to spot. When perps trade rich (crowded longs), longs pay shorts a funding rate every 8 hours. A trader who is **short the perp and long an equal amount of spot** has zero net price exposure and collects the funding as income.

This is a risk premium, not an anomaly: you are being paid to warehouse the crowded side of the book. It persists because it requires capital on two legs, operational discipline, and tolerance for the rate flipping negative.

Documented performance (2025 practitioner + academic data): 10-20% APY on deployed capital, drawdowns under 3%. Expect the lower half of that range going forward — the trade is getting crowded.

## 2. Position structure

For a chosen symbol and notional N:

| Leg | Venue | Direction | Size |
|---|---|---|---|
| Spot | Binance Spot | BUY (hold) | N |
| Perp | Binance USD-M | SELL (short) | N |

Delta ≈ 0. Income = funding rate × N every 8h while funding is positive.

**Capital required** at perp leverage L: `N + N/L`. At L=2: capital = 1.5N → funding APY on capital = funding APR × N / 1.5N = ×0.667. The backtest reports return on *capital*, not notional.

Leverage cap: **L ≤ 2**. Liquidation on the short perp requires price to roughly double against you before the spot hedge is realized — at L=2 with active monitoring this is manageable; higher leverage is how documented blowups happen.

## 3. Entry rule

ALL must hold:

1. Trailing mean funding over the last **9 events (3 days)** ≥ **ENTRY_BPS = 10 bps/8h** (≈ 11% APR gross)
2. Symbol in the active liquid universe (24h volume ≥ $50M) — same floor as trend strategy
3. Spot market for the symbol exists on Binance (excludes 1000-prefixed perps unless mapped to underlying spot)
4. Max concurrent carry positions: **K = 3** (equal capital split)
5. Symbol not currently held by the trend strategy (avoid hedging our own trend position)

## 4. Exit rule

ANY closes the position:

1. Trailing 9-event mean funding ≤ **EXIT_BPS = 3 bps/8h**
2. Any single funding event < **−5 bps** (regime flip; don't wait for the mean)
3. Position age > 90 days (stale-position audit, same discipline as trend time stop)

## 5. Cost model (backtest and live)

| Cost | Value | Notes |
|---|---|---|
| Spot taker fee | 10 bps | per side (0.10%, standard tier) |
| Perp taker fee | 4 bps | per side |
| Slippage | 5 bps | per leg per side |
| **Round trip total** | **≈ 48 bps of notional** | 4 executions × (fee + slippage) |

Implication: at 10 bps/8h funding (30 bps/day), the round-trip cost is recovered in ~1.6 days of funding. Episodes shorter than ~3 days are likely unprofitable — the entry rule's 3-day persistence requirement exists precisely to avoid churn.

## 6. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Funding flips negative | Exit rule #2 (single-event −5bps hard exit) |
| Perp price spike → margin call on short | L ≤ 2, alerts via daily_check, keep 25% spare margin |
| Basis widening at entry/exit | Use limit orders where possible; cost model already conservative |
| Venue risk (exchange failure) | Same Binance risk as the trend strategy; not additive |
| Churn (fees eat funding) | 3-day persistence entry; backtest measures episode-length distribution |
| Correlation with trend strategy | Rule 3.5 blocks overlap; carry earns most in euphoric longs (macro ON) and squeezes — partially anti-correlated with trend drawdowns |

## 7. Backtest design (`funding_carry_backtest.py`)

- Data: full 8h funding history per symbol from Binance (`fetch_funding`), 2019→now where available
- Per-symbol simulation of the entry/exit state machine with the cost model
- Portfolio simulation: at each 8h step, capital split equally across up to K active positions; idle capital earns SIMPLE_EARN_APY (default 4%) as the benchmark alternative
- Outputs: `funding_carry_episodes.csv` (every episode with gross/net return, duration), `funding_carry_summary.csv`, console report

**Metrics that matter:** net APY on capital, % of time deployed, episode win rate, worst episode, worst 30-day window, comparison vs. pure Simple Earn.

## 8. Pre-committed decision rule

Ship to paper trading only if backtest shows ALL:

1. Net APY on capital > **7%** (must clearly beat Simple Earn ~4-5% after the extra risk)
2. Episode win rate > **70%** (carry should win most episodes; if not, costs dominate)
3. Worst 30-day window > **−2%** (this is a yield strategy; it must not have trend-sized drawdowns)
4. ≥ **30 episodes** in the sample (statistical floor)

If it fails any: stay in Simple Earn and drop the project. That outcome is fine — Simple Earn is the honest benchmark, not zero.

## 9. Deployment path (if validated)

1. Backtest → this doc's decision rule
2. Paper trade 30 days (log hypothetical episodes from live alerter data)
3. Live with $200 (20% of capital), L=1 (no leverage) for first month
4. Scale to 50% of idle capital, L≤2, only after one clean live month
5. Trend strategy always has capital priority: if BTC macro flips ON, carry positions are wound down as trend signals consume capital

---
*Created 2026-07-31. Companion code: `funding_carry_backtest.py`.*
