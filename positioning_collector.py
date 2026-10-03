"""
Positioning Collector — plant the dataset today, harvest the hypothesis later.
==============================================================================

Binance's positioning endpoints (/futures/data/*) only serve ~30 days of
history, and true liquidation prints are websocket-only (no history at all).
You cannot backtest what you never recorded — so this script records, nightly:

  per universe symbol, one row per day:
    open interest (contracts + USD), global long/short account ratio,
    top-trader position ratio, taker buy/sell volume ratio,
    funding (bps/8h), mark price

Appended to positioning_history.csv (deduped on date+symbol). After ~180
days this becomes a dataset for a liquidation-cascade / positioning-squeeze
hypothesis — a NEW-DATA test with its own budget (see EDGE_FRAMEWORK.md,
Sketch 2: forced liquidations are the purest forced-loser mechanism in
crypto). Until then: collect quietly, test nothing.

Public endpoints only (no API key). Advisory: partial failures skip the
symbol; exit code always 0.
"""

from __future__ import annotations

import sys as _sys
try:
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
UNIVERSE_FILE = HERE / "active_universe.json"
OUT = HERE / "positioning_history.csv"

HOSTS = ["https://fapi.binance.com", "https://fapi1.binance.com",
         "https://fapi2.binance.com", "https://fapi3.binance.com"]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

COLS = ["date", "symbol", "oi_contracts", "oi_usd", "global_ls_acct_ratio",
        "top_pos_ls_ratio", "taker_buy_sell_ratio", "funding_bps", "mark_price"]


def get(path: str, params: dict):
    last = None
    for host in HOSTS:
        try:
            r = requests.get(host + path, params=params, headers=UA, timeout=10)
            if r.status_code == 451:      # geo-block: try next host
                last = Exception("451")
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
    raise last or Exception(f"all hosts failed for {path}")


def collect_symbol(sym: str) -> dict | None:
    row = {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
           "symbol": sym}
    try:
        oi = get("/fapi/v1/openInterest", {"symbol": sym})
        row["oi_contracts"] = float(oi["openInterest"])
    except Exception:
        row["oi_contracts"] = ""
    try:
        h = get("/futures/data/openInterestHist",
                {"symbol": sym, "period": "1d", "limit": 1})
        row["oi_usd"] = float(h[-1]["sumOpenInterestValue"]) if h else ""
    except Exception:
        row["oi_usd"] = ""
    try:
        g = get("/futures/data/globalLongShortAccountRatio",
                {"symbol": sym, "period": "1d", "limit": 1})
        row["global_ls_acct_ratio"] = float(g[-1]["longShortRatio"]) if g else ""
    except Exception:
        row["global_ls_acct_ratio"] = ""
    try:
        t = get("/futures/data/topLongShortPositionRatio",
                {"symbol": sym, "period": "1d", "limit": 1})
        row["top_pos_ls_ratio"] = float(t[-1]["longShortRatio"]) if t else ""
    except Exception:
        row["top_pos_ls_ratio"] = ""
    try:
        k = get("/futures/data/takerlongshortRatio",
                {"symbol": sym, "period": "1d", "limit": 1})
        row["taker_buy_sell_ratio"] = float(k[-1]["buySellRatio"]) if k else ""
    except Exception:
        row["taker_buy_sell_ratio"] = ""
    try:
        p = get("/fapi/v1/premiumIndex", {"symbol": sym})
        row["funding_bps"] = round(float(p["lastFundingRate"]) * 1e4, 3)
        row["mark_price"] = float(p["markPrice"])
    except Exception:
        row["funding_bps"] = ""
        row["mark_price"] = ""
    # a row with nothing but date+symbol is a wasted line
    if all(row.get(c, "") == "" for c in COLS[2:]):
        return None
    return row


def main() -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] positioning collector ...")
    try:
        universe = json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))["universe"]
    except Exception as e:
        print(f"  cannot read active_universe.json ({e}) — abort (advisory)")
        return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing = None
    if OUT.exists():
        try:
            existing = pd.read_csv(OUT, usecols=["date", "symbol"])
            done = set(existing[existing["date"] == today]["symbol"])
        except Exception:
            done = set()
    else:
        done = set()

    rows, failed = [], []
    for sym in universe:
        if sym in done:
            continue
        try:
            row = collect_symbol(sym)
            if row:
                rows.append(row)
            else:
                failed.append(sym)
        except Exception:
            failed.append(sym)
        time.sleep(0.25)   # ~6 calls/symbol; stay far from rate limits

    if rows:
        df = pd.DataFrame(rows, columns=COLS)
        df.to_csv(OUT, mode="a", header=not OUT.exists(), index=False)
    n_hist = 0
    if OUT.exists():
        try:
            n_hist = len(pd.read_csv(OUT, usecols=["date"])["date"].unique())
        except Exception:
            pass
    print(f"  collected {len(rows)} symbols"
          + (f", failed {len(failed)}: {','.join(failed[:5])}" if failed else "")
          + f" — dataset now spans {n_hist} day(s)"
          + (" (target ~180 before any hypothesis)" if n_hist < 180 else
             " — DATASET MATURE: a cascade hypothesis may now be formed "
             "(new-data budget, via EDGE_FRAMEWORK + gauntlet)"))


if __name__ == "__main__":
    main()
