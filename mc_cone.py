"""
Monte Carlo Outcome Cone — "what does NORMAL look like?"
=========================================================

Simulates 5,000 possible 20-trade sequences by resampling the validated
strategy's OOS trade distribution (walkforward_v7 rs_only — the shipped
variant). Saves percentile bands to mc_cone.json.

Purpose: when live trading starts, the dashboard plots actual cumulative R
against this cone. If the live path is INSIDE the cone, results are noise —
no decision warranted, regardless of how it feels. Only a path breaking
BELOW the 5th percentile band is evidence of degradation.

This is computed BEFORE any live results exist, so it cannot be biased by
them. Rerun only after a re-validation changes the trade distribution.

Usage:
    python mc_cone.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

SOURCE = "walkforward_v7_rs_only_trades.csv"
N_SIMS = 5_000
N_TRADES = 20
PCTS = [5, 25, 50, 75, 95]


def main():
    df = pd.read_csv(SOURCE)
    r = pd.to_numeric(df["r_multiple"], errors="coerce").dropna().values
    print(f"source: {SOURCE} — {len(r)} trades, mean {r.mean():+.2f}R")

    rng = np.random.default_rng(42)
    # paths[s, t] = cumulative R after trade t+1 in simulation s
    draws = rng.choice(r, size=(N_SIMS, N_TRADES), replace=True)
    paths = draws.cumsum(axis=1)

    bands = {str(p): [round(float(v), 2) for v in np.percentile(paths, p, axis=0)]
             for p in PCTS}

    out = {
        "source": SOURCE,
        "n_source_trades": int(len(r)),
        "n_sims": N_SIMS,
        "n_trades": N_TRADES,
        "bands": bands,
        "p_negative_after_20": round(float((paths[:, -1] < 0).mean()), 3),
        "generated": pd.Timestamp.now(tz="UTC").isoformat(),
    }
    Path("mc_cone.json").write_text(json.dumps(out, indent=2), encoding="utf-8")

    print(f"\nAfter 20 trades (cumulative R):")
    for p in PCTS:
        print(f"  p{p:>2}: {bands[str(p)][-1]:+.1f}R")
    print(f"\nP(negative after 20 trades even though the edge is real): "
          f"{out['p_negative_after_20']:.0%}")
    print("wrote mc_cone.json")


if __name__ == "__main__":
    main()
