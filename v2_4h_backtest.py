"""
v2 — 4H bars, expanded universe, real Binance funding.
======================================================

Three upgrades over the Daily baseline:
  1. 4H bars (6x more data points → larger sample size for inference)
  2. Universe: BTC, ETH, SOL, BNB, AVAX, LINK, DOGE, XRP (+4 alts)
  3. Real per-bar funding from Binance USD-M perp endpoint
     + optional funding entry filter

Bar-count-dependent params are rescaled (1 Daily bar = 6 × 4H bars):
  - anchor_window:  180 daily   → 1080 4H  (still ~180 days)
  - atr_period:      14         → still 14 (smoothing scale, not lookback)
  - sma_period:      50         → 300     (~50 days)
  - sma_slope_lookback: 10      → 60      (~10 days)
  - time_stop_bars: 60          → 360     (~60 days)

The break_k_atr default is set to 0.0 based on the v1 ablation result.

Usage:
    python v2_4h_backtest.py
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

import pandas as pd

import mtf_structural_backtest as bt
from funding import fetch_funding, align_funding_to_bars


# Expanded universe — top liquid USD-M perps with deep history.
V2_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
              "AVAXUSDT", "LINKUSDT", "DOGEUSDT", "XRPUSDT")

CFG_V2 = replace(
    bt.CFG,
    symbols=V2_SYMBOLS,
    interval="4h",
    start_date="2020-09-01",   # 4H + perp funding starts later than Daily spot
    anchor_window=1080,        # ~180 days at 4H
    anchor_offset_n=18,        # ~3 days at 4H
    min_bars_between_resets=30,  # ~5 days
    atr_period=14,
    sma_period=300,            # ~50 days
    sma_slope_lookback=60,     # ~10 days
    initial_stop_atr_mult=1.5,
    trail_atr_mult=2.0,
    time_stop_bars=360,        # ~60 days
    break_k_atr=0.0,           # v1 ablation winner
    # Filters: keep the helpful ones; drop SMA which didn't pay for itself.
    use_sma_filter=False,
    require_prev_close_outside=True,
    use_structural_invalidation=True,
    use_funding_filter=True,
    funding_filter_max_bps_8h=20.0,
    # Funding placeholder unused once real funding is supplied:
    funding_bps_per_day=0.0,
)


BAR_MINUTES_BY_INTERVAL = {"1d": 1440, "12h": 720, "4h": 240, "1h": 60}


def load_funding(symbols, start_ms, end_ms, bar_index_by_symbol, bar_minutes):
    out = {}
    for s in symbols:
        print(f"  funding: {s} ...")
        ev = fetch_funding(s, start_ms, end_ms)
        if ev.empty:
            print(f"    WARN: no funding history for {s}")
            continue
        idx = bar_index_by_symbol.get(s)
        if idx is None or len(idx) == 0:
            continue
        out[s] = align_funding_to_bars(ev, idx, bar_minutes)
    return out


def main():
    cfg = CFG_V2
    bar_minutes = BAR_MINUTES_BY_INTERVAL.get(cfg.interval, 240)

    print(f"v2 backtest — interval={cfg.interval}, universe={len(cfg.symbols)} symbols")
    print("Loading OHLCV ...")
    data = bt.load_universe(cfg)
    if not data:
        print("No data — exiting.")
        return

    start_ms = int(pd.Timestamp(cfg.start_date, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    print("\nLoading funding history ...")
    bar_index_by_symbol = {s: df.index for s, df in data.items()}
    funding_by_symbol = load_funding(list(data.keys()), start_ms, end_ms,
                                     bar_index_by_symbol, bar_minutes)

    print("\nRunning backtest ...")
    result = bt.run(cfg, data=data, funding_by_symbol=funding_by_symbol,
                    verbose=True, write_csv=False)

    # Dump trades with v2 name to avoid overwriting v1 results.
    trades = result["trades"]
    if trades:
        trades_df = pd.DataFrame([t.__dict__ for t in trades])
        trades_df.to_csv("v2_trades.csv", index=False)
        print(f"\nWrote v2_trades.csv ({len(trades)} trades)")


if __name__ == "__main__":
    main()
