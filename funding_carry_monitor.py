"""
Funding Carry Paper-Trading Monitor
====================================

Daily-run companion to FUNDING_CARRY_SPEC.md §9 step 2 (30-day paper phase).

Each run:
  1. Fetches recent funding events (last ~5 days) for the liquid universe
  2. Computes the trailing 9-event (3-day) mean funding per symbol
  3. Applies the entry/exit state machine to HYPOTHETICAL positions stored
     in paper_carry_state.json
  4. On entry/exit, appends the episode to paper_carry_log.csv and prints
     an ACTION line (picked up by daily_check -> automation.log)
  5. Prints a one-line status when nothing is actionable

This never places orders. It exists to answer, after 30 days:
  "Did the live-observed episodes match what the backtest predicted?"

Usage:
    python funding_carry_monitor.py
    python funding_carry_monitor.py --status     # just show current state
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import os
import urllib.request

from funding import fetch_funding
from market_data import fetch_all_perp_symbols, filter_by_history, fetch_24h_ticker_all


def deliver_discord(message: str) -> None:
    """Send to Discord if DISCORD_WEBHOOK_URL is set. UA header required
    (Cloudflare 403s the default Python-urllib agent)."""
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

# Spec parameters (keep in sync with FUNDING_CARRY_SPEC.md)
ENTRY_BPS = 10.0
EXIT_BPS = 3.0
HARD_EXIT_BPS = -5.0
TRAIL_EVENTS = 9
MAX_CONCURRENT = 3
MIN_VOL_USD = 50_000_000
UNIVERSE_TOP_N = 40

STATE_FILE = Path("paper_carry_state.json")
LOG_FILE = Path("paper_carry_log.csv")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"open": {}}
    return {"open": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def append_log(row: dict) -> None:
    df = pd.DataFrame([row])
    header = not LOG_FILE.exists()
    df.to_csv(LOG_FILE, mode="a", header=header, index=False)


def fetch_trailing_funding(symbol: str) -> pd.DataFrame:
    """Last ~5 days of 8h funding events."""
    end_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    start_ms = end_ms - 5 * 86400 * 1000
    return fetch_funding(symbol, start_ms, end_ms)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="show state, no fetch")
    args = ap.parse_args()

    state = load_state()
    open_pos: dict = state.get("open", {})

    if args.status:
        print(f"Open paper carry positions: {len(open_pos)}")
        for sym, p in open_pos.items():
            print(f"  {sym}: entered {p['entry_time']}, accrued {p['accrued_bps']:+.1f}bps")
        if LOG_FILE.exists():
            df = pd.read_csv(LOG_FILE)
            closed = df[df["action"] == "EXIT"]
            if not closed.empty:
                print(f"Closed episodes: {len(closed)}, "
                      f"mean net {closed['net_bps'].mean():+.0f}bps, "
                      f"win rate {(closed['net_bps'] > 0).mean():.0%}")
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    print(f"[{now_iso}] funding carry monitor ...")

    # --- Universe ---
    try:
        universe = fetch_all_perp_symbols()
        qualified = filter_by_history(universe, 180)
        ticker = fetch_24h_ticker_all()
        vol_map = dict(zip(ticker["symbol"], ticker["quoteVolume"]))
        syms = [s for s in qualified["symbol"].tolist()
                if vol_map.get(s, 0) >= MIN_VOL_USD]
        syms.sort(key=lambda s: -vol_map.get(s, 0))
        syms = syms[:UNIVERSE_TOP_N]
    except Exception as e:
        print(f"  universe fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Always include symbols we already hold (need exit checks even if they
    # fell out of the volume ranking)
    watch = list(dict.fromkeys(list(open_pos.keys()) + syms))

    actions = []
    trail_by_sym = {}
    for sym in watch:
        try:
            ev = fetch_trailing_funding(sym)
            if ev.empty or len(ev) < TRAIL_EVENTS:
                continue
            bps = ev["funding_rate"] * 10_000.0
            trail_mean = float(bps.tail(TRAIL_EVENTS).mean())
            last_bps = float(bps.iloc[-1])
            trail_by_sym[sym] = (trail_mean, last_bps)
        except Exception as e:
            print(f"  WARN {sym}: {e}", file=sys.stderr)

    # --- Exit checks on open paper positions ---
    for sym in list(open_pos.keys()):
        tm_last = trail_by_sym.get(sym)
        if tm_last is None:
            continue
        trail_mean, last_bps = tm_last
        pos = open_pos[sym]
        # accrue latest event
        pos["accrued_bps"] = float(pos.get("accrued_bps", 0.0)) + last_bps
        pos["n_events"] = int(pos.get("n_events", 0)) + 1

        reason = None
        if last_bps < HARD_EXIT_BPS:
            reason = "hard_flip"
        elif trail_mean <= EXIT_BPS:
            reason = "funding_decay"
        else:
            age_days = (pd.Timestamp.now(tz="UTC")
                        - pd.Timestamp(pos["entry_time"])).days
            if age_days >= 90:
                reason = "time_stop"

        if reason:
            net = pos["accrued_bps"] - 48.0   # spec cost model
            why = {
                "hard_flip": "single funding event < -5bps — regime flipped, exit immediately",
                "funding_decay": "3d mean funding fell to <= 3bps — carry no longer pays",
                "time_stop": "position age >= 90d — stale-position audit",
            }.get(reason, reason)
            actions.append(
                f"CARRY EXIT {sym} ({reason}): accrued {pos['accrued_bps']:+.0f}bps, "
                f"net {net:+.0f}bps over {pos['n_events']} events\n"
                f"    WHY: {why}\n"
                f"    ACTION (paper): close short perp + sell spot; log outcome")
            append_log({
                "time": now_iso, "action": "EXIT", "symbol": sym,
                "reason": reason, "entry_time": pos["entry_time"],
                "n_events": pos["n_events"],
                "gross_bps": round(pos["accrued_bps"], 1),
                "net_bps": round(net, 1),
            })
            del open_pos[sym]

    # --- Entry checks ---
    if len(open_pos) < MAX_CONCURRENT:
        candidates = [
            (sym, tm) for sym, (tm, _) in trail_by_sym.items()
            if sym not in open_pos and tm >= ENTRY_BPS
        ]
        candidates.sort(key=lambda x: -x[1])
        for sym, tm in candidates[:MAX_CONCURRENT - len(open_pos)]:
            open_pos[sym] = {
                "entry_time": now_iso,
                "entry_trail_bps": round(tm, 2),
                "accrued_bps": 0.0,
                "n_events": 0,
            }
            apr = tm * 3 * 365 / 100.0   # bps/8h -> % APR
            actions.append(
                f"CARRY ENTRY {sym}: 3d mean funding {tm:+.1f}bps/8h\n"
                f"    WHY: funding sustained >= 10bps/8h for 3 days — crowded longs "
                f"paying shorts ~{apr:.0f}% APR; delta-neutral carry captures it\n"
                f"    ACTION (paper): long spot + short perp, equal notional, "
                f"max 2x perp leverage; exit alerts will follow automatically")
            append_log({
                "time": now_iso, "action": "ENTRY", "symbol": sym,
                "reason": f"trail_{tm:+.1f}bps", "entry_time": now_iso,
                "n_events": 0, "gross_bps": 0.0, "net_bps": 0.0,
            })

    state["open"] = open_pos
    state["last_run"] = now_iso
    save_state(state)

    if actions:
        print("ACTIONS:")
        for a in actions:
            print(f"  - {a}")
        deliver_discord("**[Funding Carry — paper]**\n```\n" + "\n\n".join(actions) + "\n```")
    else:
        best = max(trail_by_sym.values(), key=lambda x: x[0])[0] if trail_by_sym else float("nan")
        print(f"  no carry actions  ({len(open_pos)} open paper positions; "
              f"best 3d-mean funding {best:+.1f}bps vs entry {ENTRY_BPS:.0f}bps)")


if __name__ == "__main__":
    main()
