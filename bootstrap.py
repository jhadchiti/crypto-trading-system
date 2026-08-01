"""
Bootstrap confidence intervals on trade-level expectancy and Sharpe.
====================================================================

Reads trades.csv (produced by mtf_structural_backtest.py), resamples the
R-multiple distribution with replacement, and reports 95% CIs.

The interpretation:
  - If the 95% CI on expectancy_R includes 0, you cannot reject "no edge"
    at the 95% level. Small samples almost always land here.
  - The CI on Sharpe is computed from the resampled R-multiple series treated
    as a sequence of equal-time returns — it's a rough trade-level Sharpe,
    not the time-series Sharpe of the equity curve. Use it for relative
    comparison across configs, not as a substitute for time-series Sharpe.

Usage:
    python bootstrap.py                       # default: trades.csv, 10k iters
    python bootstrap.py --iters 50000
    python bootstrap.py --file my_trades.csv
"""

from __future__ import annotations

import argparse
import math
import sys

import numpy as np
import pandas as pd


def bootstrap_stat(samples: np.ndarray, stat_fn, n_iter: int, rng: np.random.Generator):
    n = len(samples)
    if n == 0:
        return np.nan, np.nan, np.nan
    draws = np.empty(n_iter)
    for i in range(n_iter):
        idx = rng.integers(0, n, size=n)
        draws[i] = stat_fn(samples[idx])
    return float(np.mean(draws)), float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))


def trade_sharpe(rs: np.ndarray) -> float:
    if len(rs) < 2 or rs.std() == 0:
        return 0.0
    # Rough annualization: assume ~30 trades/year as a typical scale for this strategy.
    # This is for *relative* comparison only; documented in the module docstring.
    trades_per_year = 30.0
    return (rs.mean() / rs.std()) * math.sqrt(trades_per_year)


RISK_PER_TRADE = 0.0075  # 0.75% of equity at risk per trade (matches Config)


def annual_return_pct(rs: np.ndarray, years: float) -> float:
    """Approximate annualized % return on equity.

    Sum of R-multiples × risk-per-trade gives total additive return on equity.
    Divide by years for a linear annualized rate. Suitable for a backtest
    summary; understates compounded reality but doesn't pretend to know about
    drawdown sequencing.
    """
    if years <= 0:
        return float("nan")
    total_return_frac = float(rs.sum()) * RISK_PER_TRADE
    return (total_return_frac / years) * 100.0


def summarize(label: str, rs: np.ndarray, n_iter: int, rng: np.random.Generator,
              years: float = None):
    n = len(rs)
    if n == 0:
        print(f"{label:<14}  n=0   (no trades)")
        return

    exp_mean, exp_lo, exp_hi = bootstrap_stat(rs, np.mean, n_iter, rng)
    sh_mean, sh_lo, sh_hi = bootstrap_stat(rs, trade_sharpe, n_iter, rng)
    win_rate = float(np.mean(rs > 0))

    excludes_zero_exp = (exp_lo > 0) or (exp_hi < 0)
    flag_exp = "  *" if excludes_zero_exp else "   "

    excludes_zero_sh = (sh_lo > 0) or (sh_hi < 0)
    flag_sh = "  *" if excludes_zero_sh else "   "

    ar_part = ""
    if years is not None and years > 0:
        ar = annual_return_pct(rs, years)
        ar_part = f"  ann_ret={ar:+.2f}%"

    print(f"{label:<14}  n={n:<4d}  win_rate={win_rate:.3f}{ar_part}  "
          f"exp_R={exp_mean:+.3f}  CI95=[{exp_lo:+.3f}, {exp_hi:+.3f}]{flag_exp}  "
          f"trade_sharpe={sh_mean:+.3f}  CI95=[{sh_lo:+.3f}, {sh_hi:+.3f}]{flag_sh}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="trades.csv")
    ap.add_argument("--iters", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    try:
        df = pd.read_csv(args.file)
    except FileNotFoundError:
        print(f"Could not find {args.file}. Run the backtest first.", file=sys.stderr)
        sys.exit(1)

    if "r_multiple" not in df.columns:
        print("trades.csv is missing 'r_multiple' column.", file=sys.stderr)
        sys.exit(1)

    rng = np.random.default_rng(args.seed)
    print(f"Bootstrap CI on expectancy and trade-Sharpe \u2014 {args.iters} resamples")
    print(f"  * = 95% CI excludes zero (reject null at p~0.05)")
    print(f"  ann_ret assumes {RISK_PER_TRADE*100:.2f}% risk per trade, linear annualization")
    print()

    # Compute total time span for annualized return
    years_total = None
    if "exit_date" in df.columns and "entry_date" in df.columns:
        df["entry_date"] = pd.to_datetime(df["entry_date"], utc=True, errors="coerce")
        df["exit_date"] = pd.to_datetime(df["exit_date"], utc=True, errors="coerce")
        span_days = (df["exit_date"].max() - df["entry_date"].min()).days
        years_total = span_days / 365.25 if span_days > 0 else None

    # Portfolio
    rs_all = df["r_multiple"].dropna().to_numpy()
    summarize("PORTFOLIO", rs_all, args.iters, rng, years=years_total)
    print()

    # Per-symbol
    for sym, g in df.groupby("symbol"):
        rs = g["r_multiple"].dropna().to_numpy()
        summarize(sym, rs, args.iters, rng, years=years_total)

    # In-sample vs out-of-sample
    if "exit_date" in df.columns:
        is_cut = pd.Timestamp("2024-01-01", tz="UTC")
        is_df = df[df["exit_date"] < is_cut]
        oos_df = df[df["exit_date"] >= is_cut]
        is_rs = is_df["r_multiple"].dropna().to_numpy()
        oos_rs = oos_df["r_multiple"].dropna().to_numpy()
        is_years = ((is_df["exit_date"].max() - is_df["entry_date"].min()).days / 365.25
                    if len(is_df) else None)
        oos_years = ((oos_df["exit_date"].max() - oos_df["entry_date"].min()).days / 365.25
                     if len(oos_df) else None)
        print()
        summarize("IN_SAMPLE", is_rs, args.iters, rng, years=is_years)
        summarize("OUT_OF_SAMPLE", oos_rs, args.iters, rng, years=oos_years)


if __name__ == "__main__":
    main()
