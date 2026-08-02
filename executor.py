"""
Executor — automated trade execution for the Donchian trend system.
====================================================================

SANDBOX PRINCIPLE: uses a Futures-only API key (Reading + Futures enabled,
nothing else). It can only trade with USDT already in the USD-M futures
wallet. It cannot touch Spot, Simple Earn, or withdrawals.

What it does each run (invoked by daily_check after the alerter):
  1. Safety gate: kill-file, daily loss limit, config sanity
  2. EXITS first: for each tracked open position, check channel/time exits
     and close at market; detect exchange-side ATR-stop fills
  3. ENTRIES: for each LONG_ENTRY/SHORT_ENTRY verdict (same computation as
     the alerter/dashboard), apply rails, size by risk, place MARKET entry
     + exchange-side STOP_MARKET stop, record to live_trades.csv
  4. Discord report of every action (or inaction reason)

Safety rails (executor_config.json):
  - live: false -> dry-run (log/Discord only, no orders)
  - kill file:  create a file named STOP_TRADING in this folder to halt
  - max_notional_per_trade, max_open_positions
  - daily_loss_limit_pct: equity drop beyond this since last run creates
    STOP_TRADING automatically and halts
  - price sanity: skip entry if mark price deviates >2% from signal close
  - idempotency: a signal id never executes twice (executor_state.json)

Env vars required: BINANCE_API_KEY / BINANCE_API_SECRET (the FUTURES key).

Usage:
    python executor.py            # normal run
    python executor.py --selftest # connectivity/permission check, no orders
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

import secrets_local  # loads secrets.env into os.environ
import dashboard as db
import donchian_baseline as dc
import walk_forward_v3 as wf3
from net_utils import DEFAULT_HEADERS, FUTURES_HOSTS

CONFIG_FILE = Path("executor_config.json")
STATE_FILE = Path("executor_state.json")
KILL_FILE = Path("STOP_TRADING")
TRADES_FILE = Path("live_trades.csv")
ACCOUNT_STATE = Path("account_state.json")

DEFAULT_CONFIG = {
    "live": True,
    "risk_per_trade": 0.0075,
    "max_notional_per_trade": 40.0,
    "max_open_positions": 4,
    "daily_loss_limit_pct": 5.0,
    "leverage": 3,
    "margin_type": "ISOLATED",
    "price_sanity_pct": 2.0,
}


# ============================================================================
# Signed request plumbing (futures)
# ============================================================================

def _creds():
    key = os.environ.get("BINANCE_API_KEY")
    sec = os.environ.get("BINANCE_API_SECRET")
    if not key or not sec:
        print("ERROR: BINANCE_API_KEY / BINANCE_API_SECRET not set", file=sys.stderr)
        sys.exit(1)
    return key, sec


def fapi(method: str, path: str, params: dict | None = None,
         timeout: int = 15):
    key, sec = _creds()
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 10_000
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(sec.encode(), qs.encode(), hashlib.sha256).hexdigest()
    headers = dict(DEFAULT_HEADERS)
    headers["X-MBX-APIKEY"] = key

    last_err = None
    for host in FUTURES_HOSTS:
        url = f"https://{host}{path}?{qs}&signature={sig}"
        try:
            r = requests.request(method, url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 451:
                last_err = RuntimeError(f"451 at {host}")
                continue
            # surface Binance error body for diagnostics
            raise RuntimeError(f"{r.status_code} {path}: {r.text[:300]}")
        except requests.exceptions.RequestException as e:
            last_err = e
            continue
    raise last_err if last_err else RuntimeError(f"all hosts failed: {path}")


def fapi_public(path: str, params: dict | None = None, timeout: int = 15):
    for host in FUTURES_HOSTS:
        try:
            r = requests.get(f"https://{host}{path}", params=params,
                             headers=DEFAULT_HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except requests.exceptions.RequestException:
            continue
    raise RuntimeError(f"public fetch failed: {path}")


# ============================================================================
# Exchange filters (quantity/price rounding)
# ============================================================================

_FILTERS: dict = {}


def load_filters():
    global _FILTERS
    if _FILTERS:
        return
    info = fapi_public("/fapi/v1/exchangeInfo")
    for s in info.get("symbols", []):
        f = {"stepSize": 0.001, "tickSize": 0.01, "minNotional": 5.0, "minQty": 0.001}
        for flt in s.get("filters", []):
            if flt["filterType"] == "LOT_SIZE":
                f["stepSize"] = float(flt["stepSize"])
                f["minQty"] = float(flt["minQty"])
            elif flt["filterType"] == "PRICE_FILTER":
                f["tickSize"] = float(flt["tickSize"])
            elif flt["filterType"] == "MIN_NOTIONAL":
                f["minNotional"] = float(flt.get("notional", 5.0))
        _FILTERS[s["symbol"]] = f


def round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step


def fmt_qty(symbol: str, qty: float) -> str:
    step = _FILTERS.get(symbol, {}).get("stepSize", 0.001)
    q = round_step(qty, step)
    decimals = max(0, -int(round(math.log10(step)))) if step < 1 else 0
    return f"{q:.{decimals}f}"


def fmt_price(symbol: str, price: float) -> str:
    tick = _FILTERS.get(symbol, {}).get("tickSize", 0.01)
    p = round_step(price, tick)
    decimals = max(0, -int(round(math.log10(tick)))) if tick < 1 else 0
    return f"{p:.{decimals}f}"


# ============================================================================
# State / config / notification
# ============================================================================

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    else:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


COLOR_GREEN = 0x2ECC71   # entries
COLOR_RED = 0xE74C3C     # exits / halts
COLOR_GRAY = 0x95A5A6    # routine


def notify(message: str, urgent: bool = False, title: str = "",
           color: int = COLOR_GRAY) -> None:
    """Discord delivery. urgent=True sends an @everyone ping + a colored
    embed so real trades look NOTHING like routine FYI messages."""
    print(message)
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        return
    if urgent:
        body = {
            "content": "@everyone",
            "embeds": [{
                "title": title or "TRADE EVENT",
                "description": message[:3900],
                "color": color,
            }],
        }
    else:
        body = {"content": message[:1900]}
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=payload,
        headers={"Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as e:
        print(f"  WARN: Discord failed: {e}", file=sys.stderr)


def halt(reason: str) -> None:
    KILL_FILE.write_text(f"halted {datetime.now(timezone.utc).isoformat()}: {reason}",
                         encoding="utf-8")
    notify(f"{reason}\n\nTrading stopped. Delete the STOP_TRADING file to "
           f"re-enable after you investigate.",
           urgent=True, title="🛑 EXECUTOR HALTED", color=COLOR_RED)


# ============================================================================
# Account helpers
# ============================================================================

def futures_account() -> dict:
    return fapi("GET", "/fapi/v2/account")


def open_exchange_positions(acct: dict) -> dict:
    out = {}
    for p in acct.get("positions", []):
        amt = float(p.get("positionAmt", 0))
        if abs(amt) > 0:
            out[p["symbol"]] = {
                "amount": amt,
                "entry_price": float(p["entryPrice"]),
                "unrealized": float(p["unRealizedProfit"]),
            }
    return out


def total_equity_for_sizing(acct: dict) -> float:
    """Prefer full account equity from last sync (incl. Simple Earn) so
    sizing reflects total capital; fall back to futures margin balance."""
    try:
        s = json.loads(ACCOUNT_STATE.read_text(encoding="utf-8"))
        synced = pd.Timestamp(s["synced_at"])
        if pd.Timestamp.now(tz="UTC") - synced < pd.Timedelta(days=3):
            v = float(s.get("total_equity_usd", 0))
            if v > 0:
                return v
    except Exception:
        pass
    return float(acct.get("totalMarginBalance", 0))


def mark_price(symbol: str) -> float:
    d = fapi_public("/fapi/v1/premiumIndex", {"symbol": symbol})
    return float(d["markPrice"])


# ============================================================================
# live_trades.csv helpers
# ============================================================================

TRADE_COLS = ["symbol", "side", "entry_date", "exit_date", "entry_price",
              "exit_price", "size", "pnl_gross", "pnl_net", "r_multiple",
              "exit_reason", "bars_held"]


def load_trades() -> pd.DataFrame:
    if TRADES_FILE.exists():
        df = pd.read_csv(TRADES_FILE)
        return df
    return pd.DataFrame(columns=TRADE_COLS)


def append_entry(symbol: str, side: int, price: float, qty: float,
                 stop: float) -> None:
    df = load_trades()
    row = {c: "" for c in TRADE_COLS}
    row.update({
        "symbol": symbol, "side": side,
        "entry_date": datetime.now(timezone.utc).isoformat(),
        "exit_date": "", "entry_price": price, "size": qty if side > 0 else -qty,
    })
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df.to_csv(TRADES_FILE, index=False)


def write_trade_review(symbol: str, side: int, entry_px: float,
                       exit_px: float, reason: str, r_realized,
                       signal_px: float | None, bars_held) -> None:
    """Post-trade review card: execution quality per closed trade.
    Feeds the 20-trade calibration with per-trade slippage detail."""
    slip_bps = ""
    if signal_px and signal_px > 0:
        # exit slippage vs the signal close that triggered the exit
        slip_bps = round((exit_px - signal_px) / signal_px * 1e4 * (1 if side < 0 else -1), 1)
    row = {
        "closed": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol, "side": "LONG" if side > 0 else "SHORT",
        "entry_px": entry_px, "exit_px": exit_px, "exit_reason": reason,
        "r_realized": r_realized, "exit_slip_bps": slip_bps,
        "bars_held": bars_held,
    }
    f = Path("trade_reviews.csv")
    df = pd.DataFrame([row])
    df.to_csv(f, mode="a", header=not f.exists(), index=False)


def close_trade_row(symbol: str, exit_price: float, reason: str,
                    risk_dollars: float) -> None:
    df = load_trades()
    if df.empty:
        return
    mask = (df["symbol"] == symbol) & (df["exit_date"].isna() | (df["exit_date"] == ""))
    idx = df[mask].index
    if len(idx) == 0:
        return
    i = idx[-1]
    entry_price = float(df.at[i, "entry_price"])
    size = float(df.at[i, "size"])
    gross = (exit_price - entry_price) * size
    fees = 0.0009 * abs(size) * (entry_price + exit_price)  # ~4.5bps/side
    net = gross - fees
    df.at[i, "exit_date"] = datetime.now(timezone.utc).isoformat()
    df.at[i, "exit_price"] = exit_price
    df.at[i, "pnl_gross"] = round(gross, 4)
    df.at[i, "pnl_net"] = round(net, 4)
    df.at[i, "r_multiple"] = round(net / risk_dollars, 3) if risk_dollars > 0 else ""
    df.at[i, "exit_reason"] = reason
    bars = ""
    try:
        ed = pd.Timestamp(df.at[i, "entry_date"])
        bars = (pd.Timestamp.now(tz="UTC") - ed).days
        df.at[i, "bars_held"] = bars
    except Exception:
        pass
    df.to_csv(TRADES_FILE, index=False)
    try:
        write_trade_review(symbol, 1 if size > 0 else -1, entry_price,
                           exit_price, reason,
                           df.at[i, "r_multiple"], None, bars)
    except Exception as e:
        print(f"  WARN trade review: {e}", file=sys.stderr)


# ============================================================================
# Order placement
# ============================================================================

def place_entry(symbol: str, side: int, qty_str: str) -> dict:
    return fapi("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "BUY" if side > 0 else "SELL",
        "type": "MARKET",
        "quantity": qty_str,
        "newOrderRespType": "RESULT",
    })


def place_stop(symbol: str, side: int, stop_price_str: str) -> dict:
    """Exchange-side stop: closes the whole position if hit (works 24/7,
    even when this script isn't running)."""
    return fapi("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "SELL" if side > 0 else "BUY",
        "type": "STOP_MARKET",
        "stopPrice": stop_price_str,
        "closePosition": "true",
        "workingType": "MARK_PRICE",
    })


def close_position(symbol: str, side: int, qty_str: str) -> dict:
    return fapi("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "SELL" if side > 0 else "BUY",
        "type": "MARKET",
        "quantity": qty_str,
        "reduceOnly": "true",
        "newOrderRespType": "RESULT",
    })


def cancel_all(symbol: str) -> None:
    try:
        fapi("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
    except Exception:
        pass


def set_margin_and_leverage(symbol: str, leverage: int, margin_type: str) -> None:
    try:
        fapi("POST", "/fapi/v1/marginType",
             {"symbol": symbol, "marginType": margin_type})
    except Exception:
        pass  # already set -> error -4046, ignore
    try:
        fapi("POST", "/fapi/v1/leverage",
             {"symbol": symbol, "leverage": leverage})
    except Exception as e:
        print(f"  WARN leverage {symbol}: {e}", file=sys.stderr)


# ============================================================================
# Main logic
# ============================================================================

def run(selftest: bool = False) -> None:
    cfg = load_config()
    state = load_state()
    now = datetime.now(timezone.utc)
    today_iso = now.strftime("%Y-%m-%d")
    mode = "LIVE" if cfg.get("live") else "DRY-RUN"

    print(f"[{now.isoformat()}] executor starting ({mode})")

    # --- Gate 0: kill file ---
    if KILL_FILE.exists():
        print(f"  STOP_TRADING file present — executor idle. "
              f"({KILL_FILE.read_text(encoding='utf-8')[:100]})")
        return

    # --- Connectivity / permissions ---
    try:
        acct = futures_account()
    except Exception as e:
        notify(f"**[Executor]** cannot reach futures account: {e}")
        sys.exit(1)

    load_filters()
    avail = float(acct.get("availableBalance", 0))
    margin_bal = float(acct.get("totalMarginBalance", 0))
    equity = total_equity_for_sizing(acct)
    ex_pos = open_exchange_positions(acct)

    if selftest:
        print(f"  SELFTEST OK: futures wallet ${margin_bal:.2f} "
              f"(available ${avail:.2f}), {len(ex_pos)} open positions, "
              f"{len(_FILTERS)} symbols in filter cache, sizing equity ${equity:.2f}")
        return

    # --- Gate 1: daily loss limit (transfer-aware) ---
    # Wallet transfers in/out are NOT losses. Adjust the previous balance by
    # net transfers since the last run before computing the drop.
    prev_equity = state.get("last_margin_balance")
    if prev_equity and margin_bal > 0:
        transfers = 0.0
        try:
            last_run = state.get("last_run")
            if last_run:
                start_ms = int(pd.Timestamp(last_run).timestamp() * 1000)
                inc = fapi("GET", "/fapi/v1/income",
                           {"incomeType": "TRANSFER", "startTime": start_ms,
                            "limit": 100})
                transfers = sum(float(i.get("income", 0)) for i in inc)
        except Exception as e:
            print(f"  WARN transfer check failed: {e}", file=sys.stderr)
        expected = float(prev_equity) + transfers
        if expected > 0:
            drop_pct = (expected - margin_bal) / expected * 100.0
            if drop_pct > cfg["daily_loss_limit_pct"]:
                halt(f"futures equity dropped {drop_pct:.1f}% since last run "
                     f"(expected ~${expected:.2f} after ${transfers:+.2f} "
                     f"transfers, actual ${margin_bal:.2f}), limit "
                     f"{cfg['daily_loss_limit_pct']}%")
                return

    # --- Compute signals (same code path as alerter/dashboard) ---
    symbol_data = {}
    for s in db.SYMBOLS:
        try:
            symbol_data[s] = db.fetch_recent(s, bars=300)
            symbol_data[f"_funding_{s}"] = db.fetch_current_funding_bps(s)
        except Exception as e:
            print(f"  WARN fetch {s}: {e}", file=sys.stderr)

    if "BTCUSDT" not in symbol_data or symbol_data["BTCUSDT"].empty:
        notify("**[Executor]** BTCUSDT data missing — no action taken.")
        sys.exit(1)

    btc_regime = wf3.compute_btc_regime(symbol_data["BTCUSDT"])
    macro_on = bool(btc_regime.iloc[-1])

    fng_value = float("nan")
    btc_rel_by_symbol = {}
    try:
        from sentiment_filters import fetch_fear_greed_history, btc_relative_return
        fng_df = fetch_fear_greed_history(limit=30)
        if not fng_df.empty:
            fng_value = float(fng_df["fng_value"].iloc[-1])
        btc_df = symbol_data["BTCUSDT"]
        btc_rel_by_symbol = {
            s: (btc_relative_return(symbol_data[s], btc_df)
                if s != "BTCUSDT" and isinstance(symbol_data.get(s), pd.DataFrame) else None)
            for s in db.SYMBOLS
        }
    except Exception:
        pass

    signals = db.compute_signals(symbol_data, macro_on, fng_value, btc_rel_by_symbol)
    actions = []

    # ------------------------------------------------------------------
    # PHASE 1 — EXITS on tracked positions
    # ------------------------------------------------------------------
    trades = load_trades()
    open_mask = (trades["exit_date"].isna() | (trades["exit_date"] == "")) \
        if not trades.empty else pd.Series(dtype=bool)
    tracked_open = trades[open_mask] if not trades.empty else pd.DataFrame()

    for _, row in tracked_open.iterrows():
        sym = row["symbol"]
        side = 1 if float(row["size"]) > 0 else -1
        qty = abs(float(row["size"]))
        entry_price = float(row["entry_price"])
        risk_dollars = equity * cfg["risk_per_trade"]

        # Case A: exchange position gone -> ATR stop filled on-exchange
        if sym not in ex_pos:
            px = mark_price(sym)
            close_trade_row(sym, px, "atr_stop_exchange", risk_dollars)
            cancel_all(sym)
            actions.append(f"STOP FILLED (exchange): {sym} — recorded exit ~{px}")
            continue

        # Case B: channel / time exit
        df_sym = symbol_data.get(sym)
        if df_sym is None or df_sym.empty:
            continue
        d = dc.build_donchian(df_sym, dc.DCFG)
        last = d.iloc[-1]
        close_px = float(last["close"])
        reason = None
        if side > 0 and not math.isnan(last["exit_low"]) and close_px < last["exit_low"]:
            reason = "channel_exit"
        elif side < 0 and not math.isnan(last["exit_high"]) and close_px > last["exit_high"]:
            reason = "channel_exit"
        else:
            try:
                age = (now - pd.Timestamp(row["entry_date"])).days
                if age >= 90:
                    reason = "time_stop"
            except Exception:
                pass

        if reason:
            qty_str = fmt_qty(sym, qty)
            if cfg.get("live"):
                try:
                    res = close_position(sym, side, qty_str)
                    fill = float(res.get("avgPrice") or close_px)
                    cancel_all(sym)
                    close_trade_row(sym, fill, reason, risk_dollars)
                    actions.append(f"EXIT {('LONG' if side>0 else 'SHORT')} {sym} "
                                   f"@ {fill} ({reason})")
                except Exception as e:
                    actions.append(f"EXIT FAILED {sym}: {e}")
            else:
                actions.append(f"[DRY] would EXIT {sym} @ ~{close_px} ({reason})")

    # ------------------------------------------------------------------
    # PHASE 2 — ENTRIES
    # ------------------------------------------------------------------
    executed = set(state.get("executed_signals", []))
    n_open = len(open_exchange_positions(futures_account())) if cfg.get("live") else len(ex_pos)

    for s in signals:
        if s.verdict not in ("LONG_ENTRY", "SHORT_ENTRY"):
            continue
        sid = f"{today_iso}|{s.symbol}|{s.verdict}"
        if sid in executed:
            continue
        side = 1 if s.verdict == "LONG_ENTRY" else -1

        # Rails
        if n_open >= cfg["max_open_positions"]:
            actions.append(f"SKIP {s.symbol}: max positions ({n_open}) reached")
            continue
        if s.symbol in ex_pos:
            actions.append(f"SKIP {s.symbol}: already have a position")
            continue
        if s.symbol not in _FILTERS:
            actions.append(f"SKIP {s.symbol}: no exchange filters (not tradeable?)")
            continue

        # Price sanity
        try:
            mp = mark_price(s.symbol)
        except Exception as e:
            actions.append(f"SKIP {s.symbol}: mark price failed ({e})")
            continue
        dev = abs(mp - s.last_close) / s.last_close * 100.0
        if dev > cfg["price_sanity_pct"]:
            actions.append(f"SKIP {s.symbol}: mark {mp} deviates {dev:.1f}% "
                           f"from signal close {s.last_close}")
            continue

        # Sizing
        stop = s.last_close - 2 * s.atr if side > 0 else s.last_close + 2 * s.atr
        stop_dist = abs(s.last_close - stop)
        if stop_dist <= 0:
            continue
        risk_dollars = equity * cfg["risk_per_trade"]
        qty = risk_dollars / stop_dist
        notional = qty * mp
        if notional > cfg["max_notional_per_trade"]:
            qty = cfg["max_notional_per_trade"] / mp
            notional = qty * mp
        minN = _FILTERS[s.symbol]["minNotional"]
        if notional < minN:
            actions.append(f"SKIP {s.symbol}: notional ${notional:.2f} < "
                           f"exchange minimum ${minN:.2f} (account too small "
                           f"for this symbol at current risk settings)")
            continue
        qty_str = fmt_qty(s.symbol, qty)
        if float(qty_str) <= 0:
            actions.append(f"SKIP {s.symbol}: quantity rounds to zero")
            continue
        # margin check: need notional/leverage + buffer
        margin_needed = notional / cfg["leverage"] * 1.25
        if cfg.get("live") and margin_needed > avail:
            actions.append(
                f"SKIP {s.symbol}: needs ~${margin_needed:.2f} margin, only "
                f"${avail:.2f} available in futures wallet. Transfer USDT "
                f"from Simple Earn -> Spot -> Futures to enable trading.")
            continue

        stop_str = fmt_price(s.symbol, stop)
        detail = (f"{s.verdict} {s.symbol}: qty {qty_str} (~${notional:.2f}), "
                  f"stop {stop_str}, risk ${risk_dollars:.2f}")

        if cfg.get("live"):
            try:
                set_margin_and_leverage(s.symbol, cfg["leverage"], cfg["margin_type"])
                res = place_entry(s.symbol, side, qty_str)
                fill = float(res.get("avgPrice") or mp)
                place_stop(s.symbol, side, stop_str)
                append_entry(s.symbol, side, fill, float(qty_str), float(stop_str))
                executed.add(sid)
                n_open += 1
                avail -= margin_needed
                actions.append(f"EXECUTED {detail} — filled @ {fill}, "
                               f"exchange stop placed")
            except Exception as e:
                actions.append(f"ENTRY FAILED {s.symbol}: {e}")
        else:
            executed.add(sid)
            actions.append(f"[DRY] would execute {detail}")

    # ------------------------------------------------------------------
    # Persist + report
    # ------------------------------------------------------------------
    state["executed_signals"] = sorted(executed)[-200:]
    state["last_margin_balance"] = margin_bal
    state["last_run"] = now.isoformat()
    save_state(state)

    if actions:
        # Split real trade events from routine skips: trades get the loud
        # treatment (@everyone + colored embed), skips stay plain.
        TRADE_PREFIXES = ("EXECUTED", "EXIT ", "STOP FILLED",
                          "ENTRY FAILED", "EXIT FAILED")
        trade_events = [a for a in actions if a.startswith(TRADE_PREFIXES)]
        routine = [a for a in actions if not a.startswith(TRADE_PREFIXES)]

        if trade_events:
            is_entry = any(a.startswith("EXECUTED") for a in trade_events)
            has_fail = any("FAILED" in a for a in trade_events)
            if has_fail:
                title, color = "⚠️ ORDER PROBLEM — CHECK NOW", COLOR_RED
            elif is_entry:
                title, color = "🟢 TRADE EXECUTED — position opened", COLOR_GREEN
            else:
                title, color = "🔴 POSITION CLOSED", COLOR_RED
            notify("\n\n".join(trade_events), urgent=True,
                   title=f"{title}  [{mode}]", color=color)
        if routine:
            notify(f"**[Executor — {mode}]**\n```\n" + "\n".join(routine) + "\n```")
    else:
        print(f"  no executor actions (macro "
              f"{'ON' if macro_on else 'OFF'}, {len(ex_pos)} open, "
              f"futures wallet ${margin_bal:.2f})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    run(selftest=args.selftest)
