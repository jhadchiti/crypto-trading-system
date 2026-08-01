"""
Walk-Forward v5 — adds dynamic sizing based on regime states.
==============================================================

Three variants on the same baseline (v3 btc_only + frozen 55/20 params):

  baseline      : fixed 0.75% risk per trade
  dynamic_vol   : sizing scaled by BTC vol regime only
  dynamic_full  : sizing scaled by vol + correlation + funding

Same fold structure, same metrics, same bootstrap-ready CSV outputs.

Usage:
    python walk_forward_v5.py
    python bootstrap.py --file walkforward_v5_baseline_trades.csv
    python bootstrap.py --file walkforward_v5_dynamic_vol_trades.csv
    python bootstrap.py --file walkforward_v5_dynamic_full_trades.csv
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
import walk_forward_v3 as wf3
from funding import fetch_funding, align_funding_to_bars
from walk_forward import FOLDS, filter_trades_by_entry, trade_sharpe
from regime_signals import all_regimes
from dynamic_sizing import risk_multiplier


V2_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
              "AVAXUSDT", "LINKUSDT", "DOGEUSDT", "XRPUSDT")
FROZEN_N_ENTRY = 55
FROZEN_N_EXIT = 20
RISK_PER_TRADE = 0.0075


# ============================================================================
# Backtest with dynamic sizing
# ============================================================================

def backtest_v5(df: pd.DataFrame, symbol: str, equity: float,
                dcfg: dc.DonchianConfig,
                funding_bps_by_bar: Optional[pd.Series],
                funding_bps_8h_last: Optional[pd.Series],
                btc_regime: Optional[pd.Series],
                regime_df: Optional[pd.DataFrame],
                use_vol: bool,
                use_corr: bool,
                use_funding: bool,
                ) -> tuple[list[bt.Trade], dict]:
    """
    Identical to wf3 btc_only logic, except risk_per_trade is dynamically
    scaled per entry using the regime state at that bar.
    """
    trades: list[bt.Trade] = []
    pos: Optional[bt.Position] = None
    rt_cost_bps = 2 * (dcfg.taker_fee_bps + dcfg.slippage_bps)
    mult_log = []   # track applied multipliers for stats

    sizing_cfg_base = bt.Config(
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

            if btc_regime is not None:
                try:
                    macro_ok = bool(btc_regime.loc[date])
                except KeyError:
                    macro_ok = False
                if not macro_ok:
                    allow_long = False
                    allow_short = False

            is_long_break = (not math.isnan(row["entry_high"])
                             and row["close"] > row["entry_high"])
            is_short_break = (not math.isnan(row["entry_low"])
                              and row["close"] < row["entry_low"])

            if (allow_long and is_long_break) or (allow_short and is_short_break):
                # --- compute dynamic risk multiplier for this entry ---
                vol_state = corr_state = funding_state = "NORMAL"
                if regime_df is not None:
                    try:
                        rr = regime_df.loc[date]
                        vol_state = str(rr.get("vol_regime", "NORMAL"))
                        corr_state = str(rr.get("corr_regime", "MIXED"))
                        funding_state = str(rr.get("funding_regime", "NEUTRAL"))
                    except KeyError:
                        pass
                side_is_long = bool(allow_long and is_long_break)
                mult = risk_multiplier(vol_state, corr_state, funding_state,
                                        is_long=side_is_long,
                                        use_vol=use_vol, use_corr=use_corr,
                                        use_funding=use_funding)
                mult_log.append(mult)

                # apply multiplier via a per-entry sizing cfg
                sizing_cfg = bt.Config(
                    risk_per_trade=dcfg.risk_per_trade * mult,
                    vol_target_annual=dcfg.vol_target_annual,
                )

                if allow_long and is_long_break:
                    entry = row["close"]; stop = entry - dcfg.atr_stop_mult * row["atr"]
                    size = bt._size_position(equity, entry, stop, row["atr"], sizing_cfg)
                    if size > 0:
                        pos = bt.Position(symbol=symbol, side=+1,
                                          entry_date=date, entry_price=entry,
                                          size=size, stop=stop, initial_stop=stop,
                                          risk_dollars=size * (entry - stop),
                                          high_since_entry=entry, low_since_entry=entry)
                elif allow_short and is_short_break:
                    entry = row["close"]; stop = entry + dcfg.atr_stop_mult * row["atr"]
                    size = bt._size_position(equity, entry, stop, row["atr"], sizing_cfg)
                    if size > 0:
                        pos = bt.Position(symbol=symbol, side=-1,
                                          entry_date=date, entry_price=entry,
                                          size=-size, stop=stop, initial_stop=stop,
                                          risk_dollars=size * (stop - entry),
                                          high_since_entry=entry, low_since_entry=entry)

    stats = {
        "n_trades": len(trades),
        "avg_multiplier": float(np.mean(mult_log)) if mult_log else 1.0,
        "min_multiplier": float(np.min(mult_log)) if mult_log else 1.0,
        "max_multiplier": float(np.max(mult_log)) if mult_log else 1.0,
    }
    return trades, stats


# ============================================================================
# Driver
# ============================================================================

def run_full(data, funding_by_symbol, btc_regime, regime_df, dcfg,
             use_vol: bool, use_corr: bool, use_funding: bool):
    per_symbol_equity = 100_000.0 / len(data)
    all_trades = []
    agg_stats = {"n_trades": 0, "avg_multiplier": [], "min_multiplier": 1.0, "max_multiplier": 1.0}
    for sym, df in data.items():
        d = dc.build_donchian(df, dcfg)
        fbb = funding_by_symbol.get(sym)
        fund_carry = fbb["funding_bps_in_bar"] if fbb is not None else None
        fund_last = fbb["funding_bps_8h_last"] if fbb is not None else None
        trades, st = backtest_v5(
            d, sym, per_symbol_equity, dcfg,
            funding_bps_by_bar=fund_carry,
            funding_bps_8h_last=fund_last,
            btc_regime=btc_regime,
            regime_df=regime_df,
            use_vol=use_vol, use_corr=use_corr, use_funding=use_funding,
        )
        all_trades.extend(trades)
        agg_stats["n_trades"] += st["n_trades"]
        if st["n_trades"]:
            agg_stats["avg_multiplier"].append(st["avg_multiplier"])
            agg_stats["min_multiplier"] = min(agg_stats["min_multiplier"], st["min_multiplier"])
            agg_stats["max_multiplier"] = max(agg_stats["max_multiplier"], st["max_multiplier"])
    agg_stats["avg_multiplier"] = (float(np.mean(agg_stats["avg_multiplier"]))
                                    if agg_stats["avg_multiplier"] else 1.0)
    return all_trades, agg_stats


def fold_stats(trades, te_start: str, te_end: str) -> dict:
    tr = filter_trades_by_entry(trades, te_start, te_end)
    s = pd.Timestamp(te_start, tz="UTC")
    e = pd.Timestamp(te_end, tz="UTC")
    years = max((e - s).days / 365.25, 1e-9)
    if not tr:
        return {"n": 0, "win": float("nan"), "exp": float("nan"),
                "sh": float("nan"), "ann_ret": float("nan")}
    rs = np.array([t.r_multiple for t in tr])
    return {
        "n": len(tr),
        "win": float((rs > 0).mean()),
        "exp": float(rs.mean()),
        "sh":  trade_sharpe(rs),
        "ann_ret": (float(rs.sum()) / years) * RISK_PER_TRADE * 100.0,
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
        print("Data load failed."); return

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

    print("\nComputing regime signals ...")
    regime_df = all_regimes(data, funding_by_symbol)
    print(f"  regime row count: {len(regime_df)}")
    if not regime_df.empty:
        print(f"  vol regime dist:     {dict(regime_df['vol_regime'].value_counts())}")
        print(f"  corr regime dist:    {dict(regime_df['corr_regime'].value_counts())}")
        print(f"  funding regime dist: {dict(regime_df['funding_regime'].value_counts())}")

    btc_regime = wf3.compute_btc_regime(data["BTCUSDT"])
    dcfg = replace(dc.DCFG, n_entry=FROZEN_N_ENTRY, n_exit=FROZEN_N_EXIT)

    variants = [
        ("baseline",     False, False, False),
        ("dynamic_vol",  True,  False, False),
        ("dynamic_full", True,  True,  True),
    ]

    print()
    all_var_trades = {}
    all_var_stats = {}
    for label, uv, uc, uf in variants:
        print(f"Running variant '{label}' (use_vol={uv}, use_corr={uc}, use_funding={uf}) ...")
        trades, st = run_full(data, funding_by_symbol, btc_regime, regime_df,
                              dcfg, use_vol=uv, use_corr=uc, use_funding=uf)
        print(f"  {len(trades)} trades, avg_mult={st['avg_multiplier']:.3f}, "
              f"range [{st['min_multiplier']:.2f}, {st['max_multiplier']:.2f}]")
        all_var_trades[label] = trades
        all_var_stats[label] = st

    # ---- per-fold table ----
    print("\n=== PER-FOLD TEST RESULTS ===\n")
    header = (f"  {'fold':<4} {'window':<20} | "
              f"{'BASELINE':^32} | {'DYNAMIC_VOL':^32} | {'DYNAMIC_FULL':^32}")
    print(header)
    sub = (f"  {'':<4} {'':<20} | "
           f"{'n':>3} {'win':>5} {'expR':>6} {'%/yr':>7}  | "
           f"{'n':>3} {'win':>5} {'expR':>6} {'%/yr':>7}  | "
           f"{'n':>3} {'win':>5} {'expR':>6} {'%/yr':>7}")
    print(sub)
    print("-" * len(sub))

    rows = []
    for (label, _, _, te_start, te_end) in FOLDS:
        cells_by_var = {}
        for v in ("baseline", "dynamic_vol", "dynamic_full"):
            cells_by_var[v] = fold_stats(all_var_trades[v], te_start, te_end)
        def cells(d):
            return (f"{d['n']:>3} {fmt_cell(d['win'], 5)} {fmt_cell(d['exp'], 6)} "
                    f"{fmt_cell(d['ann_ret'], 7, is_pct=True)}")
        window_str = f"{te_start[:7]}->{te_end[:7]}"
        print(f"  {label:<4} {window_str:<20} | "
              f"{cells(cells_by_var['baseline'])}  | "
              f"{cells(cells_by_var['dynamic_vol'])}  | "
              f"{cells(cells_by_var['dynamic_full'])}")
        rows.append({"fold": label, "window": window_str,
                     "base_n": cells_by_var['baseline']['n'], "base_exp": cells_by_var['baseline']['exp'],
                     "base_ann": cells_by_var['baseline']['ann_ret'],
                     "dv_n": cells_by_var['dynamic_vol']['n'], "dv_exp": cells_by_var['dynamic_vol']['exp'],
                     "dv_ann": cells_by_var['dynamic_vol']['ann_ret'],
                     "df_n": cells_by_var['dynamic_full']['n'], "df_exp": cells_by_var['dynamic_full']['exp'],
                     "df_ann": cells_by_var['dynamic_full']['ann_ret']})

    pd.DataFrame(rows).to_csv("walkforward_v5_fold_table.csv", index=False)

    # ---- aggregate ----
    print("\n=== AGGREGATE (all test windows) ===\n")
    for label in ("baseline", "dynamic_vol", "dynamic_full"):
        agg = aggregate_stats(all_var_trades[label], FOLDS)
        if agg is None:
            print(f"  {label:<14}: 0 trades"); continue
        st = all_var_stats[label]
        print(f"  {label:<14}  n={agg['n']:<4}  win_rate={agg['win']:.3f}  "
              f"exp_R={agg['exp']:+.3f}  trade_sh={agg['sh']:+.3f}  "
              f"ann_ret={agg['ann_ret']:+.2f}%  avg_mult={st['avg_multiplier']:.2f}")
        if agg["trades"]:
            out = f"walkforward_v5_{label}_trades.csv"
            pd.DataFrame([t.__dict__ for t in agg["trades"]]).to_csv(out, index=False)

    print("\nWrote walkforward_v5_fold_table.csv and per-variant trade CSVs.")
    print("\nFinal step:")
    for label in ("baseline", "dynamic_vol", "dynamic_full"):
        print(f"  python bootstrap.py --file walkforward_v5_{label}_trades.csv")


if __name__ == "__main__":
    main()
