"""
Multi-Timeframe Structural Continuation — Backtest Skeleton (v0.1)
==================================================================

Single-file, runnable scaffold. Pulls Daily OHLCV from Binance public REST,
builds the structural trendlines deterministically, generates signals on
Daily close, sizes positions to a risk-per-trade target with a vol cap,
runs a simple bar-by-bar backtest, and prints a metrics table.

This is a STARTING POINT. It is intentionally not optimized. The point is
that every rule lives in code and can be A/B tested by flipping parameters.

Dependencies:
    pip install pandas numpy requests

Run:
    python mtf_structural_backtest.py
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
import requests


# ============================================================================
# CONFIG
# ============================================================================

@dataclass
class Config:
    # Universe
    symbols: tuple = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")
    interval: str = "1d"                      # Daily bars
    start_date: str = "2019-09-01"
    end_date: Optional[str] = None            # None = up to now

    # Trendline construction
    anchor_window: int = 180                  # bars to search for extremum
    anchor_offset_n: int = 3                  # bars between anchor 1 and 2
    min_bars_between_resets: int = 5

    # Signal
    atr_period: int = 14
    break_k_atr: float = 0.25                 # close must exceed line by k*ATR
    sma_period: int = 50
    sma_slope_lookback: int = 10

    # Exits
    initial_stop_atr_mult: float = 1.5
    trail_atr_mult: float = 2.0
    time_stop_bars: int = 60

    # Sizing
    risk_per_trade: float = 0.0075            # 0.75% of equity at risk
    vol_target_annual: float = 0.15           # 15% per-position notional vol cap
    portfolio_heat_cap: float = 0.03          # 3% summed open risk

    # Costs
    taker_fee_bps: float = 4.0                # per side
    slippage_bps: float = 5.0                 # per side
    # Funding is asset-and-period dependent. v0.1 placeholder = constant.
    funding_bps_per_day: float = 1.0          # ~3 bps/8h average longs pay; tune later

    # Backtest
    starting_equity: float = 100_000.0

    # OOS split
    oos_start: str = "2024-01-01"

    # Filter toggles (for ablation studies)
    use_sma_filter: bool = True               # require SMA slope alignment
    require_prev_close_outside: bool = True   # prev close must be at/beyond line
    use_structural_invalidation: bool = True  # exit on close through opposite line

    # Funding filter (only effective when per-bar funding column is supplied)
    use_funding_filter: bool = False
    funding_filter_max_bps_8h: float = 20.0   # skip longs if last 8h funding > this

    # Invert mode — flip long/short signals (test the contrarian hypothesis).
    invert_signals: bool = False


CFG = Config()


# ============================================================================
# DATA FETCH
# ============================================================================

KLINES_PATH = "/fapi/v1/klines"   # USD-M perp klines


def fetch_binance_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Page through Binance klines (1000 per call) and return a DataFrame.
    Uses net_utils.fetch_binance_futures which rotates across fapi/fapi1/fapi2/fapi3
    on 451 (geo-block) responses and attaches a realistic browser User-Agent to
    bypass Cloudflare bot detection."""
    from net_utils import fetch_binance_futures
    rows = []
    cursor = start_ms
    while cursor < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1000,
        }
        r = fetch_binance_futures(KLINES_PATH, params=params, timeout=20)
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        last_open = batch[-1][0]
        # advance one bar past the last open time
        cursor = last_open + 1
        if len(batch) < 1000:
            break
        time.sleep(0.15)  # be polite

    if not rows:
        return pd.DataFrame()

    cols = ["open_time", "open", "high", "low", "close", "volume",
            "close_time", "qav", "n_trades", "taker_buy_base",
            "taker_buy_quote", "ignore"]
    df = pd.DataFrame(rows, columns=cols)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df["date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df[["date", "open", "high", "low", "close", "volume"]].set_index("date")
    df = df[~df.index.duplicated(keep="first")].sort_index()
    return df


def load_universe(cfg: Config) -> dict[str, pd.DataFrame]:
    start_ms = int(pd.Timestamp(cfg.start_date, tz="UTC").timestamp() * 1000)
    end_ms = int((pd.Timestamp(cfg.end_date, tz="UTC") if cfg.end_date
                  else pd.Timestamp.now(tz="UTC")).timestamp() * 1000)
    out = {}
    for s in cfg.symbols:
        print(f"  fetching {s} ...")
        df = fetch_binance_klines(s, cfg.interval, start_ms, end_ms)
        if df.empty:
            print(f"  WARN: no data for {s}")
            continue
        out[s] = df
    return out


# ============================================================================
# INDICATORS
# ============================================================================

def atr(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def sma(s: pd.Series, period: int) -> pd.Series:
    return s.rolling(period).mean()


# ============================================================================
# STRUCTURAL TRENDLINES
# ============================================================================

@dataclass
class Anchors:
    """One pair of (index, log-price) anchors plus the resulting line params."""
    a_idx: int
    a_logp: float
    b_idx: int
    b_logp: float
    slope: float    # in log-price per bar
    intercept: float  # at a_idx

    def value_at(self, idx: int) -> float:
        """Return line value (in log-price) at the given bar index."""
        return self.intercept + self.slope * (idx - self.a_idx)


def build_lines(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """
    For each bar, compute the current upper (bullish-continuation) and lower
    (bearish-continuation) line values in price space.

    Upper line:  anchored at recent high close + close N bars after.
    Lower line:  anchored at recent low close + close N bars after.

    Both are recomputed when a new extremum prints (with minimum spacing).
    """
    logp = np.log(df["close"].values)
    n = len(df)

    upper_vals = np.full(n, np.nan)
    lower_vals = np.full(n, np.nan)

    upper: Optional[Anchors] = None
    lower: Optional[Anchors] = None
    last_upper_reset = -10**9
    last_lower_reset = -10**9

    for i in range(n):
        win_start = max(0, i - cfg.anchor_window + 1)
        window_logp = logp[win_start:i + 1]

        # ----- Upper line (anchored at recent HIGH close) -----
        local_high_rel = int(np.argmax(window_logp))
        local_high_abs = win_start + local_high_rel
        if (local_high_abs + cfg.anchor_offset_n <= i
                and (i - last_upper_reset) >= cfg.min_bars_between_resets):
            a_idx = local_high_abs
            b_idx = local_high_abs + cfg.anchor_offset_n
            a_logp, b_logp = logp[a_idx], logp[b_idx]
            if b_logp < a_logp:  # sanity: line must slope down from the high
                slope = (b_logp - a_logp) / (b_idx - a_idx)
                new_line = Anchors(a_idx, a_logp, b_idx, b_logp, slope, a_logp)
                if upper is None or new_line.a_idx != upper.a_idx:
                    upper = new_line
                    last_upper_reset = i

        # ----- Lower line (anchored at recent LOW close) -----
        local_low_rel = int(np.argmin(window_logp))
        local_low_abs = win_start + local_low_rel
        if (local_low_abs + cfg.anchor_offset_n <= i
                and (i - last_lower_reset) >= cfg.min_bars_between_resets):
            a_idx = local_low_abs
            b_idx = local_low_abs + cfg.anchor_offset_n
            a_logp, b_logp = logp[a_idx], logp[b_idx]
            if b_logp > a_logp:  # sanity: line must slope up from the low
                slope = (b_logp - a_logp) / (b_idx - a_idx)
                new_line = Anchors(a_idx, a_logp, b_idx, b_logp, slope, a_logp)
                if lower is None or new_line.a_idx != lower.a_idx:
                    lower = new_line
                    last_lower_reset = i

        if upper is not None:
            upper_vals[i] = math.exp(upper.value_at(i))
        if lower is not None:
            lower_vals[i] = math.exp(lower.value_at(i))

    out = df.copy()
    out["upper_line"] = upper_vals
    out["lower_line"] = lower_vals
    return out


# ============================================================================
# SIGNALS
# ============================================================================

def generate_signals(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Add ATR, SMA-slope, and binary entry signals."""
    df = df.copy()
    df["atr"] = atr(df, cfg.atr_period)
    df["sma"] = sma(df["close"], cfg.sma_period)
    df["sma_slope_up"] = df["sma"] > df["sma"].shift(cfg.sma_slope_lookback)
    df["sma_slope_dn"] = df["sma"] < df["sma"].shift(cfg.sma_slope_lookback)

    close = df["close"]
    prev_close = close.shift(1)

    # Long: today closes above lower line by k*ATR.
    long_break = (close > df["lower_line"] + cfg.break_k_atr * df["atr"])
    if cfg.require_prev_close_outside:
        long_break = long_break & (prev_close <= df["lower_line"].shift(1))
    if cfg.use_sma_filter:
        long_break = long_break & df["sma_slope_up"]

    # Short: today closes below upper line by k*ATR.
    short_break = (close < df["upper_line"] - cfg.break_k_atr * df["atr"])
    if cfg.require_prev_close_outside:
        short_break = short_break & (prev_close >= df["upper_line"].shift(1))
    if cfg.use_sma_filter:
        short_break = short_break & df["sma_slope_dn"]

    # Funding-aware entry filter (only applies if the column exists)
    if cfg.use_funding_filter and "funding_bps_8h_last" in df.columns:
        fund = df["funding_bps_8h_last"]
        long_break = long_break & (fund <= cfg.funding_filter_max_bps_8h)
        short_break = short_break & (fund >= -cfg.funding_filter_max_bps_8h)

    long_sig = long_break.fillna(False)
    short_sig = short_break.fillna(False)
    if cfg.invert_signals:
        df["signal_long"] = short_sig
        df["signal_short"] = long_sig
    else:
        df["signal_long"] = long_sig
        df["signal_short"] = short_sig
    return df


# ============================================================================
# BACKTEST
# ============================================================================

@dataclass
class Position:
    symbol: str
    side: int            # +1 long, -1 short
    entry_date: pd.Timestamp
    entry_price: float
    size: float          # units of base asset (signed)
    stop: float
    initial_stop: float
    risk_dollars: float
    bars_held: int = 0
    high_since_entry: float = field(default=0.0)
    low_since_entry: float = field(default=float("inf"))
    reached_1r: bool = False
    reached_2r: bool = False


@dataclass
class Trade:
    symbol: str
    side: int
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    size: float
    pnl_gross: float
    pnl_net: float
    r_multiple: float
    exit_reason: str
    bars_held: int


def _round_trip_cost_bps(cfg: Config) -> float:
    return 2 * (cfg.taker_fee_bps + cfg.slippage_bps)


def _size_position(equity: float, entry: float, stop: float, atr_val: float,
                   cfg: Config) -> float:
    """Return signed-magnitude size in base-asset units (caller assigns sign)."""
    risk_dollars = cfg.risk_per_trade * equity
    per_unit_risk = abs(entry - stop)
    if per_unit_risk <= 0 or math.isnan(per_unit_risk):
        return 0.0
    size_by_risk = risk_dollars / per_unit_risk

    # Vol cap: cap notional s.t. annualized notional vol <= target.
    # Approximate daily vol of asset = ATR / close (rough).
    daily_vol = atr_val / entry if entry > 0 else 0.0
    annual_vol = daily_vol * math.sqrt(365)
    if annual_vol > 0:
        max_notional = (cfg.vol_target_annual / annual_vol) * equity
        size_by_vol = max_notional / entry
        return min(size_by_risk, size_by_vol)
    return size_by_risk


def backtest_symbol(df: pd.DataFrame, symbol: str, equity_ref: list[float],
                    cfg: Config,
                    funding_bps_by_bar: Optional[pd.Series] = None
                    ) -> tuple[list[Trade], pd.Series]:
    """
    Run the strategy on a single symbol. `equity_ref` is a 1-element list so
    callers can share/update equity across symbols if they want to.

    If `funding_bps_by_bar` is provided, it should be a Series indexed by the
    same bar timestamps as `df`, giving per-bar realized funding in bps.
    Otherwise the constant cfg.funding_bps_per_day is used (Daily fallback).
    """
    trades: list[Trade] = []
    pos: Optional[Position] = None

    equity = equity_ref[0]
    eq_curve = []

    for i, (date, row) in enumerate(df.iterrows()):
        # ---- Mark to market existing position ----
        if pos is not None:
            pos.bars_held += 1
            pos.high_since_entry = max(pos.high_since_entry, row["close"])
            pos.low_since_entry = min(pos.low_since_entry, row["close"])

            # Funding carry — use per-bar lookup if available, else constant.
            if funding_bps_by_bar is not None:
                try:
                    bar_bps = float(funding_bps_by_bar.loc[date])
                except KeyError:
                    bar_bps = 0.0
            else:
                bar_bps = cfg.funding_bps_per_day
            funding_drag = (bar_bps / 10000.0) * abs(pos.size) * row["close"]
            equity -= funding_drag if pos.side > 0 else -funding_drag  # shorts earn it on net long-funded markets

            exit_reason = None
            exit_price = None

            # Stop hit?
            if pos.side > 0 and row["low"] <= pos.stop:
                exit_price = pos.stop
                exit_reason = "stop"
            elif pos.side < 0 and row["high"] >= pos.stop:
                exit_price = pos.stop
                exit_reason = "stop"

            # Structural invalidation (close beyond opposite line)
            if (exit_reason is None and cfg.use_structural_invalidation
                    and not math.isnan(row["upper_line"]) and not math.isnan(row["lower_line"])):
                if pos.side > 0 and row["close"] < row["upper_line"]:
                    exit_price = row["close"]
                    exit_reason = "structural_invalidation"
                elif pos.side < 0 and row["close"] > row["lower_line"]:
                    exit_price = row["close"]
                    exit_reason = "structural_invalidation"

            # Time stop
            if exit_reason is None and pos.bars_held >= cfg.time_stop_bars:
                exit_price = row["close"]
                exit_reason = "time_stop"

            # Apply trailing-stop logic for the NEXT bar (don't exit on it this bar)
            if exit_reason is None:
                r_move = (row["close"] - pos.entry_price) * pos.side / abs(pos.entry_price - pos.initial_stop)
                if not pos.reached_1r and r_move >= 1.0:
                    pos.stop = pos.entry_price  # breakeven
                    pos.reached_1r = True
                if not pos.reached_2r and r_move >= 2.0:
                    pos.reached_2r = True
                if pos.reached_2r and not math.isnan(row["atr"]):
                    if pos.side > 0:
                        pos.stop = max(pos.stop, pos.high_since_entry - cfg.trail_atr_mult * row["atr"])
                    else:
                        pos.stop = min(pos.stop, pos.low_since_entry + cfg.trail_atr_mult * row["atr"])

            if exit_reason is not None:
                cost = (_round_trip_cost_bps(cfg) / 10000.0) * abs(pos.size) * exit_price
                gross = (exit_price - pos.entry_price) * pos.size  # size already signed
                net = gross - cost
                equity += net
                r_mult = net / pos.risk_dollars if pos.risk_dollars > 0 else 0.0
                trades.append(Trade(
                    symbol=symbol, side=pos.side,
                    entry_date=pos.entry_date, exit_date=date,
                    entry_price=pos.entry_price, exit_price=exit_price,
                    size=pos.size, pnl_gross=gross, pnl_net=net,
                    r_multiple=r_mult, exit_reason=exit_reason,
                    bars_held=pos.bars_held,
                ))
                pos = None

        # ---- New entry? ----
        if pos is None:
            if row["signal_long"] and not math.isnan(row["atr"]):
                entry = row["close"]
                stop = entry - cfg.initial_stop_atr_mult * row["atr"]
                size = _size_position(equity, entry, stop, row["atr"], cfg)
                if size > 0:
                    pos = Position(
                        symbol=symbol, side=+1,
                        entry_date=date, entry_price=entry,
                        size=size, stop=stop, initial_stop=stop,
                        risk_dollars=size * (entry - stop),
                        high_since_entry=entry, low_since_entry=entry,
                    )
            elif row["signal_short"] and not math.isnan(row["atr"]):
                entry = row["close"]
                stop = entry + cfg.initial_stop_atr_mult * row["atr"]
                size = _size_position(equity, entry, stop, row["atr"], cfg)
                if size > 0:
                    pos = Position(
                        symbol=symbol, side=-1,
                        entry_date=date, entry_price=entry,
                        size=-size, stop=stop, initial_stop=stop,
                        risk_dollars=size * (stop - entry),
                        high_since_entry=entry, low_since_entry=entry,
                    )

        eq_curve.append(equity)

    equity_ref[0] = equity
    return trades, pd.Series(eq_curve, index=df.index, name=symbol)


# ============================================================================
# METRICS
# ============================================================================

def compute_metrics(trades: list[Trade], equity_curve: pd.Series,
                    starting_equity: float) -> dict:
    if not trades:
        return {"trade_count": 0}

    rs = np.array([t.r_multiple for t in trades])
    wins = rs[rs > 0]
    losses = rs[rs <= 0]

    daily_returns = equity_curve.pct_change().dropna()
    ann_factor = math.sqrt(365)
    sharpe = (daily_returns.mean() / daily_returns.std()) * ann_factor if daily_returns.std() > 0 else float("nan")

    final_eq = equity_curve.iloc[-1]
    years = (equity_curve.index[-1] - equity_curve.index[0]).days / 365.25
    cagr = (final_eq / starting_equity) ** (1 / years) - 1 if years > 0 and final_eq > 0 else float("nan")

    rolling_peak = equity_curve.cummax()
    drawdown = (equity_curve - rolling_peak) / rolling_peak
    max_dd = drawdown.min()

    gross_win = sum(t.pnl_net for t in trades if t.pnl_net > 0)
    gross_loss = -sum(t.pnl_net for t in trades if t.pnl_net <= 0)
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")

    return {
        "trade_count": len(trades),
        "hit_rate": float(np.mean(rs > 0)),
        "avg_win_R": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss_R": float(losses.mean()) if len(losses) else 0.0,
        "expectancy_R": float(rs.mean()),
        "CAGR": cagr,
        "ann_vol": float(daily_returns.std() * ann_factor),
        "sharpe": sharpe,
        "max_drawdown": float(max_dd),
        "calmar": cagr / abs(max_dd) if max_dd < 0 else float("nan"),
        "profit_factor": pf,
        "avg_bars_held": float(np.mean([t.bars_held for t in trades])),
    }


def print_metrics_table(per_symbol: dict[str, dict]):
    if not per_symbol:
        print("No metrics to display.")
        return
    keys = ["trade_count", "hit_rate", "expectancy_R", "CAGR", "sharpe",
            "max_drawdown", "calmar", "profit_factor", "avg_bars_held"]
    header = f"{'symbol':<10} " + " ".join(f"{k:>14}" for k in keys)
    print("\n" + header)
    print("-" * len(header))
    for sym, m in per_symbol.items():
        vals = []
        for k in keys:
            v = m.get(k)
            if v is None or (isinstance(v, float) and math.isnan(v)):
                vals.append(f"{'n/a':>14}")
            elif isinstance(v, float):
                vals.append(f"{v:>14.3f}")
            else:
                vals.append(f"{v:>14}")
        print(f"{sym:<10} " + " ".join(vals))


# ============================================================================
# MAIN
# ============================================================================

def run(cfg: Config = CFG, data: Optional[dict[str, pd.DataFrame]] = None,
        funding_by_symbol: Optional[dict[str, pd.DataFrame]] = None,
        verbose: bool = True, write_csv: bool = True) -> dict:
    """
    Run the backtest end-to-end.

    Returns a dict with keys: 'metrics', 'trades', 'equity_curves', 'signal_frames'.

    `data`: optional pre-loaded OHLCV dict keyed by symbol.
    `funding_by_symbol`: optional dict of funding DataFrames per symbol with
        columns 'funding_bps_in_bar' (used by backtest carry) and
        'funding_bps_8h_last' (used by entry filter when use_funding_filter=True).
    """
    if data is None:
        if verbose:
            print("Loading universe ...")
        data = load_universe(cfg)
        if not data:
            if verbose:
                print("No data loaded — exiting.")
            return {"metrics": {}, "trades": [], "equity_curves": {}, "signal_frames": {}}

    per_symbol_metrics: dict[str, dict] = {}
    all_trades: list[Trade] = []
    equity_curves: dict[str, pd.Series] = {}
    signal_frames: dict[str, pd.DataFrame] = {}

    per_symbol_equity = cfg.starting_equity / len(data)

    for sym, df in data.items():
        if verbose:
            print(f"\n=== {sym} ===")
        df = build_lines(df, cfg)
        # Splice funding columns onto df BEFORE signal generation so the
        # entry-filter sees them.
        funding_bps_by_bar = None
        if funding_by_symbol is not None and sym in funding_by_symbol:
            fdf = funding_by_symbol[sym]
            df = df.join(fdf, how="left").fillna({"funding_bps_in_bar": 0.0,
                                                   "funding_bps_8h_last": 0.0})
            funding_bps_by_bar = df["funding_bps_in_bar"]
        df = generate_signals(df, cfg)
        signal_frames[sym] = df
        equity_ref = [per_symbol_equity]
        trades, eq = backtest_symbol(df, sym, equity_ref, cfg,
                                     funding_bps_by_bar=funding_bps_by_bar)
        all_trades.extend(trades)
        equity_curves[sym] = eq
        per_symbol_metrics[sym] = compute_metrics(trades, eq, per_symbol_equity)
        if verbose:
            print(f"  trades: {len(trades)}, final equity: ${eq.iloc[-1]:,.0f}")

    port = pd.concat(equity_curves.values(), axis=1).ffill().sum(axis=1)
    port.name = "PORTFOLIO"
    per_symbol_metrics["PORTFOLIO"] = compute_metrics(all_trades, port, cfg.starting_equity)
    equity_curves["PORTFOLIO"] = port

    if verbose:
        print_metrics_table(per_symbol_metrics)
        oos = pd.Timestamp(cfg.oos_start, tz="UTC")
        is_trades = [t for t in all_trades if t.exit_date < oos]
        oos_trades = [t for t in all_trades if t.exit_date >= oos]
        print(f"\nIn-sample trades:  {len(is_trades)}  (through {cfg.oos_start})")
        print(f"Out-of-sample trades: {len(oos_trades)}")

    if write_csv and all_trades:
        trades_df = pd.DataFrame([t.__dict__ for t in all_trades])
        out_path = "trades.csv"
        trades_df.to_csv(out_path, index=False)
        if verbose:
            print(f"\nWrote {len(all_trades)} trades to {out_path}")

    return {
        "metrics": per_symbol_metrics,
        "trades": all_trades,
        "equity_curves": equity_curves,
        "signal_frames": signal_frames,
    }


if __name__ == "__main__":
    run()
