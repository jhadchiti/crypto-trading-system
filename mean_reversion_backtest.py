"""
Mean-Reversion Backtest Engine.
================================

Runs the MR strategy bar-by-bar on a single symbol. Produces bt.Trade objects
compatible with the existing infrastructure (bootstrap.py, strategy_report.py,
walk_forward_*).

Reuses mtf_structural_backtest.Position and bt.Trade dataclasses so the trade
schema matches the trend strategy. This lets one bootstrap test, one report
generator, and one dashboard work for both strategies.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

import mtf_structural_backtest as bt
import mean_reversion_strategy as mrs


# ============================================================================
# Single-symbol backtest
# ============================================================================

def backtest_mr_symbol(
    df: pd.DataFrame,
    symbol: str,
    equity: float,
    fng_series: pd.Series,
    funding_series: pd.Series,
    cfg: mrs.MeanReversionConfig,
    btc_regime: Optional[pd.Series] = None,
    funding_carry: Optional[pd.Series] = None,
) -> tuple[list[bt.Trade], pd.Series]:
    """
    Bar-by-bar backtest of MR on one symbol.

    Inputs:
      df:              OHLCV DataFrame indexed by Daily timestamp (UTC)
      symbol:          symbol name (for Trade.symbol)
      equity:          starting equity (will be updated as trades close)
      fng_series:      Fear & Greed daily series (aligned to df.index)
      funding_series:  8h funding rate in bps (most recent value, ffilled to bar)
      cfg:             MeanReversionConfig
      btc_regime:      optional Series of macro on/off booleans
                       if cfg.require_macro_off, only trade when this is False
      funding_carry:   optional per-bar funding cost in bps (for MTM drag)

    Returns:
      (trades_list, equity_curve)
    """
    # Build indicators
    d = mrs.build_indicators(df, cfg)
    d = mrs.compute_mr_signals(d, fng_series, funding_series, cfg)

    trades: list[bt.Trade] = []
    pos: Optional[bt.Position] = None
    rt_cost_bps = 2 * (cfg.taker_fee_bps + cfg.slippage_bps)
    eq_curve = []

    for i, (date, row) in enumerate(d.iterrows()):
        # ---- MTM existing position ----
        if pos is not None:
            pos.bars_held += 1

            # Funding carry (constant fallback or per-bar if provided)
            if funding_carry is not None:
                try:
                    bar_bps = float(funding_carry.loc[date])
                except KeyError:
                    bar_bps = 0.0
            else:
                bar_bps = cfg.funding_bps_per_day
            funding_drag = (bar_bps / 10000.0) * abs(pos.size) * row["close"]
            equity -= funding_drag if pos.side > 0 else -funding_drag

            # Compute current R-multiple
            r_so_far = 0.0
            if pos.risk_dollars > 0:
                pnl_so_far = (row["close"] - pos.entry_price) * pos.size
                r_so_far = pnl_so_far / pos.risk_dollars

            # Check exits
            exit_result = mrs.check_exit(
                side=pos.side,
                current_close=row["close"],
                current_low=row["low"],
                current_high=row["high"],
                stop=pos.stop,
                entry_price=pos.entry_price,
                sma_mean=row["sma_mean"],
                atr=row["atr"],
                bars_held=pos.bars_held,
                cfg=cfg,
                r_multiple_so_far=r_so_far,
            )

            if exit_result is not None:
                exit_reason, exit_price = exit_result
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

        # ---- Entry check ----
        if pos is None and not math.isnan(row.get("atr", float("nan"))):
            # Optional macro gate (only trade MR when trend system is dormant)
            if cfg.require_macro_off and btc_regime is not None:
                try:
                    macro_on = bool(btc_regime.loc[date])
                except KeyError:
                    macro_on = False
                if macro_on:
                    eq_curve.append(equity)
                    continue

            long_sig = bool(row.get("mr_long_signal", False))
            short_sig = bool(row.get("mr_short_signal", False))

            if long_sig:
                entry = row["close"]
                stop = entry - cfg.atr_stop_mult * row["atr"]
                size = mrs.position_size(equity, entry, stop, row["atr"], cfg)
                if size > 0:
                    pos = bt.Position(
                        symbol=symbol, side=+1,
                        entry_date=date, entry_price=entry,
                        size=size, stop=stop, initial_stop=stop,
                        risk_dollars=size * (entry - stop),
                        high_since_entry=entry, low_since_entry=entry,
                    )
            elif short_sig:
                entry = row["close"]
                stop = entry + cfg.atr_stop_mult * row["atr"]
                size = mrs.position_size(equity, entry, stop, row["atr"], cfg)
                if size > 0:
                    pos = bt.Position(
                        symbol=symbol, side=-1,
                        entry_date=date, entry_price=entry,
                        size=-size, stop=stop, initial_stop=stop,
                        risk_dollars=size * (stop - entry),
                        high_since_entry=entry, low_since_entry=entry,
                    )

        eq_curve.append(equity)

    return trades, pd.Series(eq_curve, index=d.index, name=symbol)


# ============================================================================
# Convenience: run on universe
# ============================================================================

def run_mr_universe(
    data: dict,
    fng_df: pd.DataFrame,
    funding_by_symbol: dict,
    btc_regime: Optional[pd.Series] = None,
    cfg: Optional[mrs.MeanReversionConfig] = None,
    starting_equity_per_symbol: float = 12_500.0,
) -> tuple[list[bt.Trade], dict]:
    """
    Run MR backtest across a dict of symbols. Returns (all_trades, equity_curves).

    Inputs:
      data:              {symbol: ohlcv_df}
      fng_df:            DataFrame with 'fng_value' column indexed by date
      funding_by_symbol: {symbol: DataFrame with funding_bps_8h_last and funding_bps_in_bar}
      btc_regime:        macro on/off series (used if cfg.require_macro_off=True)
      cfg:               MeanReversionConfig (defaults if None)
    """
    if cfg is None:
        cfg = mrs.MeanReversionConfig()

    all_trades: list[bt.Trade] = []
    equity_curves = {}

    # Build FNG series aligned to a common timeline
    fng_series = fng_df["fng_value"] if "fng_value" in fng_df.columns else pd.Series(dtype=float)

    for sym, df in data.items():
        if not isinstance(df, pd.DataFrame) or df.empty:
            continue
        fund_info = funding_by_symbol.get(sym) if funding_by_symbol else None
        if fund_info is not None and "funding_bps_8h_last" in fund_info.columns:
            funding_series = fund_info["funding_bps_8h_last"]
            funding_carry = fund_info.get("funding_bps_in_bar")
        else:
            funding_series = pd.Series(0.0, index=df.index)
            funding_carry = None

        trades, eq = backtest_mr_symbol(
            df=df, symbol=sym, equity=starting_equity_per_symbol,
            fng_series=funding_series.reindex(df.index, method="ffill").fillna(0.0)
                if False else fng_series.reindex(df.index, method="ffill").fillna(50.0),
            funding_series=funding_series,
            cfg=cfg,
            btc_regime=btc_regime,
            funding_carry=funding_carry,
        )
        all_trades.extend(trades)
        equity_curves[sym] = eq

    return all_trades, equity_curves
