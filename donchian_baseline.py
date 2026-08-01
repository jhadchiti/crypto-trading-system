"""
Donchian Channel Breakout — Baseline Strategy
=============================================

Vanilla trend-following sanity check on the same universe, costs, and sizing
as the structural strategy. Rules:

  Long entry:   Daily close > rolling N_entry-day high  (default N_entry=55)
  Long exit:    Daily close < rolling N_exit-day low    (default N_exit=20)

  Short entry: mirror.

  Sizing:       same risk-per-trade + vol-target as core (0.75% per trade).
  Stops:        the exit channel acts as a soft stop. We also keep a hard
                ATR stop at 2×ATR(20) as a fail-safe.

If the structural-trendline strategy can't beat Donchian after costs, the
trendline geometry isn't adding edge — it's just complexity.

Usage:
    python donchian_baseline.py
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

import mtf_structural_backtest as bt   # reuse fetcher, ATR, sizing, metrics


@dataclass
class DonchianConfig:
    n_entry: int = 55
    n_exit: int = 20
    atr_period: int = 20
    atr_stop_mult: float = 2.0
    time_stop_bars: int = 90

    # reuse the core sizing/cost knobs from bt.CFG
    risk_per_trade: float = 0.0075
    vol_target_annual: float = 0.15
    portfolio_heat_cap: float = 0.03
    taker_fee_bps: float = 4.0
    slippage_bps: float = 5.0
    funding_bps_per_day: float = 1.0
    starting_equity: float = 100_000.0
    oos_start: str = "2024-01-01"


DCFG = DonchianConfig()


def build_donchian(df: pd.DataFrame, dcfg: DonchianConfig) -> pd.DataFrame:
    df = df.copy()
    df["atr"] = bt.atr(df, dcfg.atr_period)
    # use prior bar's channel to avoid lookahead
    df["entry_high"] = df["close"].rolling(dcfg.n_entry).max().shift(1)
    df["entry_low"]  = df["close"].rolling(dcfg.n_entry).min().shift(1)
    df["exit_low"]   = df["close"].rolling(dcfg.n_exit).min().shift(1)
    df["exit_high"]  = df["close"].rolling(dcfg.n_exit).max().shift(1)
    return df


def backtest_donchian(df: pd.DataFrame, symbol: str, equity: float,
                      dcfg: DonchianConfig) -> tuple[list[bt.Trade], pd.Series]:
    trades: list[bt.Trade] = []
    pos: Optional[bt.Position] = None
    rt_cost_bps = 2 * (dcfg.taker_fee_bps + dcfg.slippage_bps)
    eq_curve = []

    # cfg-like surrogate for the sizing function (which expects bt.Config)
    sizing_cfg = bt.Config(
        risk_per_trade=dcfg.risk_per_trade,
        vol_target_annual=dcfg.vol_target_annual,
    )

    for i, (date, row) in enumerate(df.iterrows()):
        # ---- mark-to-market existing pos ----
        if pos is not None:
            pos.bars_held += 1
            funding_drag = (dcfg.funding_bps_per_day / 10000.0) * abs(pos.size) * row["close"]
            equity -= funding_drag if pos.side > 0 else -funding_drag

            exit_reason = None
            exit_price = None

            # hard ATR stop
            if pos.side > 0 and row["low"] <= pos.stop:
                exit_price = pos.stop
                exit_reason = "atr_stop"
            elif pos.side < 0 and row["high"] >= pos.stop:
                exit_price = pos.stop
                exit_reason = "atr_stop"

            # channel exit
            if exit_reason is None:
                if pos.side > 0 and not math.isnan(row["exit_low"]) and row["close"] < row["exit_low"]:
                    exit_price = row["close"]
                    exit_reason = "channel_exit"
                elif pos.side < 0 and not math.isnan(row["exit_high"]) and row["close"] > row["exit_high"]:
                    exit_price = row["close"]
                    exit_reason = "channel_exit"

            # time stop
            if exit_reason is None and pos.bars_held >= dcfg.time_stop_bars:
                exit_price = row["close"]
                exit_reason = "time_stop"

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

        # ---- new entry? ----
        if pos is None and not math.isnan(row["atr"]):
            if not math.isnan(row["entry_high"]) and row["close"] > row["entry_high"]:
                entry = row["close"]
                stop = entry - dcfg.atr_stop_mult * row["atr"]
                size = bt._size_position(equity, entry, stop, row["atr"], sizing_cfg)
                if size > 0:
                    pos = bt.Position(symbol=symbol, side=+1,
                                      entry_date=date, entry_price=entry,
                                      size=size, stop=stop, initial_stop=stop,
                                      risk_dollars=size * (entry - stop),
                                      high_since_entry=entry, low_since_entry=entry)
            elif not math.isnan(row["entry_low"]) and row["close"] < row["entry_low"]:
                entry = row["close"]
                stop = entry + dcfg.atr_stop_mult * row["atr"]
                size = bt._size_position(equity, entry, stop, row["atr"], sizing_cfg)
                if size > 0:
                    pos = bt.Position(symbol=symbol, side=-1,
                                      entry_date=date, entry_price=entry,
                                      size=-size, stop=stop, initial_stop=stop,
                                      risk_dollars=size * (stop - entry),
                                      high_since_entry=entry, low_since_entry=entry)

        eq_curve.append(equity)

    return trades, pd.Series(eq_curve, index=df.index, name=symbol)


def main():
    print("Loading universe ...")
    cfg = bt.CFG
    data = bt.load_universe(cfg)
    if not data:
        print("No data — exiting.")
        return

    dcfg = DCFG
    per_symbol_metrics = {}
    all_trades: list[bt.Trade] = []
    equity_curves = {}
    per_symbol_equity = dcfg.starting_equity / len(data)

    for sym, df in data.items():
        print(f"\n=== {sym} ===")
        df = build_donchian(df, dcfg)
        trades, eq = backtest_donchian(df, sym, per_symbol_equity, dcfg)
        all_trades.extend(trades)
        equity_curves[sym] = eq
        per_symbol_metrics[sym] = bt.compute_metrics(trades, eq, per_symbol_equity)
        print(f"  trades: {len(trades)}, final equity: ${eq.iloc[-1]:,.0f}")

    port = pd.concat(equity_curves.values(), axis=1).ffill().sum(axis=1)
    per_symbol_metrics["PORTFOLIO"] = bt.compute_metrics(all_trades, port, dcfg.starting_equity)

    bt.print_metrics_table(per_symbol_metrics)

    if all_trades:
        trades_df = pd.DataFrame([t.__dict__ for t in all_trades])
        trades_df.to_csv("donchian_trades.csv", index=False)
        print(f"\nWrote donchian_trades.csv ({len(all_trades)} trades)")


if __name__ == "__main__":
    main()
