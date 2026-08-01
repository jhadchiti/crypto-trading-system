"""
Walk-Forward v2 — frozen Donchian params + ADX(14) regime filter.
==================================================================

Two changes from walk_forward.py:
  1. NO parameter sweep. Params are frozen to (55, 20) before any test data
     is touched. This eliminates the in-sample-selection noise that polluted
     walk_forward.py (where train-Sharpe anti-predicted test-Sharpe).
  2. Optional ADX(14) > threshold filter at entry. Only takes trades when the
     market is genuinely trending. Classic regime filter for trend systems.

The script runs BOTH variants (ADX-on and ADX-off) on the same data and
prints them side by side, fold by fold.

Outputs:
  walkforward_v2_fold_table.csv         — per-fold metrics for both variants
  walkforward_v2_adx_on_trades.csv      — trades from ADX-filtered variant
  walkforward_v2_adx_off_trades.csv     — trades from unfiltered variant

Then:
  python bootstrap.py --file walkforward_v2_adx_on_trades.csv
  python bootstrap.py --file walkforward_v2_adx_off_trades.csv

Usage:
    python walk_forward_v2.py
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
from walk_forward import FOLDS, filter_trades_by_entry, trade_sharpe


V2_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
              "AVAXUSDT", "LINKUSDT", "DOGEUSDT", "XRPUSDT")

# Frozen params — chosen from the v1 sweep BEFORE seeing walk-forward results.
# (55, 20) is the classical Donchian setting, had Sharpe ~1.00 in the full
# backtest, and produces enough trades per fold for stable estimates.
FROZEN_N_ENTRY = 55
FROZEN_N_EXIT = 20

# Regime filter parameters
ADX_PERIOD = 14
ADX_THRESHOLD = 25.0


# ============================================================================
# ADX (Wilder's Average Directional Index)
# ============================================================================

def wilder_smooth(s: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing is an EWMA with alpha = 1/period."""
    return s.ewm(alpha=1.0 / period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Compute ADX(period). Returns a Series aligned to df.index.

    Standard Wilder calculation:
      +DM = max(high - prev_high, 0)  if (high - prev_high) > (prev_low - low) else 0
      -DM = max(prev_low - low, 0)    if (prev_low - low) > (high - prev_high) else 0
      TR  = true range
      +DI = 100 * Wilder(+DM) / Wilder(TR)
      -DI = 100 * Wilder(-DM) / Wilder(TR)
      DX  = 100 * |+DI - -DI| / (+DI + -DI)
      ADX = Wilder(DX)
    """
    high, low, close = df["high"], df["low"], df["close"]
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    up_move = high - prev_high
    down_move = prev_low - low

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr_w = wilder_smooth(tr, period)
    plus_di = 100.0 * wilder_smooth(plus_dm, period) / atr_w.replace(0, np.nan)
    minus_di = 100.0 * wilder_smooth(minus_dm, period) / atr_w.replace(0, np.nan)

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    return wilder_smooth(dx.fillna(0), period)


# ============================================================================
# Donchian backtest with optional ADX gate
# ============================================================================

def backtest_donchian_adx(df: pd.DataFrame, symbol: str, equity: float,
                          dcfg: dc.DonchianConfig,
                          funding_bps_by_bar: Optional[pd.Series] = None,
                          funding_bps_8h_last: Optional[pd.Series] = None,
                          use_adx_filter: bool = False,
                          adx_threshold: float = ADX_THRESHOLD,
                          ) -> tuple[list[bt.Trade], pd.Series, int]:
    """
    Returns (trades, equity_curve, n_blocked_by_adx).
    """
    trades: list[bt.Trade] = []
    pos: Optional[bt.Position] = None
    rt_cost_bps = 2 * (dcfg.taker_fee_bps + dcfg.slippage_bps)
    eq_curve = []
    n_blocked_adx = 0

    sizing_cfg = bt.Config(
        risk_per_trade=dcfg.risk_per_trade,
        vol_target_annual=dcfg.vol_target_annual,
    )

    for i, (date, row) in enumerate(df.iterrows()):
        # ---- MTM existing position ----
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
            # Funding entry filter
            fund_8h = 0.0
            if funding_bps_8h_last is not None:
                try:
                    fund_8h = float(funding_bps_8h_last.loc[date])
                except KeyError:
                    fund_8h = 0.0

            allow_long = fund_8h <= d2.FUNDING_FILTER_MAX_BPS_8H
            allow_short = fund_8h >= -d2.FUNDING_FILTER_MAX_BPS_8H

            # ADX regime gate
            if use_adx_filter:
                adx_val = row.get("adx", float("nan"))
                if math.isnan(adx_val) or adx_val < adx_threshold:
                    long_signal_raw = (not math.isnan(row["entry_high"])
                                       and row["close"] > row["entry_high"])
                    short_signal_raw = (not math.isnan(row["entry_low"])
                                        and row["close"] < row["entry_low"])
                    if long_signal_raw or short_signal_raw:
                        n_blocked_adx += 1
                    allow_long = False
                    allow_short = False

            if (allow_long and not math.isnan(row["entry_high"])
                    and row["close"] > row["entry_high"]):
                entry = row["close"]; stop = entry - dcfg.atr_stop_mult * row["atr"]
                size = bt._size_position(equity, entry, stop, row["atr"], sizing_cfg)
                if size > 0:
                    pos = bt.Position(symbol=symbol, side=+1,
                                      entry_date=date, entry_price=entry,
                                      size=size, stop=stop, initial_stop=stop,
                                      risk_dollars=size * (entry - stop),
                                      high_since_entry=entry, low_since_entry=entry)
            elif (allow_short and not math.isnan(row["entry_low"])
                    and row["close"] < row["entry_low"]):
                entry = row["close"]; stop = entry + dcfg.atr_stop_mult * row["atr"]
                size = bt._size_position(equity, entry, stop, row["atr"], sizing_cfg)
                if size > 0:
                    pos = bt.Position(symbol=symbol, side=-1,
                                      entry_date=date, entry_price=entry,
                                      size=-size, stop=stop, initial_stop=stop,
                                      risk_dollars=size * (stop - entry),
                                      high_since_entry=entry, low_since_entry=entry)

        eq_curve.append(equity)

    return trades, pd.Series(eq_curve, index=df.index, name=symbol), n_blocked_adx


# ============================================================================
# Driver
# ============================================================================

def run_full(data, funding_by_symbol, dcfg, use_adx: bool) -> tuple[list, int]:
    """Run one full backtest across all symbols. Returns (trades, n_blocked)."""
    per_symbol_equity = 100_000.0 / len(data)
    all_trades = []
    total_blocked = 0
    for sym, df in data.items():
        d = dc.build_donchian(df, dcfg)
        d["adx"] = adx(df, ADX_PERIOD)
        fbb = funding_by_symbol.get(sym)
        fund_carry = fbb["funding_bps_in_bar"] if fbb is not None else None
        fund_last = fbb["funding_bps_8h_last"] if fbb is not None else None
        trades, _, blocked = backtest_donchian_adx(
            d, sym, per_symbol_equity, dcfg,
            funding_bps_by_bar=fund_carry,
            funding_bps_8h_last=fund_last,
            use_adx_filter=use_adx,
            adx_threshold=ADX_THRESHOLD,
        )
        all_trades.extend(trades)
        total_blocked += blocked
    return all_trades, total_blocked


def fold_summary(trades, te_start, te_end):
    tr = filter_trades_by_entry(trades, te_start, te_end)
    if not tr:
        return {"n": 0, "exp": float("nan"), "sh": float("nan"), "hit": float("nan")}
    rs = np.array([t.r_multiple for t in tr])
    return {
        "n": len(tr),
        "exp": float(rs.mean()),
        "sh": trade_sharpe(rs),
        "hit": float((rs > 0).mean()),
    }


def main():
    # --- load data + funding once ---
    cfg = replace(bt.CFG, symbols=V2_SYMBOLS)
    print(f"Loading Daily OHLCV for {len(V2_SYMBOLS)} symbols ...")
    data = bt.load_universe(cfg)
    if not data:
        print("No data — exiting.")
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

    dcfg = replace(dc.DCFG, n_entry=FROZEN_N_ENTRY, n_exit=FROZEN_N_EXIT)

    # --- run both variants on full data ---
    print(f"\nRunning frozen params ({FROZEN_N_ENTRY}/{FROZEN_N_EXIT}) — ADX OFF ...")
    trades_off, _ = run_full(data, funding_by_symbol, dcfg, use_adx=False)
    print(f"  {len(trades_off)} trades")

    print(f"\nRunning frozen params ({FROZEN_N_ENTRY}/{FROZEN_N_EXIT}) — ADX ON (>{ADX_THRESHOLD}) ...")
    trades_on, blocked = run_full(data, funding_by_symbol, dcfg, use_adx=True)
    print(f"  {len(trades_on)} trades  ({blocked} entries blocked by ADX)")

    # --- per-fold table ---
    rows = []
    for (label, _, _, te_start, te_end) in FOLDS:
        off = fold_summary(trades_off, te_start, te_end)
        on = fold_summary(trades_on, te_start, te_end)
        rows.append({
            "fold": label,
            "test_window": f"{te_start[:7]} → {te_end[:7]}",
            "off_n": off["n"], "off_exp": off["exp"], "off_sh": off["sh"], "off_hit": off["hit"],
            "on_n":  on["n"],  "on_exp":  on["exp"],  "on_sh":  on["sh"],  "on_hit":  on["hit"],
        })

    df = pd.DataFrame(rows)
    print("\n=== PER-FOLD TEST RESULTS (frozen params) ===\n")
    print("       window                ADX-OFF                          ADX-ON")
    print("                       n     exp_R   sh    hit      n     exp_R   sh    hit")
    print("-" * 78)
    for _, r in df.iterrows():
        def f(x, w=6):
            if x is None or (isinstance(x, float) and math.isnan(x)):
                return f"{'n/a':>{w}}"
            if isinstance(x, float):
                return f"{x:>+{w}.2f}"
            return f"{x:>{w}}"
        print(f"  {r['fold']:<3} {r['test_window']:<16} "
              f"{r['off_n']:>4}  {f(r['off_exp'])}  {f(r['off_sh'])}  {f(r['off_hit'])}  "
              f"{r['on_n']:>5}  {f(r['on_exp'])}  {f(r['on_sh'])}  {f(r['on_hit'])}")

    df.to_csv("walkforward_v2_fold_table.csv", index=False)

    # --- aggregate test windows ---
    def collect(trades):
        out = []
        for (_, _, _, te_start, te_end) in FOLDS:
            out.extend(filter_trades_by_entry(trades, te_start, te_end))
        return out

    agg_off = collect(trades_off)
    agg_on = collect(trades_on)

    print("\n=== AGGREGATE (all test windows) ===")
    for label, agg in [("ADX-OFF", agg_off), ("ADX-ON ", agg_on)]:
        if not agg:
            print(f"  {label}: 0 trades"); continue
        rs = np.array([t.r_multiple for t in agg])
        print(f"  {label}:  n={len(agg):<4}  hit={float((rs>0).mean()):.3f}  "
              f"exp={rs.mean():+.3f}R  trade_sh={trade_sharpe(rs):+.3f}")

    if agg_off:
        pd.DataFrame([t.__dict__ for t in agg_off]).to_csv(
            "walkforward_v2_adx_off_trades.csv", index=False)
    if agg_on:
        pd.DataFrame([t.__dict__ for t in agg_on]).to_csv(
            "walkforward_v2_adx_on_trades.csv", index=False)
    print("\nWrote walkforward_v2_fold_table.csv, walkforward_v2_adx_off_trades.csv, walkforward_v2_adx_on_trades.csv")
    print("\nFinal step:")
    print("  python bootstrap.py --file walkforward_v2_adx_off_trades.csv")
    print("  python bootstrap.py --file walkforward_v2_adx_on_trades.csv")


if __name__ == "__main__":
    main()
