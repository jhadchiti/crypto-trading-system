"""
Dashboard Digest — daily Discord summary + full dashboard.html attachment.
===========================================================================

Runs as the LAST step of daily_check (after sync, so all data is fresh).
Reads only local state files — no market fetches. Sends one quiet message:

    [Daily Digest] NO ACTION NEEDED
    equity $111.80 (+0.03 today) | macro OFF slope -3.48% | carry best +4.8bps
    unlocks: PROVE open (+9.8%) | next: Aug 05 PROVE unlock
    (dashboard.html attached)

Usage:
    python dashboard_digest.py            # send to Discord
    python dashboard_digest.py --dry-run  # print only
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import requests

import secrets_local  # loads secrets.env into os.environ
import os


def _j(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def build_digest() -> str:
    lines = []

    # --- action status ---
    halted = Path("STOP_TRADING").exists()
    acct = _j("account_state.json")
    issues = acct.get("cross_check_issues", [])
    if halted:
        status = "🛑 ACTION NEEDED: executor halted"
    elif issues:
        status = f"⚠ ACTION NEEDED: {issues[0][:80]}"
    else:
        status = "✅ no action needed"
    lines.append(status)

    # --- equity + today's move ---
    eq_line = ""
    eq = acct.get("total_equity_usd")
    if eq is not None:
        eq_line = f"equity ${eq:,.2f}"
        try:
            eh = pd.read_csv("equity_history.csv")
            if len(eh) >= 2:
                d = float(eh["total"].iloc[-1]) - float(eh["total"].iloc[-2])
                eq_line += f" ({d:+.2f} today)"
        except Exception:
            pass

    # --- macro ---
    dstate = _j("last_dashboard_state.json")
    macro = "ON" if dstate.get("btc_macro_on") else "OFF"
    macro_line = f"macro {macro}"
    # slope from cache if available
    try:
        btc = pd.read_csv("cache/ohlcv/BTCUSDT.csv", parse_dates=["date"],
                          index_col="date")
        sma = btc["close"].rolling(200).mean()
        macro_line += f" slope {(sma.iloc[-1]/sma.iloc[-21]-1)*100:+.2f}%"
    except Exception:
        pass

    # --- carry ---
    cst = _j("paper_carry_state.json")
    best = cst.get("best_trail_bps")
    carry_line = (f"carry {len(cst.get('open', {}))} open"
                  + (f", best {best:+.1f}bps vs 10" if best is not None else ""))

    lines.append(" | ".join(x for x in [eq_line, macro_line, carry_line] if x))

    # --- unlock sleeve ---
    uev = _j("unlock_events.json") or []
    if isinstance(uev, list) and uev:
        parts = []
        for e in uev:
            if e.get("status") == "open":
                parts.append(f"{e['symbol'].replace('USDT','')} open "
                             f"(entry {e.get('entry_px')})")
            elif e.get("status") == "pending":
                parts.append(f"{e['symbol'].replace('USDT','')} pending "
                             f"{e['unlock_date']}")
        done = [e for e in uev if e.get("status") in ("completed", "stopped")]
        if done:
            wins = sum(1 for e in done if e.get("net_pct", 0) > 0)
            parts.append(f"trial {wins}W/{len(done)-wins}L of {len(done)}")
        if parts:
            lines.append("unlocks: " + " | ".join(parts))

    # --- signals of note ---
    sigs = {k: v for k, v in (dstate.get("signals") or {}).items() if v != "NONE"}
    if sigs:
        lines.append("blocked/active: " + ", ".join(f"{k.replace('USDT','')}:{v}"
                                                    for k, v in list(sigs.items())[:5]))
    return "\n".join(lines)


def send(msg: str) -> bool:
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        print("no DISCORD_WEBHOOK_URL set")
        return False
    payload = {"content": f"**[Daily Digest]**\n{msg}"}
    files = {}
    dash = Path("dashboard.html")
    if dash.exists():
        files = {"file": ("dashboard.html", dash.read_bytes(), "text/html")}
    try:
        r = requests.post(webhook,
                          data={"payload_json": json.dumps(payload)},
                          files=files or None,
                          headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                          timeout=20)
        ok = r.status_code in (200, 204)
        if not ok:
            print(f"discord {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return ok
    except Exception as e:
        print(f"discord failed: {e}", file=sys.stderr)
        return False


if __name__ == "__main__":
    digest = build_digest()
    print(digest)
    if "--dry-run" not in sys.argv:
        if send(digest):
            print("\nsent to Discord (with dashboard.html attached)")
