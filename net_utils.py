"""
Shared HTTP utilities for Binance fetchers.
===========================================

Solves two related problems:

1. **Cloudflare bot detection** blocks naked `python-requests` User-Agent with
   HTTP 451 (blanket "Unavailable for Legal Reasons"). We attach a realistic
   browser User-Agent + Accept-Language headers to bypass this.

2. **Geo-blocking on specific Binance edge servers.** Binance runs multiple
   futures endpoints (fapi, fapi1, fapi2, fapi3) that route to different edge
   clusters. When one edge blocks your IP, retrying against another edge often
   succeeds. `fetch_with_rotation` tries each in sequence.

Usage:

    from net_utils import fetch_binance_futures

    r = fetch_binance_futures("/fapi/v1/klines", params={"symbol": "BTCUSDT", ...})
    data = r.json()
"""

from __future__ import annotations

import time
from typing import Optional

import requests


# ============================================================================
# Realistic browser headers (bypasses Cloudflare bot detection)
# ============================================================================

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
}


# ============================================================================
# Binance futures endpoint pool — try each in order on 451
# ============================================================================

FUTURES_HOSTS = [
    "fapi.binance.com",
    "fapi1.binance.com",
    "fapi2.binance.com",
    "fapi3.binance.com",
]

# For the spot API (if we ever fall back to it)
SPOT_HOSTS = [
    "api.binance.com",
    "api1.binance.com",
    "api2.binance.com",
    "api3.binance.com",
    "api4.binance.com",
]


# ============================================================================
# Core rotation logic
# ============================================================================

def fetch_with_rotation(
    path: str,
    params: Optional[dict] = None,
    hosts: Optional[list] = None,
    timeout: int = 20,
    retry_delay: float = 0.2,
) -> requests.Response:
    """
    GET `path` (e.g. "/fapi/v1/klines") across multiple hosts in `hosts`.
    Returns the first successful response.

    On HTTP 451 (geo-block) or connection error: sleep briefly and try the next host.
    On other HTTP errors: raise immediately (they are not caused by geo-blocking).

    Raises the last exception if ALL hosts fail.
    """
    if hosts is None:
        hosts = FUTURES_HOSTS

    last_error: Optional[Exception] = None
    for host in hosts:
        url = f"https://{host}{path}"
        try:
            r = requests.get(url, params=params, headers=DEFAULT_HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code == 451:
                # This edge is geo-blocked; try the next one
                last_error = requests.exceptions.HTTPError(
                    f"451 at {host}", response=r
                )
                time.sleep(retry_delay)
                continue
            # Non-451 error: not a geo-block issue, raise now
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            last_error = e
            time.sleep(retry_delay)
            continue

    # All hosts failed
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"All hosts returned 451 for {path}")


# ============================================================================
# Convenience wrappers
# ============================================================================

def fetch_binance_futures(path: str, params: Optional[dict] = None,
                          timeout: int = 20) -> requests.Response:
    """Convenience wrapper: futures endpoint with rotation + browser headers."""
    return fetch_with_rotation(path, params=params, hosts=FUTURES_HOSTS, timeout=timeout)


def fetch_binance_spot(path: str, params: Optional[dict] = None,
                       timeout: int = 20) -> requests.Response:
    """Convenience wrapper: spot endpoint with rotation + browser headers."""
    return fetch_with_rotation(path, params=params, hosts=SPOT_HOSTS, timeout=timeout)


def get_with_headers(url: str, params: Optional[dict] = None,
                     timeout: int = 20) -> requests.Response:
    """
    Simple wrapper for non-Binance endpoints (Fear & Greed, etc.).
    Just adds the browser headers — no rotation needed.
    """
    r = requests.get(url, params=params, headers=DEFAULT_HEADERS, timeout=timeout)
    r.raise_for_status()
    return r
