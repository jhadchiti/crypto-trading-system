"""
Walk-Forward v7 — Rel-strength + vol-scaled sizing + early-listing bias.
========================================================================

Tests three improvements on top of the v6 winner (static_top_30 universe +
Donchian 55/20 + BTC macro filter + funding filter):

  baseline          : v6 static_top_30 (the current live strategy)
  rs_only           : baseline + BTC-relative-strength top-quintile gate
  rs_vol_sized      : rs_only + volatility-scaled sizing
  rs_vol_early      : rs_vol_sized + early-listing weight boost on universe

Each variant runs through the same walk-forward folds. The output CSV +
per-variant trade CSVs let you bootstrap each and decide which to ship.

Decision rule (pre-committed):
  Ship a variant only if BOTH:
    (a) OOS annualized return > baseline's OOS annualized return, AND
    (b) Bootstrap 95% CI lower bound on trade R > 0 (starred).

Usage:
    python walk_forward_v7.py                          # default: top_n=30
    python walk_forward_v7.py --top-n 30 --max-symbols 100

Then:
    python bootstrap.py --file walkforward_v7_baseline_trades.csv
    python bootstrap.py --file walkforward_v7_rs_only_trades.csv
    python bootstrap.py --file walkforward_v7_rs_vol_sized_trades.csv
    python bootstrap.py --file walkforward_v7_rs_vol_early_trades.csv

Compare aggregate OOS ann_ret + starred/unstarred lower-bound R.
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
import walk_forward_v6 as wf6
from funding import fetch_funding, align_funding_to_bars
from walk_forward import FOLDS, filter_trades_by_entry, trade_sharpe
from market_data import (
    fetch_all_perp_symbols, filter_by_history, fetch_24h_ticker_all,
)
from rel_strength_filter import (
    build_rs_matrix,
    top_quintile_gate,
    volatility_scalar,
    compute_realized_vol,
    listing_age_days,
    early_listing_boost,
    DEFAULT_LOOKBACK,
    DEFAULT_TOP_FRACTION,
)


FROZEN_N_ENTRY = 55
FROZEN_N_EXIT = 20
RISK_PER_TRADE = 0.0075
ALWAYS_KEEP = {"BTCUSDT"}


# ============================================================================
# Core backtest with pluggable entry gating + sizing
# ============================================================================

def backtest_one_symbol_v7(
    symbol: str,
    df: pd.DataFrame,
    funding_carry: Optional[pd.Series],
    funding_8h_last: Optional[pd.Series],
    btc_regime: pd.Series,
    is_active_at: Optional[pd.Series],
    # NEW knobs
    rs_gate_fn=None,           # (date, symbol) -> bool. None = pass all
    use_vol_sizing: bool = False,
    realized_vol_series: Optional[pd.Series] = None,
    listing_age_boost_fn=None,  # (date) -> float. None = 1.0
) -> list[bt.Trade]:
    """
    Extended v6 backtest with optional rel-strength gate, vol-scaled sizing,
    and listing-age boost.
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

        # ---- Entry gating ----
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

            # Rel-strength gate: only applies when signal actually fires
            if (long_break or short_break) and rs_gate_fn is not None:
                if not rs_gate_fn(date, symbol):
                    allow_long = allow_short = False

            # Compute per-entry risk multiplier
            risk_mult = 1.0
            if use_vol_sizing and realized_vol_series is not None:
                try:
                    rv = float(realized_vol_series.loc[date])
                    if not math.isnan(rv):
                        risk_mult *= volatility_scalar(rv, target_vol=0.60,
                                                       floor=0.5, cap=1.5)
                except KeyError:
                    pass

            if listing_age_boost_fn is not None:
                risk_mult *= listing_age_boost_fn(date)

            # Apply risk multiplier via a temporary sizing_cfg copy
            local_sizing_cfg = bt.Config(
                risk_per_trade=dcfg.risk_per_trade * risk_mult,
                vol_target_annual=dcfg.vol_target_annual,
            )

            if allow_long and long_break:
                entry = row["close"]; stop = entry - dcfg.atr_stop_mult * row["atr"]
                size = bt._size_position(equity, entry, stop, row["atr"], local_sizing_cfg)
                if size > 0:
                    pos = bt.Position(symbol=symbol, side=+1, entry_date=date,
                                      entry_price=entry, size=size, stop=stop,
                                      initial_stop=stop, risk_dollars=size*(entry-stop),
                                      high_since_entry=entry, low_since_entry=entry)
            elif allow_short and short_break:
                entry = row["close"]; stop = entry + dcfg.atr_stop_mult * row["atr"]
                size = bt._size_position(equity, entry, stop, row["atr"], local_sizing_cfg)
                if size > 0:
                    pos = bt.Position(symbol=symbol, side=-1, entry_date=date,
                                      entry_price=entry, size=-size, stop=stop,
                                      initial_stop=stop, risk_dollars=size*(stop-entry),
                                      high_since_entry=entry, low_since_entry=entry)
    return trades


# ============================================================================
# Reporting helpers (reused from v6)
# ============================================================================

def fold_stats(trades, te_start, te_end):
    return wf6.fold_stats(trades, te_start, te_end)


def aggregate(trades, fold_windows):
    return wf6.aggregate(trades, fold_windows)


def fmt(x, w=6, is_pct=False):
    return wf6.fmt(x, w, is_pct)


# ============================================================================
# Main
# ============================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=30)
    ap.add_argument("--max-symbols", type=int, default=100)
    ap.add_argument("--min-history-days", type=int, default=730)
    ap.add_argument("--rs-lookback", type=int, default=DEFAULT_LOOKBACK)
    ap.add_argument("--rs-top-fraction", type=float, default=DEFAULT_TOP_FRACTION)
    ap.add_argument("--vol-target", type=float, default=0.60)
    ap.add_argument("--early-listing-cutoff-days", type=int, default=730)
    ap.add_argument("--early-listing-max-boost", type=float, default=1.25)
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

    end_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    start_ms = int(pd.Timestamp("2019-09-01", tz="UTC").timestamp() * 1000)

    print("\nLoading OHLCV (cached) ...")
    data = {}
    funding_by_symbol = {}
    for i, sym in enumerate(syms, 1):
        try:
            df = wf6.fetch_ohlcv_cached(sym, start_ms, end_ms)
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

    # Determine static top-N universe (matches v6 winner)
    top_n_now = set(
        x for x, v in sorted(vol_map.items(), key=lambda kv: -kv[1])[:args.top_n]
    ) | ALWAYS_KEEP

    def static_top_n_filter(sym, idx):
        return pd.Series(sym in top_n_now, index=idx)

    # ---- Build rel-strength matrix once ----
    print(f"\nBuilding relative-strength matrix (lookback={args.rs_lookback}) ...")
    rs_matrix = build_rs_matrix(data, lookback=args.rs_lookback)
    print(f"  RS matrix shape: {rs_matrix.shape}")

    # Cached RS gate closure — recomputes per-date top-quintile membership
    # among the STATIC top-N universe.
    top_n_universe_set = top_n_now
    def rs_gate(date, symbol):
        return top_quintile_gate(rs_matrix, date, symbol,
                                 active_universe=top_n_universe_set,
                                 top_fraction=args.rs_top_fraction,
                                 exclude_btc=True)

    # ---- Realized-vol series per symbol (for vol-sized variant) ----
    print("Computing realized volatility per symbol ...")
    realized_vol_by_sym = {}
    for sym, df in data.items():
        realized_vol_by_sym[sym] = compute_realized_vol(df["close"], window=20)

    # ---- Listing-age boost fn per symbol ----
    def make_listing_boost_fn(sym_df):
        def _fn(date):
            age = listing_age_days(sym_df, date)
            return early_listing_boost(age,
                                       cutoff_days=args.early_listing_cutoff_days,
                                       max_boost=args.early_listing_max_boost)
        return _fn

    # ---- Variant runners ----
    def run_variant(label, rs_on=False, vol_on=False, early_on=False):
        all_trades = []
        for sym, df in data.items():
            is_active = static_top_n_filter(sym, df.index)
            fb = funding_by_symbol.get(sym)
            fund_carry = fb["funding_bps_in_bar"] if fb is not None else None
            fund_last = fb["funding_bps_8h_last"] if fb is not None else None

            gate = rs_gate if rs_on else None
            rv_series = realized_vol_by_sym.get(sym) if vol_on else None
            boost_fn = make_listing_boost_fn(df) if early_on else None

            trades = backtest_one_symbol_v7(
                sym, df, fund_carry, fund_last, btc_regime, is_active,
                rs_gate_fn=gate,
                use_vol_sizing=vol_on,
                realized_vol_series=rv_series,
                listing_age_boost_fn=boost_fn,
            )
            all_trades.extend(trades)
        return all_trades

    print(f"\nRunning variant baseline (v6 static_top_{args.top_n} equivalent) ...")
    trades_baseline = run_variant("baseline")
    print(f"  {len(trades_baseline)} trades")

    print(f"Running variant rs_only ...")
    trades_rs = run_variant("rs_only", rs_on=True)
    print(f"  {len(trades_rs)} trades")

    print(f"Running variant rs_vol_sized ...")
    trades_rsv = run_variant("rs_vol_sized", rs_on=True, vol_on=True)
    print(f"  {len(trades_rsv)} trades")

    print(f"Running variant rs_vol_early ...")
    trades_rsve = run_variant("rs_vol_early", rs_on=True, vol_on=True, early_on=True)
    print(f"  {len(trades_rsve)} trades")

    variants = [
        ("baseline",     trades_baseline),
        ("rs_only",      trades_rs),
        ("rs_vol_sized", trades_rsv),
        ("rs_vol_early", trades_rsve),
    ]

    # ---- Per-fold table ----
    print("\n=== PER-FOLD TEST RESULTS (annualized % return) ===\n")
    header = f"  {'fold':<4} {'window':<20} | "
    for label, _ in variants:
        header += f"{label:^22} | "
    print(header.rstrip(" |"))

    subheader = f"  {'':<4} {'':<20} | "
    for _ in variants:
        subheader += f"{'n':>4} {'expR':>7} {'%/yr':>8} | "
    print(subheader.rstrip(" |"))
    print("-" * len(header))

    rows = []
    for (label, _, _, te_start, te_end) in FOLDS:
        line = f"  {label:<4} {te_start[:7]}->{te_end[:7]:<7} | "
        row = {"fold": label, "window": f"{te_start[:7]}->{te_end[:7]}"}
        for vlabel, trades in variants:
            s = fold_stats(trades, te_start, te_end)
            line += f"{s['n']:>4} {fmt(s['exp']):>7} {fmt(s['ann_ret'], 8, is_pct=True):>8} | "
            row[f"{vlabel}_n"] = s["n"]
            row[f"{vlabel}_exp"] = s["exp"]
            row[f"{vlabel}_ann"] = s["ann_ret"]
        print(line.rstrip(" |"))
        rows.append(row)

    pd.DataFrame(rows).to_csv("walkforward_v7_fold_table.csv", index=False)

    # ---- Aggregate ----
    print("\n=== AGGREGATE (all test windows) ===\n")
    for vlabel, trades in variants:
        agg = aggregate(trades, FOLDS)
        if agg is None:
            print(f"  {vlabel:<15}: 0 trades"); continue
        print(f"  {vlabel:<15}  n={agg['n']:<5}  win={agg['win']:.3f}  "
              f"exp_R={agg['exp']:+.3f}  trade_sh={agg['sh']:+.3f}  "
              f"ann_ret={agg['ann_ret']:+.2f}%")
        if agg["trades"]:
            pd.DataFrame([t.__dict__ for t in agg["trades"]]).to_csv(
                f"walkforward_v7_{vlabel}_trades.csv", index=False)

    print("\nWrote walkforward_v7_fold_table.csv and per-variant trade CSVs.")
    print("\nNext: bootstrap each variant to check starred/unstarred CI lower bound.\n")
    for vlabel, _ in variants:
        print(f"  python bootstrap.py --file walkforward_v7_{vlabel}_trades.csv")

    print("\nDECISION RULE:")
    print("  Ship a variant iff:")
    print("    (a) its aggregate OOS ann_ret > baseline's, AND")
    print("    (b) bootstrap 95% CI lower bound on R > 0 (starred).")
    print("\nIf multiple variants meet both, pick the SIMPLEST one that does.")
    print("Do NOT combine improvements from different variants without re-testing.")


if __name__ == "__main__":
    main()
