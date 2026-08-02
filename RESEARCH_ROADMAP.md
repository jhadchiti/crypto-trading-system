# Research Roadmap — the gated strategy-testing pipeline

*Created 2026-08-02. This is the complete, pre-registered list of what we may
test next, what would have to be true first, and how each test will be judged.
It exists so research reopens on TRIGGERS, not on impatience.*

---

## 0. Principles (read before adding anything)

1. **Pre-registration.** Every test's rules, parameters, and pass/fail bar are
   written BEFORE running it. No tuning after seeing results. One shot each.
2. **The false-positive budget.** At a 95% bar, ~1 in 20 worthless strategies
   shows a starred result by luck. We have run 12 tests (expected false stars
   so far: ~0.6). Budget: **max 4 new tests per year** on the same price
   dataset. Tests on genuinely NEW data (on-chain, options) don't share this
   contamination and get their own budget.
3. **Every result is recorded** — ships go to specs, failures go to the
   SYSTEM_QA tombstone registry. Nothing is retested without new data.
4. **The bar always includes:** OOS bootstrap CI lower bound > 0, minimum
   sample size, no single-fold dependence, and net-of-costs at our liquidity.
5. **What legitimately reopens research:** an unlock condition below is met ·
   a scheduled review fires · live results diverge from backtest · a regime
   change breaks a live strategy. **What does not:** dormancy, FOMO, a
   YouTube video, or a good month elsewhere.

---

## 1. Tier 0 — Already running (the live research program)

These ARE the active experiments; they produce evidence on a schedule:

| Experiment | Readout date | Question |
|---|---|---|
| Funding carry paper trial | **2026-09-01** | Do live episodes match backtest (81% win, ~200bps median)? Go/no-go for $200 live tranche |
| Trend live calibration | after **20 live trades** | Is live R ≥ 60% of backtest R? If not, re-validate everything |
| Quarterly re-validation | every ~3 months | Re-run walk_forward_v3/v7 — does the edge still hold on fresh data? |
| ANCHOR re-test (pre-registered, Q74) | **2026-08** (2027) | Identical factor_lab run with ~50 more weeks; stars → promote, fails → permanent tombstone |

## 2. Tier 1 — Unlocked by PAID DATA (~$30-100/month, Glassnode or CryptoQuant)

The only tier with genuinely new information content. Unlock when willing to
pay for 3+ months of data (needs full history download for validation).

| # | Hypothesis | Test design | Prior | Ship bar |
|---|---|---|---|---|
| 1.1 | **Exchange netflow gate**: block trend longs when exchange inflows spike (holders selling into breakout) | Add as 5th gate to walk_forward_v7 harness; compare rs_only vs rs_only+netflow | 35% | OOS starred AND exp_R ≥ rs_only's |
| 1.2 | **Stablecoin supply regime**: aggregate stablecoin mcap growth as an alternative/additional macro filter vs BTC SMA200 | Replace/AND with macro filter in v3 harness, side by side | 30% | Beats SMA200 macro variant OOS |
| 1.3 | **Whale accumulation tilt**: overweight RS-eligible coins with rising >$1M-wallet balances | RS-filter refinement in v7 harness | 25% | OOS starred AND exp_R ≥ rs_only's |

## 3. Tier 2 — Unlocked by CAPITAL

| # | Strategy | Unlock | Prior | Notes |
|---|---|---|---|---|
| 2.1 | **Deribit vol selling** (variance risk premium: sell strangles/covered structures on BTC/ETH) | portfolio ≥ $25k + 3 months paper on Deribit testnet | 45% — VRP is a real documented premium | Blowup-risk class: position sizing rules must be written BEFORE data access. Sharpe 1.2-1.8 documented in equities-analog |
| 2.2 | **Cash-and-carry basis** (long spot / short quarterly future at premium) | portfolio ≥ $10k (capital lockup to expiry) | 60% — arithmetic, not alpha | Yield-class like funding carry; only worth complexity if quarterly premium > Simple Earn + 4% |
| 2.3 | **MTF weekly confirm revisit** (Q71) | portfolio ≥ $50k OR leverage use | already validated Sharpe+38%/return−45% | Not a re-test — a re-DECISION when Sharpe becomes worth more than return |

## 4. Tier 3 — Unlocked by OPERATOR TIME (manual research, ~1hr/month)

| # | Strategy | Design | Prior | Ship bar |
|---|---|---|---|---|
| 3.1 | **Token unlock event shorts**: short >10% supply-dilution events 7-14d before unlock, cover 3-5d after | Manual calendar (Tokenomist/similar) + event log; paper 10 events before any live | 40% — documented drift, but crowding increasing | ≥7 of first 10 paper events profitable after costs |

Note: this is the ONLY strategy available today with no money gate — the gate
is Joseph committing to monthly calendar homework. It also works in bear
markets. If dormancy anxiety returns, this is the productive outlet.

## 5. Tier 4 — Unlocked by NEW INFRASTRUCTURE (probably never at retail)

Listed to make the refusal explicit: intraday strategies (need execution
quality we don't have), order-book/microstructure models (need tick data +
colocation), ML price prediction (needs more history than crypto has),
market making (institutional moat). **These stay closed regardless of capital.**

## 6. The Tombstone Registry — NEVER retest without new data

| Strategy | Verdict | Where |
|---|---|---|
| Structural trendline framework | significantly negative | original backtests |
| RSI/FNG mean reversion | no valid signal | MEAN_REVERSION_SPEC |
| MTF weekly confirmation | Sharpe+/return− trade-off, wrong at current size | Q71 |
| MAX / lottery factor | dies at our liquidity floor | Q72 |
| Weekly cross-sectional reversal | significantly negative (= momentum confirmation) | Q72 |
| Day-of-week seasonality | fails multiple-comparison bar | Q72 |
| BTC hourly seasonality | real but unharvestable (8bps vs 18bps costs) | Q72 |
| Betting-against-beta | in-sample artifact, decayed OOS | Q74 |
| 52-week-high ANCHOR | near-miss — pre-registered re-test Aug 2027 ONLY | Q74 |
| Macro-OFF bear shorts | lost even in the 2022 bear | Q75 |
| Pairs / cointegration stat-arb | significantly negative; blowouts 2:1 over convergence | Q76 |

**The meta-lesson across all tombstones:** crypto pays continuation and
punishes reversion, at every scale tested. Any future hypothesis that is
secretly a reversion bet should be priced at a <15% prior.

## 7. Current queue state

- Tests available to run today with free data: **none** (space exhausted; budget preserved)
- Next scheduled research event: **carry review, 2026-09-01**
- Next unlockable test: Tier 3.1 (unlock = operator commitment) or Tier 1.x (unlock = data subscription)
- Annual same-data test budget remaining for 2026-27: 4

---
*Rule of the roadmap: if an idea isn't on this list, the first step is adding
it here WITH its unlock condition, prior, and ship bar — not testing it.*
