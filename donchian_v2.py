"""
Donchian v2 — expanded universe, real funding, parameter sweep.
================================================================

Builds on donchian_baseline.py:
  - Universe: 8 symbols (BTC/ETH/SOL/BNB/AVAX/LINK/DOGE/XRP)
  - Real Binance funding pulled and applied per bar
  - Funding entry filter (skip longs when 8h funding > 20bps for crowded longs)
  - Parameter sweep across (n_entry, n_exit) pairs
  - Daily bars (Donchian is robust to bar size; Daily keeps it simple)

Output:
  - donchian_v2_sweep.csv : portfolio metrics for each (n_entry, n_exit) combo
  - donchian_v2_best_trades.csv : trades from the best-Sharpe combo

Usage:
    python donchian_v2.py
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Optional

import numpy as np
import pandas as pd

import mtf_structural_backtest as bt
import donchian_baseline as dc
from funding import fetch_funding, align_funding_to_bars


V2_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
              "AVAXUSDT", "LINKUSDT", "DOGEUSDT", "XRPUSDT")

# Parameter grid for the sweep.
SWEEP = [
    (20, 10),
    (40, 15),
    (55, 20),
    (100, 30),
    (200, 50),
]

FUNDING_FILTER_MAX_BPS_8H = 20.0


def backtest_donchian_with_funding(df: pd.DataFrame, symbol: str, equity: float,
                                   dcfg: dc.DonchianConfig,
                                   funding_bps_by_bar: Optional[pd.Series] = None,
                                   funding_bps_8h_last: Optional[pd.Series] = None,
                                   ) -> tuple[list[bt.Trade], pd.Series]:
    """
    Donchian backtest with optional per-bar funding integration AND an entry
    filter that gates trades when 8h funding signals a crowded book.
    """
    trades: list[bt.Trade] = []
    pos: Optional[bt.Position] = None
    rt_cost_bps = 2 * (dcfg.taker_fee_bps + dcfg.slippage_bps)
    eq_curve = []

    sizing_cfg = bt.Config(
        risk_per_trade=dcfg.risk_per_trade,
        vol_target_annual=dcfg.vol_target_annual,
    )

    for i, (date, row) in enumerate(df.iterrows()):
        # ---- MTM existing position ----
        if pos is not None:
            pos.bars_held += 1

            # funding carry
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

            allow_long = fund_8h <= FUNDING_FILTER_MAX_BPS_8H
            allow_short = fund_8h >= -FUNDING_FILTER_MAX_BPS_8H

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

    return trades, pd.Series(eq_curve, index=df.index, name=symbol)


def run_one(data, funding_by_symbol, dcfg: dc.DonchianConfig,
            per_symbol_equity: float):
    all_trades: list[bt.Trade] = []
    equity_curves: dict[str, pd.Series] = {}
    per_symbol_metrics = {}
    for sym, df in data.items():
        d = dc.build_donchian(df, dcfg)
        fbb = funding_by_symbol.get(sym)
        fund_carry = fbb["funding_bps_in_bar"] if fbb is not None else None
        fund_last = fbb["funding_bps_8h_last"] if fbb is not None else None
        trades, eq = backtest_donchian_with_funding(
            d, sym, per_symbol_equity, dcfg,
            funding_bps_by_bar=fund_carry,
            funding_bps_8h_last=fund_last,
        )
        all_trades.extend(trades)
        equity_curves[sym] = eq
        per_symbol_metrics[sym] = bt.compute_metrics(trades, eq, per_symbol_equity)
    port = pd.concat(equity_curves.values(), axis=1).ffill().sum(axis=1)
    starting_equity = per_symbol_equity * len(data)
    per_symbol_metrics["PORTFOLIO"] = bt.compute_metrics(all_trades, port, starting_equity)
    return per_symbol_metrics, all_trades


def main():
    # Load Daily OHLCV for the v2 universe ONCE
    cfg = replace(bt.CFG, symbols=V2_SYMBOLS)
    print(f"Loading Daily OHLCV for {len(V2_SYMBOLS)} symbols ...")
    data = bt.load_universe(cfg)
    if not data:
        print("No data — exiting.")
        return

    # Load funding ONCE
    start_ms = int(pd.Timestamp(cfg.start_date, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    print("\nLoading funding history ...")
    funding_by_symbol = {}
    for s in data.keys():
        print(f"  funding: {s} ...")
        ev = fetch_funding(s, start_ms, end_ms)
        if ev.empty:
            continue
        # Daily bars = 1440 minutes
        funding_by_symbol[s] = align_funding_to_bars(ev, data[s].index, 1440)

    per_symbol_equity = 100_000.0 / len(data)

    print("\nSweeping (n_entry, n_exit) pairs ...")
    rows = []
    best = None
    best_sharpe = -1e9
    for (n_in, n_out) in SWEEP:
        dcfg = replace(dc.DCFG, n_entry=n_in, n_exit=n_out)
        m, trades = run_one(data, funding_by_symbol, dcfg, per_symbol_equity)
        port = m["PORTFOLIO"]
        rows.append({
            "n_entry": n_in,
            "n_exit": n_out,
            "trades": port.get("trade_count"),
            "hit_rate": port.get("hit_rate"),
            "expectancy_R": port.get("expectancy_R"),
            "sharpe": port.get("sharpe"),
            "max_dd": port.get("max_drawdown"),
            "CAGR": port.get("CAGR"),
            "profit_factor": port.get("profit_factor"),
        })
        print(f"  ({n_in:>3}/{n_out:>3})  trades={port.get('trade_count'):<4}  "
              f"sharpe={port.get('sharpe', float('nan')):+.3f}  "
              f"exp_R={port.get('expectancy_R', float('nan')):+.3f}")
        if port.get("sharpe") is not None and not math.isnan(port.get("sharpe")) and port["sharpe"] > best_sharpe:
            best_sharpe = port["sharpe"]
            best = ((n_in, n_out), trades)

    df = pd.DataFrame(rows)
    df.to_csv("donchian_v2_sweep.csv", index=False)
    print("\n=== SWEEP RESULTS (PORTFOLIO, expanded universe, real funding) ===\n")
    print(df.to_string(index=False, float_format=lambda x: f"{x:+.3f}"))
    print(f"\nWrote donchian_v2_sweep.csv")

    if best is not None:
        (n_in, n_out), trades = best
        if trades:
            pd.DataFrame([t.__dict__ for t in trades]).to_csv(
                "donchian_v2_best_trades.csv", index=False)
            print(f"\nBest combo: n_entry={n_in}, n_exit={n_out} — wrote donchian_v2_best_trades.csv")
            print(f"Now run:  python bootstrap.py --file donchian_v2_best_trades.csv")


if __name__ == "__main__":
    main()
