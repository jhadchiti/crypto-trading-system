# Critical Register — open questions, known weaknesses, standing skepticism

*The living document of everything that could be wrong. Reviewed at every
quarterly re-validation. If a concern isn't on this list, the first step is
adding it — the second is testing it. Created 2026-08-02 after the exit audit
proved that unasked skeptical questions are where systems rot.*

## Protocol (standing)

1. Every new component ships WITH its own adversarial audit (as the dashboard
   16/16 and exit audit were done) — not after, not on request.
2. Quarterly RED TEAM pass: re-read this register top to bottom, attack every
   "monitoring" item, attempt to falsify one "accepted" item.
3. Any surprise in live behavior — first stop is this document: was it a
   known weakness? If not, it gets added with a post-mortem.
4. TWO LENSES, always: the ENGINEER'S sweep (is the code doing what was
   validated?) and the TRADER'S sweep (what is the book actually exposed to,
   what does current positioning assume about the market, which two sleeves
   could collide, and what will the next decision feel like vs what the
   evidence says?). An audit that only ran one lens is half an audit.

## RESOLVED (audited, receipts on file)

| Question | Verdict | Where |
|---|---|---|
| Are dashboard numbers correct? | 16/16 independently reproduced; 2 bugs found+fixed | 2026-08-01 audit |
| Are exits computed correctly? | 4/4 logic tests; all 90 trades replayed vs raw bars | Q5b |
| Are exits well-designed? | Near-optimal; TP/trailing/tightening all destroy the tail | Q5b |
| Does the doc match the code? | One lie found (trailing stops) and corrected | Q5 |
| Is the macro filter costing money? | Blocked signals = −31R if taken; filter saved ~23% of account | discipline audit |
| Does the circuit breaker work? | Fired correctly on day one (false-positive path, since made transfer-aware) | executor |
| Is the weekly universe refresh automated? | Yes — verified in setup_automation.ps1, file 1 day old | 2026-08-02 |
| Is Kelly over-betting a risk? | 0.75% is far under implied Kelly; monitored on dashboard | analytics |

## MITIGATED (real weakness, bounded, fix applied)

| Weakness | Exposure | Mitigation |
|---|---|---|
| **Fill-vs-signal geometry**: executor sizes off signal-close stop distance but fills at market minutes later; a gap shifts actual risk away from 0.75% | was up to ±2% price gap → risk up to ~1.5x intended on wide gaps | price-sanity tightened 2%→1% (2026-08-02); first live trades' fill-vs-signal slippage tracked in trade_reviews.csv — revisit after 10 fills |
| Equity-history gaps (rows only on successful sync) bias ratio denominators slightly | small while uptime <100% | uptime tracked; ratios gated to 31d+ anyway |

## RESOLVED IN THE 2026-08-02 EXPANSION SWEEP

| Question | Finding | Fix |
|---|---|---|
| **Do signals evaluate a partial bar?** | YES — fetch_recent included the still-forming daily candle; on delayed runs its "close" was hours of unfinished price action the backtest never saw | fetch_recent now drops any bar that hasn't completed; signals evaluate completed closes only, matching backtest semantics exactly |
| **Naked position if stop placement fails after entry fills?** | YES — a stop-order rejection left the position live, unprotected, and untracked until next sync | 3x stop retry; if all fail, position is closed immediately (flat is safe); if even the close fails, hard HALT + manual-action alert |

## POST-MORTEM 2026-08-09: false STALE-DATA banner (protocol rule 3 applied)

**Surprise:** operator saw "market data is STALE — 45h old" banner; assistant's
"check now" review missed it entirely.
**Root cause:** interaction of two individually-correct fixes — the partial-bar
drop (2026-08-02) made the last bar always yesterday's, while the staleness
alarm measured age from bar OPEN time. Result: false alarm every day after
12:00 UTC. Data and signals were correct throughout.
**Fixes:** (1) staleness now measured from bar CLOSE; (2) review protocol
amended — any "check the system" pass must recompute the ACTION BANNER state,
not just read state files. A status report that skips the operator's first
pixel is not a status report.
**Lesson:** every fix must be re-audited against the alarms that observe the
thing it changed. Interactions, not components, are where audited systems break.

## FIRE DRILL LOG

| Date | Drill | Result |
|---|---|---|
| 2026-08-09 | First `--rehearse` (macro forced ON, dry) | Chain verified end-to-end: 7 breakouts → RS gate passed 2 → sanity gate refused both. **CAUGHT: scheduled task ran at 00:05 LOCAL (21:05 UTC) — 21h-stale signals vs live marks; would have vetoed/mispriced flip-night entries. Fixed same day: trigger moved to 03:05 local ≈ 00:05 UTC.** One rehearsal, one premiere-night defect found with zero dollars on stage. Re-drill after any executor change and quarterly. |

## POST-MORTEM 2026-08-18: six dark days — the canary died first

**Timeline:** Aug 11 — digest began crashing on an emoji (cp1252 console under
Task Scheduler; crash occurred BEFORE the Discord send). Aug 13 — VPN stopped
connecting (likely NordVPN update reset auto-connect). Aug 13-17 — nightly
runs fired, network gate correctly aborted each one. Operator noticed Aug 18.
**What worked:** every abort was safe and logged; no stale data, no bad
trades; real money untouched (macro OFF, no positions; paper sleeves froze).
**What failed:** the dead-man's switch. Its heartbeat channel (digest) was
KILLED BY A COSMETIC BUG two days before the real outage, so "absence of
digest" carried no signal. A monitoring channel must be strictly more robust
than the things it monitors.
**Fixes:** (1) digest forces UTF-8 with replacement — console rendering can
never kill delivery again; (2) delivery reordered BEFORE printing; (3) rule
reaffirmed with teeth: ANY morning without a digest = check the laptop that
day, not "two mornings" — the six-day gap proves grace periods compound.
**Operator action from this event:** re-enable NordVPN auto-connect (app
updates reset it); verify after every NordVPN update.

## POST-MORTEM 2026-09-13: the SAME bug class struck on FLIP MORNING

**Event:** BTC macro flipped ON (~Sept 11-13, slope +1.52%) — the moment the
system waited 290+ days for. That morning's alerter crashed printing the flip
message itself (the arrow in "OFF→ON" is unprintable in cp1252); the executor
crashed printing its routine report. The flip alert never reached Discord.
**Money impact: none.** The executor completed all trade logic BEFORE the
report crash: FLOCKUSDT's LONG_ENTRY was evaluated and skipped by a rail
(reason lost with the crashed report; re-evaluated on the recovery run).
Signal was never marked executed, so no opportunity was consumed.
**Root cause of recurrence — process, not code:** the 2026-08-18 post-mortem
identified this exact class and said "fix the class"; the fix was applied to
ONE script (digest) instead of the fleet. A post-mortem whose fix isn't
applied class-wide is a diary entry, not a fix.
**Fix:** UTF-8 console guard installed in ALL nine pipeline scripts
(2026-09-13). New rule: any bug found in one component triggers an immediate
grep-sweep for the same pattern in every component, same day.

## POST-MORTEM 2026-09-18: first-ever live order — rejected -2015 (read-only key)

**Event:** the first properly-timed run in system history (00:05 UTC,
keep-awake engaged, ZEC signal 2 min after bar close, every rail passed) sent
the first real order ever — and Binance rejected it:
`-2015 Invalid API-key, IP, or permissions` on /fapi/v1/order AND
/fapi/v1/leverage. **Money impact: none** — rejection happened before any
position or margin change ($50.45 untouched, no naked exposure).
**Root cause:** the key in secrets.env can READ but not TRADE — most likely
the September secrets recovery restored the *read-only* key's values, or
"Enable Futures" was never checked / auto-expired. Every check we had
(selftest, account_sync, weeks of green runs) only exercised READ endpoints,
so a read-only key looked healthy for weeks. The order path had never been
live-verified: `--selftest` reads, `--rehearse` is dry.
**Fix (class-wide, not diary):** selftest now probes the TRADING permission
directly (harmless POST /fapi/v1/leverage BTCUSDT) and prints
`TRADING PERMISSION OK` or a loud failure. Rule: a permission is only
"configured" when a call *requiring* it has succeeded — green reads prove
nothing about writes.
**Secondary observation:** 龙虾USDT skipped at 1.3% deviation vs the 1.0%
sanity gate — the gate worked as designed on a coin moving 20%+/day. A fixed-%
gate is stricter in R-terms for high-vol symbols (1.3% ≈ 0.06R vs its 23%
stop); any change goes through the gauntlet, not through frustration.

## DECISION 2026-09-19: exchange-floor sizing override (operator-accepted risk)

**Context:** first fully-working run (key fixed, -2015 gone) produced 4 valid
signals and ZERO trades — all fell below Binance's $5 minimum notional at
0.75% risk on $112 equity (ZEC $4.68 post-rounding, USELESS $3.22, UNI
rounds to 0 coins). The ladder's rung 1 is unimplementable at this equity.
**Decision (Joseph):** "make it minimum $5 — I want these to trade."
**Implementation:** executor bumps quantity up to the exchange floor ONLY
when the implied risk stays ≤ `min_notional_risk_cap` = **1.25% of equity**;
above the cap the trade skips with a receipt. Actual risk (not the 0.75%
target) is now stored per trade in live_trades.csv and used for R-multiples,
so capture metrics stay honest. Tonight's four would have risked 0.77% /
1.11% / 1.17% — ~4.5% combined heat, under the 5% daily breaker.
**Sunset clause:** the override self-deactivates as equity grows (0.75%
clears $5 naturally from roughly $200+ equity on typical stops). The ladder's
20-trade / ≥60%-capture gate for rung 2 counts these trades at their ACTUAL
risk. This is a size-floor accommodation, not a sizing increase by appetite.
**Trader's honest note:** at 1.17% risk per trade, a normal 8-loss streak
(p≈26% of 20-trade runs start negative) costs ~9% instead of ~6%. Accepted.

## MONITORING (known, unresolved, watched)

| Question | Why it matters | Watch via |
|---|---|---|
| **Does the 90d time stop cut monsters?** Both time-stopped trades in validation averaged +17.8R — winners forcibly closed | n=2, no conclusion possible; if live time-stops also close big winners, a longer cap deserves a budgeted test | trade_reviews.csv exit reasons |
| **MC cone assumes i.i.d. trades** — real trades cluster by regime; cone may be too narrow in bad regimes | could misjudge "normal" during a correlated losing streak | treat cone breaches as *investigate*, not auto-verdict |
| **Carry backtest survivorship** — funding history only exists for surviving symbols | live carry APY likely below the 9.3% backtest | Sept 1 paper review is the honest test |
| **Live-vs-backtest capture** — the master question; everything above is secondary to it | unknowable until ~20 trades | calibration panel |
| **PROVE-style late entries** (T-2 vs T-10) in unlock trial dilute comparability to the entry-window design | small n trial gets noisier | note entry lag per event in the log |

| **Stop trigger basis: backtest fires on candle LOW (last price); live stops fire on MARK price** | mark is smoother → live stops trigger *less* on wicks than backtest assumed (likely favorable, but a basis difference) | compare live stop fills vs candle lows in trade_reviews |
| **Winner's curse on the RS filter**: rs_only was the best of 4 variants in one test — selection inflates its +69% uplift | expect live uplift meaningfully below +69%; not a reason to doubt direction, only magnitude | 20-trade calibration |
| **Universe survivorship in headline numbers** (top-30 by CURRENT volume — validation admitted this) | live expectancy will run below the +45% OOS headline independent of execution quality | calibration vs the DISCOUNTED expectation (60-80% capture already assumes this) |
| **Fold overlap**: all validation folds share BTC's regime as a common factor — effective sample < nominal n=90 | CIs are somewhat narrower than the truth; another reason gates stay strict | quarterly re-validation adds independent data |
| **Clock drift**: >10s of Windows clock error breaks ALL signed calls (Binance -1021), looking like an outage | would show as executor+sync failing with -1021 | if -1021 appears in logs: `w32tm /resync` |
| **Delisting risk**: Binance can delist a coin we hold; forced settlement at a bad print | rare on top-30 universe; universe filter reduces exposure | universe refresh drops delisting-bound symbols |
| **Ledger residual blind spot**: an UNLOGGED deposit inflates the "yield" line silently (only negative residuals alarm) | attribution quality depends on flow-logging discipline | any yield-line jump > plausible interest = check for unlogged flow |

## TRADER'S REGISTER (market-level, added 2026-08-02 — reviewed with the book, not the code)

| Item | Read | Pre-commitment |
|---|---|---|
| PROVE short entered T-2, uncrowded funding on a fully telegraphed 160% unlock | possible trap: anticipatory move missed; uncrowded obvious trades often mean pre-absorption/OTC | ride the rule; critique logged BEFORE outcome (Aug 9) so the trial teaches either way |
| KAITO: trend's top RS leader AND unlock sleeve's next short (Aug 10 window) | cross-sleeve conflict if macro flips before late Aug; spec only blocks one direction of the clash | paper: allow (it's information). BEFORE unlock-sleeve promotion: add rule — no unlock short where trend has an entry-eligible signal, trend priority |
| First post-flip entries will FEEL like chasing (wide ATRs, coins already ran) | that feeling is the validated entry point; the no-macro "early" variant loses | first signals after the flip taken at full size, no hesitation |
| Capital contention: carry go-live (Sept 1) may coincide with trend waking | $110 cannot fund both sleeves | PRE-REGISTERED: trend gets 100% of capital; carry stays paper until equity > $300. AND: zero August carry episodes → Sept 1 verdict is EXTEND, never go-live on backtest alone |
| Idle $50 futures margin earns 0% (~45% of AUM) | considered thinning to $25 + top-up on flip alert; yield gain ≈ $0.35/yr vs missed-entry risk | REJECTED — idle margin is cheap insurance; logged as example of killing small-number optimizations |

## THE SIZING LADDER (pre-registered 2026-08-02 — BINDING)

Risk per trade is earned by evidence, never by desire, impatience, or a good
week. Decided while flat and calm; no renegotiation mid-drawdown or mid-streak.

| Step | Risk/trade | Unlock condition (ALL required) |
|---|---|---|
| 0 (now) | **0.75%** | — (the proving level: first 20 live trades) |
| 1 | **1.00%** | ≥20 closed live trades AND live capture ≥ 60% of backtest expectancy AND max-DD within cone |
| 2 | **1.50%** | ≥50 closed live trades AND profile intact (win rate 20-50%, payoff ≥2x, skew > 0) |
| CEILING | **2.00%** | lifetime hard cap = quarter-Kelly of the DISCOUNTED edge. Never exceeded for any reason, at any equity, after any winning streak. |

Rules of the ladder:
- Steps go UP only via the table. Steps go DOWN immediately if: live capture
  falls below 50%, OR equity drawdown exceeds −10% (drop one step), OR −15%
  (return to 0.75% and trigger re-validation).
- A ladder change is a journal entry (decision_journal.py) at the moment of change.
- Why 5% was rejected (for the record): discounted Kelly ≈ 6-8%, quarter-Kelly
  ≈ 1.5-2%; at 5% the NORMAL p5 path is a −42% drawdown and a routine 7-loss
  streak is −30% — the guaranteed psychological conditions for abandoning a
  working system at its low. Full analysis: chat 2026-08-02.

## ACCEPTED RISKS (eyes open, no fix planned)

| Risk | Statement |
|---|---|
| **VPN + Binance ToS** | Accessing Binance through a VPN from a restricted region likely violates Binance's Terms of Service. If detected, consequences can include account restriction or forced position closure. This is an operator-accepted risk, not a technical bug. Funds kept small partly for this reason. Not legal advice; worth periodic reconsideration. |
| **Single points of failure** | One laptop, one exchange, one VPN provider, one human. A failure of any pauses (not destroys) the system: stops live on-exchange, capital is recoverable via Binance directly, code is on GitHub. Accepted at current capital; revisit at $10k+ (VPS, second venue). |
| **Regime dependence** | The entire directional edge lives in trending regimes. A permanently choppy crypto market (no precedent, but possible) would grind slow losses within validated drawdown bounds. The quarterly re-validation is the tripwire. |
| **Small-sample everything** | $110, 0 live trades, 1 paper event. Every conclusion is provisional until samples exist. The gates enforce this; impatience is the threat model. |
| **Webhook spoofing** | The Discord webhook URL was shared in a chat session; anyone holding it can post FAKE messages to the channel, including fake "TRADE EXECUTED" banners. Rule: NEVER act on a Discord message alone — verify against the dashboard or Binance before any manual response. Rotate the webhook if anything ever looks off. |
| **Cloud-sync of secrets** | The project lives under C:\Users\Admin\Documents — a folder Windows often syncs to OneDrive. If sync is ON, secrets.env and all financial state files are silently copied to Microsoft's cloud. OPERATOR CHECK REQUIRED: confirm OneDrive is not syncing this folder (File Explorer → folder icon has no cloud/checkmark overlay), or exclude it. |
| **Dead-man's switch is the operator** | If Task Scheduler silently dies (Windows update, task disabled), nothing alerts — the signal is the ABSENCE of the daily digest. Standing rule: two consecutive mornings without a digest = investigate immediately. Absence of news is itself an alarm. |
