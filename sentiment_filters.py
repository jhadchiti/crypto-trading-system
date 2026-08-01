"""
Sentiment-aware filters for the Donchian strategy.
===================================================

Provides two free, low-noise sentiment signals:

  1. Fear & Greed Index — daily 0-100 score from alternative.me. Used as an
     extreme-sentiment circuit breaker:
        FNG > 85  → block LONG entries  (extreme greed → mean-reversion risk)
        FNG < 15  → block SHORT entries (extreme fear → mean-reversion risk)

  2. BTC-relative return — for each alt, compare 7-day return vs BTC's 7-day
     return. If BTC has outperformed the alt by > 3%, BTC is in a "rotation
     leg" and alts tend to underperform. Block alt LONG entries during these
     periods. Mirror for shorts.

Both are computed once per run and aligned to the daily bar grid.

Endpoints used:
  - https://api.alternative.me/fng/?limit=0   (free, no auth, daily F&G history)

Usage:
    from sentiment_filters import fetch_fear_greed_history, btc_relative_return
"""

from __future__ import annotations

import time
from typing import Optional

import pandas as pd
import requests


# ============================================================================
# Constants — thresholds chosen from common practice; tune via walk-forward.
# ============================================================================

FNG_GREED_THRESHOLD = 85.0       # block longs above this
FNG_FEAR_THRESHOLD = 15.0        # block shorts below this

BTC_REL_LOOKBACK = 7             # days to measure BTC vs alt rel performance
BTC_REL_THRESHOLD = 0.03         # BTC outperforming by > 3% blocks alt longs


# ============================================================================
# Fear & Greed
# ============================================================================

FNG_URL = "https://api.alternative.me/fng/"


def fetch_fear_greed_history(limit: int = 0,
                             retries: int = 3) -> pd.DataFrame:
    """
    Pull the full Fear & Greed history from alternative.me.

    Returns a DataFrame indexed by UTC date with columns:
      - fng_value    : int 0-100
      - fng_class    : string ("Extreme Fear" ... "Extreme Greed")

    limit=0 means "all history" per the API spec.
    """
    params = {"limit": limit, "format": "json"}
    last_err = None
    for attempt in range(retries):
        try:
            from net_utils import DEFAULT_HEADERS
            r = requests.get(FNG_URL, params=params, headers=DEFAULT_HEADERS, timeout=20)
            r.raise_for_status()
            payload = r.json()
            data = payload.get("data", [])
            if not data:
                return pd.DataFrame()
            rows = []
            for item in data:
                ts = int(item["timestamp"])
                rows.append({
                    "date": pd.to_datetime(ts, unit="s", utc=True).normalize(),
                    "fng_value": int(item["value"]),
                    "fng_class": item.get("value_classification", ""),
                })
            df = pd.DataFrame(rows).set_index("date").sort_index()
            return df[~df.index.duplicated(keep="last")]
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Fear & Greed fetch failed after {retries} attempts: {last_err}")


def align_fng_to_bars(fng_df: pd.DataFrame,
                      bar_index: pd.DatetimeIndex) -> pd.Series:
    """
    Forward-fill the Fear & Greed daily values onto a bar grid (e.g. a symbol's
    OHLCV daily index). Returns a Series indexed by bar_index with the most
    recent known FNG value at each bar.
    """
    if fng_df.empty:
        return pd.Series(index=bar_index, dtype=float)
    # Normalize bar_index to daily for the join, then map back
    daily_idx = bar_index.normalize()
    s = fng_df["fng_value"].reindex(daily_idx, method="ffill")
    s.index = bar_index
    return s


# ============================================================================
# BTC-relative return (per-alt rotation filter)
# ============================================================================

def btc_relative_return(symbol_df: pd.DataFrame,
                        btc_df: pd.DataFrame,
                        lookback: int = BTC_REL_LOOKBACK) -> pd.Series:
    """
    Return a Series indexed by symbol_df.index giving:
        (BTC return over `lookback` days) - (symbol return over `lookback` days)

    Positive values mean BTC outperformed (alt is losing rotation share).
    Negative values mean alt outperformed BTC.
    """
    s_ret = symbol_df["close"].pct_change(lookback)
    # Align BTC to symbol_df index (in case of slight index mismatches)
    btc_close = btc_df["close"].reindex(symbol_df.index, method="ffill")
    b_ret = btc_close.pct_change(lookback)
    return (b_ret - s_ret).fillna(0.0)


# ============================================================================
# Gate evaluation helpers (pure functions for testing)
# ============================================================================

def fng_blocks_entry(fng_value: float, is_long: bool,
                     greed_threshold: float = FNG_GREED_THRESHOLD,
                     fear_threshold: float = FNG_FEAR_THRESHOLD) -> bool:
    """True if the FNG circuit breaker says skip this entry."""
    if pd.isna(fng_value):
        return False
    if is_long and fng_value > greed_threshold:
        return True
    if (not is_long) and fng_value < fear_threshold:
        return True
    return False


def btc_rel_blocks_entry(btc_rel_value: float, is_long: bool, symbol: str,
                         threshold: float = BTC_REL_THRESHOLD) -> bool:
    """
    True if the BTC-rotation filter says skip this entry.
    Does not apply to BTCUSDT itself.
    """
    if symbol == "BTCUSDT":
        return False
    if pd.isna(btc_rel_value):
        return False
    # BTC outperforming alt by > threshold → block alt LONG
    if is_long and btc_rel_value > threshold:
        return True
    # BTC underperforming alt by > threshold (rel < -threshold)
    # → block alt SHORT (alt is too strong relative to BTC)
    if (not is_long) and btc_rel_value < -threshold:
        return True
    return False
