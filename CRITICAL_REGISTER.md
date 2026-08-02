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

## MONITORING (known, unresolved, watched)

| Question | Why it matters | Watch via |
|---|---|---|
| **Does the 90d time stop cut monsters?** Both time-stopped trades in validation averaged +17.8R — winners forcibly closed | n=2, no conclusion possible; if live time-stops also close big winners, a longer cap deserves a budgeted test | trade_reviews.csv exit reasons |
| **MC cone assumes i.i.d. trades** — real trades cluster by regime; cone may be too narrow in bad regimes | could misjudge "normal" during a correlated losing streak | treat cone breaches as *investigate*, not auto-verdict |
| **Carry backtest survivorship** — funding history only exists for surviving symbols | live carry APY likely below the 9.3% backtest | Sept 1 paper review is the honest test |
| **Live-vs-backtest capture** — the master question; everything above is secondary to it | unknowable until ~20 trades | calibration panel |
| **PROVE-style late entries** (T-2 vs T-10) in unlock trial dilute comparability to the entry-window design | small n trial gets noisier | note entry lag per event in the log |

## ACCEPTED RISKS (eyes open, no fix planned)

| Risk | Statement |
|---|---|
| **VPN + Binance ToS** | Accessing Binance through a VPN from a restricted region likely violates Binance's Terms of Service. If detected, consequences can include account restriction or forced position closure. This is an operator-accepted risk, not a technical bug. Funds kept small partly for this reason. Not legal advice; worth periodic reconsideration. |
| **Single points of failure** | One laptop, one exchange, one VPN provider, one human. A failure of any pauses (not destroys) the system: stops live on-exchange, capital is recoverable via Binance directly, code is on GitHub. Accepted at current capital; revisit at $10k+ (VPS, second venue). |
| **Regime dependence** | The entire directional edge lives in trending regimes. A permanently choppy crypto market (no precedent, but possible) would grind slow losses within validated drawdown bounds. The quarterly re-validation is the tripwire. |
| **Small-sample everything** | $110, 0 live trades, 1 paper event. Every conclusion is provisional until samples exist. The gates enforce this; impatience is the threat model. |
