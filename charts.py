"""
Diagnostic charts for the structural backtest.
=============================================

Generates three plots per asset and one portfolio plot:
  1. Price (log scale) + upper/lower lines + entry/exit markers.
  2. Per-asset equity curve.
  3. Drawdown curve.
  + Portfolio equity + drawdown.

PNGs are written to ./charts/.

Usage:
    python charts.py                 # run a fresh backtest, then plot
    python charts.py --use-cache     # reuse cached data dir if present

Dependencies:
    pip install matplotlib
"""

from __future__ import annotations

import os
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd

import mtf_structural_backtest as bt


OUT_DIR = Path("charts")


def _ensure_outdir():
    OUT_DIR.mkdir(exist_ok=True)


def plot_price_with_lines(symbol: str, df: pd.DataFrame, trades: list[bt.Trade]):
    fig, ax = plt.subplots(figsize=(14, 7))
    ax.set_yscale("log")
    ax.plot(df.index, df["close"], color="#222", linewidth=0.9, label="Close")
    ax.plot(df.index, df["upper_line"], color="#c0392b", linewidth=0.9,
            linestyle="--", label="Upper line (bullish reference)")
    ax.plot(df.index, df["lower_line"], color="#2980b9", linewidth=0.9,
            linestyle="--", label="Lower line (bearish reference)")

    sym_trades = [t for t in trades if t.symbol == symbol]
    for t in sym_trades:
        color = "#27ae60" if t.side > 0 else "#c0392b"
        marker_entry = "^" if t.side > 0 else "v"
        ax.scatter(t.entry_date, t.entry_price, marker=marker_entry,
                   color=color, edgecolor="black", linewidth=0.5, s=70, zorder=5)
        ax.scatter(t.exit_date, t.exit_price, marker="x",
                   color=color, s=70, zorder=5)
        ax.plot([t.entry_date, t.exit_date], [t.entry_price, t.exit_price],
                color=color, linewidth=0.6, alpha=0.5)

    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.grid(True, which="both", alpha=0.2)
    ax.set_title(f"{symbol} — price (log), structural lines, trades  "
                 f"[{len(sym_trades)} trades]")
    ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout()
    out = OUT_DIR / f"{symbol}_price.png"
    fig.savefig(out, dpi=120)
    plt.close(fig)
    print(f"  wrote {out}")


def plot_equity_and_dd(name: str, equity: pd.Series, starting_equity: float):
    rolling_peak = equity.cummax()
    dd = (equity - rolling_peak) / rolling_peak

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True,
                                    gridspec_kw={"height_ratios": [3, 1]})
    ax1.plot(equity.index, equity.values, color="#2c3e50", linewidth=1.1)
    ax1.axhline(starting_equity, color="#888", linestyle=":", linewidth=0.8)
    ax1.set_ylabel("Equity ($)")
    ax1.set_title(f"{name} — equity curve and drawdown")
    ax1.grid(True, alpha=0.25)

    ax2.fill_between(dd.index, dd.values, 0, color="#c0392b", alpha=0.4)
    ax2.set_ylabel("Drawdown")
    ax2.grid(True, alpha=0.25)
    ax2.xaxis.set_major_locator(mdates.YearLocator())
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    fig.tight_layout()
    out = OUT_DIR / f"{name}_equity.png"
    fig.savefig(out, dpi=120)
    plt.close(fig)
    print(f"  wrote {out}")


def main():
    _ensure_outdir()
    print("Running backtest ...")
    result = bt.run(verbose=False, write_csv=False)
    if not result["metrics"]:
        print("Backtest returned no data.")
        return

    trades = result["trades"]
    signal_frames = result["signal_frames"]
    eq_curves = result["equity_curves"]
    starting_equity = bt.CFG.starting_equity / len(signal_frames)

    print("\nPlotting per-asset charts ...")
    for sym, df in signal_frames.items():
        plot_price_with_lines(sym, df, trades)
        plot_equity_and_dd(sym, eq_curves[sym], starting_equity)

    print("\nPlotting portfolio equity ...")
    plot_equity_and_dd("PORTFOLIO", eq_curves["PORTFOLIO"], bt.CFG.starting_equity)

    print("\nDone. PNGs are in ./charts/")


if __name__ == "__main__":
    main()
