"""
BTC-relative strength filter for entry gating.
==============================================

Motivation
----------
The current strategy takes any breakout that passes filters. Many of these
are "beta breakouts" — the coin is breaking out only because BTC is going up
and everything moves together. These trades have poor forward expectancy
because they're just leveraged BTC exposure with worse slippage.

Solution
--------
At each entry candidate date, rank all candidates by:
    rel_strength(sym) = 30d_return(sym) - 30d_return(BTC)

Take only candidates in the top quintile (top 20%) of relative strength.
This concentrates risk in genuine leaders vs. beta laggards.

Academic support
----------------
Cross-sectional momentum is well-established (Jegadeesh & Titman 1993,
Asness/Moskowitz/Pedersen 2013). In crypto, the anomaly is stronger and
faster-decaying than equities — a ~30d lookback captures it without being
too noisy.

Public interface
----------------
    rel_strength(symbol_df, btc_df, lookback=30) -> pd.Series
        Returns per-bar relative strength (positive = alt beats BTC)

    is_top_quintile(rs_by_symbol_at_date, symbol) -> bool
        Is this symbol in the top 20% of relative strength today?

    build_rs_matrix(data, lookback=30) -> pd.DataFrame
        Precompute a DataFrame indexed by date, cols per symbol, values = rel_strength.

    top_quintile_gate(rs_matrix, date, symbol, k=0.2) -> bool
        Point-in-time check: is `symbol` in top-k share of relative strength on `date`?
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


DEFAULT_LOOKBACK = 30
DEFAULT_TOP_FRACTION = 0.20   # top 20% = top-quintile


def rel_strength(symbol_df: pd.DataFrame, btc_df: pd.DataFrame,
                 lookback: int = DEFAULT_LOOKBACK) -> pd.Series:
    """
    Per-bar relative strength of a single symbol vs BTC.

    Positive = symbol outperformed BTC over the lookback window.
    Aligns BTC to symbol's index; symbol values missing from BTC forward-fill.
    """
    s_ret = symbol_df["close"].pct_change(lookback)
    btc_close = btc_df["close"].reindex(symbol_df.index, method="ffill")
    b_ret = btc_close.pct_change(lookback)
    return (s_ret - b_ret).astype(float)


def build_rs_matrix(data: dict, lookback: int = DEFAULT_LOOKBACK) -> pd.DataFrame:
    """
    Build a matrix of relative strength values across the universe.

    Args:
        data: dict of symbol -> OHLCV DataFrame (must contain 'close', UTC-indexed)
        lookback: window in bars

    Returns:
        DataFrame indexed by date, one column per symbol.
        Values are (symbol_return - btc_return) over the trailing `lookback` days.
        BTCUSDT column exists but will be all zeros (BTC's rel-strength vs itself).
    """
    if "BTCUSDT" not in data:
        raise ValueError("BTCUSDT is required for relative-strength computation")
    btc = data["BTCUSDT"]

    cols = {}
    for sym, df in data.items():
        if df is None or df.empty:
            continue
        cols[sym] = rel_strength(df, btc, lookback)
    return pd.DataFrame(cols).sort_index()


def top_quintile_gate(rs_matrix: pd.DataFrame, date: pd.Timestamp,
                      symbol: str,
                      active_universe: Optional[set] = None,
                      top_fraction: float = DEFAULT_TOP_FRACTION,
                      exclude_btc: bool = True) -> bool:
    """
    Return True if `symbol` is in the top `top_fraction` share of relative
    strength on `date`, among the `active_universe`.

    Args:
        rs_matrix: DataFrame from build_rs_matrix()
        date: the entry candidate date
        symbol: the symbol being considered
        active_universe: set of symbols eligible for ranking on this date
                         (default: all non-null columns)
        top_fraction: 0.2 = top 20% (top-quintile)
        exclude_btc: BTC always passes (it's the benchmark; not filtered)

    BTCUSDT is exempt from this filter — it's the benchmark and always eligible
    if its own signal fires.
    """
    if exclude_btc and symbol == "BTCUSDT":
        return True

    if rs_matrix.empty:
        return True  # fail-open: no matrix, don't block

    # Get row at date (use last available if date not exact)
    try:
        row = rs_matrix.loc[date]
    except KeyError:
        idx = rs_matrix.index[rs_matrix.index <= date]
        if len(idx) == 0:
            return True
        row = rs_matrix.loc[idx[-1]]

    row = row.dropna()
    if active_universe is not None:
        row = row[[c for c in row.index if c in active_universe]]

    if len(row) < 3:
        # too few candidates to rank; fail-open
        return True

    # Exclude BTC from the ranking universe (it's the benchmark)
    if exclude_btc and "BTCUSDT" in row.index:
        row = row.drop("BTCUSDT")

    if symbol not in row.index:
        return False

    threshold = row.quantile(1 - top_fraction)
    return float(row[symbol]) >= float(threshold)


def build_top_quintile_series(rs_matrix: pd.DataFrame, symbol: str,
                              active_universe_at: Optional[dict] = None,
                              top_fraction: float = DEFAULT_TOP_FRACTION,
                              exclude_btc: bool = True) -> pd.Series:
    """
    Vectorized: build a bool series indexed by date indicating whether `symbol`
    passes the top-quintile gate on each date.

    Args:
        rs_matrix: matrix from build_rs_matrix()
        symbol: symbol to check
        active_universe_at: optional dict of date -> set of eligible symbols.
                            If None, uses all non-null columns on each date.
        top_fraction: quintile size (0.2 default)
        exclude_btc: BTC bypasses the filter and always returns True

    Returns:
        pd.Series of bool, indexed by rs_matrix.index.
    """
    if exclude_btc and symbol == "BTCUSDT":
        return pd.Series(True, index=rs_matrix.index)

    if symbol not in rs_matrix.columns:
        return pd.Series(False, index=rs_matrix.index)

    out = pd.Series(False, index=rs_matrix.index)

    for date in rs_matrix.index:
        row = rs_matrix.loc[date].dropna()
        if active_universe_at is not None:
            uni = active_universe_at.get(date, None)
            if uni is not None:
                row = row[[c for c in row.index if c in uni]]
        if exclude_btc and "BTCUSDT" in row.index:
            row = row.drop("BTCUSDT")
        if len(row) < 3 or symbol not in row.index:
            continue
        threshold = row.quantile(1 - top_fraction)
        if float(row[symbol]) >= float(threshold):
            out.loc[date] = True

    return out


# ============================================================================
# Vol-scaled sizing helper (also part of the improvement set)
# ============================================================================

def volatility_scalar(realized_vol_annual: float, target_vol: float = 0.60,
                      floor: float = 0.5, cap: float = 1.5) -> float:
    """
    Scalar to apply to risk_per_trade based on realized annualized volatility.

    When realized vol is LOW (calm regime, often pre-trend), scale UP.
    When realized vol is HIGH (chop/panic), scale DOWN.

    Args:
        realized_vol_annual: e.g. 0.80 = 80% annualized
        target_vol: what vol we want to be exposed to (0.60 = 60% ann)
        floor: minimum scalar (0.5x baseline risk)
        cap: maximum scalar (1.5x baseline risk)

    Returns:
        multiplier for risk_per_trade
    """
    if realized_vol_annual is None or realized_vol_annual <= 0:
        return 1.0
    raw = target_vol / realized_vol_annual
    return max(floor, min(cap, raw))


def compute_realized_vol(close_series: pd.Series, window: int = 20) -> pd.Series:
    """
    Annualized realized volatility from log returns.

    Args:
        close_series: pd.Series of daily close prices
        window: lookback in bars (daily data → 20 = ~1 month)

    Returns:
        pd.Series of annualized vol (e.g. 0.60 = 60%)
    """
    log_ret = np.log(close_series / close_series.shift(1))
    return log_ret.rolling(window).std() * np.sqrt(365.0)


# ============================================================================
# Early-listing bias (also part of the improvement set)
# ============================================================================

def listing_age_days(symbol_df: pd.DataFrame, date: pd.Timestamp) -> Optional[int]:
    """Days since first bar for this symbol as of `date`."""
    if symbol_df is None or symbol_df.empty:
        return None
    first = symbol_df.index[0]
    return int((date - first).days)


def early_listing_boost(age_days: int, cutoff_days: int = 730,
                        max_boost: float = 1.25) -> float:
    """
    Scoring boost for coins listed less than `cutoff_days` on Binance.

    Newly listed coins historically show stronger trends (IPO-drift analog).
    Coins older than cutoff → boost = 1.0 (no adjustment).
    Newest coin (age 0) → boost = max_boost.
    Linear interpolation between.
    """
    if age_days is None or age_days >= cutoff_days:
        return 1.0
    if age_days < 0:
        return 1.0
    ratio = 1.0 - (age_days / cutoff_days)
    return 1.0 + (max_boost - 1.0) * ratio
