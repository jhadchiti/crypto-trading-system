"""
Walk-Forward v4 — adds sentiment-aware filters on top of v3 baseline.
======================================================================

Tests four variants on the same data, same folds, same frozen params (55/20):

  baseline     : BTC macro + funding filter           (= v3 btc_only)
  +fng         : baseline + Fear & Greed extreme circuit breaker
  +btcrel      : baseline + BTC-relative rotation filter (alts only)
  +both        : baseline + FNG + BTC-relative

Per-fold table and aggregate include win rate and annualized return %.
Outputs bootstrap-ready CSVs for each variant.

Usage:
    python walk_forward_v4.py
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
import walk_forward_v2 as wf2
import walk_forward_v3 as wf3
from funding import fetch_funding, align_funding_to_bars
from walk_forward import FOLDS, filter_trades_by_entry, trade_sharpe
from sentiment_filters import (
    fetch_fear_greed_history, align_fng_to_bars, btc_relative_return,
    fng_blocks_entry, btc_rel_blocks_entry,
    FNG_GREED_THRESHOLD, FNG_FEAR_THRESHOLD, BTC_REL_LOOKBACK, BTC_REL_THRESHOLD,
)


V2_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
              "AVAXUSDT", "LINKUSDT", "DOGEUSDT", "XRPUSDT")

FROZEN_N_ENTRY = 55
FROZEN_N_EXIT = 20
RISK_PER_TRADE = 0.0075


# ============================================================================
# Backtest with all gates (extends wf3's logic with sentiment filters)
# ============================================================================

def backtest_v4(df: pd.DataFrame, symbol: str, equity: float,
                dcfg: dc.DonchianConfig,
                funding_bps_by_bar: Optional[pd.Series],
                funding_bps_8h_last: Optional[pd.Series],
                btc_regime: Optional[pd.Series],
                fng_series: Optional[pd.Series],
                btc_rel_series: Optional[pd.Series],
                use_fng: bool,
                use_btc_rel: bool,
                ) -> tuple[list[bt.Trade], dict]:
    """
    Same shell as wf3.backtest_with_gates but with two more filters bolted on
    at entry time. Baseline (use_fng=False, use_btc_rel=False) reproduces v3
    btc_only behavior.

    Returns (trades, counters) where counters tracks how many entries were
    blocked by each filter.
    """
    trades: list[bt.Trade] = []
    pos: Optional[bt.Position] = None
    rt_cost_bps = 2 * (dcfg.taker_fee_bps + dcfg.slippage_bps)
    counters = {"fng_blocked": 0, "btc_rel_blocked": 0, "fund_blocked": 0,
                "btc_macro_blocked": 0}

    sizing_cfg = bt.Config(
        risk_per_trade=dcfg.risk_per_trade,
        vol_target_annual=dcfg.vol_target_annual,
    )

    for i, (date, row) in enumerate(df.iterrows()):
        # ---- MTM existing position (identical to wf3) ----
        if pos is not None:
            pos.bars_held += 1
            if funding_bps_by_bar is not None:
                try:
                    bar_bps = float(funding_bps_by_bar.loc[date])
                except KeyError:
                    bar_bps = 0.0
            else:
                bar_bps = dcfg.funding_bps_per_day
            funding_drag = (bar_bps / 10000.0) * abs(pos.size) * row["close"]
            equity -= funding_drag if pos.side > 0 else -funding_drag

            exit_reason = None
            exit_price = None

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
                r_mult = net / pos.risk_dollars if pos.risk_dollars > 0 else 0.0
                trades.append(bt.Trade(
                    symbol=symbol, side=pos.side,
                    entry_date=pos.entry_date, exit_date=date,
                    entry_price=pos.entry_price, exit_price=exit_price,
                    size=pos.size, pnl_gross=gross, pnl_net=net,
                    r_multiple=r_mult, exit_reason=exit_reason,
                    bars_held=pos.bars_held,
                ))
                pos = None

        # ---- entries ----
        if pos is None and not math.isnan(row["atr"]):
            # funding gate
            fund_8h = 0.0
            if funding_bps_8h_last is not None:
                try:
                    fund_8h = float(funding_bps_8h_last.loc[date])
                except KeyError:
                    fund_8h = 0.0
            allow_long = fund_8h <= d2.FUNDING_FILTER_MAX_BPS_8H
            allow_short = fund_8h >= -d2.FUNDING_FILTER_MAX_BPS_8H

            is_long_break = (not math.isnan(row["entry_high"])
                             and row["close"] > row["entry_high"])
            is_short_break = (not math.isnan(row["entry_low"])
                              and row["close"] < row["entry_low"])

            if (is_long_break and not allow_long) or (is_short_break and not allow_short):
                counters["fund_blocked"] += 1

            # BTC macro gate
            if btc_regime is not None:
                try:
                    macro_ok = bool(btc_regime.loc[date])
                except KeyError:
                    macro_ok = False
                if not macro_ok:
                    if is_long_break or is_short_break:
                        counters["btc_macro_blocked"] += 1
                    allow_long = False
                    allow_short = False

            # FNG circuit breaker
            if use_fng and fng_series is not None and (allow_long or allow_short):
                try:
                    fng_val = float(fng_series.loc[date])
                except KeyError:
                    fng_val = float("nan")
                if is_long_break and fng_blocks_entry(fng_val, True):
                    counters["fng_blocked"] += 1
                    allow_long = False
                if is_short_break and fng_blocks_entry(fng_val, False):
                    counters["fng_blocked"] += 1
                    allow_short = False

            # BTC-relative rotation filter (alts only)
            if use_btc_rel and btc_rel_series is not None and (allow_long or allow_short):
                try:
                    rel_val = float(btc_rel_series.loc[date])
                except KeyError:
                    rel_val = float("nan")
                if is_long_break and btc_rel_blocks_entry(rel_val, True, symbol):
                    counters["btc_rel_blocked"] += 1
                    allow_long = False
                if is_short_break and btc_rel_blocks_entry(rel_val, False, symbol):
                    counters["btc_rel_blocked"] += 1
                    allow_short = False

            # ---- place order ----
            if (allow_long and is_long_break):
                entry = row["close"]; stop = entry - dcfg.atr_stop_mult * row["atr"]
                size = bt._size_position(equity, entry, stop, row["atr"], sizing_cfg)
                if size > 0:
                    pos = bt.Position(symbol=symbol, side=+1,
                                      entry_date=date, entry_price=entry,
                                      size=size, stop=stop, initial_stop=stop,
                                      risk_dollars=size * (entry - stop),
                                      high_since_entry=entry, low_since_entry=entry)
            elif (allow_short and is_short_break):
                entry = row["close"]; stop = entry + dcfg.atr_stop_mult * row["atr"]
                size = bt._size_position(equity, entry, stop, row["atr"], sizing_cfg)
                if size > 0:
                    pos = bt.Position(symbol=symbol, side=-1,
                                      entry_date=date, entry_price=entry,
                                      size=-size, stop=stop, initial_stop=stop,
                                      risk_dollars=size * (stop - entry),
                                      high_since_entry=entry, low_since_entry=entry)

    return trades, counters


# ============================================================================
# Driver
# ============================================================================

def run_full(data, funding_by_symbol, btc_regime, fng_by_symbol,
             btc_rel_by_symbol, dcfg,
             use_fng: bool, use_btc_rel: bool) -> tuple[list, dict]:
    per_symbol_equity = 100_000.0 / len(data)
    all_trades = []
    total_counters = {"fng_blocked": 0, "btc_rel_blocked": 0,
                      "fund_blocked": 0, "btc_macro_blocked": 0}
    for sym, df in data.items():
        d = dc.build_donchian(df, dcfg)
        fbb = funding_by_symbol.get(sym)
        fund_carry = fbb["funding_bps_in_bar"] if fbb is not None else None
        fund_last = fbb["funding_bps_8h_last"] if fbb is not None else None
        fng = fng_by_symbol.get(sym)
        rel = btc_rel_by_symbol.get(sym)
        trades, counters = backtest_v4(
            d, sym, per_symbol_equity, dcfg,
            funding_bps_by_bar=fund_carry,
            funding_bps_8h_last=fund_last,
            btc_regime=btc_regime,
            fng_series=fng,
            btc_rel_series=rel,
            use_fng=use_fng,
            use_btc_rel=use_btc_rel,
        )
        all_trades.extend(trades)
        for k in total_counters:
            total_counters[k] += counters[k]
    return all_trades, total_counters


def fold_stats(trades, te_start: str, te_end: str) -> dict:
    tr = filter_trades_by_entry(trades, te_start, te_end)
    s = pd.Timestamp(te_start, tz="UTC")
    e = pd.Timestamp(te_end, tz="UTC")
    years = max((e - s).days / 365.25, 1e-9)
    if not tr:
        return {"n": 0, "win": float("nan"), "exp": float("nan"),
                "sh": float("nan"), "ann_ret": float("nan")}
    rs = np.array([t.r_multiple for t in tr])
    total_R = float(rs.sum())
    return {
        "n": len(tr),
        "win": float((rs > 0).mean()),
        "exp": float(rs.mean()),
        "sh":  trade_sharpe(rs),
        "ann_ret": (total_R / years) * RISK_PER_TRADE * 100.0,
    }


def aggregate_stats(trades, fold_windows):
    rs_list = []
    total_years = 0.0
    collected = []
    for (_, _, _, te_start, te_end) in fold_windows:
        tr = filter_trades_by_entry(trades, te_start, te_end)
        collected.extend(tr)
        rs_list.extend([t.r_multiple for t in tr])
        s = pd.Timestamp(te_start, tz="UTC")
        e = pd.Timestamp(te_end, tz="UTC")
        total_years += max((e - s).days / 365.25, 0.0)
    rs = np.array(rs_list)
    if len(rs) == 0 or total_years <= 0:
        return None
    return {
        "n": len(rs),
        "win": float((rs > 0).mean()),
        "exp": float(rs.mean()),
        "sh":  trade_sharpe(rs),
        "ann_ret": (float(rs.sum()) / total_years) * RISK_PER_TRADE * 100.0,
        "trades": collected,
    }


def fmt_cell(x, w=7, is_pct=False):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return f"{'n/a':>{w}}"
    if isinstance(x, float):
        if is_pct:
            return f"{x:>+{w}.1f}"
        return f"{x:>+{w}.2f}"
    return f"{x:>{w}}"


def main():
    cfg = replace(bt.CFG, symbols=V2_SYMBOLS)
    print(f"Loading Daily OHLCV for {len(V2_SYMBOLS)} symbols ...")
    data = bt.load_universe(cfg)
    if not data or "BTCUSDT" not in data:
        print("Data load failed.")
        return

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

    print("\nLoading Fear & Greed history ...")
    try:
        fng_df = fetch_fear_greed_history()
        print(f"  {len(fng_df)} daily FNG values, "
              f"range {fng_df.index.min().date()} to {fng_df.index.max().date()}")
    except Exception as e:
        print(f"  WARN: FNG fetch failed: {e}")
        fng_df = pd.DataFrame()

    # Align FNG per symbol (each symbol has its own daily index)
    fng_by_symbol = {}
    for sym, df in data.items():
        fng_by_symbol[sym] = align_fng_to_bars(fng_df, df.index) if not fng_df.empty else None

    # BTC-relative per alt (skip for BTC itself)
    btc_df = data["BTCUSDT"]
    btc_rel_by_symbol = {}
    for sym, df in data.items():
        if sym == "BTCUSDT":
            btc_rel_by_symbol[sym] = None
        else:
            btc_rel_by_symbol[sym] = btc_relative_return(df, btc_df)

    # Report filter coverage
    if not fng_df.empty:
        fng_btc = fng_by_symbol["BTCUSDT"]
        extreme_greed_pct = float((fng_btc > FNG_GREED_THRESHOLD).mean()) * 100.0
        extreme_fear_pct = float((fng_btc < FNG_FEAR_THRESHOLD).mean()) * 100.0
        print(f"\nFNG coverage: {extreme_greed_pct:.1f}% bars in extreme greed (>{FNG_GREED_THRESHOLD:.0f}), "
              f"{extreme_fear_pct:.1f}% in extreme fear (<{FNG_FEAR_THRESHOLD:.0f})")

    btc_regime = wf3.compute_btc_regime(btc_df)
    dcfg = replace(dc.DCFG, n_entry=FROZEN_N_ENTRY, n_exit=FROZEN_N_EXIT)

    variants = [
        ("baseline", False, False),
        ("fng",      True,  False),
        ("btcrel",   False, True),
        ("both",     True,  True),
    ]

    print()
    all_var_trades = {}
    all_var_counters = {}
    for label, use_fng, use_btc_rel in variants:
        print(f"Running variant '{label}' ...")
        trades, counters = run_full(
            data, funding_by_symbol, btc_regime, fng_by_symbol, btc_rel_by_symbol,
            dcfg, use_fng=use_fng, use_btc_rel=use_btc_rel)
        print(f"  {len(trades)} trades  "
              f"(blocked: fng={counters['fng_blocked']}, "
              f"btcrel={counters['btc_rel_blocked']}, "
              f"fund={counters['fund_blocked']}, "
              f"btc_macro={counters['btc_macro_blocked']})")
        all_var_trades[label] = trades
        all_var_counters[label] = counters

    # ---- per-fold table ----
    print("\n=== PER-FOLD TEST RESULTS ===\n")
    header = (f"  {'fold':<4} {'window':<20} | "
              f"{'BASELINE':^32} | {'+FNG':^32} | {'+BTC-REL':^32} | {'+BOTH':^32}")
    print(header)
    sub = (f"  {'':<4} {'':<20} | "
           f"{'n':>3} {'win':>5} {'expR':>6} {'%/yr':>7}  | "
           f"{'n':>3} {'win':>5} {'expR':>6} {'%/yr':>7}  | "
           f"{'n':>3} {'win':>5} {'expR':>6} {'%/yr':>7}  | "
           f"{'n':>3} {'win':>5} {'expR':>6} {'%/yr':>7}")
    print(sub)
    print("-" * len(sub))

    rows = []
    for (label, _, _, te_start, te_end) in FOLDS:
        cells_by_var = {}
        for var_label in ("baseline", "fng", "btcrel", "both"):
            d = fold_stats(all_var_trades[var_label], te_start, te_end)
            cells_by_var[var_label] = d

        def cells(d):
            return (f"{d['n']:>3} {fmt_cell(d['win'], 5)} {fmt_cell(d['exp'], 6)} "
                    f"{fmt_cell(d['ann_ret'], 7, is_pct=True)}")
        window_str = f"{te_start[:7]}->{te_end[:7]}"
        print(f"  {label:<4} {window_str:<20} | "
              f"{cells(cells_by_var['baseline'])}  | "
              f"{cells(cells_by_var['fng'])}  | "
              f"{cells(cells_by_var['btcrel'])}  | "
              f"{cells(cells_by_var['both'])}")
        rows.append({
            "fold": label, "window": window_str,
            "base_n": cells_by_var['baseline']['n'], "base_win": cells_by_var['baseline']['win'],
            "base_exp": cells_by_var['baseline']['exp'], "base_ann": cells_by_var['baseline']['ann_ret'],
            "fng_n": cells_by_var['fng']['n'], "fng_win": cells_by_var['fng']['win'],
            "fng_exp": cells_by_var['fng']['exp'], "fng_ann": cells_by_var['fng']['ann_ret'],
            "btcrel_n": cells_by_var['btcrel']['n'], "btcrel_win": cells_by_var['btcrel']['win'],
            "btcrel_exp": cells_by_var['btcrel']['exp'], "btcrel_ann": cells_by_var['btcrel']['ann_ret'],
            "both_n": cells_by_var['both']['n'], "both_win": cells_by_var['both']['win'],
            "both_exp": cells_by_var['both']['exp'], "both_ann": cells_by_var['both']['ann_ret'],
        })

    pd.DataFrame(rows).to_csv("walkforward_v4_fold_table.csv", index=False)

    # ---- aggregate ----
    print("\n=== AGGREGATE (all test windows) ===\n")
    for label in ("baseline", "fng", "btcrel", "both"):
        agg = aggregate_stats(all_var_trades[label], FOLDS)
        if agg is None:
            print(f"  {label:<10}: 0 trades")
            continue
        print(f"  {label:<10}  n={agg['n']:<4}  win_rate={agg['win']:.3f}  "
              f"exp_R={agg['exp']:+.3f}  trade_sh={agg['sh']:+.3f}  "
              f"ann_ret={agg['ann_ret']:+.2f}%")
        if agg["trades"]:
            out = f"walkforward_v4_{label}_trades.csv"
            pd.DataFrame([t.__dict__ for t in agg["trades"]]).to_csv(out, index=False)

    print("\nWrote walkforward_v4_fold_table.csv and per-variant trade CSVs")
    print("\nFinal step:")
    for label in ("baseline", "fng", "btcrel", "both"):
        print(f"  python bootstrap.py --file walkforward_v4_{label}_trades.csv")


if __name__ == "__main__":
    main()
