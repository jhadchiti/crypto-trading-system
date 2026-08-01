"""
Walk-Forward v8 — Multi-timeframe confirmation on top of rs_only.
==================================================================

Extends v7's rs_only (currently live) by adding a weekly-timeframe gate:

  rs_only         : v7 currently-shipped baseline (Donchian + rs_top_quintile)
  rs_mtf_confirm  : rs_only + weekly Donchian confirmation
                    (only take a daily breakout if the last completed weekly
                     bar closed above the weekly N-week high)

The weekly gate is computed from the LAST COMPLETED weekly bar to avoid
any within-week lookahead. Weekly Donchian entry window defaults to 20
weeks (~5 months of price context).

Decision rule (pre-committed):
  Ship rs_mtf_confirm only if BOTH:
    (a) OOS aggregate ann_ret > rs_only's, AND
    (b) bootstrap 95% CI lower bound on R > 0 (starred), AND
    (c) OOS trade Sharpe > rs_only's.

If (a) or (c) is close (within 5%) but (b) holds, we still hold shipping
and inspect per-fold consistency — MTF gates often trade absolute return
for consistency, and we care about the tradeoff explicitly.

Usage:
    python walk_forward_v8.py
    python walk_forward_v8.py --weekly-n 20 --top-n 30

Then:
    python bootstrap.py --file walkforward_v8_rs_only_trades.csv
    python bootstrap.py --file walkforward_v8_rs_mtf_confirm_trades.csv
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
from rel_strength_filter import build_rs_matrix, top_quintile_gate
from mtf_confirm import build_mtf_series, DEFAULT_WEEKLY_ENTRY_N


FROZEN_N_ENTRY = 55
FROZEN_N_EXIT = 20
RISK_PER_TRADE = 0.0075
ALWAYS_KEEP = {"BTCUSDT"}


def backtest_one_symbol_v8(
    symbol: str,
    df: pd.DataFrame,
    funding_carry: Optional[pd.Series],
    funding_8h_last: Optional[pd.Series],
    btc_regime: pd.Series,
    is_active_at: Optional[pd.Series],
    rs_gate_fn,                       # required (date, sym) -> bool
    mtf_long_ok: Optional[pd.Series] = None,
    mtf_short_ok: Optional[pd.Series] = None,
) -> list[bt.Trade]:
    """Extended v7 backtest with an optional MTF confirmation gate."""
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

            # Rel-strength gate (matches live system)
            if (long_break or short_break) and not rs_gate_fn(date, symbol):
                allow_long = allow_short = False

            # MTF confirmation gate (NEW)
            if long_break and mtf_long_ok is not None:
                try:
                    if not bool(mtf_long_ok.loc[date]):
                        allow_long = False
                except KeyError:
                    allow_long = False

            if short_break and mtf_short_ok is not None:
                try:
                    if not bool(mtf_short_ok.loc[date]):
                        allow_short = False
                except KeyError:
                    allow_short = False

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=30)
    ap.add_argument("--max-symbols", type=int, default=100)
    ap.add_argument("--min-history-days", type=int, default=730)
    ap.add_argument("--rs-lookback", type=int, default=30)
    ap.add_argument("--rs-top-fraction", type=float, default=0.20)
    ap.add_argument("--weekly-n", type=int, default=DEFAULT_WEEKLY_ENTRY_N,
                    help="weeks in weekly Donchian entry channel")
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
        print("BTCUSDT missing — aborting."); return

    btc_regime = wf3.compute_btc_regime(data["BTCUSDT"])

    top_n_now = set(
        x for x, v in sorted(vol_map.items(), key=lambda kv: -kv[1])[:args.top_n]
    ) | ALWAYS_KEEP

    def static_top_n_filter(sym, idx):
        return pd.Series(sym in top_n_now, index=idx)

    print(f"\nBuilding relative-strength matrix (lookback={args.rs_lookback}) ...")
    rs_matrix = build_rs_matrix(data, lookback=args.rs_lookback)

    def rs_gate(date, symbol):
        return top_quintile_gate(rs_matrix, date, symbol,
                                 active_universe=top_n_now,
                                 top_fraction=args.rs_top_fraction,
                                 exclude_btc=True)

    print(f"Building MTF confirmation series (weekly-n={args.weekly_n}) ...")
    mtf_by_sym = {}
    for sym, df in data.items():
        long_ok, short_ok = build_mtf_series(df, n_entry=args.weekly_n)
        mtf_by_sym[sym] = (long_ok, short_ok)

    def run_variant(label, mtf_on: bool):
        all_trades = []
        for sym, df in data.items():
            is_active = static_top_n_filter(sym, df.index)
            fb = funding_by_symbol.get(sym)
            fund_carry = fb["funding_bps_in_bar"] if fb is not None else None
            fund_last = fb["funding_bps_8h_last"] if fb is not None else None

            mtf_long = mtf_by_sym[sym][0] if mtf_on else None
            mtf_short = mtf_by_sym[sym][1] if mtf_on else None

            trades = backtest_one_symbol_v8(
                sym, df, fund_carry, fund_last, btc_regime, is_active,
                rs_gate_fn=rs_gate,
                mtf_long_ok=mtf_long,
                mtf_short_ok=mtf_short,
            )
            all_trades.extend(trades)
        return all_trades

    print("\nRunning variant rs_only (current live equivalent) ...")
    trades_rs = run_variant("rs_only", mtf_on=False)
    print(f"  {len(trades_rs)} trades")

    print("Running variant rs_mtf_confirm ...")
    trades_mtf = run_variant("rs_mtf_confirm", mtf_on=True)
    print(f"  {len(trades_mtf)} trades")

    variants = [
        ("rs_only",         trades_rs),
        ("rs_mtf_confirm",  trades_mtf),
    ]

    print("\n=== PER-FOLD TEST RESULTS ===\n")
    header = f"  {'fold':<4} {'window':<20} | "
    for label, _ in variants:
        header += f"{label:^24} | "
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
            s = wf6.fold_stats(trades, te_start, te_end)
            line += f"{s['n']:>4} {wf6.fmt(s['exp']):>7} {wf6.fmt(s['ann_ret'], 8, is_pct=True):>8} | "
            row[f"{vlabel}_n"] = s["n"]
            row[f"{vlabel}_exp"] = s["exp"]
            row[f"{vlabel}_ann"] = s["ann_ret"]
        print(line.rstrip(" |"))
        rows.append(row)

    pd.DataFrame(rows).to_csv("walkforward_v8_fold_table.csv", index=False)

    print("\n=== AGGREGATE (all test windows) ===\n")
    for vlabel, trades in variants:
        agg = wf6.aggregate(trades, FOLDS)
        if agg is None:
            print(f"  {vlabel:<20}: 0 trades"); continue
        print(f"  {vlabel:<20}  n={agg['n']:<5}  win={agg['win']:.3f}  "
              f"exp_R={agg['exp']:+.3f}  trade_sh={agg['sh']:+.3f}  "
              f"ann_ret={agg['ann_ret']:+.2f}%")
        if agg["trades"]:
            pd.DataFrame([t.__dict__ for t in agg["trades"]]).to_csv(
                f"walkforward_v8_{vlabel}_trades.csv", index=False)

    print("\nWrote walkforward_v8_fold_table.csv and per-variant trade CSVs.")
    print("\nNext: bootstrap each variant to check starred/unstarred CI lower bound.\n")
    for vlabel, _ in variants:
        print(f"  python bootstrap.py --file walkforward_v8_{vlabel}_trades.csv")

    print("\nDECISION RULE:")
    print("  Ship rs_mtf_confirm iff:")
    print("    (a) its aggregate OOS ann_ret > rs_only's, AND")
    print("    (b) bootstrap 95% CI lower bound on R > 0 (starred), AND")
    print("    (c) its OOS trade Sharpe > rs_only's.")
    print("\n  If (a) or (c) is CLOSE (<5% off) but (b) holds, DO NOT auto-ship —")
    print("  paste the outputs and we'll decide based on per-fold consistency.")


if __name__ == "__main__":
    main()
