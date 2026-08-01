"""
Walk-Forward v9 — Bear-market short sleeve (macro-OFF shorts).
===============================================================

Hypothesis: the system is structurally dormant during BTC downtrends
(300+ days as of Aug 2026). Downtrends have their own trends — down.
Can we harvest them with the mirrored rules?

  BEAR SHORT entry (only when BTC macro is OFF):
    - close < rolling 55d low (breakdown)
    - funding >= -20 bps/8h (shorts not already crowded)
    - symbol in BOTTOM quintile of 30d return vs BTC (short the weakest,
      mirror of the validated buy-the-strongest RS filter)
    - never short BTCUSDT itself in this sleeve (it IS the regime signal)

  Exits: mirror of longs — 20d high channel exit, 2xATR stop, 90d time stop.

Variants:
  rs_only        : current live system (control — macro-ON only)
  bear_shorts    : ONLY the macro-off short sleeve (the real test)
  combined       : rs_only + bear_shorts

Decision rule (pre-committed):
  Ship bear_shorts iff its OOS trades alone have bootstrap 95% CI lower
  bound > 0 (starred) AND n >= 30 in test windows AND per-fold results are
  not carried by a single fold. Expectation set honestly: shorts in crypto
  fight a rising long-run tide, funding often pays shorts (helps), but
  squeezes are violent. Prior: ~30% chance this validates.

Usage:
    python walk_forward_v9.py --top-n 30 --max-symbols 100
Then:
    python bootstrap.py --file walkforward_v9_bear_shorts_trades.csv
"""

from __future__ import annotations

import argparse
import math
from dataclasses import replace
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
from rel_strength_filter import build_rs_matrix

FROZEN_N_ENTRY = 55
FROZEN_N_EXIT = 20
RISK_PER_TRADE = 0.0075
ALWAYS_KEEP = {"BTCUSDT"}


def quintile_gate(rs_matrix: pd.DataFrame, date, symbol: str,
                  universe: set, bottom: bool, frac: float = 0.20) -> bool:
    """True if symbol is in the top (bottom=False) or bottom (bottom=True)
    `frac` share of relative strength on `date` among `universe`."""
    if symbol == "BTCUSDT":
        return not bottom   # BTC passes the long gate, never the short gate
    if rs_matrix.empty:
        return False
    try:
        row = rs_matrix.loc[date]
    except KeyError:
        idx = rs_matrix.index[rs_matrix.index <= date]
        if len(idx) == 0:
            return False
        row = rs_matrix.loc[idx[-1]]
    row = row.dropna()
    row = row[[c for c in row.index if c in universe and c != "BTCUSDT"]]
    if len(row) < 3 or symbol not in row.index:
        return False
    if bottom:
        thr = row.quantile(frac)
        return float(row[symbol]) <= float(thr)
    thr = row.quantile(1 - frac)
    return float(row[symbol]) >= float(thr)


def backtest_one_symbol_v9(
    symbol: str,
    df: pd.DataFrame,
    funding_carry: Optional[pd.Series],
    funding_8h_last: Optional[pd.Series],
    btc_regime: pd.Series,
    is_active_at: Optional[pd.Series],
    rs_matrix: pd.DataFrame,
    universe: set,
    mode: str,          # "macro_on" | "bear_shorts" | "combined"
) -> list[bt.Trade]:
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

            try: macro_ok = bool(btc_regime.loc[date])
            except KeyError: macro_ok = False

            long_break = (not math.isnan(row["entry_high"]) and row["close"] > row["entry_high"])
            short_break = (not math.isnan(row["entry_low"]) and row["close"] < row["entry_low"])

            allow_long = allow_short = False

            if mode in ("macro_on", "combined") and macro_ok:
                # live system rules: macro ON, funding gate, TOP-quintile RS
                if long_break and fund_8h <= d2.FUNDING_FILTER_MAX_BPS_8H:
                    allow_long = quintile_gate(rs_matrix, date, symbol,
                                               universe, bottom=False)
                if short_break and fund_8h >= -d2.FUNDING_FILTER_MAX_BPS_8H:
                    allow_short = quintile_gate(rs_matrix, date, symbol,
                                                universe, bottom=False)

            if mode in ("bear_shorts", "combined") and not macro_ok:
                # NEW sleeve: macro OFF, short breakdowns in the WEAKEST names
                if (short_break and symbol != "BTCUSDT"
                        and fund_8h >= -d2.FUNDING_FILTER_MAX_BPS_8H):
                    allow_short = quintile_gate(rs_matrix, date, symbol,
                                                universe, bottom=True)

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
    args = ap.parse_args()

    print("Loading universe ...")
    universe = fetch_all_perp_symbols()
    qualified = filter_by_history(universe, args.min_history_days)
    ticker = fetch_24h_ticker_all()
    vol_map = dict(zip(ticker["symbol"], ticker["quoteVolume"]))
    syms = qualified["symbol"].tolist()
    syms.sort(key=lambda s: -vol_map.get(s, 0))
    syms = syms[:args.max_symbols]

    end_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    start_ms = int(pd.Timestamp("2019-09-01", tz="UTC").timestamp() * 1000)

    print("Loading OHLCV (cached) ...")
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
            if i % 25 == 0:
                print(f"  {i}/{len(syms)}")
        except Exception as e:
            print(f"  WARN {sym}: {e}")
    print(f"  loaded {len(data)} symbols")

    if "BTCUSDT" not in data:
        print("BTCUSDT missing — aborting."); return
    btc_regime = wf3.compute_btc_regime(data["BTCUSDT"])
    off_days = int((~btc_regime).sum())
    print(f"  BTC macro OFF on {off_days}/{len(btc_regime)} days "
          f"({off_days/len(btc_regime):.0%} of history — the sleeve's habitat)")

    top_n_now = set(
        x for x, v in sorted(vol_map.items(), key=lambda kv: -kv[1])[:args.top_n]
    ) | ALWAYS_KEEP

    def static_filter(sym, idx):
        return pd.Series(sym in top_n_now, index=idx)

    print(f"Building RS matrix (lookback={args.rs_lookback}) ...")
    rs_matrix = build_rs_matrix(data, lookback=args.rs_lookback)

    def run_variant(mode):
        all_trades = []
        for sym, df in data.items():
            fb = funding_by_symbol.get(sym)
            fc = fb["funding_bps_in_bar"] if fb is not None else None
            fl = fb["funding_bps_8h_last"] if fb is not None else None
            all_trades.extend(backtest_one_symbol_v9(
                sym, df, fc, fl, btc_regime, static_filter(sym, df.index),
                rs_matrix, top_n_now, mode))
        return all_trades

    variants = []
    for mode in ("macro_on", "bear_shorts", "combined"):
        print(f"Running variant {mode} ...")
        tr = run_variant(mode)
        print(f"  {len(tr)} trades")
        variants.append((mode, tr))

    print("\n=== PER-FOLD TEST RESULTS ===\n")
    header = f"  {'fold':<4} {'window':<18} | " + " | ".join(
        f"{lbl:^24}" for lbl, _ in variants)
    print(header)
    print(f"  {'':<4} {'':<18} | " + " | ".join(
        f"{'n':>4} {'expR':>7} {'%/yr':>8}" for _ in variants))
    print("-" * len(header))
    rows = []
    for (label, _, _, te_s, te_e) in FOLDS:
        line = f"  {label:<4} {te_s[:7]}->{te_e[:7]} | "
        row = {"fold": label}
        for vl, tr in variants:
            s = wf6.fold_stats(tr, te_s, te_e)
            line += f"{s['n']:>4} {wf6.fmt(s['exp']):>7} {wf6.fmt(s['ann_ret'], 8, is_pct=True):>8} | "
            row[f"{vl}_n"], row[f"{vl}_exp"], row[f"{vl}_ann"] = s["n"], s["exp"], s["ann_ret"]
        print(line.rstrip(" |"))
        rows.append(row)
    pd.DataFrame(rows).to_csv("walkforward_v9_fold_table.csv", index=False)

    print("\n=== AGGREGATE (all test windows) ===\n")
    for vl, tr in variants:
        agg = wf6.aggregate(tr, FOLDS)
        if agg is None:
            print(f"  {vl:<12}: 0 trades in test windows"); continue
        print(f"  {vl:<12}  n={agg['n']:<5} win={agg['win']:.3f}  "
              f"exp_R={agg['exp']:+.3f}  sh={agg['sh']:+.3f}  "
              f"ann_ret={agg['ann_ret']:+.2f}%")
        if agg["trades"]:
            pd.DataFrame([t.__dict__ for t in agg["trades"]]).to_csv(
                f"walkforward_v9_{vl}_trades.csv", index=False)

    print("\nBootstrap the sleeve on its own:")
    print("  python bootstrap.py --file walkforward_v9_bear_shorts_trades.csv")
    print("\nDECISION RULE: ship bear_shorts iff its OOS bootstrap LB > 0 "
          "(starred) AND n >= 30 AND no single fold carries the result.")


if __name__ == "__main__":
    main()
