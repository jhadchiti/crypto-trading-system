"""
Binance USD-M perp funding history fetcher and bar alignment.
=============================================================

Endpoint: https://fapi.binance.com/fapi/v1/fundingRate
  - Returns 8-hour funding events for a symbol.
  - Funding is paid at 00:00, 08:00, 16:00 UTC.
  - Rate is decimal (e.g. 0.0001 = 1bp per 8h).

Use:
    events = fetch_funding(symbol, start_ms, end_ms)
    per_bar = align_funding_to_bars(events, bar_index, bar_minutes=240)

`per_bar` is a DataFrame indexed by bar timestamp with:
    - funding_bps_in_bar: cumulative bps that hit during that bar
    - funding_bps_8h_avg: rolling average of 8h funding (for entry filter)
"""

from __future__ import annotations

import time
from typing import Optional

import pandas as pd
import requests


FUNDING_PATH = "/fapi/v1/fundingRate"


def fetch_funding(symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Page through funding history. Each call returns up to 1000 events.
    Uses net_utils.fetch_binance_futures for rotation across fapi/fapi1/fapi2/fapi3
    endpoints on 451 responses."""
    from net_utils import fetch_binance_futures
    rows = []
    cursor = start_ms
    while cursor < end_ms:
        params = {
            "symbol": symbol,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1000,
        }
        try:
            r = fetch_binance_futures(FUNDING_PATH, params=params, timeout=20)
        except requests.HTTPError:
            # Some symbols may not exist on the perp endpoint; bail gracefully.
            break
        except Exception:
            # Network error after exhausting all edges — treat like symbol not found.
            break
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        last_t = batch[-1]["fundingTime"]
        cursor = last_t + 1
        if len(batch) < 1000:
            break
        time.sleep(0.15)

    if not rows:
        return pd.DataFrame(columns=["funding_time", "funding_rate"])

    df = pd.DataFrame(rows)
    df["funding_time"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
    df["funding_rate"] = df["fundingRate"].astype(float)
    df = df[["funding_time", "funding_rate"]].sort_values("funding_time").reset_index(drop=True)
    return df


def align_funding_to_bars(events: pd.DataFrame, bar_index: pd.DatetimeIndex,
                          bar_minutes: int) -> pd.DataFrame:
    """
    Aggregate 8h funding events into per-bar bps.

    For each bar [t, t+bar_minutes), sum the funding rates of events whose
    funding_time falls in that interval. Result is in bps (basis points).
    """
    if events.empty:
        out = pd.DataFrame(index=bar_index)
        out["funding_bps_in_bar"] = 0.0
        out["funding_bps_8h_last"] = 0.0
        return out

    # Snap each event timestamp to the bar it falls into.
    bar_freq = f"{bar_minutes}min"
    snapped = events["funding_time"].dt.floor(bar_freq)
    grouped = events.assign(bar=snapped).groupby("bar")["funding_rate"].sum()
    # rate -> bps
    bps_per_bar = grouped * 10_000.0

    out = pd.DataFrame(index=bar_index)
    out["funding_bps_in_bar"] = bps_per_bar.reindex(bar_index).fillna(0.0)

    # Also keep the last 8h funding rate observed (for the entry filter).
    last_event_bps = events.set_index("funding_time")["funding_rate"] * 10_000.0
    # forward-fill onto the bar grid
    out["funding_bps_8h_last"] = last_event_bps.reindex(bar_index, method="ffill").fillna(0.0)

    return out


def load_funding_for_universe(symbols: list[str], start_ms: int, end_ms: int,
                              bar_index_by_symbol: dict[str, pd.DatetimeIndex],
                              bar_minutes: int) -> dict[str, pd.DataFrame]:
    """Convenience: fetch + align for a list of symbols. Returns dict by symbol."""
    out = {}
    for s in symbols:
        print(f"  funding: {s} ...")
        ev = fetch_funding(s, start_ms, end_ms)
        idx = bar_index_by_symbol.get(s)
        if idx is None or len(idx) == 0:
            continue
        out[s] = align_funding_to_bars(ev, idx, bar_minutes)
    return out
