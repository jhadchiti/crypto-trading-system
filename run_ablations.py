"""
Ablation runner — flips one filter at a time and compares results.
==================================================================

Loads data once, then runs the backtest under each ablation config and prints
a side-by-side comparison of trade_count, hit_rate, expectancy_R, sharpe,
max_drawdown, and CAGR at the PORTFOLIO level.

The point: see which filter is paying for itself in edge versus just suppressing
trade count.

Usage:
    python run_ablations.py
"""

from __future__ import annotations

import copy
import math
from dataclasses import replace

import pandas as pd

import mtf_structural_backtest as bt


ABLATIONS = [
    # (label, dict of Config overrides)
    ("baseline",                 {}),
    ("no_sma_filter",            {"use_sma_filter": False}),
    ("no_prev_close_req",        {"require_prev_close_outside": False}),
    ("no_structural_exit",       {"use_structural_invalidation": False}),
    ("anchor_window_90",         {"anchor_window": 90}),
    ("anchor_window_360",        {"anchor_window": 360}),
    ("break_k_0.0",              {"break_k_atr": 0.0}),
    ("break_k_0.5",              {"break_k_atr": 0.5}),
    ("loose_all",                {"use_sma_filter": False,
                                  "require_prev_close_outside": False,
                                  "anchor_window": 90}),
]


def fmt(v) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        if math.isnan(v):
            return "n/a"
        return f"{v:.3f}"
    return str(v)


def main():
    print("Loading universe (one-time fetch) ...")
    data = bt.load_universe(bt.CFG)
    if not data:
        print("No data — exiting.")
        return

    rows = []
    for label, overrides in ABLATIONS:
        cfg = replace(bt.CFG, **overrides)
        # Pass a fresh shallow copy so each run sees the original OHLCV
        data_copy = {k: v.copy() for k, v in data.items()}
        result = bt.run(cfg, data=data_copy, verbose=False, write_csv=False)
        m = result["metrics"].get("PORTFOLIO", {})
        rows.append({
            "ablation": label,
            "trades": m.get("trade_count"),
            "hit_rate": m.get("hit_rate"),
            "expectancy_R": m.get("expectancy_R"),
            "sharpe": m.get("sharpe"),
            "max_dd": m.get("max_drawdown"),
            "CAGR": m.get("CAGR"),
            "profit_factor": m.get("profit_factor"),
        })
        print(f"  ran: {label:<22}  trades={m.get('trade_count')}")

    df = pd.DataFrame(rows)
    print("\n=== ABLATION COMPARISON (PORTFOLIO) ===\n")
    # pretty print
    col_w = {"ablation": 22, "trades": 8, "hit_rate": 10, "expectancy_R": 14,
             "sharpe": 9, "max_dd": 9, "CAGR": 9, "profit_factor": 14}
    header = " ".join(f"{c:<{col_w[c]}}" for c in df.columns)
    print(header)
    print("-" * len(header))
    for _, r in df.iterrows():
        cells = []
        for c in df.columns:
            v = r[c]
            cells.append(f"{fmt(v):<{col_w[c]}}")
        print(" ".join(cells))

    df.to_csv("ablations.csv", index=False)
    print(f"\nWrote ablations.csv ({len(df)} rows)")


if __name__ == "__main__":
    main()
