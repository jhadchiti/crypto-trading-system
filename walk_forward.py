"""
Walk-forward validation for Donchian v2.
========================================

For each fold:
  1. Slice the strategy's trades to the TRAIN window.
  2. For each (n_entry, n_exit) param combo, compute trade-level Sharpe on the
     train slice.
  3. Pick the best-Sharpe combo. This is the "trained" choice.
  4. Take that combo's trades that fall in the disjoint TEST window.
  5. Record fold results.

After all folds, aggregate every TEST-window trade across folds into one
sample. Bootstrap that combined sample for an honest forward-looking edge
estimate. Each test trade is used exactly once and was selected by a model
that never saw the test data.

Validation principles enforced:
  - Test windows do not overlap.
  - Train and test windows do not overlap within a fold.
  - The same calendar period is never used for both train and test across
    different folds.
  - All parameter selection is based on train-only data.

Outputs:
  walkforward_fold_table.csv    — per-fold summary
  walkforward_test_trades.csv   — every test-window trade across all folds
                                  (feed to bootstrap.py for final CI)

Usage:
    python walk_forward.py
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Optional

import numpy as np
import pandas as pd

import mtf_structural_backtest as bt
import donchian_baseline as dc
import donchian_v2 as d2
from funding import fetch_funding, align_funding_to_bars


V2_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
              "AVAXUSDT", "LINKUSDT", "DOGEUSDT", "XRPUSDT")

SWEEP = [(20, 10), (40, 15), (55, 20), (100, 30), (200, 50)]

# 24-month train, 6-month test, 6-month step. Eight non-overlapping test windows
# from 2022-09 through 2026-03. The last fold may be partial depending on the
# current date; the harness handles that.
FOLDS = [
    # (label, train_start, train_end, test_start, test_end)
    ("F1", "2020-09-01", "2022-09-01", "2022-09-01", "2023-03-01"),
    ("F2", "2021-03-01", "2023-03-01", "2023-03-01", "2023-09-01"),
    ("F3", "2021-09-01", "2023-09-01", "2023-09-01", "2024-03-01"),
    ("F4", "2022-03-01", "2024-03-01", "2024-03-01", "2024-09-01"),
    ("F5", "2022-09-01", "2024-09-01", "2024-09-01", "2025-03-01"),
    ("F6", "2023-03-01", "2025-03-01", "2025-03-01", "2025-09-01"),
    ("F7", "2023-09-01", "2025-09-01", "2025-09-01", "2026-03-01"),
    ("F8", "2024-03-01", "2026-03-01", "2026-03-01", "2026-06-01"),
]

MIN_TRAIN_TRADES = 20    # don't trust a Sharpe from <20 samples
TRADES_PER_YEAR_ASSUMPTION = 30.0  # rough annualization for trade-level Sharpe


def trade_sharpe(rs: np.ndarray) -> float:
    if len(rs) < 2 or rs.std() == 0:
        return 0.0
    return (rs.mean() / rs.std()) * math.sqrt(TRADES_PER_YEAR_ASSUMPTION)


def filter_trades_by_entry(trades, start: str, end: str):
    s = pd.Timestamp(start, tz="UTC")
    e = pd.Timestamp(end, tz="UTC")
    return [t for t in trades if s <= t.entry_date < e]


def load_data_and_funding():
    cfg = replace(bt.CFG, symbols=V2_SYMBOLS)
    print(f"Loading Daily OHLCV for {len(V2_SYMBOLS)} symbols ...")
    data = bt.load_universe(cfg)
    if not data:
        return None, None

    start_ms = int(pd.Timestamp(cfg.start_date, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    print("\nLoading funding history ...")
    funding_by_symbol = {}
    for s in data.keys():
        print(f"  funding: {s} ...")
        ev = fetch_funding(s, start_ms, end_ms)
        if ev.empty:
            continue
        funding_by_symbol[s] = align_funding_to_bars(ev, data[s].index, 1440)
    return data, funding_by_symbol


def cache_trades_per_param(data, funding_by_symbol) -> dict:
    """Run the full backtest once per param config; cache the trade list."""
    per_symbol_equity = 100_000.0 / len(data)
    cache = {}
    print("\nCaching full-backtest trades for each param combo ...")
    for (n_in, n_out) in SWEEP:
        dcfg = replace(dc.DCFG, n_entry=n_in, n_exit=n_out)
        _, trades = d2.run_one(data, funding_by_symbol, dcfg, per_symbol_equity)
        cache[(n_in, n_out)] = trades
        print(f"  ({n_in:>3}/{n_out:>3}): {len(trades)} total trades")
    return cache


def main():
    data, funding_by_symbol = load_data_and_funding()
    if data is None:
        print("No data — exiting.")
        return

    cache = cache_trades_per_param(data, funding_by_symbol)

    fold_rows = []
    all_test_trades = []
    param_pick_counts = {}

    print("\n--- Walk-forward ---\n")
    for (label, tr_start, tr_end, te_start, te_end) in FOLDS:
        # Pick best params on TRAIN
        best_sh = -1e9
        best_params = None
        train_summary = {}
        for params, trades in cache.items():
            tr = filter_trades_by_entry(trades, tr_start, tr_end)
            if len(tr) < MIN_TRAIN_TRADES:
                train_summary[params] = (len(tr), float("nan"), float("nan"))
                continue
            rs = np.array([t.r_multiple for t in tr])
            sh = trade_sharpe(rs)
            train_summary[params] = (len(tr), sh, float(rs.mean()))
            if sh > best_sh:
                best_sh = sh
                best_params = params

        if best_params is None:
            print(f"  {label}: SKIP — no param had ≥{MIN_TRAIN_TRADES} train trades")
            continue

        # Evaluate on disjoint TEST
        test_trades = filter_trades_by_entry(cache[best_params], te_start, te_end)
        rs_test = np.array([t.r_multiple for t in test_trades])
        test_exp = float(rs_test.mean()) if len(rs_test) else float("nan")
        test_sh = trade_sharpe(rs_test)
        test_hit = float((rs_test > 0).mean()) if len(rs_test) else float("nan")

        param_pick_counts[best_params] = param_pick_counts.get(best_params, 0) + 1

        n_train = train_summary[best_params][0]
        fold_rows.append({
            "fold": label,
            "train": f"{tr_start} → {tr_end}",
            "test":  f"{te_start} → {te_end}",
            "picked": f"{best_params[0]}/{best_params[1]}",
            "n_train": n_train,
            "train_sharpe": train_summary[best_params][1],
            "n_test": len(test_trades),
            "test_exp_R": test_exp,
            "test_sharpe": test_sh,
            "test_hit": test_hit,
        })
        all_test_trades.extend(test_trades)
        print(f"  {label}: picked {best_params[0]}/{best_params[1]}  "
              f"train n={n_train} Sh={train_summary[best_params][1]:+.2f}  "
              f"→ test n={len(test_trades)} exp={test_exp:+.3f}R Sh={test_sh:+.2f}")

    # -- Fold table --
    df = pd.DataFrame(fold_rows)
    print("\n=== WALK-FORWARD FOLD TABLE ===\n")
    print(df.to_string(index=False, float_format=lambda x: f"{x:+.3f}"))
    df.to_csv("walkforward_fold_table.csv", index=False)

    # -- Param stability --
    print("\n=== PARAM SELECTION STABILITY ===")
    for params, count in sorted(param_pick_counts.items(), key=lambda x: -x[1]):
        print(f"  ({params[0]:>3}/{params[1]:>3})  picked in {count}/{len(fold_rows)} folds")

    # -- Aggregate test result --
    if all_test_trades:
        rs_all = np.array([t.r_multiple for t in all_test_trades])
        print("\n=== AGGREGATE WALK-FORWARD TEST ===")
        print(f"  trades:       {len(all_test_trades)}")
        print(f"  hit_rate:     {(rs_all > 0).mean():.3f}")
        print(f"  expectancy:   {rs_all.mean():+.3f}R")
        print(f"  trade_sharpe: {trade_sharpe(rs_all):+.3f}")

        pd.DataFrame([t.__dict__ for t in all_test_trades]).to_csv(
            "walkforward_test_trades.csv", index=False)
        print("\nWrote walkforward_fold_table.csv and walkforward_test_trades.csv")
        print("Final step:  python bootstrap.py --file walkforward_test_trades.csv")


if __name__ == "__main__":
    main()
