"""
Mean-Reversion Strategy Core Logic.
====================================

The contrarian complement to the Donchian trend system. Trades extreme
capitulation/euphoria patterns during macro-OFF and chop regimes.

This module provides the pure strategy logic:
  - MeanReversionConfig dataclass
  - RSI computation (Wilder)
  - Signal generation (long/short entry conditions)
  - Exit logic (mean revert, ATR stop, time stop, profit target)

See MEAN_REVERSION_SPEC.md for the full strategy specification.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


# ============================================================================
# CONFIG
# ============================================================================

@dataclass
class MeanReversionConfig:
    # RSI thresholds — textbook oversold/overbought (RSI<30 / >70 standard
    # in technical analysis literature; restrictive enough to filter noise,
    # common enough to fire meaningfully)
    rsi_period: int = 2
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0

    # FNG (sentiment) thresholds — fear/greed regimes
    fng_extreme_fear: float = 40.0      # "fear" zone of the FNG scale
    fng_extreme_greed: float = 60.0     # "greed" zone

    # Funding thresholds (bps per 8h) — any cross of zero indicates regime tilt
    funding_short_crowded_min: float = 0.0    # funding < 0 → shorts paying longs
    funding_long_crowded_min: float = 10.0    # funding > 10bps → longs paying shorts
    funding_persistence: int = 1               # single reading sufficient

    # Trend filter for "don't catch falling knives"
    long_term_sma_period: int = 200
    stretch_50d_multiplier: float = 1.30        # short only if price > 50d SMA × this

    # Exit parameters
    mean_revert_sma_period: int = 20
    atr_period: int = 14
    atr_stop_mult: float = 1.0                  # tighter than trend (which uses 2.0)
    time_stop_bars: int = 5                     # mean-reversion either works fast or doesn't
    profit_target_r: float = 1.5                # take profit at +1.5R unrealized

    # Sizing
    risk_per_trade: float = 0.0075
    vol_target_annual: float = 0.15

    # Costs (same as trend)
    taker_fee_bps: float = 4.0
    slippage_bps: float = 5.0
    funding_bps_per_day: float = 1.0   # constant fallback if no per-bar data

    # Regime gating
    require_macro_off: bool = False    # default: trade in both regimes
    # If True, MR only fires when BTC macro is OFF (purely complementary to trend)


# ============================================================================
# Indicators
# ============================================================================

def compute_rsi(close: pd.Series, period: int = 2) -> pd.Series:
    """
    Wilder's RSI. Standard formula. With period=2, this is the Connors RSI(2)
    extreme-mean-reversion trigger.
    """
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def compute_sma(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(period).mean()


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """True-range based ATR (Wilder smoothing)."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


# ============================================================================
# Funding persistence check
# ============================================================================

def funding_short_crowded(funding_series: pd.Series, threshold_bps: float,
                          n_consecutive: int) -> pd.Series:
    """
    Returns boolean series: True at index t if funding < threshold for the
    last n_consecutive readings ending at t.
    Negative threshold means shorts paying longs.
    """
    return (funding_series < threshold_bps).rolling(n_consecutive).sum() == n_consecutive


def funding_long_crowded(funding_series: pd.Series, threshold_bps: float,
                         n_consecutive: int) -> pd.Series:
    """True if funding > threshold for n consecutive readings (longs crowded)."""
    return (funding_series > threshold_bps).rolling(n_consecutive).sum() == n_consecutive


# ============================================================================
# Signal generation
# ============================================================================

def build_indicators(df: pd.DataFrame, cfg: MeanReversionConfig) -> pd.DataFrame:
    """Add RSI, SMAs, ATR to the OHLCV DataFrame."""
    out = df.copy()
    out["rsi"] = compute_rsi(out["close"], cfg.rsi_period)
    out["sma_long"] = compute_sma(out["close"], cfg.long_term_sma_period)
    out["sma_50"] = compute_sma(out["close"], 50)
    out["sma_mean"] = compute_sma(out["close"], cfg.mean_revert_sma_period)
    out["atr"] = compute_atr(out, cfg.atr_period)
    return out


def compute_mr_signals(df_with_indicators: pd.DataFrame,
                       fng_series: pd.Series,
                       funding_series: pd.Series,
                       cfg: MeanReversionConfig) -> pd.DataFrame:
    """
    Returns df with added columns:
      mr_long_signal, mr_short_signal (bool)
    All entry conditions evaluated.
    """
    out = df_with_indicators.copy()

    # Align FNG and funding to bar index (forward-fill)
    out["fng"] = fng_series.reindex(out.index, method="ffill").fillna(50.0)
    out["funding_8h"] = funding_series.reindex(out.index, method="ffill").fillna(0.0)

    # Persistence of crowded funding
    out["short_crowded"] = funding_short_crowded(
        out["funding_8h"], cfg.funding_short_crowded_min, cfg.funding_persistence
    )
    out["long_crowded"] = funding_long_crowded(
        out["funding_8h"], cfg.funding_long_crowded_min, cfg.funding_persistence
    )

    # LONG: extreme oversold + extreme fear + crowded shorts + above long-term SMA
    out["mr_long_signal"] = (
        (out["rsi"] < cfg.rsi_oversold)
        & (out["fng"] < cfg.fng_extreme_fear)
        & out["short_crowded"]
        & (out["close"] > out["sma_long"])
    ).fillna(False)

    # SHORT: extreme overbought + extreme greed + crowded longs + stretched above 50d
    out["mr_short_signal"] = (
        (out["rsi"] > cfg.rsi_overbought)
        & (out["fng"] > cfg.fng_extreme_greed)
        & out["long_crowded"]
        & (out["close"] > out["sma_50"] * cfg.stretch_50d_multiplier)
    ).fillna(False)

    return out


# ============================================================================
# Exit logic — pure functions (called by backtest engine)
# ============================================================================

def check_exit(side: int, current_close: float, current_low: float, current_high: float,
               stop: float, entry_price: float, sma_mean: float,
               atr: float, bars_held: int, cfg: MeanReversionConfig,
               r_multiple_so_far: float) -> Optional[tuple[str, float]]:
    """
    Returns (exit_reason, exit_price) if the position should close this bar,
    else None.

    side: +1 for long, -1 for short
    """
    # Hard ATR stop (intra-bar)
    if side > 0 and current_low <= stop:
        return ("atr_stop", stop)
    if side < 0 and current_high >= stop:
        return ("atr_stop", stop)

    # Mean reversion target — price crossed back to SMA20
    if not math.isnan(sma_mean):
        if side > 0 and current_close >= sma_mean:
            return ("mean_revert", current_close)
        if side < 0 and current_close <= sma_mean:
            return ("mean_revert", current_close)

    # Profit target (+1.5R)
    if r_multiple_so_far >= cfg.profit_target_r:
        return ("target_hit", current_close)

    # Time stop
    if bars_held >= cfg.time_stop_bars:
        return ("time_stop", current_close)

    return None


# ============================================================================
# Position sizing (compatible with bt._size_position)
# ============================================================================

def position_size(equity: float, entry_price: float, stop_price: float,
                  atr_value: float, cfg: MeanReversionConfig) -> float:
    """
    Returns position size in base-asset units (positive scalar; sign assigned
    by caller based on side).
    """
    risk_dollars = cfg.risk_per_trade * equity
    per_unit_risk = abs(entry_price - stop_price)
    if per_unit_risk <= 0 or math.isnan(per_unit_risk):
        return 0.0
    size_by_risk = risk_dollars / per_unit_risk

    # Vol cap
    daily_vol = atr_value / entry_price if entry_price > 0 else 0.0
    annual_vol = daily_vol * math.sqrt(365)
    if annual_vol > 0:
        max_notional = (cfg.vol_target_annual / annual_vol) * equity
        size_by_vol = max_notional / entry_price
        return min(size_by_risk, size_by_vol)
    return size_by_risk
