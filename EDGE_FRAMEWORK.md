# Edge Framework — from mathematical idea to testable hypothesis
*Created 2026-10-03. Companion to the validation gauntlet: the gauntlet JUDGES
hypotheses; this framework helps FORM them. An idea that can't complete this
page is not ready to spend one of the 4 annual test slots.*

## The five questions (answer ALL, in writing, before any code)

**1. MECHANISM — what structural force creates the pattern?**
Not "prices exhibit X" (description) but "agent Z is forced/incentivized to
do W, which pushes price in a predictable way." Forced = vesting schedules,
funding payments, liquidations, index rebalancing, margin calls, mandates.
Incentivized-but-adaptive agents arbitrage away; FORCED agents keep paying.

**2. LOSER — who is on the other side, and why don't they stop?**
Every validated sleeve here has an answer:
- Trend: under-reaction by slow capital + disposition effect (sellers of
  winners are behaviorally sticky — they keep coming).
- Unlock shorts: locked insiders CANNOT sell early; the supply cliff is
  contractual, published, and still under-hedged on thin alt books.
- Carry: longs pay funding for leverage access; crowding is observable.
Every tombstone had no answer (pairs: who pays for convergence? nobody is
forced to converge anything).

**3. SHAPE PRIOR — continuation or reversion?**
- Continuation/flow-shaped: start at ~35% prior.
- Reversion-shaped (dips, spreads, oversold, "it must come back"): <15%.
  Confirmed three independent ways in this project. Crypto is a momentum
  market; reversion priors are not negotiable sentiment, they are paid-for
  scars (v9 bear-shorts lost money IN THE 2022 BEAR).
- Physics/math imagery with no mechanism (fractals, entropy, waves, cycles,
  Fibonacci): prior ≈ 0. Decoration, not mechanism. Refuse at intake.

**4. DECAY CHECK — why does this still exist?**
Published (Quantpedia/SSRN) = decayed by default; assume half the paper's
Sharpe, then subtract costs. Valid reasons an edge persists: capacity too
small for funds (our size is an advantage!), career risk (funds can't short
unlocks of their own portfolio companies), structural (funding mechanics),
behavioral at scale (disposition effect survives because humans remain human).
"Nobody noticed" is NOT a valid reason. Everyone noticed.

**5. COST REALITY — does the edge survive OUR frictions?**
9 bps round trip + slippage + $5 min notional + daily bars at 00:04 UTC +
laptop uptime. Any idea needing intraday execution, sub-daily data, or
market-neutral legs (double fees, funding on shorts) must clear a
proportionally higher bar. Holding periods under ~5 days rarely survive.

## Mathematics that EARNS here (apply more of this)
- Ergodicity / multiplicative processes → sizing > signals (the ladder).
- Extreme value theory → tail sizing, stop placement, ruin estimates.
- Bootstrap / permutation inference → every CI in the project.
- Multiple-testing control → the 4-test budget (López de Prado).
- Regime conditioning (simple, observable states — macro ON/OFF) →
  the single most profitable line of code in the system.

## Mathematics that COSTS here (museum, not toolbox)
Elliott waves, Fibonacci levels, Gann, harmonic patterns, raw chaos theory,
quantum analogies, most econophysics prediction claims. Beautiful
descriptions of fat tails and clustering — zero surviving net-of-cost
predictions in 30 years of literature. If one ever produces a MECHANISM
(question 1), it re-enters through the front door like anything else.

## Intake verdicts
- All five answered convincingly → write it as a one-line hypothesis in
  RESEARCH_ROADMAP.md, assign the shape prior, decide whether it merits one
  of the remaining test slots (currently 4 for 2026-27, space-exhausted
  verdict standing). Gauntlet takes it from there.
- Any question unanswered → park it in the "ideas parking lot" below with
  the missing piece named. Ideas are free; tests are not.

## Ideas parking lot
| Date | Idea | Missing piece |
|---|---|---|
| 2026-10-03 | Calendar/seasonality entry filter (Quantpedia) | Mechanism: WHO is forced to trade weekends? No loser identified |
| 2026-10-03 | BTC→alt lead-lag confirmation | Cost reality: 1-2d holds vs 9bps round trip; needs cost math first |
