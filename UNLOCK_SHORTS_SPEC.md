# Token Unlock Event Shorts — Specification v1.0 (PRE-REGISTERED)

Status: **PAPER TRIAL** — 10 events, then the §6 decision rule. No live money
before that. Registered 2026-08-02, before observing any event outcomes.

## 1. Economic rationale

Token unlocks are calendar-known supply expansions: team/investor tokens
vesting into circulation. Large unlocks (>5-10% of circulating supply) create
predictable sell pressure — recipients hold tokens at near-zero cost basis and
diversify. The event date is public months ahead, but markets systematically
underprice the pressure until it is imminent (documented drift by practitioner
research; attention is finite, calendars are boring). Edge class: event-driven
supply/demand, NOT price reversion — consistent with the project's meta-lesson
that only continuation/flow edges survive in crypto.

## 2. Event eligibility (ALL required)

1. Unlock releases ≥ **5% of circulating supply** on a single date
   (≥10% = full size; 5-10% = half size)
2. Token has an active Binance USDT perp with 24h volume ≥ **$20M**
3. Unlock date verified on two sources (e.g. Tokenomist + CryptoRank/project docs)
4. Funding rate at entry ≥ **−20 bps/8h** (shorts not already crowded — if the
   whole market is short the unlock, the edge is priced and squeeze risk high)

## 3. Trade rules (mechanical once event is registered)

- **ENTRY:** short at daily close, **10 days before** the unlock date
  (first nightly run where days-to-unlock ≤ 10)
- **EXIT:** buy back at daily close, **4 days after** the unlock date
  (~14-day holding period)
- **STOP:** exit immediately if price rises **+15%** from entry (squeeze guard)
- **Paper size:** $100 notional per event. Live size (if promoted): 0.5% equity
  risk against the 15% stop.
- Max **3 concurrent** events. Never on a symbol the trend system holds long.

## 4. Cost model

18 bps round trip (2 perp executions × (4 fee + 5 slippage)). Funding P&L
ignored in paper (shorts typically RECEIVE funding pre-unlock — conservative).

## 5. Workflow

- **Monthly (~1 hr, operator):** check unlock calendars, register qualifying
  events: `python unlock_tracker.py add SYMBOL YYYY-MM-DD PCT_SUPPLY`
- **Nightly (automatic):** `unlock_tracker.py check` runs inside daily_check —
  opens/closes/stops paper positions per rules, Discords every action
- **Anytime:** `python unlock_tracker.py status`

## 6. Decision rule (pre-committed)

After **10 completed events**:
- **≥ 7 profitable** after costs AND **mean net > +1.0%** → promote to live
  at 0.5% risk, next 10 events
- 5-6 profitable → extend paper 5 more events, then final call (one extension only)
- **≤ 4 profitable** → tombstone in SYSTEM_QA; do not revisit without a
  structurally different design

## 7. Known failure modes

- Pre-priced unlocks (everyone shorted first) → funding gate + stop
- Team/OTC absorption (unlock sold privately, no market pressure) → diversify
  across 10 events; single events prove nothing
- Squeeze on unlock-day "sell the news is over" bounce → the +15% stop and
  the T+4 exit cap exposure
- Calendar errors → two-source verification rule
