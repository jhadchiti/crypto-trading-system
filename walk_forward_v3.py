"""
Walk-Forward v3 — frozen params + ADX + BTC macro regime overlay.
==================================================================

Adds a master regime gate on top of walk_forward_v2:
  BTC_TRENDING = SMA(BTC, 200) today > SMA(BTC, 200) 20 days ago

When BTC's 200-day SMA is sloping up, the strategy is "on" for every symbol.
When it's sloping flat or down, no new entries are allowed on any symbol
(existing positions still run their normal exits).

This is the simplest possible macro regime filter — one indicator, one
threshold, derived from one asset, applied across the whole portfolio. It
encodes the prior that "crypto trend-following works when BTC itself is
trending and bleeds when it isn't."

Runs three variants for comparison:
  V1: ADX only                          (= walk_forward_v2 ADX-ON baseline)
  V2: BTC macro only                    (no ADX gate)
  V3: BTC macro AND ADX                 (both gates active)

Per-fold and aggregate reports include win rate and annualized return %.

Outputs:
  walkforward_v3_fold_table.csv
  walkforward_v3_adx_only_trades.csv
  walkforward_v3_btc_only_trades.csv
  walkforward_v3_combined_trades.csv

Usage:
    python walk_forward_v3.py
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
from funding import fetch_funding, align_funding_to_bars
from walk_forward import FOLDS, filter_trades_by_entry, trade_sharpe


V2_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
              "AVAXUSDT", "LINKUSDT", "DOGEUSDT", "XRPUSDT")

FROZEN_N_ENTRY = 55
FROZEN_N_EXIT = 20
ADX_PERIOD = 14
ADX_THRESHOLD = 25.0

BTC_SMA_PERIOD = 200
BTC_SLOPE_LOOKBACK = 20  # SMA today vs SMA 20 days ago

RISK_PER_TRADE = 0.0075


# ============================================================================
# BTC macro regime
# ============================================================================

def compute_btc_regime(btc_df: pd.DataFrame,
                       sma_period: int = BTC_SMA_PERIOD,
                       slope_lookback: int = BTC_SLOPE_LOOKBACK) -> pd.Series:
    """Return a boolean Series: True when BTC's SMA is sloping up."""
    sma = btc_df["close"].rolling(sma_period).mean()
    return (sma > sma.shift(slope_lookback)).fillna(False)


# ============================================================================
# Backtest with both gates
# ============================================================================

def backtest_with_gates(df: pd.DataFrame, symbol: str, equity: float,
                        dcfg: dc.DonchianConfig,
                        funding_bps_by_bar: Optional[pd.Series],
                        funding_bps_8h_last: Optional[pd.Series],
                        btc_regime: Optional[pd.Series],
                        use_adx: bool,
                        use_btc_macro: bool,
                        ) -> tuple[list[bt.Trade], int]:
    trades: list[bt.Trade] = []
    pos: Optional[bt.Position] = None
    rt_cost_bps = 2 * (dcfg.taker_fee_bps + dcfg.slippage_bps)
    n_blocked = 0

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
            fund_8h = 0.0
            if funding_bps_8h_last is not None:
                try:
                    fund_8h = float(funding_bps_8h_last.loc[date])
                except KeyError:
                    fund_8h = 0.0

            allow_long = fund_8h <= d2.FUNDING_FILTER_MAX_BPS_8H
            allow_short = fund_8h >= -d2.FUNDING_FILTER_MAX_BPS_8H

            # BTC macro gate
            if use_btc_macro and btc_regime is not None:
                try:
                    btc_ok = bool(btc_regime.loc[date])
                except KeyError:
                    btc_ok = False
                if not btc_ok:
                    long_raw = (not math.isnan(row["entry_high"])
                                and row["close"] > row["entry_high"])
                    short_raw = (not math.isnan(row["entry_low"])
                                 and row["close"] < row["entry_low"])
                    if long_raw or short_raw:
                        n_blocked += 1
                    allow_long = False
                    allow_short = False

            # ADX gate
            if use_adx and (allow_long or allow_short):
                adx_val = row.get("adx", float("nan"))
                if math.isnan(adx_val) or adx_val < ADX_THRESHOLD:
                    long_raw = (not math.isnan(row["entry_high"])
                                and row["close"] > row["entry_high"])
                    short_raw = (not math.isnan(row["entry_low"])
                                 and row["close"] < row["entry_low"])
                    if long_raw or short_raw:
                        n_blocked += 1
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

    return trades, n_blocked


# ============================================================================
# Driver
# ============================================================================

def run_full(data, funding_by_symbol, dcfg, btc_regime,
             use_adx: bool, use_btc_macro: bool) -> tuple[list, int]:
    per_symbol_equity = 100_000.0 / len(data)
    all_trades = []
    total_blocked = 0
    for sym, df in data.items():
        d = dc.build_donchian(df, dcfg)
        d["adx"] = wf2.adx(df, ADX_PERIOD)
        fbb = funding_by_symbol.get(sym)
        fund_carry = fbb["funding_bps_in_bar"] if fbb is not None else None
        fund_last = fbb["funding_bps_8h_last"] if fbb is not None else None
        trades, blocked = backtest_with_gates(
            d, sym, per_symbol_equity, dcfg,
            funding_bps_by_bar=fund_carry,
            funding_bps_8h_last=fund_last,
            btc_regime=btc_regime,
            use_adx=use_adx,
            use_btc_macro=use_btc_macro,
        )
        all_trades.extend(trades)
        total_blocked += blocked
    return all_trades, total_blocked


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


def fmt_cell(x, w=7, is_pct=False):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return f"{'n/a':>{w}}"
    if isinstance(x, float):
        if is_pct:
            return f"{x:>+{w}.1f}"
        return f"{x:>+{w}.2f}"
    return f"{x:>{w}}"


def aggregate_stats(trades, fold_windows):
    """Aggregate across fold test windows. Window years summed for ann_ret."""
    rs_list = []
    total_years = 0.0
    for (_, _, _, te_start, te_end) in fold_windows:
        tr = filter_trades_by_entry(trades, te_start, te_end)
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
        "trades": [t for window in fold_windows
                   for t in filter_trades_by_entry(trades, window[3], window[4])],
    }


def main():
    cfg = replace(bt.CFG, symbols=V2_SYMBOLS)
    print(f"Loading Daily OHLCV for {len(V2_SYMBOLS)} symbols ...")
    data = bt.load_universe(cfg)
    if not data:
        print("No data — exiting.")
        return
    if "BTCUSDT" not in data:
        print("BTCUSDT missing — cannot compute BTC macro regime.")
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

    btc_regime = compute_btc_regime(data["BTCUSDT"])
    on_pct = float(btc_regime.mean()) * 100.0
    print(f"\nBTC macro regime: ON for {on_pct:.1f}% of bars "
          f"(SMA{BTC_SMA_PERIOD} rising over {BTC_SLOPE_LOOKBACK} days)")

    dcfg = replace(dc.DCFG, n_entry=FROZEN_N_ENTRY, n_exit=FROZEN_N_EXIT)

    variants = [
        ("adx_only",  True,  False),
        ("btc_only",  False, True),
        ("combined",  True,  True),
    ]

    print()
    all_var_trades = {}
    for label, use_adx, use_btc in variants:
        gates = []
        if use_adx: gates.append("ADX")
        if use_btc: gates.append("BTC_macro")
        gate_str = "+".join(gates) if gates else "none"
        print(f"Running variant '{label}' — gates: {gate_str} ...")
        trades, blocked = run_full(data, funding_by_symbol, dcfg, btc_regime,
                                   use_adx=use_adx, use_btc_macro=use_btc)
        print(f"  {len(trades)} trades  ({blocked} entries blocked by gates)")
        all_var_trades[label] = trades

    # ---- per-fold table ----
    print("\n=== PER-FOLD TEST RESULTS ===\n")
    header = (f"  {'fold':<4} {'window':<20} | "
              f"{'ADX-only':^36} | {'BTC-only':^36} | {'COMBINED':^36}")
    print(header)
    sub = (f"  {'':<4} {'':<20} | "
           f"{'n':>3} {'win':>5} {'expR':>6} {'sh':>6} {'%/yr':>7} | "
           f"{'n':>3} {'win':>5} {'expR':>6} {'sh':>6} {'%/yr':>7} | "
           f"{'n':>3} {'win':>5} {'expR':>6} {'sh':>6} {'%/yr':>7}")
    print(sub)
    print("-" * len(sub))

    rows = []
    for (label, _, _, te_start, te_end) in FOLDS:
        a = fold_stats(all_var_trades["adx_only"], te_start, te_end)
        b = fold_stats(all_var_trades["btc_only"], te_start, te_end)
        c = fold_stats(all_var_trades["combined"], te_start, te_end)
        window_str = f"{te_start[:7]}->{te_end[:7]}"

        def cells(d):
            return (f"{d['n']:>3} {fmt_cell(d['win'], 5)} {fmt_cell(d['exp'], 6)} "
                    f"{fmt_cell(d['sh'], 6)} {fmt_cell(d['ann_ret'], 7, is_pct=True)}")
        print(f"  {label:<4} {window_str:<20} | {cells(a)} | {cells(b)} | {cells(c)}")
        rows.append({
            "fold": label, "window": window_str,
            "adx_n": a["n"], "adx_win": a["win"], "adx_exp": a["exp"], "adx_sh": a["sh"], "adx_ann_pct": a["ann_ret"],
            "btc_n": b["n"], "btc_win": b["win"], "btc_exp": b["exp"], "btc_sh": b["sh"], "btc_ann_pct": b["ann_ret"],
            "cmb_n": c["n"], "cmb_win": c["win"], "cmb_exp": c["exp"], "cmb_sh": c["sh"], "cmb_ann_pct": c["ann_ret"],
        })

    pd.DataFrame(rows).to_csv("walkforward_v3_fold_table.csv", index=False)

    # ---- aggregate ----
    print("\n=== AGGREGATE (all test windows) ===\n")
    agg_for_csv = {}
    for label in ("adx_only", "btc_only", "combined"):
        agg = aggregate_stats(all_var_trades[label], FOLDS)
        if agg is None:
            print(f"  {label:<10}: 0 trades")
            continue
        print(f"  {label:<10}  n={agg['n']:<4}  win_rate={agg['win']:.3f}  "
              f"exp_R={agg['exp']:+.3f}  trade_sh={agg['sh']:+.3f}  "
              f"ann_ret={agg['ann_ret']:+.2f}%")
        agg_for_csv[label] = agg["trades"]

    for label, trades in agg_for_csv.items():
        if trades:
            out = f"walkforward_v3_{label}_trades.csv"
            pd.DataFrame([t.__dict__ for t in trades]).to_csv(out, index=False)

    print("\nWrote walkforward_v3_fold_table.csv and walkforward_v3_*_trades.csv")
    print("\nFinal step:")
    print("  python bootstrap.py --file walkforward_v3_adx_only_trades.csv")
    print("  python bootstrap.py --file walkforward_v3_btc_only_trades.csv")
    print("  python bootstrap.py --file walkforward_v3_combined_trades.csv")


if __name__ == "__main__":
    main()
