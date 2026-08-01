"""
Inverted-structural test.
=========================

Runs the v2 config (4H, 8-symbol universe, real funding) in BOTH directions
side-by-side:

  normal   = original framework (long on bullish break, short on bearish break)
  inverted = contrarian (short on bullish break, long on bearish break)

If the framework has statistically significant negative edge, the inverse
should have statistically significant positive edge. This is the simplest
way to recover the user's structural work — as a mean-reversion signal
rather than a trend signal.

Writes inverted_trades.csv. Bootstrap it with:
    python bootstrap.py --file inverted_trades.csv

Usage:
    python run_inverted.py
"""

from __future__ import annotations

import math
from dataclasses import replace

import pandas as pd

import mtf_structural_backtest as bt
import v2_4h_backtest as v2
from funding import fetch_funding, align_funding_to_bars


def fmt(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def print_compare(rows: list[dict]):
    cols = ["variant", "trades", "hit_rate", "expectancy_R", "sharpe",
            "max_dd", "CAGR", "profit_factor"]
    w = {"variant": 14, "trades": 8, "hit_rate": 10, "expectancy_R": 14,
         "sharpe": 9, "max_dd": 9, "CAGR": 9, "profit_factor": 14}
    header = " ".join(f"{c:<{w[c]}}" for c in cols)
    print(header)
    print("-" * len(header))
    for r in rows:
        print(" ".join(f"{fmt(r.get(c)):<{w[c]}}" for c in cols))


def run_variant(cfg, data, funding_by_symbol, label: str) -> dict:
    # Pass a fresh copy of data so the funding-splice doesn't mutate across runs
    data_copy = {k: v.copy() for k, v in data.items()}
    fund_copy = {k: v.copy() for k, v in funding_by_symbol.items()}
    result = bt.run(cfg, data=data_copy, funding_by_symbol=fund_copy,
                    verbose=False, write_csv=False)
    m = result["metrics"].get("PORTFOLIO", {})
    return {
        "variant": label,
        "trades": m.get("trade_count"),
        "hit_rate": m.get("hit_rate"),
        "expectancy_R": m.get("expectancy_R"),
        "sharpe": m.get("sharpe"),
        "max_dd": m.get("max_drawdown"),
        "CAGR": m.get("CAGR"),
        "profit_factor": m.get("profit_factor"),
        "trades_obj": result["trades"],
    }


def main():
    cfg_normal = v2.CFG_V2
    cfg_inverted = replace(cfg_normal, invert_signals=True)

    print(f"v2 universe ({len(cfg_normal.symbols)} symbols, {cfg_normal.interval}) — loading OHLCV ...")
    data = bt.load_universe(cfg_normal)
    if not data:
        print("No data — exiting.")
        return

    bar_minutes = v2.BAR_MINUTES_BY_INTERVAL.get(cfg_normal.interval, 240)
    start_ms = int(pd.Timestamp(cfg_normal.start_date, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)

    print("\nLoading funding history ...")
    funding_by_symbol = {}
    for s in data.keys():
        print(f"  funding: {s} ...")
        ev = fetch_funding(s, start_ms, end_ms)
        if ev.empty:
            continue
        funding_by_symbol[s] = align_funding_to_bars(ev, data[s].index, bar_minutes)

    print("\n--- running NORMAL ---")
    normal = run_variant(cfg_normal, data, funding_by_symbol, "normal")
    print(f"  {normal['trades']} trades")

    print("\n--- running INVERTED ---")
    inverted = run_variant(cfg_inverted, data, funding_by_symbol, "inverted")
    print(f"  {inverted['trades']} trades")

    print("\n=== COMPARISON (PORTFOLIO) ===\n")
    print_compare([normal, inverted])

    # Write trades for bootstrap
    if normal["trades_obj"]:
        pd.DataFrame([t.__dict__ for t in normal["trades_obj"]]).to_csv(
            "normal_trades.csv", index=False)
    if inverted["trades_obj"]:
        pd.DataFrame([t.__dict__ for t in inverted["trades_obj"]]).to_csv(
            "inverted_trades.csv", index=False)
    print("\nWrote normal_trades.csv and inverted_trades.csv")
    print("Now run:  python bootstrap.py --file inverted_trades.csv")


if __name__ == "__main__":
    main()
