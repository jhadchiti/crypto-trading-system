"""
Pairs Lab — market-neutral statistical arbitrage test.
=======================================================

The last untested free-data strategy family, and the only one that is
REGIME-INDEPENDENT: it trades whether BTC macro is ON or OFF, because it
holds long one coin / short another with (near) zero net market exposure.

Method (practical cointegration proxy, no lookahead):
  Formation (rolling 90d, computed on trailing data only):
    - candidate pairs from the top-N liquid universe
    - require log-price correlation >= 0.90 over the window
    - hedge ratio beta from OLS of log(A) on log(B)
    - spread = log(A) - beta*log(B); require AR(1) coeff of spread < 0.97
      (mean-reverting with half-life < ~23 days) — a practical stand-in for
      a formal cointegration test that avoids heavy dependencies
  Trading:
    - z = (spread - mean90) / std90
    - ENTER when |z| >= 2.0 : long the cheap leg, short the rich leg,
      equal notional per leg
    - EXIT when z crosses 0 (converged), |z| >= 4.0 (relationship broke —
      stop), or 30 days elapsed (time stop)
  Costs: 4 executions x (4bps fee + 5bps slippage) = 36 bps of one-leg
  notional per round trip.

Evaluation:
  - Episodes marked in/out-of-sample by entry date vs the same FOLDS test
    windows used by every other validation in this project
  - Bootstrap 95% CI on per-episode net returns
  - Portfolio: K=3 concurrent pair slots, equal capital

DECISION RULE (pre-committed):
  Ship to paper only if ALL:
    (a) OOS bootstrap CI lower bound on episode net return > 0 (starred)
    (b) OOS episodes n >= 30
    (c) portfolio annualized return on capital > 8% (must beat Simple Earn
        by enough to justify 4-legged execution complexity)

Usage:
    python pairs_lab.py --top-n 20
"""

from __future__ import annotations

import argparse
import itertools
import math

import numpy as np
import pandas as pd

import walk_forward_v6 as wf6
from walk_forward import FOLDS
from market_data import fetch_all_perp_symbols, filter_by_history, fetch_24h_ticker_all

FORMATION_D = 90
CORR_MIN = 0.90
AR1_MAX = 0.97
Z_ENTRY = 2.0
Z_STOP = 4.0
TIME_STOP_D = 30
RT_COST = 0.0036          # 36 bps of one-leg notional, all four executions
MAX_SLOTS = 3
BOOT_ITERS = 10_000


# ============================================================================
# Pair screening + episode simulation (no lookahead: all stats trailing)
# ============================================================================

def simulate_pair(a: pd.Series, b: pd.Series, sym_a: str, sym_b: str) -> list[dict]:
    """Walk the joint history of one pair. At each day, formation stats are
    computed from the TRAILING window only. Returns episode dicts."""
    df = pd.concat([np.log(a.rename("la")), np.log(b.rename("lb"))],
                   axis=1, sort=False).dropna()
    if len(df) < FORMATION_D + 40:
        return []

    la, lb = df["la"].values, df["lb"].values
    idx = df.index
    episodes = []
    pos = None  # dict(entry_i, sign, entry_z, beta, mu, sd)

    for i in range(FORMATION_D, len(df)):
        wa, wb = la[i - FORMATION_D:i], lb[i - FORMATION_D:i]

        if pos is None:
            # formation screen (trailing window only)
            ca = np.corrcoef(wa, wb)[0, 1]
            if not np.isfinite(ca) or ca < CORR_MIN:
                continue
            beta = np.cov(wa, wb)[0, 1] / max(np.var(wb), 1e-12)
            spread_w = wa - beta * wb
            mu, sd = spread_w.mean(), spread_w.std()
            if sd <= 1e-9:
                continue
            # mean-reversion check: AR(1) of the spread
            s0, s1 = spread_w[:-1], spread_w[1:]
            denom = np.var(s0)
            ar1 = (np.cov(s0, s1)[0, 1] / denom) if denom > 1e-12 else 1.0
            if ar1 >= AR1_MAX:
                continue
            z = (la[i] - beta * lb[i] - mu) / sd
            if abs(z) >= Z_ENTRY and abs(z) < Z_STOP:
                pos = {"entry_i": i, "sign": -np.sign(z), "beta": beta,
                       "mu": mu, "sd": sd, "entry_z": z}
        else:
            beta, mu, sd = pos["beta"], pos["mu"], pos["sd"]
            z = (la[i] - beta * lb[i] - mu) / sd
            days = i - pos["entry_i"]
            reason = None
            if pos["sign"] > 0 and z >= 0:
                reason = "converged"
            elif pos["sign"] < 0 and z <= 0:
                reason = "converged"
            elif abs(z) >= Z_STOP:
                reason = "spread_blowout"
            elif days >= TIME_STOP_D:
                reason = "time_stop"
            if reason:
                j = pos["entry_i"]
                # P&L: sign>0 means spread was too LOW -> long A, short beta*B
                ret_a = la[i] - la[j]          # log return of A
                ret_b = lb[i] - lb[j]
                gross = pos["sign"] * (ret_a - beta * ret_b)
                net = gross - RT_COST
                episodes.append({
                    "pair": f"{sym_a}/{sym_b}",
                    "entry_time": idx[j], "exit_time": idx[i],
                    "days": days, "entry_z": round(pos["entry_z"], 2),
                    "gross_pct": round(gross * 100, 3),
                    "net_pct": round(net * 100, 3),
                    "exit_reason": reason,
                })
                pos = None
    return episodes


# ============================================================================
# Evaluation helpers
# ============================================================================

def in_oos(ts: pd.Timestamp) -> bool:
    for (_, _, _, s, e) in FOLDS:
        if pd.Timestamp(s, tz="UTC") <= ts <= pd.Timestamp(e, tz="UTC"):
            return True
    return False


def bootstrap_ci(x: np.ndarray, iters: int = BOOT_ITERS):
    if len(x) < 8:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(42)
    means = np.array([rng.choice(x, len(x), replace=True).mean()
                      for _ in range(iters)])
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def portfolio_apy(eps: list[dict], max_slots: int = MAX_SLOTS) -> dict:
    """K equal-capital slots, episodes admitted chronologically. Capital per
    pair = 2 legs -> return on slot capital = net_pct/2 (both legs funded)."""
    if not eps:
        return {}
    eps = sorted(eps, key=lambda e: e["entry_time"])
    t0, t1 = eps[0]["entry_time"], max(e["exit_time"] for e in eps)
    years = max((t1 - t0).days / 365.25, 1e-9)
    equity = 1.0
    active: list = []
    queue = list(eps)
    grid = pd.date_range(t0, t1, freq="D")
    slots_used = 0
    for t in grid:
        for e in [e for e in active if e["exit_time"] <= t]:
            equity *= (1 + (e["net_pct"] / 100.0) / 2 / max_slots)
        active = [e for e in active if e["exit_time"] > t]
        while queue and queue[0]["entry_time"] <= t and len(active) < max_slots:
            active.append(queue.pop(0))
        while queue and queue[0]["entry_time"] <= t and len(active) >= max_slots:
            if queue[0]["exit_time"] <= t:
                queue.pop(0)
            else:
                break
        slots_used += len(active)
    apy = equity ** (1 / years) - 1
    return {"apy": apy, "years": years,
            "deployment": slots_used / (len(grid) * max_slots)}


# ============================================================================
# Main
# ============================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=20,
                    help="universe size (pairs = n*(n-1)/2)")
    ap.add_argument("--min-history-days", type=int, default=730)
    args = ap.parse_args()

    print("Loading universe ...")
    universe = fetch_all_perp_symbols()
    qualified = filter_by_history(universe, args.min_history_days)
    ticker = fetch_24h_ticker_all()
    vol_map = dict(zip(ticker["symbol"], ticker["quoteVolume"]))
    syms = qualified["symbol"].tolist()
    syms.sort(key=lambda s: -vol_map.get(s, 0))
    syms = syms[:args.top_n]
    print(f"  {len(syms)} symbols -> {len(syms)*(len(syms)-1)//2} candidate pairs")

    end_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    start_ms = int(pd.Timestamp("2019-09-01", tz="UTC").timestamp() * 1000)
    closes = {}
    for s in syms:
        try:
            df = wf6.fetch_ohlcv_cached(s, start_ms, end_ms)
            if not df.empty and len(df) > 400:
                closes[s] = df["close"]
        except Exception as e:
            print(f"  WARN {s}: {e}")
    print(f"  loaded {len(closes)}")

    all_eps = []
    pairs = list(itertools.combinations(sorted(closes.keys()), 2))
    for k, (a, b) in enumerate(pairs, 1):
        all_eps.extend(simulate_pair(closes[a], closes[b], a, b))
        if k % 30 == 0:
            print(f"  {k}/{len(pairs)} pairs simulated ({len(all_eps)} episodes)")

    if not all_eps:
        print("\nNo episodes at all — screen too strict or no co-moving pairs. REJECT.")
        return

    ep = pd.DataFrame(all_eps).sort_values("entry_time")
    ep.to_csv("pairs_lab_episodes.csv", index=False)

    full = ep["net_pct"].values
    oos_mask = ep["entry_time"].apply(in_oos)
    oos = ep.loc[oos_mask, "net_pct"].values

    def report(label, x):
        if len(x) == 0:
            print(f"  {label}: 0 episodes"); return None
        lb, ub = bootstrap_ci(np.asarray(x))
        win = float((np.asarray(x) > 0).mean())
        star = "*" if (not math.isnan(lb)) and lb > 0 else " "
        print(f"  {label}: n={len(x)}  win={win:.0%}  mean_net={np.mean(x):+.2f}%  "
              f"CI95=[{lb:+.3f},{ub:+.3f}] {star}")
        return lb

    print(f"\n=== EPISODES (net of {RT_COST*100:.2f}% round-trip costs) ===")
    report("FULL", full)
    oos_lb = report("OOS ", oos)
    print(f"  exit reasons: {ep['exit_reason'].value_counts().to_dict()}")
    print(f"  top pairs by episode count:")
    print(ep.groupby("pair")["net_pct"].agg(["count", "mean"])
            .sort_values("count", ascending=False).head(8).to_string())

    port = portfolio_apy(all_eps)
    if port:
        print(f"\n=== PORTFOLIO (K={MAX_SLOTS} slots) ===")
        print(f"  ann return on capital: {port['apy']:+.2%}  "
              f"(deployment {port['deployment']:.0%}, span {port['years']:.1f}y)")

    print(f"\n=== DECISION RULE ===")
    checks = [
        ("OOS CI lower bound > 0 (starred)", oos_lb is not None and not math.isnan(oos_lb) and oos_lb > 0),
        (f"OOS n >= 30 (n={len(oos)})", len(oos) >= 30),
        ("Portfolio APY > 8%", bool(port) and port["apy"] > 0.08),
    ]
    for lbl, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {lbl}")
    print(f"\n  VERDICT: "
          f"{'PROMOTE to paper trading' if all(ok for _, ok in checks) else 'REJECT — tombstone it'}")
    print("\nWrote pairs_lab_episodes.csv")


if __name__ == "__main__":
    main()
