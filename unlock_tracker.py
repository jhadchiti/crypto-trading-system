"""
Token Unlock Shorts — event tracker + paper-trading engine.
============================================================

Implements UNLOCK_SHORTS_SPEC.md. The operator registers events (monthly
calendar research); this script does everything else mechanically.

Commands:
    python unlock_tracker.py add SYMBOL YYYY-MM-DD PCT      # register event
    python unlock_tracker.py check                          # nightly engine
    python unlock_tracker.py status                         # pipeline view
    python unlock_tracker.py remove SYMBOL                  # drop pending event

State: unlock_events.json. Every action Discords via the shared webhook.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import secrets_local  # loads secrets.env into os.environ
from net_utils import fetch_binance_futures

STATE = Path("unlock_events.json")

ENTRY_DAYS_BEFORE = 10
EXIT_DAYS_AFTER = 4
STOP_PCT = 15.0
RT_COST_PCT = 0.18
MIN_PCT_SUPPLY = 5.0
FUNDING_MIN_BPS = -20.0
MAX_CONCURRENT = 3


def load() -> list:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save(events: list) -> None:
    STATE.write_text(json.dumps(events, indent=2, default=str), encoding="utf-8")


def notify(msg: str) -> None:
    print(msg)
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        return
    payload = json.dumps({"content": f"**[Unlock Shorts — paper]**\n```\n{msg[:1800]}\n```"}).encode()
    req = urllib.request.Request(webhook, data=payload,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
    except Exception as e:
        print(f"  WARN discord: {e}", file=sys.stderr)


def mark_price(symbol: str) -> float | None:
    try:
        r = fetch_binance_futures("/fapi/v1/ticker/price", {"symbol": symbol})
        return float(r.json()["price"])
    except Exception:
        return None


def last_funding_bps(symbol: str) -> float:
    try:
        r = fetch_binance_futures("/fapi/v1/fundingRate", {"symbol": symbol, "limit": 1})
        d = r.json()
        return float(d[-1]["fundingRate"]) * 10_000 if d else 0.0
    except Exception:
        return 0.0


# ============================================================================
# Commands
# ============================================================================

def cmd_add(symbol: str, date_str: str, pct: float) -> None:
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    if pct < MIN_PCT_SUPPLY:
        print(f"REJECTED: {pct}% < {MIN_PCT_SUPPLY}% minimum supply unlock (spec §2)")
        return
    if mark_price(symbol) is None:
        print(f"REJECTED: {symbol} has no Binance USDT perp (spec §2)")
        return
    events = load()
    if any(e["symbol"] == symbol and e["status"] in ("pending", "open") for e in events):
        print(f"REJECTED: {symbol} already has an active event")
        return
    events.append({
        "symbol": symbol,
        "unlock_date": date_str,
        "pct_supply": pct,
        "status": "pending",
        "registered": datetime.now(timezone.utc).isoformat(),
    })
    save(events)
    entry_on = (pd.Timestamp(date_str) - pd.Timedelta(days=ENTRY_DAYS_BEFORE)).date()
    print(f"registered: {symbol} unlock {date_str} ({pct}% supply). "
          f"Paper short opens automatically on ~{entry_on} (T-{ENTRY_DAYS_BEFORE}).")


def cmd_remove(symbol: str) -> None:
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    events = load()
    before = len(events)
    events = [e for e in events if not (e["symbol"] == symbol and e["status"] == "pending")]
    save(events)
    print(f"removed {before - len(events)} pending event(s) for {symbol}")


def cmd_check() -> None:
    now = pd.Timestamp.now(tz="UTC")
    events = load()
    actions = []
    n_open = sum(1 for e in events if e["status"] == "open")

    for e in events:
        unlock = pd.Timestamp(e["unlock_date"], tz="UTC")

        if e["status"] == "pending":
            days_to = (unlock - now).days
            if days_to < -1:
                e["status"] = "missed"
                actions.append(f"MISSED {e['symbol']}: unlock date passed while pending")
                continue
            if days_to <= ENTRY_DAYS_BEFORE:
                if n_open >= MAX_CONCURRENT:
                    actions.append(f"SKIP {e['symbol']}: {MAX_CONCURRENT} events already open")
                    continue
                fund = last_funding_bps(e["symbol"])
                if fund < FUNDING_MIN_BPS:
                    e["status"] = "skipped_crowded"
                    actions.append(f"SKIP {e['symbol']}: funding {fund:+.1f}bps < "
                                   f"{FUNDING_MIN_BPS} — shorts crowded, edge priced (spec §2.4)")
                    continue
                px = mark_price(e["symbol"])
                if px is None:
                    continue
                e["status"] = "open"
                e["entry_px"] = px
                e["entry_time"] = now.isoformat()
                n_open += 1
                actions.append(
                    f"PAPER SHORT OPENED: {e['symbol']} @ {px} "
                    f"(unlock {e['unlock_date']}, {e['pct_supply']}% supply, T-{days_to}d)\n"
                    f"    exit: T+{EXIT_DAYS_AFTER}d or +{STOP_PCT}% stop")

        elif e["status"] == "open":
            px = mark_price(e["symbol"])
            if px is None:
                continue
            entry_px = float(e["entry_px"])
            move_pct = (px - entry_px) / entry_px * 100      # + = against short
            days_after = (now - unlock).days
            reason = None
            if move_pct >= STOP_PCT:
                reason = "stopped"
            elif days_after >= EXIT_DAYS_AFTER:
                reason = "completed"
            if reason:
                net = -move_pct - RT_COST_PCT                # short: profit when price fell
                e["status"] = reason
                e["exit_px"] = px
                e["exit_time"] = now.isoformat()
                e["net_pct"] = round(net, 2)
                actions.append(
                    f"PAPER SHORT CLOSED ({reason}): {e['symbol']} "
                    f"{entry_px} -> {px} = net {net:+.2f}% after costs")

    save(events)

    done = [e for e in events if e["status"] in ("completed", "stopped")]
    if actions:
        summary = f"\nTrial progress: {len(done)}/10 events complete"
        if done:
            wins = sum(1 for e in done if e.get("net_pct", 0) > 0)
            mean = sum(e.get("net_pct", 0) for e in done) / len(done)
            summary += f" — {wins} wins, mean {mean:+.2f}%"
        notify("\n".join(actions) + summary)
    else:
        pend = sum(1 for e in events if e["status"] == "pending")
        print(f"  no unlock actions ({pend} pending, {n_open} open, "
              f"{len(done)}/10 trial events complete)")

    # decision-rule readout at 10 completed
    if len(done) >= 10:
        wins = sum(1 for e in done[:10] if e.get("net_pct", 0) > 0)
        mean = sum(e.get("net_pct", 0) for e in done[:10]) / 10
        verdict = ("PROMOTE to live (spec §6)" if wins >= 7 and mean > 1.0 else
                   "EXTEND 5 events" if wins in (5, 6) else
                   "TOMBSTONE (spec §6)")
        notify(f"UNLOCK TRIAL COMPLETE: {wins}/10 wins, mean {mean:+.2f}% -> {verdict}")


def cmd_status() -> None:
    events = load()
    if not events:
        print("no events registered. Add with:\n"
              "  python unlock_tracker.py add SYMBOL YYYY-MM-DD PCT_SUPPLY")
        return
    for e in sorted(events, key=lambda x: x["unlock_date"]):
        line = f"  {e['symbol']:<14} unlock {e['unlock_date']}  {e['pct_supply']}%  [{e['status']}]"
        if e.get("entry_px"):
            line += f"  entry {e['entry_px']}"
        if e.get("net_pct") is not None:
            line += f"  net {e['net_pct']:+.2f}%"
        print(line)
    done = [e for e in events if e["status"] in ("completed", "stopped")]
    if done:
        wins = sum(1 for e in done if e.get("net_pct", 0) > 0)
        mean = sum(e.get("net_pct", 0) for e in done) / len(done)
        print(f"\ntrial: {len(done)}/10 complete, {wins} wins, mean {mean:+.2f}%")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "check":
        cmd_check()
    elif args[0] == "status":
        cmd_status()
    elif args[0] == "add" and len(args) == 4:
        cmd_add(args[1], args[2], float(args[3]))
    elif args[0] == "remove" and len(args) == 2:
        cmd_remove(args[1])
    else:
        print(__doc__)
