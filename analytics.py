"""
Advanced Analytics — the full allocator/PM metric suite.
=========================================================

Everything a sophisticated investor would ask for, computed from local files
and SAMPLE-SIZE GATED like the rest of the board. Feeds the dashboard's
Metrics vs Targets panel via advanced_rows().

Gates:
  31d equity history : volatility, rolling return, TWR, beta/alpha/corr/capture
  60d                : VaR, CVaR
  90d                : skew, tail ratio, Ulcer, Martin, underwater stats
  20 closed trades   : SQN, max consecutive losses, time-in-market
  30 closed trades   : implied Kelly vs actual sizing
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


def _row(sleeve, metric, value, target, status):
    return {"sleeve": sleeve, "metric": metric, "value": value,
            "target": target, "status": status}


def _gated(sleeve, metric, have, need, unit="days"):
    return _row(sleeve, metric, f"gated — {have}/{need} {unit}",
                f"computes at {need}", "gated")


def advanced_rows(closed_df: pd.DataFrame | None = None) -> list[dict]:
    rows = []

    # ---------- equity history ----------
    try:
        eh = pd.read_csv("equity_history.csv")
        eq = eh["total"].astype(float).reset_index(drop=True)
        dates = pd.to_datetime(eh["date"])
        n = len(eq)
    except Exception:
        eq, dates, n = pd.Series(dtype=float), pd.Series(dtype="datetime64[ns]"), 0
    rets = eq.pct_change().dropna()

    # --- volatility + rolling return (31d) ---
    if n < 31:
        rows.append(_gated("Risk", "volatility / rolling 30d / TWR", n, 31))
    else:
        vol = float(rets.std() * math.sqrt(365) * 100)
        rows.append(_row("Risk", "Volatility (ann.)", f"{vol:.1f}%",
                         "context: BTC ~50-70%; a diversifier should be far lower",
                         "ok" if vol < 40 else "warn"))
        r30 = float(eq.iloc[-1] / eq.iloc[-31] - 1) * 100
        rows.append(_row("Risk", "Rolling 30d return", f"{r30:+.2f}%",
                         "trend only — no target", "ok"))
        # TWR (chain-linked around deposit flows)
        try:
            flows = json.loads(Path("sleeve_ledger.json").read_text())["flows"]
            fmap = {}
            for f in flows:
                fmap[f["date"]] = fmap.get(f["date"], 0) + f["amount"]
            twr = 1.0
            for i in range(1, n):
                d = str(dates.iloc[i].date())
                flow = fmap.get(d, 0.0)
                prev = eq.iloc[i - 1]
                if prev <= 0:
                    continue
                adj = eq.iloc[i] - flow
                # guard: if the flow-adjusted factor is absurd (deposit logged
                # on a day the equity snapshot missed it), fall back to raw
                if flow != 0 and (adj <= 0 or abs(adj / prev - 1) > 0.5):
                    adj = eq.iloc[i]
                twr *= adj / prev
            twr_ann = (twr ** (365 / n) - 1) * 100
            rows.append(_row("Risk", "TWR (ann., deposit-adjusted)",
                             f"{twr_ann:+.1f}%",
                             "the SKILL number once deposits start", "ok"))
        except Exception:
            pass

    # --- VaR / CVaR (60d) ---
    if n < 60:
        rows.append(_gated("Risk", "VaR / CVaR (95%, daily)", n, 60))
    else:
        var95 = float(np.percentile(rets, 5) * 100)
        cvar = float(rets[rets <= np.percentile(rets, 5)].mean() * 100)
        rows.append(_row("Risk", "VaR 95% (daily)", f"{var95:.2f}%",
                         "worst normal day", "ok" if var95 > -3 else "warn"))
        rows.append(_row("Risk", "CVaR 95% (daily)", f"{cvar:.2f}%",
                         "average of the bad tail", "ok" if cvar > -5 else "warn"))

    # --- skew / tail / ulcer / martin / underwater (90d) ---
    if n < 90:
        rows.append(_gated("Risk", "skew / tail / Ulcer / Martin / underwater", n, 90))
    else:
        skew = float(rets.skew())
        rows.append(_row("Risk", "Skewness (daily)", f"{skew:+.2f}",
                         "> 0 REQUIRED — trend must be right-skewed live",
                         "ok" if skew > 0 else "warn"))
        p95, p5 = np.percentile(rets, 95), np.percentile(rets, 5)
        tail = abs(p95 / p5) if p5 != 0 else float("nan")
        rows.append(_row("Risk", "Tail ratio (p95/|p5|)", f"{tail:.2f}",
                         "> 1.0 (big days should be up-days)",
                         "ok" if tail > 1.0 else "warn"))
        ddser = (eq - eq.cummax()) / eq.cummax()
        ulcer = float(np.sqrt((ddser ** 2).mean()) * 100)
        ann_ret = float((eq.iloc[-1] / eq.iloc[0]) ** (365 / n) - 1) * 100
        martin = ann_ret / ulcer if ulcer > 0.01 else float("nan")
        rows.append(_row("Risk", "Ulcer Index", f"{ulcer:.2f}",
                         "pain-weighted drawdown; lower is better", "ok"))
        if not math.isnan(martin):
            rows.append(_row("Risk", "Martin ratio (ret/Ulcer)", f"{martin:.2f}",
                             "> 1.0 solid, > 2 elite",
                             "ok" if martin >= 1.0 else "warn"))
        # underwater stats
        uw = ddser < -0.001
        cur_dd = float(ddser.iloc[-1] * 100)
        days_uw = 0
        for v in reversed(uw.tolist()):
            if v: days_uw += 1
            else: break
        longest = cur_run = 0
        for v in uw:
            cur_run = cur_run + 1 if v else 0
            longest = max(longest, cur_run)
        rows.append(_row("Risk", "Current DD / days underwater",
                         f"{cur_dd:.1f}% / {days_uw}d",
                         f"longest underwater so far: {longest}d",
                         "ok" if days_uw < 60 else "warn"))

    # --- benchmark-relative vs BTC (31d) ---
    if n >= 31:
        try:
            btc = pd.read_csv("cache/ohlcv/BTCUSDT.csv", parse_dates=["date"])
            btc["d"] = btc["date"].dt.date.astype(str)
            bmap = dict(zip(btc["d"], btc["close"].astype(float)))
            pairs = []
            for i in range(1, n):
                d0, d1 = str(dates.iloc[i-1].date()), str(dates.iloc[i].date())
                if d0 in bmap and d1 in bmap and eq.iloc[i-1] > 0:
                    pairs.append((eq.iloc[i]/eq.iloc[i-1]-1,
                                  bmap[d1]/bmap[d0]-1))
            if len(pairs) >= 20:
                pr = np.array([p[0] for p in pairs])
                br = np.array([p[1] for p in pairs])
                beta = float(np.cov(pr, br)[0, 1] / max(np.var(br), 1e-12))
                corr = float(np.corrcoef(pr, br)[0, 1])
                alpha_ann = float((pr.mean() - beta * br.mean()) * 365 * 100)
                up, dn = br > 0, br < 0
                upcap = float(pr[up].mean() / br[up].mean() * 100) if up.sum() > 5 and br[up].mean() != 0 else float("nan")
                dncap = float(pr[dn].mean() / br[dn].mean() * 100) if dn.sum() > 5 and br[dn].mean() != 0 else float("nan")
                rows.append(_row("vs BTC", "Beta / correlation",
                                 f"{beta:+.2f} / {corr:+.2f}",
                                 "validated profile ~0 / ~-0.07 — diversifier claim",
                                 "ok" if abs(corr) < 0.4 else "warn"))
                rows.append(_row("vs BTC", "Alpha (ann.)", f"{alpha_ann:+.1f}%",
                                 "> 0 after enough history", "ok" if alpha_ann > 0 else "warn"))
                if not math.isnan(upcap) and not math.isnan(dncap):
                    rows.append(_row("vs BTC", "Up / down capture",
                                     f"{upcap:.0f}% / {dncap:.0f}%",
                                     "dream: high up, low down",
                                     "ok" if dncap < upcap else "warn"))
        except Exception:
            pass
    else:
        rows.append(_gated("vs BTC", "beta / alpha / corr / capture", n, 31))

    # ---------- trade-level ----------
    nt = 0
    r = pd.Series(dtype=float)
    if closed_df is not None and not closed_df.empty and "r_multiple" in closed_df.columns:
        r = pd.to_numeric(closed_df["r_multiple"], errors="coerce").dropna()
        nt = len(r)
    if nt < 20:
        rows.append(_gated("Trades+", "SQN / max losing streak / exposure", nt, 20, "trades"))
    else:
        sqn = float(r.mean() / r.std() * math.sqrt(min(nt, 100))) if r.std() > 0 else float("nan")
        grade = ("excellent" if sqn >= 3 else "good" if sqn >= 2 else
                 "average" if sqn >= 1.6 else "poor")
        rows.append(_row("Trades+", "SQN (Van Tharp)", f"{sqn:.2f} ({grade})",
                         ">= 2.0 good, >= 3.0 excellent",
                         "ok" if sqn >= 2.0 else "warn"))
        streak = longest = 0
        for v in r:
            streak = streak + 1 if v <= 0 else 0
            longest = max(longest, streak)
        rows.append(_row("Trades+", "Max consecutive losses", str(longest),
                         "expect 5-8 at 30% win rate — normal, not broken",
                         "ok" if longest <= 8 else "warn"))
    if nt >= 30:
        p = float((r > 0).mean())
        b = float(r[r > 0].mean() / abs(r[r <= 0].mean())) if (r <= 0).any() else float("inf")
        kelly = p - (1 - p) / b if b > 0 else float("nan")
        rows.append(_row("Trades+", "Implied Kelly vs actual",
                         f"{kelly*100:.1f}% vs 0.75% used",
                         "actual must stay FAR below Kelly (quarter-Kelly max)",
                         "ok" if 0.0075 < max(kelly, 0) / 2 else "warn"))
    return rows
