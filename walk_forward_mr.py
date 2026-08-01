"""
Walk-Forward Validation for the Mean-Reversion Strategy.
=========================================================

Same fold structure as walk_forward_v3-v6. Tests two variants:

  baseline:        MR fires anytime conditions are met (regardless of macro)
  macro_off_only:  MR fires only when BTC macro is OFF (purely complementary
                    to trend, never overlaps)

For each variant, runs the full strategy across 8 historical folds, aggregates
test-window trades, writes CSVs ready for bootstrap.py.

Uses cached OHLCV from ./cache/ohlcv/ (populated by universe_scan.py).

Usage:
    python walk_forward_mr.py
    python bootstrap.py --file walkforward_mr_baseline_trades.csv
    python bootstrap.py --file walkforward_mr_macro_off_only_trades.csv
"""

from __future__ import annotations

import math
import time
from dataclasses import replace
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

import mtf_structural_backtest as bt
import walk_forward_v3 as wf3
from funding import fetch_funding, align_funding_to_bars
from walk_forward import FOLDS, filter_trades_by_entry, trade_sharpe
from sentiment_filters import fetch_fear_greed_history
from market_data import (
    fetch_all_perp_symbols, filter_by_history, fetch_24h_ticker_all, _ensure_cache,
)
import mean_reversion_strategy as mrs
import mean_reversion_backtest as mrb


V2_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
              "AVAXUSDT", "LINKUSDT", "DOGEUSDT", "XRPUSDT")
RISK_PER_TRADE = 0.0075


def fetch_ohlcv_cached(symbol: str, start_ms: int, end_ms: int,
                       min_bars: int = 1500) -> pd.DataFrame:
    cache_dir = _ensure_cache("ohlcv")
    cache_file = cache_dir / f"{symbol}.csv"
    if cache_file.exists():
        df = pd.read_csv(cache_file, parse_dates=["date"], index_col="date")
        df.index = pd.to_datetime(df.index, utc=True)
        recent_ok = (not df.empty and
                     df.index[-1] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=2))
        depth_ok = len(df) >= min_bars
        if recent_ok and depth_ok:
            return df
    df = bt.fetch_binance_klines(symbol, "1d", start_ms, end_ms)
    if not df.empty:
        df.to_csv(cache_file)
        time.sleep(0.15)
    return df


def fold_stats(trades, te_start, te_end):
    tr = filter_trades_by_entry(trades, te_start, te_end)
    s = pd.Timestamp(te_start, tz="UTC")
    e = pd.Timestamp(te_end, tz="UTC")
    years = max((e - s).days / 365.25, 1e-9)
    if not tr:
        return {"n": 0, "win": float("nan"), "exp": float("nan"), "ann_ret": float("nan")}
    rs = np.array([t.r_multiple for t in tr])
    return {"n": len(tr), "win": float((rs > 0).mean()),
            "exp": float(rs.mean()),
            "ann_ret": (float(rs.sum()) / years) * RISK_PER_TRADE * 100.0}


def aggregate(trades, fold_windows):
    rs = []; total_years = 0.0; collected = []
    for (_, _, _, te_start, te_end) in fold_windows:
        tr = filter_trades_by_entry(trades, te_start, te_end)
        collected.extend(tr)
        rs.extend([t.r_multiple for t in tr])
        s = pd.Timestamp(te_start, tz="UTC"); e = pd.Timestamp(te_end, tz="UTC")
        total_years += max((e - s).days / 365.25, 0.0)
    if not rs or total_years <= 0:
        return None
    rs_arr = np.array(rs)
    return {"n": len(rs), "win": float((rs_arr > 0).mean()),
            "exp": float(rs_arr.mean()),
            "sh": trade_sharpe(rs_arr),
            "ann_ret": (float(rs_arr.sum()) / total_years) * RISK_PER_TRADE * 100.0,
            "trades": collected}


def fmt(x, w=6, pct=False):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return f"{'n/a':>{w}}"
    if isinstance(x, float):
        return f"{x:>+{w}.1f}" if pct else f"{x:>+{w}.2f}"
    return f"{x:>{w}}"


def main():
    print("Loading universe (top 30 by current volume) ...")
    universe = fetch_all_perp_symbols()
    qualified = filter_by_history(universe, min_history_days=730)
    ticker = fetch_24h_ticker_all()
    vol_map = dict(zip(ticker["symbol"], ticker["quoteVolume"]))
    syms = qualified["symbol"].tolist()
    syms.sort(key=lambda s: -vol_map.get(s, 0))
    syms = syms[:30]
    # Always include BTC
    if "BTCUSDT" not in syms:
        syms.append("BTCUSDT")
    print(f"  selected {len(syms)} symbols")

    end_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    start_ms = int(pd.Timestamp("2019-09-01", tz="UTC").timestamp() * 1000)

    print("\nLoading OHLCV (cached) ...")
    data = {}
    funding_by_symbol = {}
    for i, sym in enumerate(syms, 1):
        try:
            df = fetch_ohlcv_cached(sym, start_ms, end_ms)
            if df.empty or len(df) < 365:
                continue
            data[sym] = df
            ev = fetch_funding(sym, start_ms, end_ms)
            if not ev.empty:
                funding_by_symbol[sym] = align_funding_to_bars(ev, df.index, 1440)
            if i % 10 == 0:
                print(f"  {i}/{len(syms)}")
        except Exception as e:
            print(f"  WARN {sym}: {e}")
    print(f"  loaded {len(data)} symbols")

    if "BTCUSDT" not in data:
        print("BTCUSDT missing — aborting.")
        return

    print("\nLoading Fear & Greed history ...")
    fng_df = fetch_fear_greed_history()
    print(f"  {len(fng_df)} daily FNG values")
    fng_series = fng_df["fng_value"] if "fng_value" in fng_df.columns else pd.Series(50, dtype=float)

    btc_regime = wf3.compute_btc_regime(data["BTCUSDT"])

    # ---- Variants ----
    variants = [
        ("baseline", mrs.MeanReversionConfig(require_macro_off=False)),
        ("macro_off_only", mrs.MeanReversionConfig(require_macro_off=True)),
    ]

    print()
    all_var_trades = {}
    for label, cfg in variants:
        print(f"Running variant '{label}' ...")
        all_trades = []
        for sym, df in data.items():
            fund_info = funding_by_symbol.get(sym)
            if fund_info is not None and "funding_bps_8h_last" in fund_info.columns:
                funding_series = fund_info["funding_bps_8h_last"]
                funding_carry = fund_info.get("funding_bps_in_bar")
            else:
                funding_series = pd.Series(0.0, index=df.index)
                funding_carry = None

            trades, _ = mrb.backtest_mr_symbol(
                df=df, symbol=sym, equity=12_500.0,
                fng_series=fng_series.reindex(df.index, method="ffill").fillna(50.0),
                funding_series=funding_series,
                cfg=cfg,
                btc_regime=btc_regime,
                funding_carry=funding_carry,
            )
            all_trades.extend(trades)
        print(f"  {len(all_trades)} trades total")
        all_var_trades[label] = all_trades

    # ---- per-fold table ----
    print("\n=== PER-FOLD TEST RESULTS ===\n")
    print(f"  {'fold':<4} {'window':<20} | "
          f"{'BASELINE':^28} | {'MACRO_OFF_ONLY':^28}")
    print(f"  {'':<4} {'':<20} | "
          f"{'n':>4} {'expR':>6} {'%/yr':>7}  | "
          f"{'n':>4} {'expR':>6} {'%/yr':>7}")
    print("-" * 86)

    for (label, _, _, te_start, te_end) in FOLDS:
        a = fold_stats(all_var_trades["baseline"], te_start, te_end)
        b = fold_stats(all_var_trades["macro_off_only"], te_start, te_end)
        ws = f"{te_start[:7]}->{te_end[:7]}"
        print(f"  {label:<4} {ws:<20} | "
              f"{a['n']:>4} {fmt(a['exp'])} {fmt(a['ann_ret'], 7, pct=True)}  | "
              f"{b['n']:>4} {fmt(b['exp'])} {fmt(b['ann_ret'], 7, pct=True)}")

    print("\n=== AGGREGATE (all test windows) ===\n")
    for label in ("baseline", "macro_off_only"):
        agg = aggregate(all_var_trades[label], FOLDS)
        if agg is None:
            print(f"  {label:<16}: 0 trades"); continue
        print(f"  {label:<16}  n={agg['n']:<4}  win={agg['win']:.3f}  "
              f"exp_R={agg['exp']:+.3f}  trade_sh={agg['sh']:+.3f}  "
              f"ann_ret={agg['ann_ret']:+.2f}%")
        if agg["trades"]:
            out = f"walkforward_mr_{label}_trades.csv"
            pd.DataFrame([t.__dict__ for t in agg["trades"]]).to_csv(out, index=False)

    print("\nWrote per-variant trade CSVs.")
    print("\nFinal step:")
    for label in ("baseline", "macro_off_only"):
        print(f"  python bootstrap.py --file walkforward_mr_{label}_trades.csv")
    print("\nDecision rule: if either variant shows starred OOS CI on aggregate,")
    print("MR sleeve is validated. If both show 0% positive expectancy, shelve it.")


if __name__ == "__main__":
    main()
