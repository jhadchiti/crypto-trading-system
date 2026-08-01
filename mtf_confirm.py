"""
Multi-timeframe confirmation gate.
==================================

Motivation
----------
A Daily-bar Donchian breakout is a local signal. Some of these fire on days
when the WEEKLY structure is still in a downtrend or a consolidation — those
are the fake breakouts that fail fastest.

Adding a Weekly-scale confirmation ("weekly close is also above the weekly
55-week high") filters out these premature entries. Only take a Daily
breakout when the Weekly is confirming the same direction.

The trade-off is obvious: some genuine early trends will be missed while
the Weekly catches up. Backtest tells us whether the improved quality of
kept trades outweighs the missed opportunities.

Design
------
For each symbol:
    - Compute weekly OHLC by resampling Daily
    - Compute weekly Donchian entry channel (default 55 weeks... too long)
      Better: use a shorter weekly window (e.g. 20 weeks = ~5 months)
    - At each Daily bar, look at the LAST COMPLETED weekly bar
      (avoids lookahead within the current week)
    - Gate: weekly_last_close > weekly_20week_high  → allow longs
             weekly_last_close < weekly_20week_low   → allow shorts

Public interface
----------------
    resample_to_weekly(df) -> pd.DataFrame
        Convert daily OHLCV to weekly (W-MON aligned).

    build_weekly_donchian(daily_df, n_entry=20) -> pd.DataFrame
        Compute weekly Donchian entry channels for the given daily series.

    mtf_confirm(daily_df, weekly_donchian, date, side='long') -> bool
        At the given daily bar date, is the weekly channel confirming
        the direction? Uses the LAST COMPLETED weekly bar to avoid lookahead.

    build_mtf_series(daily_df, n_entry=20) -> tuple[pd.Series, pd.Series]
        Vectorized: returns (long_ok_series, short_ok_series) indexed by daily bars.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


DEFAULT_WEEKLY_ENTRY_N = 20   # 20 weeks ≈ 5 months of weekly Donchian


def resample_to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert a daily OHLCV DataFrame to weekly bars. Uses W-MON aligned weeks
    (week ends on Sunday, weekly bar timestamp = Monday of that week).

    Requires columns: open, high, low, close, volume. UTC-indexed.

    Returns weekly DataFrame with same columns.
    """
    if df.empty:
        return df.copy()

    agg = {
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }
    # W-MON = week starts Monday; label='left' means bar timestamp = start of week
    # closed='left' means each week includes Mon 00:00 through Sun 23:59
    w = df.resample("W-MON", label="left", closed="left").agg(agg)
    w = w.dropna(subset=["close"])
    return w


def build_weekly_donchian(daily_df: pd.DataFrame,
                          n_entry: int = DEFAULT_WEEKLY_ENTRY_N) -> pd.DataFrame:
    """
    From a daily OHLCV DataFrame, build the weekly Donchian entry channel.

    Args:
        daily_df: daily OHLCV
        n_entry: weekly lookback for the Donchian entry channel

    Returns:
        Weekly DataFrame with added columns:
          - weekly_entry_high: rolling n_entry-week max of close (SHIFTED by 1)
          - weekly_entry_low:  rolling n_entry-week min of close (SHIFTED by 1)
        The shift(1) makes it "prior weeks' channel" — no lookahead.
    """
    w = resample_to_weekly(daily_df)
    if w.empty:
        return w
    w = w.copy()
    w["weekly_entry_high"] = w["close"].rolling(n_entry).max().shift(1)
    w["weekly_entry_low"]  = w["close"].rolling(n_entry).min().shift(1)
    return w


def build_mtf_series(daily_df: pd.DataFrame,
                     n_entry: int = DEFAULT_WEEKLY_ENTRY_N,
                     ) -> tuple[pd.Series, pd.Series]:
    """
    Vectorized MTF confirmation lookup.

    Returns:
        long_ok:  pd.Series[bool], indexed by daily_df.index. True when the
                  LAST COMPLETED weekly bar has close > weekly_entry_high.
        short_ok: pd.Series[bool], mirror for shorts.

    Uses the last COMPLETED weekly bar (not the current partial week),
    preventing any within-week lookahead bias.
    """
    if daily_df.empty:
        return (pd.Series(dtype=bool), pd.Series(dtype=bool))

    weekly = build_weekly_donchian(daily_df, n_entry)
    if weekly.empty:
        idx = daily_df.index
        return (pd.Series(False, index=idx), pd.Series(False, index=idx))

    # For each daily date, find the LAST WEEKLY BAR that ENDED before this date.
    # "Ended before" means its (bar_ts + 7 days) <= daily_date.
    daily_idx = daily_df.index
    long_ok = pd.Series(False, index=daily_idx)
    short_ok = pd.Series(False, index=daily_idx)

    # Precompute the weekly bar end-times (bar_ts + 7 days)
    week_end_times = weekly.index + pd.Timedelta(days=7)

    for d in daily_idx:
        # Weekly bars whose end-time is <= d (they've fully closed before d)
        mask = week_end_times <= d
        if not mask.any():
            continue
        last_weekly_row = weekly.iloc[mask.argmin() - 1] if mask.all() else weekly[mask].iloc[-1]
        # ^ If ALL past, take the last one; otherwise take the last True index
        if mask.all():
            last_weekly_row = weekly.iloc[-1]
        else:
            # Find last index where mask is True
            last_true_idx = np.where(mask)[0]
            if len(last_true_idx) == 0:
                continue
            last_weekly_row = weekly.iloc[last_true_idx[-1]]

        w_close = last_weekly_row["close"]
        w_high = last_weekly_row["weekly_entry_high"]
        w_low = last_weekly_row["weekly_entry_low"]

        if not (np.isnan(w_high) or np.isnan(w_close)):
            if w_close > w_high:
                long_ok.loc[d] = True

        if not (np.isnan(w_low) or np.isnan(w_close)):
            if w_close < w_low:
                short_ok.loc[d] = True

    return (long_ok, short_ok)


def mtf_confirm(long_ok_series: pd.Series, short_ok_series: pd.Series,
                date: pd.Timestamp, side: str = "long") -> bool:
    """
    Point-in-time check: is the weekly confirming the trade direction?

    Args:
        long_ok_series, short_ok_series: from build_mtf_series()
        date: entry candidate date
        side: 'long' or 'short'
    """
    if side == "long":
        try:
            return bool(long_ok_series.loc[date])
        except KeyError:
            return False
    else:
        try:
            return bool(short_ok_series.loc[date])
        except KeyError:
            return False
