"""
Account Sync — read-only Binance account state.
================================================

Pulls real balances and positions via signed (read-only) API calls:

  - USD-M futures: wallet balance + unrealized PnL + open positions
  - Spot: asset balances valued in USDT
  - Simple Earn: flexible positions (if any)

Writes account_state.json and prints a summary. Cross-checks open futures
positions against live_trades.csv — any mismatch is sent to Discord
(a position the tracker doesn't know about, or a tracked trade with no
matching exchange position).

Requires env vars (READ-ONLY key — reading enabled, everything else OFF):
  BINANCE_API_KEY
  BINANCE_API_SECRET

Usage:
    python account_sync.py
    python account_sync.py --quiet     # one-line output for automation
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from net_utils import DEFAULT_HEADERS, FUTURES_HOSTS, SPOT_HOSTS

STATE_FILE = Path("account_state.json")
TRADES_FILE = Path("live_trades.csv")
DUST_USD = 1.0    # ignore balances below this value


# ============================================================================
# Signed request plumbing
# ============================================================================

def _creds():
    key = os.environ.get("BINANCE_API_KEY")
    sec = os.environ.get("BINANCE_API_SECRET")
    if not key or not sec:
        print("ERROR: BINANCE_API_KEY / BINANCE_API_SECRET not set.", file=sys.stderr)
        sys.exit(1)
    return key, sec


def signed_get(hosts: list, path: str, params: dict | None = None,
               timeout: int = 15) -> dict | list:
    """Signed GET with host rotation on 451 (same geo-block strategy as
    the market-data fetchers)."""
    key, sec = _creds()
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 10_000
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(sec.encode(), qs.encode(), hashlib.sha256).hexdigest()
    headers = dict(DEFAULT_HEADERS)
    headers["X-MBX-APIKEY"] = key

    last_err = None
    for host in hosts:
        url = f"https://{host}{path}?{qs}&signature={sig}"
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 451:
                last_err = RuntimeError(f"451 at {host}")
                continue
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            last_err = e
            continue
    raise last_err if last_err else RuntimeError(f"all hosts failed for {path}")


_PRICE_CACHE: dict = {}


def signed_post(hosts: list, path: str, params: dict | None = None,
                timeout: int = 15) -> dict | list:
    """Signed POST (some /sapi endpoints are POST-only, e.g. funding wallet)."""
    key, sec = _creds()
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 10_000
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(sec.encode(), qs.encode(), hashlib.sha256).hexdigest()
    headers = dict(DEFAULT_HEADERS)
    headers["X-MBX-APIKEY"] = key

    last_err = None
    for host in hosts:
        url = f"https://{host}{path}?{qs}&signature={sig}"
        try:
            r = requests.post(url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 451:
                last_err = RuntimeError(f"451 at {host}")
                continue
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            last_err = e
            continue
    raise last_err if last_err else RuntimeError(f"all hosts failed for {path}")


def public_price(symbol: str) -> float | None:
    """Single batched call on first use: /api/v3/ticker/price with no symbol
    returns ALL pair prices in one response. Subsequent lookups are dict hits.
    This avoids per-asset host rotation on dust assets with no USDT pair."""
    global _PRICE_CACHE
    if not _PRICE_CACHE:
        for host in SPOT_HOSTS:
            try:
                r = requests.get(f"https://{host}/api/v3/ticker/price",
                                 headers=DEFAULT_HEADERS, timeout=15)
                if r.status_code == 200:
                    _PRICE_CACHE = {d["symbol"]: float(d["price"])
                                    for d in r.json()}
                    break
                if r.status_code == 451:
                    continue
            except requests.exceptions.RequestException:
                continue
        if not _PRICE_CACHE:
            _PRICE_CACHE = {"__failed__": 0.0}
    return _PRICE_CACHE.get(symbol)


# ============================================================================
# Fetchers
# ============================================================================

def fetch_futures() -> dict:
    """USD-M futures wallet + open positions."""
    acct = signed_get(FUTURES_HOSTS, "/fapi/v2/account")
    positions = [
        {
            "symbol": p["symbol"],
            "amount": float(p["positionAmt"]),
            "entry_price": float(p["entryPrice"]),
            "unrealized_pnl": float(p["unRealizedProfit"]),
            "side": "LONG" if float(p["positionAmt"]) > 0 else "SHORT",
        }
        for p in acct.get("positions", [])
        if abs(float(p.get("positionAmt", 0))) > 0
    ]
    return {
        "wallet_usdt": float(acct.get("totalWalletBalance", 0)),
        "unrealized_pnl": float(acct.get("totalUnrealizedProfit", 0)),
        "margin_balance": float(acct.get("totalMarginBalance", 0)),
        "positions": positions,
    }


def fetch_spot() -> dict:
    acct = signed_get(SPOT_HOSTS, "/api/v3/account")
    assets = []
    total = 0.0
    for b in acct.get("balances", []):
        qty = float(b["free"]) + float(b["locked"])
        if qty <= 0:
            continue
        asset = b["asset"]
        if asset in ("USDT", "USDC", "FDUSD", "BUSD"):
            usd = qty
        else:
            px = public_price(f"{asset}USDT")
            if px is None:
                continue
            usd = qty * px
        if usd < DUST_USD:
            continue
        assets.append({"asset": asset, "qty": qty, "usd_value": round(usd, 2)})
        total += usd
    return {"assets": assets, "total_usd": round(total, 2)}


def fetch_simple_earn() -> dict:
    try:
        res = signed_get(SPOT_HOSTS, "/sapi/v1/simple-earn/flexible/position",
                         {"size": 100})
        rows = res.get("rows", []) if isinstance(res, dict) else []
        positions = [{"asset": r["asset"], "amount": float(r["totalAmount"]),
                      "apr": float(r.get("latestAnnualPercentageRate", 0))}
                     for r in rows]
        # locked products too
        try:
            res2 = signed_get(SPOT_HOSTS, "/sapi/v1/simple-earn/locked/position",
                              {"size": 100})
            for r in (res2.get("rows", []) if isinstance(res2, dict) else []):
                positions.append({"asset": r["asset"],
                                  "amount": float(r["amount"]),
                                  "apr": float(r.get("apy", 0)),
                                  "locked": True})
        except Exception:
            pass
        # value ALL positions in USD (not just stables)
        total_usd = 0.0
        stable_total = 0.0
        for p in positions:
            if p["asset"] in ("USDT", "USDC", "FDUSD", "BUSD"):
                usd = p["amount"]
                stable_total += usd
            else:
                px = public_price(f"{p['asset']}USDT")
                usd = p["amount"] * px if px else 0.0
            p["usd_value"] = round(usd, 2)
            total_usd += usd
        return {"positions": positions,
                "stable_total": round(stable_total, 2),
                "total_usd": round(total_usd, 2)}
    except Exception as e:
        return {"positions": [], "stable_total": 0.0, "total_usd": 0.0,
                "error": str(e)}


def fetch_funding_wallet() -> dict:
    """Funding wallet (P2P/deposit landing zone) — POST-only endpoint."""
    try:
        res = signed_post(SPOT_HOSTS, "/sapi/v1/asset/get-funding-asset", {})
        assets = []
        total = 0.0
        for b in (res if isinstance(res, list) else []):
            qty = (float(b.get("free", 0)) + float(b.get("locked", 0))
                   + float(b.get("freeze", 0)))
            if qty <= 0:
                continue
            asset = b["asset"]
            if asset in ("USDT", "USDC", "FDUSD", "BUSD"):
                usd = qty
            else:
                px = public_price(f"{asset}USDT")
                if px is None:
                    continue
                usd = qty * px
            if usd < DUST_USD:
                continue
            assets.append({"asset": asset, "qty": qty, "usd_value": round(usd, 2)})
            total += usd
        return {"assets": assets, "total_usd": round(total, 2)}
    except Exception as e:
        return {"assets": [], "total_usd": 0.0, "error": str(e)}


# ============================================================================
# Cross-check vs live_trades.csv
# ============================================================================

def cross_check(futures_positions: list) -> list[str]:
    """Compare exchange positions against the tracker's open trades."""
    issues = []
    tracked: dict[str, int] = {}
    if TRADES_FILE.exists():
        df = pd.read_csv(TRADES_FILE)
        if "exit_date" in df.columns:
            open_df = df[df["exit_date"].isna()]
            for _, row in open_df.iterrows():
                tracked[row["symbol"]] = int(row["side"])

    exchange = {p["symbol"]: p for p in futures_positions}

    for sym, p in exchange.items():
        if sym not in tracked:
            issues.append(f"UNTRACKED position on exchange: {p['side']} {sym} "
                          f"(amt {p['amount']}, uPnL {p['unrealized_pnl']:+.2f}) "
                          f"— not in live_trades.csv")
    for sym, side in tracked.items():
        if sym not in exchange:
            issues.append(f"GHOST trade in live_trades.csv: {sym} marked open "
                          f"but no exchange position found")
    return issues


def deliver_discord(message: str) -> None:
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        return
    payload = json.dumps({"content": message[:1900]}).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=payload,
        headers={"Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as e:
        print(f"  WARN: Discord delivery failed: {e}", file=sys.stderr)


# ============================================================================
# Main
# ============================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    now = datetime.now(timezone.utc).isoformat()

    fut = {}
    spot = {}
    earn = {}
    funding_w = {}
    errors = []
    try:
        fut = fetch_futures()
    except Exception as e:
        errors.append(f"futures: {e}")
    try:
        spot = fetch_spot()
    except Exception as e:
        errors.append(f"spot: {e}")
    try:
        earn = fetch_simple_earn()
    except Exception as e:
        errors.append(f"earn: {e}")
    try:
        funding_w = fetch_funding_wallet()
    except Exception as e:
        errors.append(f"funding_wallet: {e}")

    if errors and not (fut or spot):
        print(f"account sync FAILED: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)

    total_equity = (fut.get("margin_balance", 0.0)
                    + spot.get("total_usd", 0.0)
                    + earn.get("total_usd", earn.get("stable_total", 0.0))
                    + funding_w.get("total_usd", 0.0))

    issues = cross_check(fut.get("positions", []))

    state = {
        "synced_at": now,
        "total_equity_usd": round(total_equity, 2),
        "futures": fut,
        "spot": spot,
        "simple_earn": earn,
        "funding_wallet": funding_w,
        "cross_check_issues": issues,
        "errors": errors,
    }
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")

    # append daily equity history (one row per calendar day, last write wins)
    try:
        hist_file = Path("equity_history.csv")
        today = now[:10]
        row = (f"{today},{round(total_equity, 2)},"
               f"{round(fut.get('margin_balance', 0), 2)},"
               f"{round(spot.get('total_usd', 0), 2)},"
               f"{round(earn.get('total_usd', 0), 2)}\n")
        if hist_file.exists():
            lines = [l for l in hist_file.read_text(encoding='utf-8').splitlines(True)
                     if not l.startswith(today)]
        else:
            lines = ["date,total,futures,spot,earn\n"]
        lines.append(row)
        hist_file.write_text("".join(lines), encoding="utf-8")
    except Exception as e:
        print(f"  WARN equity history: {e}", file=sys.stderr)

    if args.quiet:
        print(f"equity ${total_equity:,.2f} | "
              f"futures pos: {len(fut.get('positions', []))} | "
              f"issues: {len(issues)}")
    else:
        print(f"=== ACCOUNT STATE ({now[:19]}) ===")
        print(f"  Total equity:      ${total_equity:,.2f}")
        print(f"  Futures margin:    ${fut.get('margin_balance', 0):,.2f} "
              f"(uPnL {fut.get('unrealized_pnl', 0):+,.2f})")
        print(f"  Spot:              ${spot.get('total_usd', 0):,.2f}")
        print(f"  Funding wallet:    ${funding_w.get('total_usd', 0):,.2f}")
        print(f"  Simple Earn:       ${earn.get('total_usd', 0):,.2f} total "
              f"(${earn.get('stable_total', 0):,.2f} stables)")
        top_earn = sorted([p for p in earn.get("positions", [])
                           if p.get("usd_value", 0) >= 1.0],
                          key=lambda p: -p["usd_value"])[:5]
        for p in top_earn:
            print(f"    {p['asset']}: {p['amount']} (${p['usd_value']:,.2f}, "
                  f"APR {p['apr']:.2%})")
        if fut.get("positions"):
            print(f"  Open futures positions:")
            for p in fut["positions"]:
                print(f"    {p['side']} {p['symbol']}: amt {p['amount']}, "
                      f"entry {p['entry_price']}, uPnL {p['unrealized_pnl']:+,.2f}")
        else:
            print(f"  Open futures positions: none")
        for iss in issues:
            print(f"  !! {iss}")
        for e in errors:
            print(f"  WARN: {e}")

    if issues:
        deliver_discord("**[Account Sync] position mismatch**\n```\n"
                        + "\n".join(issues) + "\n```")

    print("wrote account_state.json")


if __name__ == "__main__":
    main()
