"""
Dynamic Universe Selector.
==========================

Maintains active_universe.json — the single source of truth for "what we're
currently trading." Other tools (dashboard, signal_alerter, etc.) read from
this file instead of hardcoded SYMBOLS tuples.

The selection is rule-based, not performance-based:
  - Status: TRADING on Binance USD-M
  - Listing age >= 180 days (so SMA200 + Donchian channels have data)
  - 24h notional volume >= $50M (liquidity threshold)
  - Universe capped at top 30 by current 24h volume
  - BTCUSDT always included (needed for macro regime computation)
  - Excludes known stablecoins / wrapped tokens that aren't tradable trends

These criteria are computable PRE-TRADE — no lookback on strategy performance,
so no overfitting on universe selection.

Run weekly or after major listings:
    python dynamic_universe.py
    python dynamic_universe.py --max-size 50      # bigger universe
    python dynamic_universe.py --min-volume 25e6  # looser liquidity filter

Reads/writes:
    active_universe.json   <- single source of truth for the active universe
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from market_data import fetch_all_perp_symbols, filter_by_history, fetch_24h_ticker_all


# ============================================================================
# DEFAULTS
# ============================================================================

UNIVERSE_FILE = Path("active_universe.json")
MAX_UNIVERSE_SIZE = 30
MIN_VOLUME_USD = 50_000_000
MIN_HISTORY_DAYS = 180
STALE_DAYS = 14   # warn if universe file is older than this

# Always include — needed for macro regime + BTC-relative computation
ALWAYS_INCLUDE = {"BTCUSDT"}

# Exclude — stablecoins, leveraged tokens, wrapped assets that don't trend
EXCLUDE = {
    "USDCUSDT", "TUSDUSDT", "BUSDUSDT", "DAIUSDT", "FDUSDUSDT", "USTUSDT",
    "USDP", "USDDUSDT", "PYUSDUSDT",
    # leveraged tokens (if any present)
    "BTCUPUSDT", "BTCDOWNUSDT", "ETHUPUSDT", "ETHDOWNUSDT",
}

# Hardcoded fallback if no universe file exists yet
DEFAULT_FALLBACK = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
                    "AVAXUSDT", "LINKUSDT", "DOGEUSDT", "XRPUSDT")


# ============================================================================
# Public API
# ============================================================================

def select_universe(max_size: int = MAX_UNIVERSE_SIZE,
                    min_volume_usd: float = MIN_VOLUME_USD,
                    min_history_days: int = MIN_HISTORY_DAYS,
                    always_include: set = ALWAYS_INCLUDE,
                    exclude: set = EXCLUDE,
                    ) -> dict:
    """
    Returns a dict with:
      'universe': sorted tuple of selected symbols
      'stats': dict of selection-pipeline counts
      'criteria': dict of selection rules used
      'top_volumes': dict mapping symbol -> 24h volume (USD)
    """
    print("Enumerating active perps ...")
    perps = fetch_all_perp_symbols()
    print(f"  total active USDT perps: {len(perps)}")

    qualified = filter_by_history(perps, min_history_days=min_history_days)
    print(f"  with >= {min_history_days}d history: {len(qualified)}")

    print("Fetching 24h volumes ...")
    ticker = fetch_24h_ticker_all()
    vol_map = dict(zip(ticker["symbol"], ticker["quoteVolume"]))

    # Apply filters
    candidates = []
    for sym in qualified["symbol"]:
        if sym in exclude:
            continue
        vol = float(vol_map.get(sym, 0.0))
        if vol < min_volume_usd:
            continue
        candidates.append((sym, vol))

    # Sort by volume descending
    candidates.sort(key=lambda x: -x[1])
    print(f"  passed liquidity filter (>= ${min_volume_usd/1e6:.0f}M): {len(candidates)}")

    # Take top max_size, but always keep the must-include set
    top = [s for s, _ in candidates[:max_size]]
    for s in always_include:
        if s not in top and s in vol_map:
            top.append(s)
    top = sorted(set(top))
    print(f"  final universe (capped at top {max_size} + must-include): {len(top)}")

    return {
        "universe": tuple(top),
        "stats": {
            "total_perps": len(perps),
            "passed_history": len(qualified),
            "passed_filters": len(candidates),
            "final_size": len(top),
        },
        "criteria": {
            "max_size": max_size,
            "min_volume_usd": min_volume_usd,
            "min_history_days": min_history_days,
            "always_include": sorted(always_include),
            "exclude": sorted(exclude),
        },
        "top_volumes": {s: float(vol_map.get(s, 0.0)) for s in top},
    }


def write_universe_file(result: dict, path: Path = UNIVERSE_FILE) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe": list(result["universe"]),
        "criteria": result["criteria"],
        "stats": result["stats"],
        "top_volumes": result["top_volumes"],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_universe(path: Path = UNIVERSE_FILE,
                  fallback: tuple = DEFAULT_FALLBACK,
                  warn_stale: bool = True) -> tuple[tuple, dict]:
    """
    Returns (universe_tuple, meta_dict).
    meta_dict has keys: source ('json'|'fallback'), generated_at, age_days, is_stale.
    """
    if not path.exists():
        return (fallback, {"source": "fallback", "generated_at": None,
                            "age_days": None, "is_stale": True,
                            "universe_size": len(fallback)})

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        gen_at = pd.Timestamp(payload.get("generated_at"))
        age = (pd.Timestamp.now(tz="UTC") - gen_at).days if gen_at else None
        is_stale = (age is not None and age > STALE_DAYS)
        return (
            tuple(payload.get("universe", fallback)),
            {
                "source": "json",
                "generated_at": payload.get("generated_at"),
                "age_days": age,
                "is_stale": is_stale,
                "universe_size": len(payload.get("universe", [])),
                "criteria": payload.get("criteria", {}),
                "stats": payload.get("stats", {}),
            },
        )
    except Exception as e:
        print(f"WARN: failed to read {path}: {e} — using fallback.")
        return (fallback, {"source": "fallback", "generated_at": None,
                            "age_days": None, "is_stale": True,
                            "universe_size": len(fallback)})


# ============================================================================
# Driver
# ============================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-size", type=int, default=MAX_UNIVERSE_SIZE)
    ap.add_argument("--min-volume", type=float, default=MIN_VOLUME_USD)
    ap.add_argument("--min-history-days", type=int, default=MIN_HISTORY_DAYS)
    ap.add_argument("--dry-run", action="store_true",
                    help="print result but don't write JSON")
    args = ap.parse_args()

    print(f"\nSelecting active universe (max={args.max_size}, "
          f"min_vol=${args.min_volume/1e6:.0f}M, min_age={args.min_history_days}d)...\n")

    result = select_universe(
        max_size=args.max_size,
        min_volume_usd=args.min_volume,
        min_history_days=args.min_history_days,
    )

    print("\n=== ACTIVE UNIVERSE ===\n")
    syms = result["universe"]
    vol_map = result["top_volumes"]
    for i, s in enumerate(sorted(syms, key=lambda x: -vol_map.get(x, 0)), 1):
        marker = " ←" if s in ALWAYS_INCLUDE else ""
        print(f"  {i:>2}. {s:<14}  ${vol_map.get(s,0)/1e6:>8,.1f}M{marker}")
    print(f"\n  Total: {len(syms)} symbols")

    if args.dry_run:
        print("\n(dry-run: active_universe.json NOT written)")
        return

    write_universe_file(result)
    print(f"\nWrote {UNIVERSE_FILE.resolve()}")
    print("dashboard.py and signal_alerter.py will pick this up on next run.")
    print("\nRe-run weekly or after major listings / delistings.")


if __name__ == "__main__":
    main()
