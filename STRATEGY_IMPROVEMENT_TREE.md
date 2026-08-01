# Strategy Improvement Decision Tree

A structured framework for deciding what to improve, in what order, when to stop, and how to validate. Read this **before** writing any new code. It prevents the most common failure modes in retail systematic trading: parameter fishing, sunk-cost iteration, and improvement theater.

---

## Section 0: When to Even Consider Improvements

Three preconditions before ANY improvement effort begins. If you can't answer YES to all three, **stop and operate the existing system longer.**

1. **Have you run the current system for 60+ days?**
   - If not: STOP. You don't yet know what its real-world performance is. Improvements without baseline data are blind.

2. **Do you have ≥ 20 realized trades to evaluate?**
   - If not: STOP. The signal is dominated by noise at smaller sample sizes. You'd be tuning to your imagination.

3. **Is your live expectancy demonstrably different from backtest?**
   - If yes (live is materially worse than backtest): the gap is *execution*, not *strategy*. Fix execution first.
   - If matching backtest: the strategy is working; improvements should target gaps it doesn't cover.

If all three are YES, proceed to Section 1.

---

## Section 1: The Diagnostic Tree

Ask one question: **What specific problem am I trying to solve?** The wrong question leads to the wrong improvement. Be honest about which branch you're actually in.

### Branch A: "I want higher absolute returns"

→ Go to **Section 2A**

**Pre-check before pursuing:**
- Is `STARTING_EQUITY × annualized_return` actually meaningful to your life? If you're at $1k and want "higher returns" — the absolute dollars from 25% vs 35% is $100. Compounding matters at scale.
- Is your risk-per-trade at the floor (0.5%)? Scaling sizing is the lowest-hanging-fruit return enhancement.

### Branch B: "I want lower drawdowns / smoother equity"

→ Go to **Section 2B**

**Pre-check:**
- What's your realized max DD? If < 15%, you're probably fine. Below 10% you might be over-conservative.
- Are you confusing volatility with risk? Smoother ≠ safer; sometimes it just means smaller.

### Branch C: "I want more trade frequency / less dormant time"

→ Go to **Section 2C**

**Pre-check:**
- Dormancy isn't a bug — it's the strategy refusing to trade in unfavorable regimes. The right question is "is dormant capital being deployed productively (yield)?" not "how do I force more trades?"

### Branch D: "I want robustness against regime change"

→ Go to **Section 2D**

**Pre-check:**
- This is the *only* branch where a second strategy is justified. Confirm you're in this branch by asking: "Would I be OK with my single strategy losing money for 12 consecutive months?" If no → branch D.

### Branch E: "I have a hypothesis and want to test it"

→ Go to **Section 2E (the validation playbook)**

**Pre-check:**
- Specifically WHAT is the hypothesis? Vague ideas don't survive contact with bootstrap testing. Sharpen the hypothesis before coding.

---

## Section 2A: Improvements that target absolute returns

In order of impact-to-effort ratio:

### A1. Scale up risk-per-trade (highest ROI, zero new code)

**When to do this:** After 60+ days live, 20+ trades, drawdowns matched backtest expectations.

**How:**
- 0.75% → 1.00% after 60 days successful paper trading
- 1.00% → 1.25% after another 60 days with no major rule violations
- 1.25% → 1.50% after first held-through drawdown (psychological proof)

**Expected impact:** Linear scaling. 2× risk = ~2× return AND 2× drawdown.

**Stop condition:** Realized DD reaches 20% — back off one tier.

**Why this beats new strategies:** Zero validation risk, zero overfitting risk, just sizing math.

### A2. Yield deployment on idle capital (zero new code)

**When:** Immediately. Already documented in SYSTEM_QA Q53-65.

**Impact:** +5-10% APY recovery on dormant cash (~35% of the time).

**Effort:** 5 clicks on Binance.

### A3. Universe expansion to top-50 (small build)

**When:** After verifying the current top-30 isn't capacity-constrained (most signals fire on a handful of symbols).

**How:** Edit `dynamic_universe.py` MAX_UNIVERSE_SIZE from 30 → 50. Re-validate via `walk_forward_v6.py`.

**Expected:** Marginal Sharpe improvement, more diversification, slightly more trade frequency.

**Validation gate:** Bootstrap CI on top-50 trades must have lower bound ≥ top-30 lower bound. If lower, revert.

### A4. Funding carry overlay (medium build)

**When:** Portfolio ≥ $10k AND you've verified trend strategy is operating cleanly.

**Concept:** Long spot + short perp = harvest funding rate. Market-neutral, captures yield from market structure.

**Effort:** ~2 weeks build. Requires both spot and perp accounts.

**Expected:** +5-15% baseline yield during bull manias; ~0% during chop. Doesn't compete with trend signals.

### A5. Bearish-regime activation (medium build)

**When:** Multi-month BTC downtrend currently visible (BTC SMA200 falling clearly).

**Concept:** Currently macro-OFF blocks all entries. Modified: when BTC SMA200 is *falling*, enable shorts only.

**Effort:** ~1-2 weeks. Modify macro filter logic, add walk-forward test.

**Expected:** Captures bear-market shorts the current strategy misses. Was untested in original spec.

**Validation gate:** Must pass walk-forward on 2022-23 data with starred CI on aggregate shorts. If fails: structurally validates the "sit out chop" design.

---

## Section 2B: Improvements that target drawdown reduction

### B1. Dynamic sizing (already built — verify it's on)

`walk_forward_v5` tested this. Empirical result: ~10-15% Calmar improvement at modest cost in CAGR.

**To enable:** in production logic, multiply `risk_per_trade` by `risk_multiplier(vol_state, corr_state, funding_state)` from `dynamic_sizing.py`.

### B2. Correlation-aware heat cap (small build)

**Problem:** Current 3% heat cap assumes positions are independent. In reality crypto positions correlate 0.85+ during stress — a 3% nominal heat = 8%+ realized risk.

**Fix:** Cap effective single-factor exposure at 2% of equity. When BTC + ETH + SOL are all long, the *combined* risk counts as one trade for the cap.

**Effort:** ~1 week. Update sizing logic, re-validate.

**Expected:** Reduces tail-drawdown risk by 30-50%. Slightly fewer max-concurrency moments.

### B3. Tighter initial stop on first 5 days (small build)

**Concept:** Most trend-strategy losers fail within the first 5 days. Use 1.0× ATR stop for the first 5 days, then loosen to 2× ATR if position survives.

**Effort:** Modify exit logic + re-walk-forward.

**Expected:** Reduces avg loss size by ~20% without changing avg win size. Calmar improves but per-trade R drops slightly.

### B4. Second uncorrelated strategy (large build)

**The genuinely impactful drawdown improvement.** Already attempted with MR sleeve (shelved — didn't validate).

**Other candidates to test (in order of viability):**
- **Volatility breakout** (different from price breakout — enter on ATR expansion)
- **Cross-sectional momentum** (long top-decile relative strength, short bottom)
- **Calendar effect** (specific days/hours when crypto reverses systematically)

Each requires the full spec → backtest → walk-forward → bootstrap pipeline. Budget 4-6 weeks per candidate.

---

## Section 2C: Improvements that target trade frequency

**Honest warning:** wanting "more trades" is often a tell for over-optimization or boredom. Few high-quality trades beats many marginal ones.

### C1. Universe expansion (see A3)

### C2. Shorter Donchian (RISKY — likely makes things worse)

**Concept:** 35/10 or 20/10 channels instead of 55/20.

**Already tested:** walk_forward_v3 swept this. Result: Sharpe roughly equal across (20/10), (40/15), (55/20), (100/30) — robustness, not improvement.

**Conclusion:** Don't bother. The parameter robustness is the proof we picked correctly.

### C3. Multi-timeframe confirmation (medium build, mixed results)

**Concept:** Donchian signal on Daily, confirmed by H4 breakout in same direction → trade.

**Expected:** Slightly more trades, slightly higher hit rate, slightly worse expectancy due to entry lag.

**Validation gate:** Combined Sharpe must exceed Daily-only Sharpe by ≥ 0.15. If not, revert.

### C4. Reduce BTC macro gate strictness (DANGEROUS)

**Concept:** Allow trades during chop (macro neutral, not just rising).

**Already tested:** walk_forward_v3 showed macro-OFF periods produce statistically significant *negative* expectancy. Loosening this filter actively destroys edge.

**Conclusion:** DO NOT do this regardless of how tempting it feels during dormant periods.

---

## Section 2D: Improvements that target regime robustness

### D1. Second uncorrelated strategy (see B4)

The right answer. Most impactful. Most effort.

### D2. Cross-asset macro overlay (medium build)

**Concept:** Track DXY (dollar strength), gold, equities S&P/NDX. When traditional markets in risk-off, dampen crypto trend signals.

**Effort:** Add fetchers for traditional market data, define regime classifier, re-validate.

**Expected:** Reduces drawdowns during global risk-off events (e.g., 2022 banking crisis). Small Sharpe improvement.

### D3. Asymmetric exit logic (small build)

**Concept:** Let winners run longer once they reach +3R (extend exit channel from 20 → 40 days). Trend literature suggests this adds 15-30% to expectancy.

**Effort:** Modify exit logic, walk-forward test.

**Expected:** Higher per-trade expectancy on winners, slightly lower win rate.

### D4. Adaptive parameter selection (DANGEROUS — overfitting trap)

**Concept:** "Optimize parameters based on recent regime."

**Why it fails:** This is in-sample optimization disguised as adaptation. Walk-forward in `walk_forward.py` already tested this — adaptive parameter picking produced *worse* OOS results than fixed parameters.

**Conclusion:** Static parameters are better than adaptive. Counterintuitive but proven.

---

## Section 2E: The Validation Playbook (For ALL improvements)

Any proposed change must clear ALL five gates before deployment.

### Gate 1: Bootstrap CI must exclude zero

Run walk-forward, then `python bootstrap.py --file <variant>_trades.csv`. CI95 lower bound on expectancy must be > 0.

**Why:** Statistical significance is the minimum bar. Without it, improvements are indistinguishable from luck.

### Gate 2: Out-of-sample expectancy ≥ 60% of in-sample

If IS expectancy is +1.0R but OOS is +0.2R, you've overfit. Real edges degrade modestly OOS (≤40%), not catastrophically.

### Gate 3: Aggregate trade count ≥ 100

Below 100 trades, bootstrap CIs are too wide to trust. If your improvement only fires 30 times in the test window, you don't have evidence — you have anecdote.

### Gate 4: Calmar improvement OR Sharpe improvement ≥ 15%

If the change doesn't move the needle on the primary risk-adjusted metric by at least 15%, the added complexity isn't justified. Marginal improvements get eaten by execution costs.

### Gate 5: Shuffled-bars control test passes

Take the same strategy logic and run it on randomly shuffled price bars. If it still shows positive expectancy, you've encoded look-ahead bias. The strategy must produce expectancy ≈ 0 on shuffled data.

**If ANY gate fails:** the improvement is rejected. No "but I think..." — that's how parameter fishing happens.

---

## Section 3: Stop Criteria (When to Stop Improving)

The hardest part. Knowing when "good enough" is good enough.

### Stop after 3 consecutive failures (3-strike rule)

If you've attempted 3 improvements and all failed validation, **stop and operate the existing system for at least 90 days.** The repeated failures are evidence you're in a parameter-fishing loop. Step away.

### Stop when Sharpe > 1.0 OOS

A trade-level Sharpe above 1.0 out-of-sample is genuinely good. Further improvements have diminishing returns and increasing overfitting risk.

### Stop when complexity exceeds maintenance budget

If you can't explain every line of strategy logic in 30 seconds, the strategy is too complex for you to operate confidently. Strip back until you can.

### Stop when live performance matches backtest

If realized Sharpe and DD are within 25% of backtest expectations for 6+ months, the strategy is working as designed. Further "improvement" is solving non-problems.

### Stop when you can't articulate the problem

"I want it to be better" is not a problem. "I want max DD below 12%" is. "I want trade frequency above 80/year" is. Without a specific measurable target, you'll iterate forever.

---

## Section 4: Anti-Patterns (What NOT to Do)

### Anti-pattern 1: ML on trade-level features

"Let me train a classifier to predict winners using RSI, MACD, funding, etc."

**Why it fails:** With ~150 trades and 10+ features, you have so many degrees of freedom that you'll find spurious patterns. The classifier will look great in-sample and break OOS.

**Already covered in QA Q&A.** Don't do it.

### Anti-pattern 2: Sweep parameters after seeing OOS results

"Let me try N_ENTRY = 50 instead of 55." 

If you've already seen OOS results, ANY parameter sweep is contaminated. The test is no longer out-of-sample because your choice was influenced by knowing what works.

### Anti-pattern 3: Adding filters after losing trades

"That trade lost because of high volatility. Let me add a vol filter."

Hindsight engineering. The filter will reject *this specific* trade but degrade overall edge. If the filter were truly improving things, you'd have proposed it BEFORE seeing the trade.

### Anti-pattern 4: "Just one more iteration"

Every iteration adds parameters. Every parameter adds overfitting risk. Pre-commit to maximum iterations before starting a research thread.

### Anti-pattern 5: Optimizing for backtest CAGR

CAGR optimization typically means more leverage or more risk. The realized live result will be worse than backtest and the drawdowns will exceed your tolerance.

### Anti-pattern 6: "This time it's different"

Every loosening of a filter, every override, every "I'll just hold this losing trade a bit longer" is a tell that you've stopped running a systematic strategy. The rules are right on average. You are not.

---

## Section 5: Worked Examples

### Example 1: "I want higher returns"

Diagnostic tree → Branch A → check preconditions:
- Live for 60 days? **No (currently in build phase)**
- 20+ trades? **No (system has been dormant)**
- Live matches backtest? **Can't tell yet**

**Conclusion:** STOP. Run the existing system first. Question is premature.

### Example 2: "I want less drawdown" (hypothetical, after 90 days live)

Diagnostic tree → Branch B → preconditions OK
- B1: Is dynamic sizing on? **Check `walk_forward_v5` output; if not enabled in production, enable.**
- B2: Correlation-aware heat cap? **Not built. Build it. ~1 week.**
- B3: Tighter early-stage stops? **Build and walk-forward test.**

For each: run all 5 validation gates before deploying.

### Example 3: "Mean-reversion failed; what next?" (current situation)

Diagnostic tree → was Branch D (regime robustness)
- D1 attempted → failed validation. Document and move on.
- D2 (cross-asset macro) is the next candidate
- D3 (asymmetric exit) is a separate, easier win

**Decision:** Park D2 for later (requires new data sources). Try D3 first (small build, high probability of validation).

But wait — preconditions: have we operated 60+ days live? **No.** 

**Conclusion:** STOP improvement work until live data exists.

---

## Section 6: The Two-Question Filter

Before starting ANY improvement effort, answer these:

### Question 1: "If this improvement succeeds, what specifically changes?"

Specific. Measurable. Pre-commit to the metric before testing.

- Bad: "It'll be better"
- Good: "Aggregate OOS expectancy goes from +0.6R to +0.8R, Calmar from 0.85 to 1.0+"

If you can't specify, you don't have a hypothesis — you have a wish.

### Question 2: "If this improvement fails, what will I do?"

If the answer is "try another version," you're in parameter-fishing mode.

The correct answer: "I'll document the negative result, shelve this branch, and move on to a different category in the decision tree."

---

## Section 7: Roadmap (Post-Paper-Trading)

Assuming 90 days of paper trading completes successfully, the proposed sequence of improvements:

```
1. (Week 1) Yield deployment to Binance Simple Earn   [DONE, just operational]
2. (Week 1) Discord webhook setup                      [DONE, just operational]
3. (Day 90) Stage 2: Read-only Binance API integration [stage gate]
4. (Day 90+30) Stage 3: Assisted execution at $50-200/trade
5. (Day 180) Asymmetric exit logic (D3) — small build, high expected ROI
6. (Day 210) Correlation-aware heat cap (B2)
7. (Day 270) Bearish-regime activation (A5)
8. (Day 360) Volatility breakout strategy as candidate second sleeve
9. (Day 540+) Funding carry as third uncorrelated edge

NOT on roadmap (excluded by decision tree):
- More mean-reversion variants
- Tighter Donchian parameters
- Multi-timeframe confirmation
- ML feature engineering
- Adaptive parameter selection
```

This is a 18-month roadmap. Most of it is waiting, not building.

---

## Section 8: The Meta-Rule

> Every improvement you make is a hypothesis. Every hypothesis has an honest probability of being wrong. The way to maximize long-run performance is not to attempt more improvements — it's to **only attempt the ones with highest prior + strongest validation gates**, and to let the existing edge compound during the time you would have spent on lower-probability ideas.
>
> Most retail systematic traders fail not from lack of improvements but from too many of them. Each "improvement" is a small move toward overfitting, complexity, and operator fatigue. The discipline to NOT improve when there's no clear case for improvement is the highest-value skill in this domain.

---

*Decision tree v1.0. This document should be updated only when a real-world result changes the prior on a specific branch. Otherwise, it remains stable — the framework is more durable than any individual hypothesis.*
