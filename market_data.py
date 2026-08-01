"""
Comprehensive market-data extraction for Binance USD-M perps.
==============================================================

All public endpoints — no auth required. Use binance-cli for the same data
under auth if you prefer; this module sticks to direct REST for portability.

Fetchers:
  - fetch_all_perp_symbols()         universe enumeration
  - fetch_24h_ticker_all()           current state of entire market
  - fetch_open_interest_history()    historical OI per symbol
  - fetch_long_short_ratio()         top trader & global positioning
  - fetch_taker_volume_ratio()       aggressive buy vs sell flow

All results are cached to ./cache/<endpoint>/<symbol>.csv so re-runs are cheap.

Use cases:
  - Universe scanning (which symbols meet liquidity / history thresholds)
  - Regime context (OI expansion, positioning extremes)
  - Strategy enhancement (taker imbalance as filter, OI delta as signal)
  - Dashboard situational awareness (long/short ratios, market breadth)
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import pandas as pd
import requests


# ============================================================================
# Endpoints
# ============================================================================

FAPI = "https://fapi.binance.com"   # legacy; kept for reference but no longer used directly
EXCHANGE_INFO_PATH = "/fapi/v1/exchangeInfo"
TICKER_24HR_PATH = "/fapi/v1/ticker/24hr"
OPEN_INTEREST_PATH = "/fapi/v1/openInterest"
OPEN_INTEREST_HIST_PATH = "/futures/data/openInterestHist"
TOP_LS_ACCOUNT_PATH = "/futures/data/topLongShortAccountRatio"
TOP_LS_POSITION_PATH = "/futures/data/topLongShortPositionRatio"
GLOBAL_LS_ACCOUNT_PATH = "/futures/data/globalLongShortAccountRatio"
TAKER_VOLUME_RATIO_PATH = "/futures/data/takerlongshortRatio"

# Kept as full URLs for callers that still reference them, but internal fetches
# now go through net_utils.fetch_binance_futures for edge rotation.
EXCHANGE_INFO = f"{FAPI}{EXCHANGE_INFO_PATH}"
TICKER_24HR = f"{FAPI}{TICKER_24HR_PATH}"
OPEN_INTEREST = f"{FAPI}{OPEN_INTEREST_PATH}"
OPEN_INTEREST_HIST = f"{FAPI}{OPEN_INTEREST_HIST_PATH}"
TOP_LS_ACCOUNT = f"{FAPI}{TOP_LS_ACCOUNT_PATH}"
TOP_LS_POSITION = f"{FAPI}{TOP_LS_POSITION_PATH}"
GLOBAL_LS_ACCOUNT = f"{FAPI}{GLOBAL_LS_ACCOUNT_PATH}"
TAKER_VOLUME_RATIO = f"{FAPI}{TAKER_VOLUME_RATIO_PATH}"

CACHE_DIR = Path("cache")


def _ensure_cache(subdir: str) -> Path:
    p = CACHE_DIR / subdir
    p.mkdir(parents=True, exist_ok=True)
    return p


def _polite(seconds: float = 0.12):
    time.sleep(seconds)


# ============================================================================
# Universe enumeration
# ============================================================================

def fetch_all_perp_symbols(quote: str = "USDT",
                           status: str = "TRADING",
                           contract_type: str = "PERPETUAL") -> pd.DataFrame:
    """
    Returns a DataFrame listing all active USD-M perps with columns:
      symbol, baseAsset, quoteAsset, contractType, status, onboardDate
    """
    from net_utils import fetch_binance_futures
    r = fetch_binance_futures(EXCHANGE_INFO_PATH, timeout=30)
    r.raise_for_status()
    info = r.json()
    rows = []
    for s in info.get("symbols", []):
        if s.get("contractType") != contract_type:
            continue
        if s.get("status") != status:
            continue
        if s.get("quoteAsset") != quote:
            continue
        rows.append({
            "symbol": s["symbol"],
            "baseAsset": s["baseAsset"],
            "quoteAsset": s["quoteAsset"],
            "contractType": s["contractType"],
            "status": s["status"],
            "onboardDate": pd.to_datetime(int(s.get("onboardDate", 0)), unit="ms", utc=True),
        })
    df = pd.DataFrame(rows).sort_values("symbol").reset_index(drop=True)
    return df


def filter_by_history(symbols_df: pd.DataFrame,
                      min_history_days: int = 730) -> pd.DataFrame:
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=min_history_days)
    return symbols_df[symbols_df["onboardDate"] <= cutoff].reset_index(drop=True)


# ============================================================================
# 24h ticker (entire market snapshot)
# ============================================================================

def fetch_24h_ticker_all() -> pd.DataFrame:
    """All symbols' last 24h stats — useful for cross-sectional comparisons."""
    from net_utils import fetch_binance_futures
    r = fetch_binance_futures(TICKER_24HR_PATH, timeout=30)
    r.raise_for_status()
    df = pd.DataFrame(r.json())
    numeric_cols = ["priceChange", "priceChangePercent", "weightedAvgPrice",
                    "lastPrice", "volume", "quoteVolume", "openPrice",
                    "highPrice", "lowPrice", "count"]
    for c in numeric_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# ============================================================================
# Open Interest history
# ============================================================================

def fetch_open_interest_history(symbol: str, period: str = "1d",
                                days: int = 30, use_cache: bool = True) -> pd.DataFrame:
    """
    period: 5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d
    days:   how far back to fetch (the endpoint caps at ~30 days for daily).
    """
    cache_dir = _ensure_cache(f"oi_hist/{period}")
    cache_file = cache_dir / f"{symbol}.csv"
    if use_cache and cache_file.exists():
        df = pd.read_csv(cache_file)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df

    end_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    start_ms = end_ms - days * 86400 * 1000

    params = {
        "symbol": symbol,
        "period": period,
        "limit": 500,
        "startTime": start_ms,
        "endTime": end_ms,
    }
    from net_utils import fetch_binance_futures
    try:
        r = fetch_binance_futures(OPEN_INTEREST_HIST_PATH, params=params, timeout=30)
    except Exception:
        return pd.DataFrame()
    if not r.ok:
        return pd.DataFrame()
    data = r.json()
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    for c in ("sumOpenInterest", "sumOpenInterestValue"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df.to_csv(cache_file, index=False)
    _polite()
    return df


# ============================================================================
# Long/Short ratios
# ============================================================================

def _fetch_ls(url: str, symbol: str, period: str, days: int,
              cache_subdir: str, use_cache: bool) -> pd.DataFrame:
    cache_dir = _ensure_cache(f"{cache_subdir}/{period}")
    cache_file = cache_dir / f"{symbol}.csv"
    if use_cache and cache_file.exists():
        df = pd.read_csv(cache_file)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df

    end_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    params = {"symbol": symbol, "period": period, "limit": 500,
              "startTime": start_ms, "endTime": end_ms}
    from net_utils import fetch_binance_futures
    # `url` is passed in as a full https:// URL; strip scheme+host to get the path
    from urllib.parse import urlparse
    path = urlparse(url).path
    try:
        r = fetch_binance_futures(path, params=params, timeout=30)
    except Exception:
        return pd.DataFrame()
    if not r.ok:
        return pd.DataFrame()
    data = r.json()
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    for c in ("longShortRatio", "longAccount", "shortAccount", "longPosition", "shortPosition"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df.to_csv(cache_file, index=False)
    _polite()
    return df


def fetch_long_short_ratio(symbol: str, kind: str = "topAccount",
                           period: str = "1d", days: int = 30,
                           use_cache: bool = True) -> pd.DataFrame:
    """
    kind: 'topAccount'  -> top trader account-count L/S ratio
          'topPosition' -> top trader position-size weighted L/S ratio
          'global'      -> universe-wide retail account L/S ratio
    """
    url_map = {
        "topAccount":  (TOP_LS_ACCOUNT, "ls_top_account"),
        "topPosition": (TOP_LS_POSITION, "ls_top_position"),
        "global":      (GLOBAL_LS_ACCOUNT, "ls_global"),
    }
    if kind not in url_map:
        raise ValueError(f"unknown kind: {kind}")
    url, subdir = url_map[kind]
    return _fetch_ls(url, symbol, period, days, subdir, use_cache)


# ============================================================================
# Taker buy/sell volume
# ============================================================================

def fetch_taker_volume_ratio(symbol: str, period: str = "1d",
                             days: int = 30, use_cache: bool = True) -> pd.DataFrame:
    """
    Returns rolling taker buy-sell volume ratio. >1 = aggressive buying dominant.
    """
    cache_dir = _ensure_cache(f"taker_ratio/{period}")
    cache_file = cache_dir / f"{symbol}.csv"
    if use_cache and cache_file.exists():
        df = pd.read_csv(cache_file)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df

    end_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    params = {"symbol": symbol, "period": period, "limit": 500,
              "startTime": start_ms, "endTime": end_ms}
    from net_utils import fetch_binance_futures
    try:
        r = fetch_binance_futures(TAKER_VOLUME_RATIO_PATH, params=params, timeout=30)
    except Exception:
        return pd.DataFrame()
    if not r.ok:
        return pd.DataFrame()
    data = r.json()
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    for c in ("buySellRatio", "buyVol", "sellVol"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df.to_csv(cache_file, index=False)
    _polite()
    return df


# ============================================================================
# Convenience: pull a complete dataset for one symbol
# ============================================================================

def fetch_all_for_symbol(symbol: str, period: str = "1d",
                          days: int = 30) -> dict:
    """Convenience: returns dict with OI, all three L/S ratios, taker ratio."""
    return {
        "open_interest": fetch_open_interest_history(symbol, period, days),
        "top_ls_account": fetch_long_short_ratio(symbol, "topAccount", period, days),
        "top_ls_position": fetch_long_short_ratio(symbol, "topPosition", period, days),
        "global_ls": fetch_long_short_ratio(symbol, "global", period, days),
        "taker_ratio": fetch_taker_volume_ratio(symbol, period, days),
    }


if __name__ == "__main__":
    print("Fetching universe ...")
    universe = fetch_all_perp_symbols()
    print(f"  {len(universe)} active USDT perps")
    qualified = filter_by_history(universe, min_history_days=730)
    print(f"  {len(qualified)} have >= 2 years of history")

    print("\nFetching 24h market snapshot ...")
    ticker = fetch_24h_ticker_all()
    print(f"  {len(ticker)} symbols, total 24h volume: "
          f"${ticker['quoteVolume'].sum() / 1e9:.1f}B")
    top_by_volume = ticker.sort_values("quoteVolume", ascending=False).head(20)
    print("\nTop 20 by 24h notional volume:")
    print(top_by_volume[["symbol", "lastPrice", "priceChangePercent", "quoteVolume"]]
          .to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
