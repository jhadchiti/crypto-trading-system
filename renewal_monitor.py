"""
Renewal Monitor — statistical process control for the trading PROCESS.
=======================================================================

The Donchian system is a renewal process: breakouts arrive with a waiting-time
distribution, trades live for a duration distribution. The backtest
(donchian_trades.csv) defines what those distributions SHOULD look like.
This monitor compares the LIVE process against them and alarms on time
structure — often earlier than P&L can.

What it checks (all advisory, judgment gated by sample size):
  1. Trade durations: live bars_held vs backtest bars_held
     (open positions are right-censored: only flagged if age exceeds the
     backtest's 95th percentile — a censored observation can only say
     "too long", never "too short").
  2. Inter-entry waiting times: gaps between consecutive live entries vs
     gaps in the backtest (pooled portfolio-level, macro-ON eras pooled —
     documented caveat: the backtest gaps include OFF eras, making the
     reference CONSERVATIVE, i.e. live-looks-too-fast fires less easily).
  3. Signal rate: live entries per 30 days vs backtest rate.

Deviation verdicts use a permutation test (no scipy dependency), two-sided,
alarm at p < 0.01 AND n_live >= 10. Below n=10: COLLECTING, no judgment —
same philosophy as the analytics gating (judge nothing early).

Output: console summary + renewal_state.json (dashboard can read it later).
Exit code always 0 (advisory — never blocks the pipeline).
"""

from __future__ import annotations

import sys as _sys
try:
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BACKTEST = HERE / "donchian_trades.csv"
LIVE = HERE / "live_trades.csv"
STATE = HERE / "renewal_state.json"

MIN_N = 10          # below this: collect, don't judge
ALARM_P = 0.01      # two-sided permutation p-value threshold
N_PERM = 10000
RNG = np.random.default_rng(55)   # fixed seed: same data -> same verdict


def perm_test_median_diff(a: np.ndarray, b: np.ndarray) -> float:
    """Two-sided permutation test on difference of medians. Returns p."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) == 0 or len(b) == 0:
        return 1.0
    obs = abs(np.median(a) - np.median(b))
    pooled = np.concatenate([a, b])
    n = len(a)
    count = 0
    for _ in range(N_PERM):
        RNG.shuffle(pooled)
        if abs(np.median(pooled[:n]) - np.median(pooled[n:])) >= obs:
            count += 1
    return (count + 1) / (N_PERM + 1)


def load_reference() -> dict:
    bt = pd.read_csv(BACKTEST, parse_dates=["entry_date", "exit_date"])
    durations = bt["bars_held"].dropna().astype(float).values
    entries = bt["entry_date"].sort_values()
    gaps = entries.diff().dt.total_seconds().dropna().values / 86400.0
    gaps = gaps[gaps > 0]   # same-day portfolio entries collapse to one epoch
    span_days = (entries.iloc[-1] - entries.iloc[0]).days or 1
    return {
        "durations": durations,
        "gaps": gaps,
        "rate_per_30d": len(bt) / span_days * 30.0,
        "dur_p95": float(np.percentile(durations, 95)),
        "dur_median": float(np.median(durations)),
        "gap_median": float(np.median(gaps)) if len(gaps) else float("nan"),
        "n": len(bt),
    }


def load_live() -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """Returns (closed_durations, inter_entry_gaps_days, open_ages_days, n_entries)."""
    if not LIVE.exists():
        return np.array([]), np.array([]), np.array([]), 0
    lv = pd.read_csv(LIVE, parse_dates=["entry_date", "exit_date"])
    if lv.empty:
        return np.array([]), np.array([]), np.array([]), 0
    closed = lv[lv["exit_date"].notna()]
    durations = closed["bars_held"].dropna().astype(float).values
    entries = lv["entry_date"].sort_values()
    gaps = entries.diff().dt.total_seconds().dropna().values / 86400.0
    gaps = gaps[gaps > 0]
    now = pd.Timestamp.now(tz="UTC")
    open_rows = lv[lv["exit_date"].isna()]
    ages = np.array([(now - e).total_seconds() / 86400.0
                     for e in open_rows["entry_date"]])
    return durations, gaps, ages, len(lv)


def main() -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] renewal monitor ...")
    if not BACKTEST.exists():
        print("  reference donchian_trades.csv missing — nothing to compare")
        return
    ref = load_reference()
    live_dur, live_gaps, open_ages, n_entries = load_live()

    checks = []

    # 1. Closed-trade durations
    if len(live_dur) >= MIN_N:
        p = perm_test_median_diff(live_dur, ref["durations"])
        status = "DEVIATION" if p < ALARM_P else "OK"
        checks.append(("durations", status,
                       f"live median {np.median(live_dur):.0f}d vs backtest "
                       f"{ref['dur_median']:.0f}d (p={p:.4f}, n={len(live_dur)})"))
    else:
        checks.append(("durations", "COLLECTING",
                       f"{len(live_dur)}/{MIN_N} closed trades — no judgment"))

    # 2. Censored ages of open positions (one-sided by nature)
    for age in open_ages:
        if age > ref["dur_p95"]:
            checks.append(("open_age", "DEVIATION",
                           f"an open position is {age:.0f}d old — beyond the "
                           f"backtest 95th pct ({ref['dur_p95']:.0f}d); the 90d "
                           f"time stop should have fired — INVESTIGATE"))
    if not any(c[0] == "open_age" for c in checks):
        oldest = f"{open_ages.max():.0f}d" if len(open_ages) else "none open"
        checks.append(("open_age", "OK",
                       f"oldest open {oldest} vs p95 {ref['dur_p95']:.0f}d"))

    # 3. Inter-entry gaps
    if len(live_gaps) >= MIN_N:
        p = perm_test_median_diff(live_gaps, ref["gaps"])
        status = "DEVIATION" if p < ALARM_P else "OK"
        checks.append(("entry_gaps", status,
                       f"live median gap {np.median(live_gaps):.1f}d vs backtest "
                       f"{ref['gap_median']:.1f}d (p={p:.4f}, n={len(live_gaps)})"))
    else:
        checks.append(("entry_gaps", "COLLECTING",
                       f"{len(live_gaps)}/{MIN_N} gaps — no judgment"))

    # 4. Signal rate (informational only — macro gating makes eras uneven)
    if n_entries >= 2:
        lv = pd.read_csv(LIVE, parse_dates=["entry_date"])
        span = (lv["entry_date"].max() - lv["entry_date"].min()).days or 1
        live_rate = n_entries / span * 30.0
        checks.append(("entry_rate", "INFO",
                       f"live {live_rate:.1f} entries/30d vs backtest "
                       f"{ref['rate_per_30d']:.1f} (macro-ON era vs mixed eras — "
                       f"expect live HIGHER while ON)"))

    worst = ("DEVIATION" if any(s == "DEVIATION" for _, s, _ in checks)
             else "COLLECTING" if any(s == "COLLECTING" for _, s, _ in checks)
             else "OK")
    for name, status, detail in checks:
        print(f"  [{status}] {name}: {detail}")
    print(f"  overall: {worst}")

    STATE.write_text(json.dumps({
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "overall": worst,
        "checks": [{"name": n, "status": s, "detail": d} for n, s, d in checks],
        "reference_n": ref["n"],
    }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
