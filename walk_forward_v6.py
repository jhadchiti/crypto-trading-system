"""
Walk-Forward v6 — historical universe rotation.
================================================

The honest validation of the dynamic-universe pivot. Three variants:

  fixed_8        : baseline = current static 8-symbol universe (v3 btc_only)
  static_top_N   : all top-N symbols by *current* volume, fixed across history
  dynamic_top_N  : universe rebalanced per bar by trailing 30d volume ranking
                   (a symbol only fires entries when it's currently in the top-N)

The third variant is the real test: "if I had used objective volume-based
universe selection at each historical point in time, would the strategy have
performed differently from the fixed 8?"

LIMITATIONS to be honest about:
  - Survivorship bias: we can only fetch data for *currently active* perps.
    Symbols that delisted (LUNA, FTT, etc.) are absent. This biases all
    variants upward, but biases dynamic_top_N more (since it would have
    *traded* those delisted names while they were still ranked).
  - Volume is computed from spot OHLCV (close × volume); for perps the actual
    notional traded may differ slightly. Close enough for ranking.
  - Liquidity-tier breaks at the edges of the top-N boundary: symbols
    oscillating around rank N will flip in/out. Acceptable for now; could
    add hysteresis (e.g., only drop if rank > N+10) in a future iteration.

Reads cached OHLCV from ./cache/ohlcv/ (populated by universe_scan.py).

Usage:
    python walk_forward_v6.py                       # default: top 30 dynamic
    python walk_forward_v6.py --top-n 50            # bigger universe
    python walk_forward_v6.py --max-symbols 100     # scan a smaller pool
"""

from __future__ import annotations

import argparse
import math
import time
from dataclasses import replace
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

import mtf_structural_backtest as bt
import donchian_baseline as dc
import donchian_v2 as d2
import walk_forward_v3 as wf3
from funding import fetch_funding, align_funding_to_bars
from walk_forward import FOLDS, filter_trades_by_entry, trade_sharpe
from market_data import (
    fetch_all_perp_symbols, filter_by_history, fetch_24h_ticker_all, _ensure_cache,
)


CURRENT_8 = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
              "AVAXUSDT", "LINKUSDT", "DOGEUSDT", "XRPUSDT")
FROZEN_N_ENTRY = 55
FROZEN_N_EXIT = 20
RISK_PER_TRADE = 0.0075
ALWAYS_KEEP = {"BTCUSDT"}     # never rotate BTC out (needed for macro)


# ============================================================================
# Cached OHLCV loader (shared with universe_scan)
# ============================================================================

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


# ============================================================================
# Rolling notional volume (for universe ranking)
# ============================================================================

def compute_rolling_notional_volume(data: dict, window: int = 30) -> pd.DataFrame:
    """
    Returns a DataFrame indexed by date, one column per symbol, with rolling
    30-day average daily notional volume (close * volume).
    """
    cols = {}
    for sym, df in data.items():
        if df is None or df.empty:
            continue
        notional = df["close"] * df["volume"]
        cols[sym] = notional.rolling(window).mean()
    if not cols:
        return pd.DataFrame()
    return pd.DataFrame(cols).sort_index()


# ============================================================================
# Universe selector (per-bar)
# ============================================================================

def universe_at_date(volumes_df: pd.DataFrame, date: pd.Timestamp,
                     top_n: int, min_notional_usd: float = 0.0) -> set:
    """
    Return the set of symbols whose trailing 30-day average notional volume
    ranks in the top N as of `date`, with a hard liquidity floor.
    """
    if volumes_df.empty:
        return set()
    if date not in volumes_df.index:
        valid = volumes_df.index[volumes_df.index <= date]
        if len(valid) == 0:
            return set()
        date = valid[-1]
    row = volumes_df.loc[date].dropna()
    qualified = row[row >= min_notional_usd]
    top = qualified.sort_values(ascending=False).head(top_n)
    return set(top.index) | ALWAYS_KEEP


# ============================================================================
# Backtest with universe rotation
# ============================================================================

def backtest_one_symbol_with_membership(
    symbol: str,
    df: pd.DataFrame,
    funding_carry: Optional[pd.Series],
    funding_8h_last: Optional[pd.Series],
    btc_regime: pd.Series,
    is_active_at: Optional[pd.Series] = None,  # bool series; None = always active
) -> list[bt.Trade]:
    """
    Same logic as walk_forward_v3 btc_only, but entries only fire when
    `is_active_at[date]` is True. Position management runs regardless of
    current membership (so a position opened when active still exits cleanly).
    """
    dcfg = replace(dc.DCFG, n_entry=FROZEN_N_ENTRY, n_exit=FROZEN_N_EXIT)
    d = dc.build_donchian(df, dcfg)
    trades: list[bt.Trade] = []
    pos: Optional[bt.Position] = None
    equity = 12_500.0
    rt_cost_bps = 2 * (dcfg.taker_fee_bps + dcfg.slippage_bps)
    sizing_cfg = bt.Config(risk_per_trade=dcfg.risk_per_trade,
                            vol_target_annual=dcfg.vol_target_annual)

    for i, (date, row) in enumerate(d.iterrows()):
        if pos is not None:
            pos.bars_held += 1
            bar_bps = 0.0
            if funding_carry is not None:
                try: bar_bps = float(funding_carry.loc[date])
                except KeyError: pass
            equity -= (bar_bps / 10000.0) * abs(pos.size) * row["close"] * (1 if pos.side > 0 else -1)

            exit_reason = None; exit_price = None
            if pos.side > 0 and row["low"] <= pos.stop:
                exit_price = pos.stop; exit_reason = "atr_stop"
            elif pos.side < 0 and row["high"] >= pos.stop:
                exit_price = pos.stop; exit_reason = "atr_stop"
            if exit_reason is None:
                if pos.side > 0 and not math.isnan(row["exit_low"]) and row["close"] < row["exit_low"]:
                    exit_price = row["close"]; exit_reason = "channel_exit"
                elif pos.side < 0 and not math.isnan(row["exit_high"]) and row["close"] > row["exit_high"]:
                    exit_price = row["close"]; exit_reason = "channel_exit"
            if exit_reason is None and pos.bars_held >= dcfg.time_stop_bars:
                exit_price = row["close"]; exit_reason = "time_stop"
            if exit_reason is not None:
                cost = (rt_cost_bps / 10000.0) * abs(pos.size) * exit_price
                gross = (exit_price - pos.entry_price) * pos.size
                net = gross - cost
                equity += net
                r = net / pos.risk_dollars if pos.risk_dollars > 0 else 0.0
                trades.append(bt.Trade(
                    symbol=symbol, side=pos.side,
                    entry_date=pos.entry_date, exit_date=date,
                    entry_price=pos.entry_price, exit_price=exit_price,
                    size=pos.size, pnl_gross=gross, pnl_net=net,
                    r_multiple=r, exit_reason=exit_reason, bars_held=pos.bars_held,
                ))
                pos = None

        # Entry only if symbol is in active universe today
        active_today = True
        if is_active_at is not None:
            try: active_today = bool(is_active_at.loc[date])
            except KeyError: active_today = False

        if pos is None and not math.isnan(row["atr"]) and active_today:
            fund_8h = 0.0
            if funding_8h_last is not None:
                try: fund_8h = float(funding_8h_last.loc[date])
                except KeyError: pass
            allow_long = fund_8h <= d2.FUNDING_FILTER_MAX_BPS_8H
            allow_short = fund_8h >= -d2.FUNDING_FILTER_MAX_BPS_8H

            try: macro_ok = bool(btc_regime.loc[date])
            except KeyError: macro_ok = False
            if not macro_ok:
                allow_long = allow_short = False

            long_break = (not math.isnan(row["entry_high"]) and row["close"] > row["entry_high"])
            short_break = (not math.isnan(row["entry_low"]) and row["close"] < row["entry_low"])

            if allow_long and long_break:
                entry = row["close"]; stop = entry - dcfg.atr_stop_mult * row["atr"]
                size = bt._size_position(equity, entry, stop, row["atr"], sizing_cfg)
                if size > 0:
                    pos = bt.Position(symbol=symbol, side=+1, entry_date=date,
                                      entry_price=entry, size=size, stop=stop,
                                      initial_stop=stop, risk_dollars=size*(entry-stop),
                                      high_since_entry=entry, low_since_entry=entry)
            elif allow_short and short_break:
                entry = row["close"]; stop = entry + dcfg.atr_stop_mult * row["atr"]
                size = bt._size_position(equity, entry, stop, row["atr"], sizing_cfg)
                if size > 0:
                    pos = bt.Position(symbol=symbol, side=-1, entry_date=date,
                                      entry_price=entry, size=-size, stop=stop,
                                      initial_stop=stop, risk_dollars=size*(stop-entry),
                                      high_since_entry=entry, low_since_entry=entry)
    return trades


# ============================================================================
# Driver: run all 3 variants
# ============================================================================

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


def fmt(x, w=6, is_pct=False):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return f"{'n/a':>{w}}"
    if isinstance(x, float):
        return f"{x:>+{w}.1f}" if is_pct else f"{x:>+{w}.2f}"
    return f"{x:>{w}}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=30, help="dynamic universe size")
    ap.add_argument("--max-symbols", type=int, default=100, help="cap on total scan")
    ap.add_argument("--min-history-days", type=int, default=730)
    ap.add_argument("--min-notional", type=float, default=10_000_000)
    args = ap.parse_args()

    print("Loading universe ...")
    universe = fetch_all_perp_symbols()
    qualified = filter_by_history(universe, args.min_history_days)
    ticker = fetch_24h_ticker_all()
    vol_map = dict(zip(ticker["symbol"], ticker["quoteVolume"]))
    syms = qualified["symbol"].tolist()
    syms.sort(key=lambda s: -vol_map.get(s, 0))
    syms = syms[:args.max_symbols]
    print(f"  scanning top {len(syms)} by current 24h volume")

    # Always fetch from 2019-09-01 so backtest window matches walk_forward_v3
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
            if i % 20 == 0:
                print(f"  {i}/{len(syms)}")
        except Exception as e:
            print(f"  WARN {sym}: {e}")
    print(f"  loaded {len(data)} symbols")

    if "BTCUSDT" not in data:
        print("BTCUSDT missing — aborting.")
        return
    btc_regime = wf3.compute_btc_regime(data["BTCUSDT"])

    # rolling notional volume per symbol
    print("\nComputing rolling 30d notional volumes ...")
    volumes_df = compute_rolling_notional_volume(data, window=30)
    print(f"  shape: {volumes_df.shape}")

    # --- variant runners ---
    def run_with_filter(universe_filter_fn, label):
        """universe_filter_fn: symbol -> bool series indexed by symbol's date index."""
        all_trades = []
        for sym, df in data.items():
            is_active = universe_filter_fn(sym, df.index)
            fb = funding_by_symbol.get(sym)
            fund_carry = fb["funding_bps_in_bar"] if fb is not None else None
            fund_last = fb["funding_bps_8h_last"] if fb is not None else None
            trades = backtest_one_symbol_with_membership(
                sym, df, fund_carry, fund_last, btc_regime, is_active)
            all_trades.extend(trades)
        return all_trades

    def fixed_8_filter(sym, idx):
        return pd.Series(sym in CURRENT_8, index=idx)

    def static_top_n_filter(sym, idx):
        # symbol in current top N by 24h volume?
        top_n_now = set(
            x for x, v in sorted(vol_map.items(), key=lambda kv: -kv[1])[:args.top_n]
        ) | ALWAYS_KEEP
        return pd.Series(sym in top_n_now, index=idx)

    def dynamic_top_n_filter(sym, idx):
        # is sym in the top N at each historical date?
        result = pd.Series(False, index=idx)
        for date in idx:
            if date not in volumes_df.index:
                continue
            uni = universe_at_date(volumes_df, date, args.top_n, args.min_notional)
            if sym in uni:
                result.loc[date] = True
        return result

    print(f"\nRunning variant fixed_8 ...")
    trades_fixed = run_with_filter(fixed_8_filter, "fixed_8")
    print(f"  {len(trades_fixed)} trades")

    print(f"Running variant static_top_{args.top_n} (current rankings, no rotation) ...")
    trades_static = run_with_filter(static_top_n_filter, f"static_top_{args.top_n}")
    print(f"  {len(trades_static)} trades")

    print(f"Running variant dynamic_top_{args.top_n} (rotates by trailing 30d vol) ...")
    trades_dynamic = run_with_filter(dynamic_top_n_filter, f"dynamic_top_{args.top_n}")
    print(f"  {len(trades_dynamic)} trades")

    # --- per-fold table ---
    print("\n=== PER-FOLD TEST RESULTS ===\n")
    print(f"  {'fold':<4} {'window':<20} | "
          f"{'fixed_8':^24} | {'static_top_'+str(args.top_n):^24} | {'dynamic_top_'+str(args.top_n):^24}")
    print(f"  {'':<4} {'':<20} | "
          f"{'n':>4} {'expR':>6} {'%/yr':>7}  | "
          f"{'n':>4} {'expR':>6} {'%/yr':>7}  | "
          f"{'n':>4} {'expR':>6} {'%/yr':>7}")
    print("-" * 110)

    rows = []
    for (label, _, _, te_start, te_end) in FOLDS:
        a = fold_stats(trades_fixed, te_start, te_end)
        b = fold_stats(trades_static, te_start, te_end)
        c = fold_stats(trades_dynamic, te_start, te_end)
        ws = f"{te_start[:7]}->{te_end[:7]}"
        print(f"  {label:<4} {ws:<20} | "
              f"{a['n']:>4} {fmt(a['exp'])} {fmt(a['ann_ret'], 7, is_pct=True)}  | "
              f"{b['n']:>4} {fmt(b['exp'])} {fmt(b['ann_ret'], 7, is_pct=True)}  | "
              f"{c['n']:>4} {fmt(c['exp'])} {fmt(c['ann_ret'], 7, is_pct=True)}")
        rows.append({"fold": label, "window": ws,
                     "fixed_n": a["n"], "fixed_exp": a["exp"], "fixed_ann": a["ann_ret"],
                     "static_n": b["n"], "static_exp": b["exp"], "static_ann": b["ann_ret"],
                     "dynamic_n": c["n"], "dynamic_exp": c["exp"], "dynamic_ann": c["ann_ret"]})

    pd.DataFrame(rows).to_csv("walkforward_v6_fold_table.csv", index=False)

    # --- aggregate ---
    print("\n=== AGGREGATE (all test windows) ===\n")
    variants = [("fixed_8", trades_fixed),
                (f"static_top_{args.top_n}", trades_static),
                (f"dynamic_top_{args.top_n}", trades_dynamic)]
    for label, trades in variants:
        agg = aggregate(trades, FOLDS)
        if agg is None:
            print(f"  {label:<22}: 0 trades"); continue
        print(f"  {label:<22}  n={agg['n']:<4}  win={agg['win']:.3f}  "
              f"exp_R={agg['exp']:+.3f}  trade_sh={agg['sh']:+.3f}  "
              f"ann_ret={agg['ann_ret']:+.2f}%")
        if agg["trades"]:
            pd.DataFrame([t.__dict__ for t in agg["trades"]]).to_csv(
                f"walkforward_v6_{label}_trades.csv", index=False)

    print("\nWrote walkforward_v6_fold_table.csv and per-variant trade CSVs.")
    print("\nFinal step:")
    for label, _ in variants:
        print(f"  python bootstrap.py --file walkforward_v6_{label}_trades.csv")
    print("\nHonest caveat: survivorship bias. Symbols delisted from Binance are not")
    print("in this scan, so all variants overstate forward returns. The DIFFERENCE")
    print("between variants is still informative -- that's what we're measuring.")


if __name__ == "__main__":
    main()
