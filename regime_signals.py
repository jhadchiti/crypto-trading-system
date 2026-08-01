"""
Regime detection for dynamic sizing.
=====================================

Computes three regime signals from existing OHLCV + funding data. All are
hand-coded percentile/threshold rules, not ML — robust, explainable, low
overfitting risk.

  1. Vol regime         — BTC 20-day realized vol vs its 180-day rolling
                          percentile. LOW (<33rd) / NORMAL (33-67) / HIGH (>67th).
  2. Correlation regime — average pairwise 30-day return correlation across
                          the symbol universe. DIVERSIFIED (<0.6) / MIXED
                          (0.6-0.85) / CONCENTRATED (>0.85).
  3. Funding regime     — universe-median 7-day funding aggregate.
                          NEGATIVE (<-5bps avg/8h) / NEUTRAL / POSITIVE_HEAVY (>15bps).

All three return pd.Series indexed by date with string state values, ready
to use for sizing decisions.
"""

from __future__ import annotations

from itertools import combinations
from typing import Optional

import numpy as np
import pandas as pd


# ============================================================================
# Constants — chosen from quant-finance literature defaults
# ============================================================================

VOL_LOOKBACK = 20            # bars for realized vol
VOL_PCT_LOOKBACK = 180       # bars for percentile reference
VOL_LOW_PCT = 33.0
VOL_HIGH_PCT = 67.0

CORR_LOOKBACK = 30           # bars for correlation window
CORR_DIVERSIFIED_MAX = 0.6
CORR_CONCENTRATED_MIN = 0.85

FUNDING_LOOKBACK = 7         # days for funding aggregate
FUNDING_NEGATIVE_MAX_BPS = -5.0
FUNDING_POSITIVE_HEAVY_MIN_BPS = 15.0


# ============================================================================
# Vol regime
# ============================================================================

def vol_regime(btc_df: pd.DataFrame,
               vol_lookback: int = VOL_LOOKBACK,
               pct_lookback: int = VOL_PCT_LOOKBACK) -> pd.Series:
    """
    Return BTC vol-regime state per bar: 'LOW', 'NORMAL', or 'HIGH'.

    Uses BTC 20-day annualized realized vol, compared to its own rolling 180-day
    percentile distribution.
    """
    ret = btc_df["close"].pct_change()
    realized_vol = ret.rolling(vol_lookback).std() * np.sqrt(365)

    # Rolling percentile rank
    def pct_rank(x):
        # x is the rolling window; rank of the last value relative to the window
        if len(x) < 2 or pd.isna(x.iloc[-1]):
            return np.nan
        last = x.iloc[-1]
        return float((x <= last).mean()) * 100.0

    pct = realized_vol.rolling(pct_lookback).apply(pct_rank, raw=False)

    def classify(p):
        if pd.isna(p):
            return "NORMAL"
        if p < VOL_LOW_PCT:
            return "LOW"
        if p > VOL_HIGH_PCT:
            return "HIGH"
        return "NORMAL"

    return pct.apply(classify).rename("vol_regime")


# ============================================================================
# Correlation regime
# ============================================================================

def correlation_regime(symbol_data: dict,
                       lookback: int = CORR_LOOKBACK) -> pd.Series:
    """
    Average pairwise rolling correlation across the universe, classified into
    DIVERSIFIED / MIXED / CONCENTRATED. Returns one Series aligned to the
    longest common date index.
    """
    # collect daily returns aligned on union of dates
    rets = {}
    for sym, df in symbol_data.items():
        if not isinstance(df, pd.DataFrame) or df.empty:
            continue
        rets[sym] = df["close"].pct_change()
    if len(rets) < 2:
        return pd.Series(dtype=object, name="corr_regime")

    rets_df = pd.DataFrame(rets).dropna(how="all")
    syms = list(rets_df.columns)
    pairs = list(combinations(syms, 2))
    if not pairs:
        return pd.Series(dtype=object, name="corr_regime")

    # rolling pairwise corr → average (build in dict, single concat to avoid
    # pandas PerformanceWarning about fragmented frame from many .insert calls)
    pair_corrs = {f"{a}__{b}": rets_df[a].rolling(lookback).corr(rets_df[b])
                  for a, b in pairs}
    corr_means = pd.concat(pair_corrs, axis=1)
    avg_corr = corr_means.mean(axis=1)

    def classify(c):
        if pd.isna(c):
            return "MIXED"
        if c < CORR_DIVERSIFIED_MAX:
            return "DIVERSIFIED"
        if c > CORR_CONCENTRATED_MIN:
            return "CONCENTRATED"
        return "MIXED"

    return avg_corr.apply(classify).rename("corr_regime")


# ============================================================================
# Funding regime
# ============================================================================

def funding_regime(funding_by_symbol: dict,
                   bar_index: pd.DatetimeIndex,
                   lookback_days: int = FUNDING_LOOKBACK) -> pd.Series:
    """
    Aggregate funding across the universe: rolling 7-day median of the
    universe-average funding (in bps per 8h), classified into NEGATIVE /
    NEUTRAL / POSITIVE_HEAVY.

    `funding_by_symbol` is a dict mapping symbol -> DataFrame with a
    'funding_bps_8h_last' column (as produced by funding.align_funding_to_bars).
    """
    if not funding_by_symbol:
        return pd.Series("NEUTRAL", index=bar_index, name="funding_regime")

    cols = {}
    for sym, df in funding_by_symbol.items():
        if df is None or "funding_bps_8h_last" not in df.columns:
            continue
        cols[sym] = df["funding_bps_8h_last"].reindex(bar_index)
    if not cols:
        return pd.Series("NEUTRAL", index=bar_index, name="funding_regime")

    fmat = pd.DataFrame(cols).fillna(0.0)
    avg_funding = fmat.mean(axis=1)
    rolling_med = avg_funding.rolling(lookback_days).median()

    def classify(v):
        if pd.isna(v):
            return "NEUTRAL"
        if v < FUNDING_NEGATIVE_MAX_BPS:
            return "NEGATIVE"
        if v > FUNDING_POSITIVE_HEAVY_MIN_BPS:
            return "POSITIVE_HEAVY"
        return "NEUTRAL"

    return rolling_med.apply(classify).rename("funding_regime")


# ============================================================================
# Convenience: compute all three at once
# ============================================================================

def all_regimes(symbol_data: dict,
                funding_by_symbol: Optional[dict] = None,
                btc_symbol: str = "BTCUSDT") -> pd.DataFrame:
    """Returns a DataFrame with columns vol_regime, corr_regime, funding_regime."""
    btc_df = symbol_data.get(btc_symbol)
    if btc_df is None or btc_df.empty:
        return pd.DataFrame()
    vr = vol_regime(btc_df)
    cr = correlation_regime({s: d for s, d in symbol_data.items()
                              if isinstance(d, pd.DataFrame)})
    fr = (funding_regime(funding_by_symbol, btc_df.index)
          if funding_by_symbol is not None
          else pd.Series("NEUTRAL", index=btc_df.index, name="funding_regime"))
    out = pd.DataFrame(index=btc_df.index)
    out["vol_regime"] = vr.reindex(out.index, method="ffill").fillna("NORMAL")
    out["corr_regime"] = cr.reindex(out.index, method="ffill").fillna("MIXED")
    out["funding_regime"] = fr.reindex(out.index, method="ffill").fillna("NEUTRAL")
    return out
