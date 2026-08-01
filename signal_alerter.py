"""
Signal Alerter — daily run, alerts only when there's something actionable.
==========================================================================

Designed for the paper-trading and live-deployment phase. Schedule this to
run once a day (e.g. UTC 00:05, just after the daily candle closes) and it
will:

  - Print and log a SUMMARY only if anything actionable is happening:
      * a new LONG_ENTRY or SHORT_ENTRY verdict fires
      * an existing open position now meets an exit condition
      * the BTC macro regime has flipped since last run
  - If nothing actionable, exit quietly (one line: "no actions")

This means most days you'll get nothing, which is the point. When you DO get
an alert, it's worth your attention.

Channels:
  - stdout                            (always)
  - alerts.log                        (always, append-only)
  - Discord webhook                   (if DISCORD_WEBHOOK_URL env var set)
  - SMTP email                        (if all SMTP_* env vars set)

Schedule on Windows:
  schtasks /create /tn "CryptoAlerter" /tr ^
    "C:\\Path\\To\\.venv\\Scripts\\python.exe C:\\Path\\To\\signal_alerter.py" ^
    /sc daily /st 00:05

Reads:
  live_trades.csv      — same format as dashboard.py
  alerter_state.json   — internal state (last regime, alerted signal IDs)

Usage:
    python signal_alerter.py
    python signal_alerter.py --force        # alert even on stale signals
    python signal_alerter.py --dry-run      # compute but skip persistence/webhook
"""

from __future__ import annotations

import argparse
import json
import math
import os
import smtplib
import sys
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import pandas as pd

import secrets_local  # loads secrets.env into os.environ
import dashboard as db   # reuse all the signal-computation machinery
import donchian_baseline as dc
import walk_forward_v3 as wf3


STATE_FILE = Path("alerter_state.json")
LOG_FILE = Path("alerts.log")
TRADES_FILE = Path("live_trades.csv")


# ============================================================================
# State persistence
# ============================================================================

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def signal_id(symbol: str, verdict: str, date_iso: str) -> str:
    """A unique id per actionable signal per day, so we don't re-alert."""
    return f"{date_iso}|{symbol}|{verdict.split(' ')[0]}"


# ============================================================================
# Exit checks for open positions
# ============================================================================

def open_position_exit_triggers(open_df: pd.DataFrame,
                                symbol_data: dict) -> list[dict]:
    """For each open position, return what exit rules (if any) now fire."""
    triggers = []
    if open_df.empty:
        return triggers
    for _, row in open_df.iterrows():
        sym = row["symbol"]
        df = symbol_data.get(sym)
        if df is None or df.empty:
            continue

        d = dc.build_donchian(df, dc.DCFG)
        last = d.iloc[-1]
        close = float(last["close"])
        side = int(row["side"])
        stop = float(row.get("stop", row.get("initial_stop", float("nan"))))
        entry_date = pd.Timestamp(row["entry_date"])
        bars_held = (pd.Timestamp.now(tz="UTC") - entry_date).days

        triggered = []

        # channel exit
        if side > 0 and not math.isnan(last["exit_low"]) and close < last["exit_low"]:
            triggered.append(("channel_exit", f"close {close:.2f} < 20-day low {last['exit_low']:.2f}"))
        elif side < 0 and not math.isnan(last["exit_high"]) and close > last["exit_high"]:
            triggered.append(("channel_exit", f"close {close:.2f} > 20-day high {last['exit_high']:.2f}"))

        # ATR stop (using last close; live execution would use intra-bar low/high)
        if not math.isnan(stop):
            if side > 0 and close <= stop:
                triggered.append(("atr_stop", f"close {close:.2f} <= stop {stop:.2f}"))
            elif side < 0 and close >= stop:
                triggered.append(("atr_stop", f"close {close:.2f} >= stop {stop:.2f}"))

        # time stop
        if bars_held >= 90:
            triggered.append(("time_stop", f"held {bars_held} days >= 90"))

        if triggered:
            triggers.append({
                "symbol": sym,
                "side": "LONG" if side > 0 else "SHORT",
                "entry_date": entry_date.strftime("%Y-%m-%d"),
                "entry_price": float(row["entry_price"]),
                "current_price": close,
                "bars_held": bars_held,
                "triggers": triggered,
            })
    return triggers


# ============================================================================
# Alert delivery
# ============================================================================

def deliver_discord(webhook: str, message: str) -> None:
    payload = json.dumps({"content": message[:1900]}).encode("utf-8")
    # User-Agent required: Cloudflare 403s the default Python-urllib UA
    req = urllib.request.Request(webhook, data=payload,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as e:
        print(f"  WARN: Discord delivery failed: {e}", file=sys.stderr)


def deliver_email(subject: str, body: str) -> None:
    host = os.environ.get("SMTP_HOST"); port = os.environ.get("SMTP_PORT")
    user = os.environ.get("SMTP_USER"); pw = os.environ.get("SMTP_PASS")
    to = os.environ.get("SMTP_TO", user)
    if not all([host, port, user, pw, to]):
        return
    msg = MIMEText(body)
    msg["Subject"] = subject; msg["From"] = user; msg["To"] = to
    try:
        with smtplib.SMTP_SSL(host, int(port), timeout=15) as s:
            s.login(user, pw); s.sendmail(user, [to], msg.as_string())
    except Exception as e:
        print(f"  WARN: SMTP delivery failed: {e}", file=sys.stderr)


def log_alert(message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"\n[{ts}]\n{message}\n")


# ============================================================================
# Message assembly
# ============================================================================

def build_message(actions: list[str], regime_flip: Optional[str],
                  signals_summary: str) -> str:
    parts = []
    if regime_flip:
        parts.append(f"REGIME: {regime_flip}")
    if actions:
        parts.append("ACTIONS:")
        for a in actions:
            parts.append(f"  - {a}")
    parts.append("")
    parts.append("Today's signals:")
    parts.append(signals_summary)
    return "\n".join(parts)


def signals_summary_text(signals) -> str:
    lines = []
    for s in signals:
        lines.append(f"  {s.symbol:<10}  px={s.last_close:>10.4f}  "
                     f"fund={s.funding_bps:+6.2f}bps  "
                     f"adx={s.adx:>5.1f}  -> {s.verdict}")
    return "\n".join(lines) if lines else "  (no signal rows)"


# ============================================================================
# Main
# ============================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="emit alert even for signals already alerted today")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print but don't update state or deliver")
    args = ap.parse_args()

    state = load_state()
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    alerted_today = set(state.get("alerted_today", []))
    if state.get("alerted_today_date") != today_iso:
        alerted_today = set()

    # --- fetch + compute signals (reuses dashboard.py machinery) ---
    print(f"[{datetime.now(timezone.utc).isoformat()}] running alerter ...")
    symbol_data = {}
    for s in db.SYMBOLS:
        try:
            df = db.fetch_recent(s, bars=300)
            symbol_data[s] = df
            symbol_data[f"_funding_{s}"] = db.fetch_current_funding_bps(s)
        except Exception as e:
            print(f"  WARN: fetch failed for {s}: {e}", file=sys.stderr)

    if "BTCUSDT" not in symbol_data or symbol_data["BTCUSDT"].empty:
        print("BTCUSDT missing — cannot determine BTC macro. aborting.")
        sys.exit(1)

    btc_regime = wf3.compute_btc_regime(symbol_data["BTCUSDT"])
    macro_on_now = bool(btc_regime.iloc[-1])
    macro_on_prev = state.get("btc_macro_on")
    regime_flip = None
    if macro_on_prev is not None and macro_on_prev != macro_on_now:
        regime_flip = (f"BTC macro flipped {'ON' if macro_on_now else 'OFF'} "
                       f"(was {'ON' if macro_on_prev else 'OFF'}). "
                       f"{'Strategy now enabled.' if macro_on_now else 'No new entries; manage open positions normally.'}")

    # compute_signals (v3) requires fng_value + btc_rel_by_symbol
    fng_value_today = float("nan")
    try:
        from sentiment_filters import fetch_fear_greed_history, btc_relative_return
        fng_df = fetch_fear_greed_history(limit=30)
        if not fng_df.empty:
            fng_value_today = float(fng_df["fng_value"].iloc[-1])
        btc_df = symbol_data["BTCUSDT"]
        btc_rel_by_symbol = {
            s: (btc_relative_return(symbol_data[s], btc_df)
                if s != "BTCUSDT" and isinstance(symbol_data.get(s), pd.DataFrame) else None)
            for s in db.SYMBOLS
        }
    except Exception as e:
        print(f"  WARN: sentiment/btc-rel fetch failed: {e}", file=sys.stderr)
        btc_rel_by_symbol = {}

    signals = db.compute_signals(symbol_data, macro_on_now, fng_value_today, btc_rel_by_symbol)

    # --- check open positions for exit triggers ---
    trades_df = db.load_live_trades() if TRADES_FILE.exists() else pd.DataFrame()
    open_df, _ = db.split_open_closed(trades_df) if not trades_df.empty else (pd.DataFrame(), pd.DataFrame())
    exit_triggers = open_position_exit_triggers(open_df, symbol_data) if not open_df.empty else []

    # --- RS rank detail for rationale (30d return vs BTC, rank in universe) ---
    def rs_rank_line(symbol: str) -> str:
        try:
            btc = symbol_data["BTCUSDT"]
            lb = db.REL_STRENGTH_LOOKBACK
            btc_ret = float(btc["close"].iloc[-1] / btc["close"].iloc[-lb - 1] - 1)
            rs = {}
            for s2, df2 in symbol_data.items():
                if s2.startswith("_") or s2 == "BTCUSDT":
                    continue
                if isinstance(df2, pd.DataFrame) and len(df2) > lb:
                    rs[s2] = float(df2["close"].iloc[-1] / df2["close"].iloc[-lb - 1] - 1) - btc_ret
            ranked = sorted(rs.items(), key=lambda kv: -kv[1])
            pos = next((i + 1 for i, (s2, _) in enumerate(ranked) if s2 == symbol), None)
            if pos is None:
                return "rel-strength: n/a (benchmark)" if symbol == "BTCUSDT" else "rel-strength: n/a"
            return (f"rel-strength: rank {pos}/{len(ranked)} "
                    f"({rs[symbol]:+.1%} vs BTC over {lb}d) — top-quintile PASS")
        except Exception:
            return "rel-strength: n/a"

    # --- collect actionable items ---
    actions = []
    new_signal_ids = []
    for s in signals:
        if s.verdict in ("LONG_ENTRY", "SHORT_ENTRY"):
            sid = signal_id(s.symbol, s.verdict, today_iso)
            if sid in alerted_today and not args.force:
                continue
            new_signal_ids.append(sid)
            is_long = s.verdict == "LONG_ENTRY"
            stop = s.last_close - 2 * s.atr if is_long else s.last_close + 2 * s.atr
            risk_frac = abs(s.last_close - stop) / s.last_close
            channel_ref = s.entry_high if is_long else s.entry_low
            rationale = "\n".join([
                f"{s.verdict} {s.symbol} @ {s.last_close:.4f}",
                f"    WHY:",
                f"    - breakout: close {s.last_close:.4f} "
                f"{'>' if is_long else '<'} 55d {'high' if is_long else 'low'} {channel_ref:.4f}",
                f"    - BTC macro: ON (SMA200 rising — trend regime)",
                f"    - funding: {s.funding_bps:+.1f}bps/8h (within ±20 limit, not crowded)",
                f"    - {rs_rank_line(s.symbol)}",
                f"    EXECUTION:",
                f"    - stop: {stop:.4f} (2xATR={s.atr:.4f}); exit also on 20d "
                f"{'low' if is_long else 'high'} or 90d",
                f"    - size: risk 0.75% of equity / {risk_frac:.1%} stop distance "
                f"= {0.0075 / max(risk_frac, 1e-9):.1%} of equity as notional",
                f"    - then log the fill in live_trades.csv",
            ])
            actions.append(rationale)

    for t in exit_triggers:
        for reason, detail in t["triggers"]:
            actions.append(
                f"EXIT {t['side']} {t['symbol']} ({reason}): {detail}  "
                f"[entered {t['entry_date']} @ {t['entry_price']:.4f}, "
                f"held {t['bars_held']}d]"
            )

    # --- FYI: blocked breakouts (informational, once per symbol per day) ---
    # A breakout that a filter rejected is not actionable, but seeing WHY
    # builds trust in the filters instead of suspicion of them.
    BLOCK_EXPLAIN = {
        "BLOCKED_BTC_MACRO": ("BTC 200d SMA falling — alt breakouts in BTC "
                              "downtrends fail at much higher rates "
                              "(validated: removing this filter degraded "
                              "every walk-forward variant)"),
        "BLOCKED_REL_STRENGTH": ("not in top 20% of 30d return vs BTC — "
                                 "beta-breakout, not a leader (v7: filter "
                                 "lifted expectancy +69%)"),
    }
    fyi = []
    for s in signals:
        if not s.verdict.startswith("BLOCKED"):
            continue
        sid = signal_id(s.symbol, f"FYI_{s.verdict}", today_iso)
        if sid in alerted_today and not args.force:
            continue
        new_signal_ids.append(sid)
        base_verdict = s.verdict.split(" ")[0]
        why = BLOCK_EXPLAIN.get(base_verdict, s.verdict)
        direction = ("LONG" if not math.isnan(s.entry_high)
                     and s.last_close > s.entry_high else "SHORT")
        fyi.append(
            f"FYI — breakout NOT taken: {s.symbol} ({direction}) broke its "
            f"55d channel at {s.last_close:.4f}\n"
            f"    blocked by {base_verdict}: {why}\n"
            f"    no action needed — this is the system declining a "
            f"statistically bad bet")

    # --- emit (or stay silent) ---
    if fyi and not actions and not regime_flip:
        # informational-only message (don't inflate the ACTIONS framing)
        body = "\n\n".join(fyi)
        print(body)
        if not args.dry_run:
            log_alert(f"[FYI blocked signals]\n{body}")
            webhook = os.environ.get("DISCORD_WEBHOOK_URL")
            if webhook:
                deliver_discord(webhook, f"**[Crypto — FYI, no action needed]**\n```\n{body}\n```")
            state["btc_macro_on"] = macro_on_now
            state["alerted_today"] = list(alerted_today | set(new_signal_ids))
            state["alerted_today_date"] = today_iso
            state["last_run"] = today_iso
            save_state(state)
        return

    if not actions and not regime_flip:
        print("  no actions  (BTC macro {}; {} open positions)".format(
            "ON" if macro_on_now else "OFF", len(open_df)))
        if not args.dry_run:
            state["btc_macro_on"] = macro_on_now
            state["last_run"] = today_iso
            save_state(state)
        return

    if fyi:
        actions = actions + [""] + fyi   # append FYIs below real actions
    body = build_message(actions, regime_flip, signals_summary_text(signals))
    subject = f"[Crypto Alerter] {len(actions)} action{'s' if len(actions)!=1 else ''}"
    if regime_flip:
        subject += " + regime flip"

    print()
    print("=" * 60)
    print(subject)
    print("=" * 60)
    print(body)
    print("=" * 60)

    if args.dry_run:
        print("\n(dry-run: state not saved, no webhook/email sent)")
        return

    log_alert(f"{subject}\n{body}")

    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if webhook:
        deliver_discord(webhook, f"**{subject}**\n```\n{body}\n```")

    deliver_email(subject, body)

    state["btc_macro_on"] = macro_on_now
    state["alerted_today"] = list(alerted_today | set(new_signal_ids))
    state["alerted_today_date"] = today_iso
    state["last_run"] = today_iso
    save_state(state)


if __name__ == "__main__":
    main()
