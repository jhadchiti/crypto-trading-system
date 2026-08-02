"""
Operational Dashboard v3 — full picture for the systematic trader.
====================================================================

Eight sections answering the questions a serious operator needs at a glance:

  1. Performance KPI strip      what's the bottom line vs BTC buy-hold
  2. System state               BTC macro, heat, system OK, deployment stage
  3. Regime state               vol/corr/funding regime + risk multiplier
  4. Today's signals + positions  what to do + what's open (the core)
  5. Pipeline / forward view    positions near exits, symbols near breakout
  6. What changed in last 24h   delta log: signals, regime, universe, positions
  7. Risk exposure              direction, notional, correlation, idle days
  8. Calibration                live vs backtest, MTD and lifetime
  9. Universe + recent alerts   active universe + alerter activity

Reads from local files (no internet beyond Binance market data):
  live_trades.csv          (you maintain)
  active_universe.json     (refresh weekly via dynamic_universe.py)
  deployment_state.json    (optional; tracks paper/live stage)
  last_dashboard_state.json (auto-managed for delta detection)
  alerts.log               (auto-maintained by signal_alerter.py)

Run:
    python dashboard.py
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

import mtf_structural_backtest as bt
import donchian_baseline as dc
import walk_forward_v2 as wf2
import walk_forward_v3 as wf3
from funding import fetch_funding
from sentiment_filters import fetch_fear_greed_history, btc_relative_return
from regime_signals import all_regimes
from dynamic_sizing import describe_multiplier
from dynamic_universe import load_universe as load_active_universe


# ============================================================================
# CONFIG
# ============================================================================

_FALLBACK = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
             "AVAXUSDT", "LINKUSDT", "DOGEUSDT", "XRPUSDT")
SYMBOLS, UNIVERSE_META = load_active_universe(fallback=_FALLBACK)

N_ENTRY = 55
N_EXIT = 20
ATR_PERIOD = 20
TIME_STOP_BARS = 90
BTC_SMA_PERIOD = 200

USE_BTC_MACRO = True
USE_ADX = False
USE_FUNDING = True
USE_FNG = False
USE_BTC_REL = False

# --- Rel-strength filter (walk_forward_v7 validated 2026-07-31) ---
# Block entries when the symbol is NOT in the top quintile of 30-day return
# vs BTC among the active universe. Validated to lift per-trade expectancy
# from +0.77R to +1.31R (+69%) with 43% fewer trades at similar ann_ret.
USE_REL_STRENGTH = True
REL_STRENGTH_LOOKBACK = 30
REL_STRENGTH_TOP_FRACTION = 0.20

# Inception equity: real capital at system go-live (2026-07-31, post
# LTC->USDT conversion, verified by account_sync). If account_state.json
# exists and is fresh, prefer the synced live equity as the current base.
STARTING_EQUITY = 111.76
ACCOUNT_STATE = Path("account_state.json")


def live_equity(fallback: float = STARTING_EQUITY) -> float:
    """Real equity from the last account sync, if fresh (<3 days)."""
    try:
        s = json.loads(ACCOUNT_STATE.read_text(encoding="utf-8"))
        synced = pd.Timestamp(s["synced_at"])
        if pd.Timestamp.now(tz="UTC") - synced < pd.Timedelta(days=3):
            v = float(s.get("total_equity_usd", 0))
            if v > 0:
                return v
    except Exception:
        pass
    return fallback
RISK_PER_TRADE = 0.0075
PORTFOLIO_HEAT_CAP = 0.03
FUNDING_LIMIT_BPS = 20.0
ADX_THRESHOLD = 25.0

# Backtest expectations for the SHIPPED variant (walk_forward_v7 rs_only,
# validated 2026-07-31): OOS exp_R +1.31 aggregate; win 30%.
BACKTEST_WIN_RATE = 0.30
BACKTEST_EXPECTANCY_R = 1.31
BACKTEST_TRADES_PER_MONTH = 4.0   # RS filter cuts trade count ~40%
# Carry paper-trial reference (funding_carry_backtest 2026-07-31)
CARRY_BT_WIN = 0.81
CARRY_BT_MEDIAN_BPS = 198.0

ALERTS_LOG = Path("alerts.log")
LIVE_TRADES = Path("live_trades.csv")
DEPLOYMENT_STATE = Path("deployment_state.json")
LAST_STATE = Path("last_dashboard_state.json")


# ============================================================================
# Data fetch (market)
# ============================================================================

def fetch_recent(symbol: str, bars: int = 300) -> pd.DataFrame:
    end_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    start_ms = end_ms - (bars + 2) * 86400 * 1000
    df = bt.fetch_binance_klines(symbol, "1d", start_ms, end_ms)
    # CRITICAL (2026-08-02 audit): drop the still-forming daily bar. Signals
    # must evaluate COMPLETED closes only — the backtest never saw partial
    # bars, and on delayed runs the partial "close" is hours of unfinished
    # price action that can reverse by the real close.
    if not df.empty:
        now = pd.Timestamp.now(tz="UTC")
        df = df[df.index + pd.Timedelta(days=1) <= now]
    return df


def fetch_current_funding_bps(symbol: str) -> float:
    end_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    start_ms = end_ms - 86400 * 1000 * 2
    ev = fetch_funding(symbol, start_ms, end_ms)
    if ev.empty:
        return 0.0
    return float(ev["funding_rate"].iloc[-1]) * 10000.0


def market_overview(symbol_data: dict) -> dict:
    out = {}
    for sym in ("BTCUSDT", "ETHUSDT"):
        df = symbol_data.get(sym)
        if df is None or not isinstance(df, pd.DataFrame) or len(df) < 2:
            out[sym] = None
            continue
        last_close = float(df["close"].iloc[-1])
        prev_close = float(df["close"].iloc[-2])
        out[sym] = {"price": last_close,
                    "change_pct": (last_close - prev_close) / prev_close * 100.0}
    return out


# ============================================================================
# Deployment state + last-state diff
# ============================================================================

def load_deployment_state() -> dict:
    if DEPLOYMENT_STATE.exists():
        try:
            return json.loads(DEPLOYMENT_STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    # default
    return {
        "deployment_started": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d"),
        "stage": "paper",
        "stage_target_days": 60,
        "notes": "auto-initialized (edit deployment_state.json to customize)",
    }


def load_last_state() -> dict:
    if LAST_STATE.exists():
        try:
            return json.loads(LAST_STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_last_state(state: dict) -> None:
    LAST_STATE.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


# ============================================================================
# Alerts log parsing
# ============================================================================

ALERT_HEADER_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC)\]")


def load_recent_alerts(n: int = 10) -> list[dict]:
    if not ALERTS_LOG.exists():
        return []
    text = ALERTS_LOG.read_text(encoding="utf-8", errors="ignore")
    parts = ALERT_HEADER_RE.split(text)
    entries = []
    for i in range(1, len(parts), 2):
        ts = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if not body:
            continue
        subject = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        entries.append({"ts": ts, "subject": subject})
    return entries[-n:][::-1]


def last_alert_age_hours() -> Optional[float]:
    alerts = load_recent_alerts(1)
    if not alerts:
        return None
    try:
        ts = pd.Timestamp(alerts[0]["ts"].replace(" UTC", "+00:00"))
        return float((pd.Timestamp.now(tz="UTC") - ts).total_seconds() / 3600)
    except Exception:
        return None


# ============================================================================
# Signal computation
# ============================================================================

@dataclass
class SignalRow:
    symbol: str
    last_close: float
    entry_high: float
    entry_low: float
    exit_high: float
    exit_low: float
    atr: float
    adx: float
    funding_bps: float
    fng_value: float
    btc_rel: float
    verdict: str


def compute_top_quintile_rs(symbol_data: dict,
                            lookback: int = REL_STRENGTH_LOOKBACK,
                            top_fraction: float = REL_STRENGTH_TOP_FRACTION) -> set:
    """
    Return the set of symbols currently in the top `top_fraction` share of
    30-day return vs BTC. Used to gate entry signals.

    BTCUSDT is always included (it's the benchmark; the filter can't reject
    a BTC signal on relative-strength grounds).
    """
    if "BTCUSDT" not in symbol_data:
        return set()
    btc = symbol_data["BTCUSDT"]
    if not isinstance(btc, pd.DataFrame) or btc.empty or len(btc) < lookback + 1:
        return set()
    btc_ret = float(btc["close"].iloc[-1] / btc["close"].iloc[-lookback - 1] - 1.0)

    rs = {}
    for sym, df in symbol_data.items():
        if sym.startswith("_") or sym == "BTCUSDT":
            continue
        if not isinstance(df, pd.DataFrame) or df.empty or len(df) < lookback + 1:
            continue
        try:
            sym_ret = float(df["close"].iloc[-1] / df["close"].iloc[-lookback - 1] - 1.0)
            rs[sym] = sym_ret - btc_ret
        except Exception:
            continue

    if len(rs) < 3:
        return set(rs.keys()) | {"BTCUSDT"}   # too few to rank; fail-open

    sorted_syms = sorted(rs.items(), key=lambda kv: -kv[1])
    top_count = max(1, int(round(len(sorted_syms) * top_fraction)))
    top_set = {s for s, _ in sorted_syms[:top_count]}
    top_set.add("BTCUSDT")
    return top_set


def compute_signals(symbol_data: dict, btc_macro_on: bool,
                    fng_value: float, btc_rel_by_symbol: dict) -> list[SignalRow]:
    # Precompute top-quintile RS membership once for this run
    top_rs_set = compute_top_quintile_rs(symbol_data) if USE_REL_STRENGTH else None

    rows = []
    for sym, df in symbol_data.items():
        if sym.startswith("_") or not isinstance(df, pd.DataFrame) or df.empty:
            continue
        d = dc.build_donchian(df, dc.DCFG)
        d = d.assign(adx=wf2.adx(df, 14))
        last = d.iloc[-1]
        funding_bps = symbol_data.get(f"_funding_{sym}", 0.0)
        rel = btc_rel_by_symbol.get(sym)
        rel_val = float(rel.iloc[-1]) if rel is not None and len(rel) else float("nan")

        is_long = (not math.isnan(last["entry_high"])
                   and last["close"] > last["entry_high"])
        is_short = (not math.isnan(last["entry_low"])
                    and last["close"] < last["entry_low"])

        verdict = "NONE"
        if is_long or is_short:
            blocked = None
            if USE_BTC_MACRO and not btc_macro_on:
                blocked = "BLOCKED_BTC_MACRO"
            elif USE_ADX and last.get("adx", float("nan")) < ADX_THRESHOLD:
                blocked = f"BLOCKED_ADX ({last['adx']:.0f})"
            elif USE_FUNDING and is_long and funding_bps > FUNDING_LIMIT_BPS:
                blocked = f"BLOCKED_FUNDING (+{funding_bps:.1f}bps)"
            elif USE_FUNDING and is_short and funding_bps < -FUNDING_LIMIT_BPS:
                blocked = f"BLOCKED_FUNDING ({funding_bps:.1f}bps)"
            elif USE_REL_STRENGTH and top_rs_set is not None and sym not in top_rs_set:
                blocked = "BLOCKED_REL_STRENGTH"
            verdict = blocked if blocked else ("LONG_ENTRY" if is_long else "SHORT_ENTRY")

        rows.append(SignalRow(
            symbol=sym, last_close=float(last["close"]),
            entry_high=float(last["entry_high"]) if not math.isnan(last["entry_high"]) else float("nan"),
            entry_low=float(last["entry_low"]) if not math.isnan(last["entry_low"]) else float("nan"),
            exit_high=float(last["exit_high"]) if not math.isnan(last["exit_high"]) else float("nan"),
            exit_low=float(last["exit_low"]) if not math.isnan(last["exit_low"]) else float("nan"),
            atr=float(last["atr"]) if not math.isnan(last["atr"]) else 0.0,
            adx=float(last["adx"]) if not math.isnan(last.get("adx", float("nan"))) else 0.0,
            funding_bps=funding_bps, fng_value=fng_value, btc_rel=rel_val,
            verdict=verdict,
        ))
    return rows


# ============================================================================
# Positions
# ============================================================================

def load_live_trades() -> pd.DataFrame:
    if not LIVE_TRADES.exists():
        return pd.DataFrame()
    df = pd.read_csv(LIVE_TRADES)
    for col in ("entry_date", "exit_date"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
    return df


def split_open_closed(df):
    if df.empty:
        return df, df
    is_open = df["exit_date"].isna() if "exit_date" in df.columns else pd.Series([], dtype=bool)
    return df[is_open], df[~is_open]


def annotate_open_positions(open_df, symbol_data) -> list[dict]:
    out = []
    for _, row in open_df.iterrows():
        sym = row["symbol"]
        df = symbol_data.get(sym)
        if df is None or df.empty:
            continue
        d = dc.build_donchian(df, dc.DCFG)
        last = d.iloc[-1]
        last_close = float(last["close"])
        entry = float(row["entry_price"]); size = float(row["size"])
        side = int(row["side"])
        stop = float(row.get("stop", row.get("initial_stop", float("nan"))))
        risk = float(row.get("risk_dollars",
            abs(entry - stop) * abs(size) if not math.isnan(stop) else 0.0))
        pnl = (last_close - entry) * size
        r = pnl / risk if risk > 0 else float("nan")
        days = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(row["entry_date"])).days
        days_to_time_stop = max(0, TIME_STOP_BARS - days)
        milestone = "TRAILING (2R+)" if r >= 2.0 else ("BREAKEVEN (1R+)" if r >= 1.0 else "INITIAL")
        atr_val = float(last["atr"]) if not math.isnan(last["atr"]) else 0.0
        exit_level = float(last["exit_low"]) if side > 0 else float(last["exit_high"])
        dist_atr = (abs(last_close - exit_level) / atr_val) if (atr_val > 0 and not math.isnan(exit_level)) else float("nan")

        out.append({
            "symbol": sym, "side": "LONG" if side > 0 else "SHORT",
            "entry_date": pd.Timestamp(row["entry_date"]).strftime("%Y-%m-%d"),
            "entry_price": entry, "current_price": last_close,
            "pnl_dollars": pnl, "r_mult": r, "milestone": milestone,
            "stop": stop, "channel_exit": exit_level, "dist_channel_atr": dist_atr,
            "days_held": days, "days_to_time_stop": days_to_time_stop,
            "risk_dollars": risk, "notional": abs(size) * last_close,
        })
    return out


# ============================================================================
# Pipeline (forward view)
# ============================================================================

def pipeline_positions(open_positions: list[dict]) -> list[dict]:
    """Top 3 open positions by 'closest to exit' likelihood."""
    if not open_positions:
        return []
    items = []
    for p in open_positions:
        # closest of: channel exit distance (in ATR), time stop (in days, normalized)
        items.append({
            "symbol": p["symbol"], "side": p["side"],
            "channel_dist_atr": p["dist_channel_atr"],
            "days_to_time_stop": p["days_to_time_stop"],
            "r_mult": p["r_mult"],
            "next_trigger": ("channel_exit" if p["dist_channel_atr"] <= p["days_to_time_stop"] / 30.0
                             else "time_stop"),
        })
    items.sort(key=lambda x: x["channel_dist_atr"] if not math.isnan(x["channel_dist_atr"]) else 99)
    return items[:3]


def pipeline_watchlist(signals: list[SignalRow], open_symbols: set) -> list[dict]:
    """Top 5 universe symbols within 3% of a Donchian breakout."""
    items = []
    for s in signals:
        if s.symbol in open_symbols:
            continue
        if s.verdict.startswith("LONG_ENTRY") or s.verdict.startswith("SHORT_ENTRY"):
            continue
        # distance to nearest break in %
        if not math.isnan(s.entry_high) and s.last_close > 0:
            dist_to_long = (s.entry_high - s.last_close) / s.last_close * 100.0
        else:
            dist_to_long = float("inf")
        if not math.isnan(s.entry_low) and s.last_close > 0:
            dist_to_short = (s.last_close - s.entry_low) / s.last_close * 100.0
        else:
            dist_to_short = float("inf")
        d, direction = (dist_to_long, "LONG") if dist_to_long < dist_to_short else (dist_to_short, "SHORT")
        if d <= 3.0:
            items.append({"symbol": s.symbol, "direction": direction,
                          "dist_pct": d, "current": s.last_close})
    items.sort(key=lambda x: x["dist_pct"])
    return items[:5]


# ============================================================================
# What changed in last 24h
# ============================================================================

def what_changed(now_state: dict, last_state: dict) -> list[str]:
    events = []
    if not last_state:
        events.append("First run — no prior state to compare. (will populate next run)")
        return events

    # regime flip
    if last_state.get("btc_macro_on") is not None:
        if last_state["btc_macro_on"] != now_state["btc_macro_on"]:
            events.append(
                f"REGIME FLIP: BTC macro {'OFF→ON' if now_state['btc_macro_on'] else 'ON→OFF'}"
            )

    # new signals (compare verdicts)
    last_verdicts = last_state.get("signals", {})
    for sym, v in now_state["signals"].items():
        if v in ("LONG_ENTRY", "SHORT_ENTRY") and last_verdicts.get(sym) != v:
            events.append(f"NEW SIGNAL: {v} on {sym}")

    # universe diff
    last_uni = set(last_state.get("universe", []))
    now_uni = set(now_state["universe"])
    added = now_uni - last_uni
    removed = last_uni - now_uni
    if added:
        events.append(f"UNIVERSE: +{', '.join(sorted(added)[:5])}{'…' if len(added)>5 else ''}")
    if removed:
        events.append(f"UNIVERSE: -{', '.join(sorted(removed)[:5])}{'…' if len(removed)>5 else ''}")

    # position changes
    last_pos = set(last_state.get("open_positions", []))
    now_pos = set(now_state["open_positions"])
    opened = now_pos - last_pos
    closed = last_pos - now_pos
    for s in opened:
        events.append(f"OPENED: {s}")
    for s in closed:
        events.append(f"CLOSED: {s}")

    # alerter health
    age_h = now_state.get("last_alert_age_h")
    if age_h is None:
        events.append("ALERTER: no alerts yet in log")
    elif age_h > 48:
        events.append(f"ALERTER: last delivery {age_h:.0f}h ago (stale)")

    if not events:
        events.append("No material changes since last run.")
    return events


# ============================================================================
# Risk exposure
# ============================================================================

def risk_exposure(open_positions: list[dict], equity: float,
                  closed_df: pd.DataFrame) -> dict:
    n_long = sum(1 for p in open_positions if p["side"] == "LONG")
    n_short = sum(1 for p in open_positions if p["side"] == "SHORT")
    total_notional = sum(p["notional"] for p in open_positions)
    gross_pct = (total_notional / equity * 100.0) if equity > 0 else 0.0

    # days since last entry
    if not closed_df.empty and "entry_date" in closed_df.columns:
        last_entry = closed_df["entry_date"].max()
        days_idle = (pd.Timestamp.now(tz="UTC") - last_entry).days if pd.notna(last_entry) else None
    else:
        days_idle = None

    return {"n_long": n_long, "n_short": n_short,
            "total_notional": total_notional, "gross_pct": gross_pct,
            "days_idle": days_idle}


# ============================================================================
# Performance KPIs
# ============================================================================

def perf_kpis(closed_df: pd.DataFrame, deployment_state: dict,
              btc_df: Optional[pd.DataFrame]) -> dict:
    dep_start = pd.Timestamp(deployment_state.get("deployment_started"), tz="UTC")
    now = pd.Timestamp.now(tz="UTC")
    days = max((now - dep_start).days, 0)

    closed_pnl = 0.0
    if not closed_df.empty and "pnl_net" in closed_df.columns:
        closed_pnl = float(closed_df["pnl_net"].sum())
    # Prefer real synced equity; fall back to inception + tracked PnL
    equity = live_equity(fallback=STARTING_EQUITY + closed_pnl)
    total_pct = ((equity - STARTING_EQUITY) / STARTING_EQUITY * 100.0)

    # max DD from running equity
    max_dd_pct = 0.0
    if not closed_df.empty and "exit_date" in closed_df.columns:
        s = closed_df.sort_values("exit_date")["pnl_net"]
        eq_curve = STARTING_EQUITY + s.cumsum()
        if len(eq_curve) > 1:
            peak = eq_curve.cummax()
            dd = (eq_curve - peak) / peak
            max_dd_pct = float(dd.min() * 100.0)

    # MTD return
    month_start = pd.Timestamp(year=now.year, month=now.month, day=1, tz="UTC")
    if not closed_df.empty:
        mtd_df = closed_df[closed_df["exit_date"] >= month_start]
        mtd_pnl = float(mtd_df["pnl_net"].sum()) if not mtd_df.empty else 0.0
        mtd_pct = (mtd_pnl / STARTING_EQUITY * 100.0)
    else:
        mtd_pct = 0.0

    # BTC since deployment
    btc_ret_pct = None
    if btc_df is not None and not btc_df.empty:
        try:
            dep_slice = btc_df[btc_df.index >= dep_start]
            if not dep_slice.empty:
                btc_ret_pct = float((dep_slice["close"].iloc[-1] / dep_slice["close"].iloc[0] - 1) * 100.0)
        except Exception:
            pass

    return {
        "equity": equity, "total_pct": total_pct, "mtd_pct": mtd_pct,
        "max_dd_pct": max_dd_pct, "days": days,
        "btc_pct_same_period": btc_ret_pct,
        "excess_vs_btc": (total_pct - btc_ret_pct) if btc_ret_pct is not None else None,
    }


# ============================================================================
# Calibration
# ============================================================================

def calibration(closed_df: pd.DataFrame) -> dict:
    out = {"this_month": {"n": 0}, "lifetime": {"n": 0}}
    if closed_df.empty or "r_multiple" not in closed_df.columns:
        return out
    now = pd.Timestamp.now(tz="UTC")
    month_start = pd.Timestamp(year=now.year, month=now.month, day=1, tz="UTC")

    for label, sub in (
        ("this_month", closed_df[closed_df["exit_date"] >= month_start]),
        ("lifetime", closed_df),
    ):
        if sub.empty:
            out[label] = {"n": 0}
            continue
        rs = sub["r_multiple"].astype(float).to_numpy()
        out[label] = {
            "n": int(len(rs)),
            "win_rate": float((rs > 0).mean()),
            "expectancy_r": float(rs.mean()),
            "ann_ret": (float(rs.sum()) * RISK_PER_TRADE * 100.0 /
                        max((sub["exit_date"].max() - sub["entry_date"].min()).days / 365.25, 1e-9))
                       if len(rs) > 1 else float("nan"),
        }
    return out


def cal_status(live: dict, exp_win: float, exp_exp: float) -> tuple[str, str]:
    if live.get("n", 0) < 3:
        return ("insufficient sample", "muted")
    win_dev = abs(live["win_rate"] - exp_win)
    exp_dev = abs(live["expectancy_r"] - exp_exp)
    if win_dev < 0.15 and exp_dev < 0.5:
        return ("tracking within tolerance", "ok")
    if win_dev > 0.25 or exp_dev > 1.0:
        return ("DIVERGENT", "err")
    return ("mild divergence", "warn")


# ============================================================================
# HTML rendering
# ============================================================================

def fmt_money(x):
    if x is None or (isinstance(x, float) and math.isnan(x)): return "—"
    # cents matter below $10k (at $110, a whole-dollar display hides real moves)
    return f"${x:,.2f}" if abs(x) < 10_000 else f"${x:,.0f}"


def fmt_pct(x, signed=True, decimals=2):
    if x is None or (isinstance(x, float) and math.isnan(x)): return "—"
    return (f"{x:+.{decimals}f}%" if signed else f"{x:.{decimals}f}%")


def fmt_num(x, decimals=2, signed=False):
    if x is None or (isinstance(x, float) and math.isnan(x)): return "—"
    if math.isinf(x): return "∞"
    spec = f"{'+' if signed else ''}.{decimals}f"
    return format(x, spec)


def verdict_class(v):
    if v.startswith("LONG_ENTRY"): return "v-long"
    if v.startswith("SHORT_ENTRY"): return "v-short"
    if v.startswith("BLOCKED"): return "v-block"
    return "v-none"


def render_html(state: dict) -> str:
    K = state["kpis"]; R = state["regime_states"]; mo = state["market"]
    rs = state["risk"]; cal = state["cal"]; dep = state["deployment"]
    ops = state.get("ops", {})

    # === Section 0: operator action banner + today's P&L + sparkline ===
    b_cls, b_head, b_detail = build_action_banner(state)
    banner = (f'<div class="reg-card {b_cls}" style="margin-bottom:14px;">'
              f'<div class="reg-v">{b_head}</div>'
              f'<div class="reg-s">{b_detail}</div></div>')

    eq_hist = ops.get("eq_hist", [])
    d_usd, d_pct, d_span = today_delta(eq_hist)
    if d_usd is None:
        today_cell = ('<div class="kpi-cell"><div class="kpi-l">Today\'s P&L</div>'
                      '<div class="kpi-v muted">—</div>'
                      '<div class="kpi-s">needs 2 days of equity history</div></div>')
    else:
        cls = "pnl-pos" if d_usd >= 0 else "pnl-neg"
        today_cell = (f'<div class="kpi-cell"><div class="kpi-l">Today\'s P&L</div>'
                      f'<div class="kpi-v {cls}">{d_usd:+.2f}$</div>'
                      f'<div class="kpi-s">{d_pct:+.2f}% · {d_span}</div></div>')
    spark_cell = (f'<div class="kpi-cell"><div class="kpi-l">Equity (60d)</div>'
                  f'<div style="margin-top:6px;">{equity_spark_svg(eq_hist)}</div></div>')

    # === Section 1: Performance KPI strip ===
    excess = K.get("excess_vs_btc")
    excess_str = fmt_pct(excess) if excess is not None else "—"
    excess_cls = "pnl-pos" if (excess is not None and excess >= 0) else "pnl-neg"
    btc_pct_str = fmt_pct(K.get("btc_pct_same_period"), decimals=1) if K.get("btc_pct_same_period") is not None else "—"

    perf_strip = f"""
    <div class="kpi-strip">
      <div class="kpi-cell">
        <div class="kpi-l">Equity</div>
        <div class="kpi-v">{fmt_money(K['equity'])}</div>
        <div class="kpi-s">from {fmt_money(STARTING_EQUITY)}</div>
      </div>
      <div class="kpi-cell">
        <div class="kpi-l">Since deployment</div>
        <div class="kpi-v {('pnl-pos' if K['total_pct'] >= 0 else 'pnl-neg')}">{fmt_pct(K['total_pct'])}</div>
        <div class="kpi-s">{K['days']} days</div>
      </div>
      <div class="kpi-cell">
        <div class="kpi-l">Month-to-date</div>
        <div class="kpi-v {('pnl-pos' if K['mtd_pct'] >= 0 else 'pnl-neg')}">{fmt_pct(K['mtd_pct'])}</div>
      </div>
      <div class="kpi-cell">
        <div class="kpi-l">Max drawdown</div>
        <div class="kpi-v pnl-neg">{fmt_pct(K['max_dd_pct'])}</div>
      </div>
      <div class="kpi-cell">
        <div class="kpi-l">vs BTC buy-hold</div>
        <div class="kpi-v {excess_cls}">{excess_str}</div>
        <div class="kpi-s">BTC: {btc_pct_str}</div>
      </div>
      {today_cell}
      {spark_cell}
    </div>
    """

    # === Section 2: System state ===
    macro_cls = "ok" if state["macro_on"] else "warn"
    macro_txt = "ON" if state["macro_on"] else "OFF"
    heat_cls = "warn" if state["heat_pct"] > PORTFOLIO_HEAT_CAP * 100 * 0.8 else "ok"
    alert_age = state.get("last_alert_age_h")
    if alert_age is None:
        alert_cls = "warn"; alert_txt = "no log"
    elif alert_age > 48:
        alert_cls = "warn"; alert_txt = f"{alert_age:.0f}h stale"
    else:
        alert_cls = "ok"; alert_txt = f"{alert_age:.0f}h ago"

    stage_pct = min(100, int(K['days'] / max(dep.get('stage_target_days', 60), 1) * 100))

    sys_state = f"""
    <div class="row-4">
      <div class="reg-card {macro_cls}">
        <div class="reg-l">BTC Macro</div>
        <div class="reg-v">{macro_txt}</div>
        <div class="reg-s">SMA{BTC_SMA_PERIOD} · {state['days_in_regime']}d in state · slope {state.get('macro_gap_pct', float('nan')):+.2f}% {'(needs > 0 to flip ON)' if not state['macro_on'] else ''}</div>
      </div>
      <div class="reg-card {heat_cls}">
        <div class="reg-l">Portfolio Heat</div>
        <div class="reg-v">{state['heat_pct']:.2f}%</div>
        <div class="reg-s">cap {PORTFOLIO_HEAT_CAP*100:.1f}% · {state['n_open']} open</div>
      </div>
      <div class="reg-card {alert_cls}">
        <div class="reg-l">Alerter</div>
        <div class="reg-v">{alert_txt}</div>
        <div class="reg-s">{'Discord' if state.get('discord_set') else 'log only'}</div>
      </div>
      <div class="reg-card ok">
        <div class="reg-l">Deployment</div>
        <div class="reg-v">{dep.get('stage', 'paper').upper()} · day {K['days']}</div>
        <div class="reg-s">{stage_pct}% to next checkpoint ({dep.get('stage_target_days', 60)}d)</div>
      </div>
    </div>
    """

    # === Section 3: Regime + risk multiplier ===
    regime_card = f"""
    <div class="card">
      <h2>Regime State + Dynamic Sizing</h2>
      <div class="row-4">
        <div class="mini-cell"><div class="mini-l">Vol</div><div class="mini-v">{R['vol']}</div></div>
        <div class="mini-cell"><div class="mini-l">Correlation</div><div class="mini-v">{R['corr']}</div></div>
        <div class="mini-cell"><div class="mini-l">Funding</div><div class="mini-v">{R['funding']}</div></div>
        <div class="mini-cell"><div class="mini-l">Risk multiplier</div><div class="mini-v">x{R['multiplier']:.2f}</div></div>
      </div>
      <div class="kpi-s" style="margin-top:8px;">{' &middot; '.join(R['reasons'])}</div>
    </div>
    """

    # === Section 3.5: Autopilot / Account / Carry (ops panels) ===
    ops = state.get("ops", {})
    if ops.get("exec_halted"):
        auto_cls, auto_txt = "err", "HALTED"
        auto_sub = f"STOP_TRADING present: {ops.get('exec_halt_reason', '')[:80]}"
    elif ops.get("exec_live"):
        auto_cls, auto_txt = "ok", "LIVE"
        auto_sub = ops.get("exec_rails", "")
    else:
        auto_cls, auto_txt = "warn", "DRY-RUN"
        auto_sub = ops.get("exec_rails", "")

    acct = ops.get("acct", {})
    fut_w = acct.get("futures", {}).get("margin_balance", 0.0)
    acct_age = ops.get("acct_age_h")
    acct_age_txt = f"{acct_age:.0f}h ago" if acct_age is not None else "never"
    sync_cls = "ok" if (acct_age is not None and acct_age < 36) else "warn"
    issues = ops.get("acct_issues", [])
    issues_html = ""
    if issues:
        issues_html = ('<div class="card" style="border-left:4px solid #f4212e;">'
                       '<h2>Position Cross-Check Warnings</h2><ul class="changed">'
                       + "".join(f"<li class='pnl-neg'>{i}</li>" for i in issues)
                       + "</ul></div>")

    n_carry = len(ops.get("carry_open", {}))
    carry_wr = ops.get("carry_win_rate")
    best_bps = ops.get("carry_best_bps")
    trigger_txt = (f"best funding {best_bps:+.1f}bps/{ops.get('carry_best_symbol','')[:8]} vs 10 entry"
                   if best_bps is not None else "trigger distance: n/a")
    carry_sub = (f"{trigger_txt} · {ops.get('carry_closed_n', 0)} closed"
                 + f" · review in {ops.get('carry_review_days', '?')}d")

    hr, ho = ops.get("health_runs", 0), ops.get("health_ok", 0)
    health_pct = (ho / hr * 100) if hr else 0
    health_cls = "ok" if health_pct >= 90 else ("warn" if health_pct >= 70 else "err")

    # --- unlock sleeve panel ---
    u_rows = []
    for e in ops.get("unlock_events", []):
        st = e.get("status", "?")
        if st in ("open", "pending"):
            extra = ""
            if st == "open" and e.get("entry_px") and e.get("cur_px"):
                mv = (float(e["cur_px"]) / float(e["entry_px"]) - 1) * 100
                pnl = -mv  # short
                c = "pnl-pos" if pnl >= 0 else "pnl-neg"
                extra = (f"<td>entry {e['entry_px']}</td>"
                         f"<td class='{c}'>{pnl:+.1f}% unrealized</td>")
            elif st == "open":
                extra = f"<td>entry {e.get('entry_px','?')}</td><td class='muted'>price n/a</td>"
            else:
                extra = "<td class='muted' colspan='2'>awaiting T-10 window</td>"
            u_rows.append(f"<tr><td>{e['symbol']}</td><td>{e['unlock_date']}</td>"
                          f"<td>{e['pct_supply']}%</td><td>{st}</td>{extra}</tr>")
        elif st in ("completed", "stopped"):
            c = "pnl-pos" if e.get("net_pct", 0) > 0 else "pnl-neg"
            u_rows.append(f"<tr><td>{e['symbol']}</td><td>{e['unlock_date']}</td>"
                          f"<td>{e['pct_supply']}%</td><td>{st}</td>"
                          f"<td colspan='2' class='{c}'>net {e.get('net_pct',0):+.2f}%</td></tr>")
    u_done = [e for e in ops.get("unlock_events", [])
              if e.get("status") in ("completed", "stopped")]
    u_wins = sum(1 for e in u_done if e.get("net_pct", 0) > 0)
    unlock_card = f"""
    <div class="card">
      <h2>Unlock Shorts (paper trial: {len(u_done)}/10 done, {u_wins} wins)</h2>
      <table>
        <thead><tr><th>symbol</th><th>unlock</th><th>supply</th><th>status</th>
          <th colspan="2">detail</th></tr></thead>
        <tbody>{''.join(u_rows) or '<tr><td colspan=6 class=muted>no events registered</td></tr>'}</tbody>
      </table>
    </div>"""

    # --- sleeve P&L attribution ---
    try:
        import ledger
        att = ledger.attribution()
    except Exception:
        att = None
    if att and att.get("equity") is not None:
        def att_row(label, v, note=""):
            c = "pnl-pos" if v >= 0 else "pnl-neg"
            return (f"<tr><td>{label}</td><td class='{c}'>{v:+.2f}$</td>"
                    f"<td class='muted'>{note}</td></tr>")
        resid_note = ("Simple Earn interest + rounding"
                      if att["yield_resid"] >= -1.0 else
                      "NEGATIVE — unexplained leak, investigate!")
        att_body = (
            att_row("Trend (realized)", att["trend_realized"],
                    f"{att['n_trades']} closed trades")
            + att_row("Trend (unrealized)", att["trend_unrealized"], "open futures uPnL")
            + att_row("Carry", att["carry_live"], "paper until Sept 1")
            + att_row("Unlock shorts", att["unlock_live"], "paper trial")
            + att_row("Yield + residual", att["yield_resid"], resid_note))
        attribution_card = f"""
    <div class="card">
      <h2>P&L Attribution by Sleeve (since inception {ledger.INCEPTION_DATE},
          ${att['inception']:.2f} + {att['deposits']:+.2f} deposits)</h2>
      <table>
        <thead><tr><th>sleeve</th><th>P&L</th><th>note</th></tr></thead>
        <tbody>{att_body}</tbody>
      </table>
      <div class="kpi-s" style="margin-top:6px;">Log every deposit:
        <code>python ledger.py deposit AMOUNT</code> — attribution breaks otherwise.</div>
    </div>"""
    else:
        attribution_card = ""

    # --- MC cone / capital planner / stress bodies ---
    cone_svg = mc_cone_svg(state.get("closed_df"))

    try:
        import ledger
        proj = ledger.project_milestones()
        ms = proj["milestones"]
        planner_body = (
            f"<div class='kpi-s'>equity ${proj['equity']:,.2f} · deposit pace "
            f"${proj['monthly_pace']:,.0f}/mo (trailing 90d) · assumed "
            f"{proj['assumed_return']:.0%}/yr</div><table><tbody>"
            + "".join(f"<tr><td>${t:,}</td><td>{ms.get(t, 'beyond 10y at current pace')}</td>"
                      f"<td class='muted'>{'basis trade unlocks' if t==10_000 else 'vol selling unlocks' if t==25_000 else 'first milestone'}</td></tr>"
                      for t in (1_000, 10_000, 25_000))
            + "</tbody></table>"
            + ("<div class='kpi-s v-block' style='margin-top:6px;'>deposit pace is $0/mo "
               "— growth is return-only; log deposits: <code>python ledger.py deposit AMT</code></div>"
               if proj["monthly_pace"] == 0 else ""))
    except Exception:
        planner_body = "<span class='muted'>ledger unavailable</span>"

    s_rows = stress_rows(ops)
    stress_body = ("".join(s_rows) if s_rows else
                   '<tr><td colspan="5" class="muted">no open positions — '
                   'panel activates with the first trade</td></tr>')

    # --- Cost of Discipline panel (blocked-signal receipts) ---
    disc = _read_json("discipline_audit.json")
    if disc.get("active"):
        verdict_good = disc.get("closed_total_r", 0) < 0
        head_cls = "pnl-pos" if verdict_good else "v-block"
        head = (f"taking every blocked signal since {disc.get('off_start')} would be "
                f"{disc.get('closed_total_r', 0):+.1f}R = "
                f"{disc.get('dollars_if_taken', 0):+.2f}$ "
                f"(vs +{disc.get('earn_alt', 0):.2f}$ in Simple Earn)")
        oh_rows = "".join(
            f"<tr><td>{t['sym']}</td><td>{t['side']}</td><td>{t['since']}</td>"
            f"<td class='{'pnl-pos' if t['r'] >= 0 else 'pnl-neg'}'>{t['r']:+.2f}R</td></tr>"
            for t in disc.get("open_hypotheticals", []))
        discipline_card = f"""
    <div class="card">
      <h2>Cost of Discipline — what the blocked signals actually did</h2>
      <div class="{head_cls}" style="font-size:14px;font-weight:600;">{head}</div>
      <div class="kpi-s" style="margin:6px 0;">
        {disc.get('n_closed', 0)} closed hypotheticals (win {disc.get('closed_win', 0) or 0:.0%}):
        longs {disc.get('longs_r', 0):+.1f}R, shorts {disc.get('shorts_r', 0):+.1f}R.
        Still-open hypotheticals below are the SURVIVORS — losers already stopped
        out and left the screen. That asymmetry is why blocked signals always
        look like missed money.</div>
      <table>
        <thead><tr><th>open hypothetical</th><th>side</th><th>since</th><th>mark</th></tr></thead>
        <tbody>{oh_rows or '<tr><td colspan=4 class=muted>none open</td></tr>'}</tbody>
      </table>
    </div>"""
    elif disc:
        discipline_card = ('<div class="card"><h2>Cost of Discipline</h2>'
                           '<div class="kpi-s">macro ON — panel dormant (no blocked signals to audit)</div></div>')
    else:
        discipline_card = ""

    # --- upcoming events calendar ---
    cal_rows = "".join(
        f"<tr><td>{d.strftime('%b %d')}</td><td>{'%+dd' % (d - pd.Timestamp.now(tz='UTC')).days}</td><td>{lbl}</td></tr>"
        for d, lbl in upcoming_events(ops))
    calendar_card = f"""
    <div class="card">
      <h2>Upcoming Events</h2>
      <table>
        <thead><tr><th>date</th><th>in</th><th>event</th></tr></thead>
        <tbody>{cal_rows or '<tr><td colspan=3 class=muted>nothing scheduled</td></tr>'}</tbody>
      </table>
    </div>"""

    ops_strip = f"""
    <div class="row-4">
      <div class="reg-card {auto_cls}">
        <div class="reg-l">Autopilot (executor)</div>
        <div class="reg-v">{auto_txt}</div>
        <div class="reg-s">{auto_sub}</div>
      </div>
      <div class="reg-card {sync_cls}">
        <div class="reg-l">Account (synced {acct_age_txt})</div>
        <div class="reg-v">{fmt_money(acct.get('total_equity_usd', 0))}</div>
        <div class="reg-s">futures ${fut_w:,.2f} armed · spot ${acct.get('spot', {}).get('total_usd', 0):,.2f} · earn ${acct.get('simple_earn', {}).get('total_usd', 0):,.2f}</div>
      </div>
      <div class="reg-card {'ok' if n_carry == 0 else 'warn'}">
        <div class="reg-l">Carry Sleeve (paper)</div>
        <div class="reg-v">{n_carry} open</div>
        <div class="reg-s">{carry_sub}</div>
      </div>
      <div class="reg-card {health_cls}">
        <div class="reg-l">Automation Health (14d)</div>
        <div class="reg-v">{ho}/{hr} clean</div>
        <div class="reg-s">last 451: {ops.get('health_last451', '?')}</div>
      </div>
    </div>
    {issues_html}
    """

    # === Section 4: Today's signals ===
    rs_set = state.get("rs_set", set())
    sig_rows = []
    for s in state["signals"]:
        rs_ok = s.symbol in rs_set
        rs_cell = ("<td class='pnl-pos'>TOP-Q</td>" if rs_ok
                   else "<td class='muted'>—</td>")
        sig_rows.append(
            f"<tr><td>{s.symbol}</td><td>{fmt_num(s.last_close, 4)}</td>"
            f"<td>{fmt_num(s.entry_high, 4)}</td><td>{fmt_num(s.entry_low, 4)}</td>"
            f"<td>{fmt_num(s.adx, 1)}</td><td>{fmt_num(s.funding_bps, 2, signed=True)}</td>"
            f"{rs_cell}"
            f"<td class='{verdict_class(s.verdict)}'>{s.verdict}</td></tr>"
        )
    sig_body = "".join(sig_rows) or '<tr><td colspan="8" class="muted">no signal data</td></tr>'

    # === Section 5: Pipeline ===
    pipeline_pos_rows = []
    for p in state["pipeline_positions"]:
        r_cls = "pnl-pos" if p["r_mult"] >= 0 else "pnl-neg"
        pipeline_pos_rows.append(
            f"<tr><td>{p['symbol']}</td><td>{p['side']}</td>"
            f"<td class='{r_cls}'>{fmt_num(p['r_mult'], 2, signed=True)}R</td>"
            f"<td>{fmt_num(p['channel_dist_atr'], 2)}× ATR</td>"
            f"<td>{p['days_to_time_stop']}d</td>"
            f"<td>{p['next_trigger']}</td></tr>"
        )
    pipeline_pos_body = "".join(pipeline_pos_rows) or '<tr><td colspan="6" class="muted">no open positions</td></tr>'

    watch_rows = []
    for w in state["pipeline_watchlist"]:
        w_rs = w["symbol"] in rs_set
        rs_txt = ("<td class='pnl-pos'>eligible</td>" if w_rs
                  else "<td class='v-block'>RS-blocked</td>")
        watch_rows.append(
            f"<tr><td>{w['symbol']}</td><td>{w['direction']}</td>"
            f"<td>{fmt_num(w['current'], 4)}</td>"
            f"<td>{w['dist_pct']:.2f}%</td>{rs_txt}</tr>"
        )
    watch_body = "".join(watch_rows) or '<tr><td colspan="5" class="muted">no symbols within 3% of breakout</td></tr>'

    # === Section 6: Open positions ===
    pos_rows = []
    for p in state["open_positions"]:
        pnl_cls = "pnl-pos" if p["pnl_dollars"] >= 0 else "pnl-neg"
        pos_rows.append(
            f"<tr><td>{p['symbol']}</td><td>{p['side']}</td>"
            f"<td>{p['entry_date']}</td><td>{fmt_num(p['entry_price'], 4)}</td>"
            f"<td>{fmt_num(p['current_price'], 4)}</td>"
            f"<td class='{pnl_cls}'>{fmt_money(p['pnl_dollars'])}</td>"
            f"<td class='{pnl_cls}'>{fmt_num(p['r_mult'], 2, signed=True)}R</td>"
            f"<td>{p['milestone']}</td>"
            f"<td>{fmt_num(p['dist_channel_atr'], 2)}× ATR</td>"
            f"<td>{p['days_held']}d / {p['days_to_time_stop']}d</td></tr>"
        )
    pos_body = "".join(pos_rows) or '<tr><td colspan="10" class="muted">no open positions</td></tr>'

    # === Section 7: What changed ===
    changed_rows = "".join(f"<li>{e}</li>" for e in state["changed"])

    # === Metrics vs Targets ===
    m_rows = []
    for m in state.get("metrics_rows", []):
        cls = {"ok": "pnl-pos", "warn": "v-block", "gated": "muted"}.get(m["status"], "muted")
        m_rows.append(f"<tr><td>{m['sleeve']}</td><td>{m['metric']}</td>"
                      f"<td class='{cls}'>{m['value']}</td><td>{m['target']}</td></tr>")
    metrics_body = "".join(m_rows) or '<tr><td colspan="4" class="muted">no data</td></tr>'

    # === Section 8: Risk exposure ===
    risk_card = f"""
    <div class="card">
      <h2>Risk Exposure</h2>
      <div class="row-4">
        <div class="mini-cell"><div class="mini-l">Direction</div>
          <div class="mini-v">{rs['n_long']}L / {rs['n_short']}S</div></div>
        <div class="mini-cell"><div class="mini-l">Gross notional</div>
          <div class="mini-v">{rs['gross_pct']:.1f}%</div>
          <div class="mini-s muted">{fmt_money(rs['total_notional'])}</div></div>
        <div class="mini-cell"><div class="mini-l">Heat utilization</div>
          <div class="mini-v">{(state['heat_pct']/(PORTFOLIO_HEAT_CAP*100))*100:.0f}%</div></div>
        <div class="mini-cell"><div class="mini-l">Days since last entry</div>
          <div class="mini-v">{rs['days_idle'] if rs['days_idle'] is not None else '—'}</div></div>
      </div>
    </div>
    """

    # === Section 9: Calibration (expanded) ===
    def cal_block(label, c, exp_win, exp_exp):
        if c.get("n", 0) == 0:
            return f"<tr><td>{label}</td><td>0</td><td>—</td><td>—</td><td>—</td><td class='muted'>no trades</td></tr>"
        status, cls = cal_status(c, exp_win, exp_exp)
        return (f"<tr><td>{label}</td>"
                f"<td>{c['n']}</td>"
                f"<td>{c['win_rate']*100:.0f}% / {exp_win*100:.0f}%</td>"
                f"<td>{c['expectancy_r']:+.2f}R / {exp_exp:+.2f}R</td>"
                f"<td>{fmt_pct(c.get('ann_ret', float('nan')))}</td>"
                f"<td class='{cls}'>{status}</td></tr>")

    cal_card = f"""
    <div class="card">
      <h2>Calibration: Live vs Backtest</h2>
      <table>
        <thead><tr><th>period</th><th>n</th><th>win (live/exp)</th>
          <th>expectancy (live/exp)</th><th>ann_ret</th><th>status</th></tr></thead>
        <tbody>
          {cal_block("This month", cal["this_month"], BACKTEST_WIN_RATE, BACKTEST_EXPECTANCY_R)}
          {cal_block("Lifetime", cal["lifetime"], BACKTEST_WIN_RATE, BACKTEST_EXPECTANCY_R)}
        </tbody>
      </table>
    </div>
    """

    # === Section 10: Universe + alerts ===
    if UNIVERSE_META.get("source") == "json":
        age = UNIVERSE_META.get("age_days", 0)
        stale = " STALE — refresh dynamic_universe.py" if UNIVERSE_META.get("is_stale") else ""
        univ_label = f"dynamic, {age}d old{stale}"
    else:
        univ_label = "fallback (hardcoded)"

    alert_rows = "".join(
        f"<tr><td class='muted'>{a['ts']}</td><td>{a['subject']}</td></tr>"
        for a in state["recent_alerts"]
    ) or '<tr><td colspan="2" class="muted">no alerts yet</td></tr>'

    universe_card = f"""
    <div class="card">
      <h2>Active Universe</h2>
      <div class="kpi-s">{len(SYMBOLS)} symbols · {univ_label}</div>
      <div class="kpi-s" style="margin-top:6px;">{', '.join(list(SYMBOLS)[:10])}{'…' if len(SYMBOLS)>10 else ''}</div>
    </div>
    """

    alerts_card = f"""
    <div class="card">
      <h2>Recent Alerts ({len(state['recent_alerts'])})</h2>
      <table><thead><tr><th>when</th><th>summary</th></tr></thead>
      <tbody>{alert_rows}</tbody></table>
    </div>
    """

    variant_parts = []
    if USE_BTC_MACRO: variant_parts.append("BTC-macro")
    if USE_FUNDING: variant_parts.append("funding")
    if USE_ADX: variant_parts.append("ADX")
    if USE_FNG: variant_parts.append("FNG")
    if USE_BTC_REL: variant_parts.append("BTC-rel")
    variant_label = " + ".join(variant_parts) if variant_parts else "unfiltered"

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Donchian Strategy Dashboard</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 20px;
       background: #0f1419; color: #e7e9ea;
       -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
h1 {{ font-size: 18px; margin: 0 0 4px 0; font-weight: 600; }}
.sub {{ color: #8899a6; font-size: 12px; margin-bottom: 16px; }}
.grid {{ display: grid; gap: 14px; }}
.card {{ background: #15202b; border: 1px solid #253341; border-radius: 8px; padding: 14px; }}
.card h2 {{ font-size: 11px; color: #8899a6; margin: 0 0 10px 0;
            text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }}
.row-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
.row-4 {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }}
.kpi-strip {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; margin-bottom: 14px; }}
.kpi-cell {{ background: #15202b; border: 1px solid #253341; border-radius: 8px; padding: 12px; }}
.kpi-l {{ font-size: 10px; color: #8899a6; text-transform: uppercase; letter-spacing: 0.05em; }}
.kpi-v {{ font-size: 22px; font-weight: 600; margin-top: 4px; }}
.kpi-s {{ font-size: 11px; color: #536471; margin-top: 2px; }}
.reg-card {{ padding: 12px; border-radius: 8px; border: 1px solid #253341; background: #15202b; }}
.reg-l {{ font-size: 10px; color: #8899a6; text-transform: uppercase; }}
.reg-v {{ font-size: 18px; font-weight: 600; margin-top: 4px; }}
.reg-s {{ font-size: 11px; color: #8899a6; margin-top: 4px; }}
.ok {{ border-left: 4px solid #00ba7c; }} .ok .reg-v {{ color: #00ba7c; }}
.warn {{ border-left: 4px solid #ffd400; }} .warn .reg-v {{ color: #ffd400; }}
.err {{ border-left: 4px solid #f4212e; }} .err .reg-v {{ color: #f4212e; }}
.mini-cell {{ padding: 10px 12px; background: #0f1419; border: 1px solid #253341; border-radius: 6px; }}
.mini-l {{ font-size: 10px; color: #8899a6; text-transform: uppercase; }}
.mini-v {{ font-size: 15px; font-weight: 600; margin-top: 3px; }}
.muted {{ color: #536471; }}
table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
th {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #253341; color: #8899a6; font-weight: 500; }}
td {{ padding: 6px 8px; border-bottom: 1px solid #1a2330; }}
tr:hover td {{ background: #1a2330; }}
.v-long {{ color: #00ba7c; font-weight: 600; }}
.v-short {{ color: #f4212e; font-weight: 600; }}
.v-block {{ color: #ffd400; font-size: 11px; }}
.v-none {{ color: #536471; }}
.pnl-pos {{ color: #00ba7c; }}
.pnl-neg {{ color: #f4212e; }}
.changed {{ list-style: none; padding: 0; margin: 0; }}
.changed li {{ padding: 4px 0; font-size: 13px; border-bottom: 1px solid #1a2330; }}
.actions {{ background: #15202b; border: 1px solid #253341; border-radius: 8px;
           padding: 12px; font-size: 12px; color: #8899a6; }}
.actions code {{ background: #0f1419; padding: 2px 6px; border-radius: 4px;
                color: #1d9bf0; font-family: ui-monospace, monospace; }}
.meta {{ font-size: 11px; color: #536471; margin-top: 14px; text-align: right; }}
</style></head><body>

<h1>Donchian (55/20) - Full Dashboard</h1>
<div class="sub">Variant: <b>{variant_label}</b> &middot; Universe: <b>{len(SYMBOLS)}</b> symbols ({univ_label}) &middot; refresh: <code style="color:#1d9bf0">python dashboard.py</code></div>

{banner}
{perf_strip}

<div class="grid">
{ops_strip}
{sys_state}
{regime_card}

<div class="card">
  <h2>Today's Signals</h2>
  <table>
    <thead><tr><th>symbol</th><th>close</th><th>{N_ENTRY}d high</th><th>{N_ENTRY}d low</th>
      <th>ADX</th><th>fund bps</th><th>rel-strength</th><th>verdict</th></tr></thead>
    <tbody>{sig_body}</tbody>
  </table>
</div>

<div class="row-2">
  <div class="card">
    <h2>Pipeline: Positions Near Exit (top 3)</h2>
    <table>
      <thead><tr><th>symbol</th><th>side</th><th>R</th>
        <th>dist channel</th><th>days to stop</th><th>next trigger</th></tr></thead>
      <tbody>{pipeline_pos_body}</tbody>
    </table>
  </div>
  <div class="card">
    <h2>Watchlist: Within 3% of Breakout (top 5)</h2>
    <table>
      <thead><tr><th>symbol</th><th>direction</th><th>current</th><th>distance</th><th>RS filter</th></tr></thead>
      <tbody>{watch_body}</tbody>
    </table>
  </div>
</div>

{discipline_card}

<div class="row-2">
  {unlock_card}
  {calendar_card}
</div>

{attribution_card}

<div class="card">
  <h2>Monte Carlo Cone — is live performance NORMAL? (green = live path;
      shaded = range of luck with a REAL edge; 26% of good sequences end 20
      trades negative — only breaking BELOW the cone is evidence)</h2>
  {cone_svg}
</div>

<div class="row-2">
  <div class="card">
    <h2>Capital Planner (deposit pace -> milestone dates)</h2>
    {planner_body}
  </div>
  <div class="card">
    <h2>Stress: BTC -30% Overnight</h2>
    <table>
      <thead><tr><th>position</th><th>side</th><th>P&L @ -30%</th>
        <th>liq dist</th><th>outcome</th></tr></thead>
      <tbody>{stress_body}</tbody>
    </table>
  </div>
</div>

<div class="card">
  <h2>Open Positions (with lifecycle)</h2>
  <table>
    <thead><tr>
      <th>symbol</th><th>side</th><th>entry</th><th>entry px</th><th>current</th>
      <th>PnL $</th><th>R</th><th>milestone</th><th>dist exit</th><th>days/stop</th>
    </tr></thead>
    <tbody>{pos_body}</tbody>
  </table>
</div>

<div class="row-2">
  <div class="card">
    <h2>What Changed (since last run)</h2>
    <ul class="changed">{changed_rows}</ul>
  </div>
  {risk_card}
</div>

{cal_card}

<div class="card">
  <h2>Metrics vs Targets (leading-trader view — grey = insufficient sample, judge nothing early)</h2>
  <table>
    <thead><tr><th>sleeve</th><th>metric</th><th>live value</th><th>target / rule</th></tr></thead>
    <tbody>{metrics_body}</tbody>
  </table>
</div>

<div class="row-2">
  {universe_card}
  {alerts_card}
</div>

<div class="actions">
  Deeper analytics: <code>python strategy_report.py --file live_trades.csv</code>
  &nbsp;|&nbsp; Refresh universe: <code>python dynamic_universe.py</code>
  &nbsp;|&nbsp; Run alerter: <code>python signal_alerter.py</code>
</div>

</div>
<div class="meta">Generated {state['generated_at']}</div>
</body></html>"""


def days_in_current_state(s):
    if s.empty:
        return 0
    flips = (s != s.shift(1))
    last_flip = flips[flips].index
    if len(last_flip) == 0:
        return len(s)
    return int((s.index[-1] - last_flip[-1]).days)


# ============================================================================
# Metrics vs Targets (leading-trader monitoring, sample-size gated)
# ============================================================================

def build_metrics_rows(closed_df: pd.DataFrame, ops: dict) -> list[dict]:
    """Each row: sleeve, metric, value, target, status (ok/warn/gated).
    BEST PRACTICE: metrics below minimum sample size are GATED — displayed
    grey with progress, never colored. Numbers before minimum n are noise."""
    rows = []

    def add(sleeve, metric, value, target, status):
        rows.append({"sleeve": sleeve, "metric": metric, "value": value,
                     "target": target, "status": status})

    # ---- Trend (min n = 20 live trades) ----
    n = len(closed_df) if closed_df is not None and not closed_df.empty else 0
    if n < 20:
        add("Trend", "expectancy / win / payoff / capture",
            f"gated — {n}/20 trades", "judge at n>=20", "gated")
    else:
        r = pd.to_numeric(closed_df["r_multiple"], errors="coerce").dropna()
        exp_r = float(r.mean())
        win = float((r > 0).mean())
        wins, losses = r[r > 0], r[r <= 0]
        payoff = (float(wins.mean() / abs(losses.mean()))
                  if len(wins) and len(losses) and losses.mean() != 0 else float("nan"))
        capture = exp_r / BACKTEST_EXPECTANCY_R
        add("Trend", "Expectancy (R/trade)", f"{exp_r:+.2f}R",
            f">= {0.9*BACKTEST_EXPECTANCY_R:.2f}R",
            "ok" if exp_r >= 0.9 * BACKTEST_EXPECTANCY_R else "warn")
        add("Trend", "Win rate", f"{win:.0%}", "20-50% band",
            "ok" if 0.20 <= win <= 0.50 else "warn")
        add("Trend", "Payoff ratio", f"{payoff:.1f}x", ">= 2.0x",
            "ok" if payoff >= 2.0 else "warn")
        add("Trend", "Backtest capture", f"{capture:.0%}", ">= 60%",
            "ok" if capture >= 0.60 else "warn")

    # ---- Carry (min n = 10 closed paper episodes) ----
    cn = ops.get("carry_closed_n", 0)
    if cn < 10:
        add("Carry", "win rate / mean bps", f"gated — {cn}/10 episodes",
            "judge at n>=10", "gated")
    else:
        cw = ops.get("carry_win_rate") or 0.0
        cmean = (ops.get("carry_closed_net", 0.0) / cn) if cn else 0.0
        add("Carry", "Episode win rate", f"{cw:.0%}",
            f">= 65% (backtest {CARRY_BT_WIN:.0%})",
            "ok" if cw >= 0.65 else "warn")
        add("Carry", "Mean net/episode", f"{cmean:+.0f}bps",
            f"backtest median {CARRY_BT_MEDIAN_BPS:.0f}bps",
            "ok" if cmean > 0 else "warn")

    # ---- Unlock events (verdict at n = 10) ----
    try:
        uev = json.loads(Path("unlock_events.json").read_text(encoding="utf-8"))
    except Exception:
        uev = []
    done = [e for e in uev if e.get("status") in ("completed", "stopped")]
    if len(done) < 10:
        open_n = sum(1 for e in uev if e.get("status") == "open")
        wins_so_far = sum(1 for e in done if e.get("net_pct", 0) > 0)
        add("Unlocks", "trial progress",
            f"{len(done)}/10 done ({wins_so_far} wins), {open_n} open",
            "verdict at 10", "gated")
    else:
        w10 = sum(1 for e in done[:10] if e.get("net_pct", 0) > 0)
        m10 = sum(e.get("net_pct", 0) for e in done[:10]) / 10
        add("Unlocks", "Hit rate (10 events)", f"{w10}/10",
            ">=7 promote, <=4 tombstone",
            "ok" if w10 >= 7 else "warn")
        add("Unlocks", "Mean net/event", f"{m10:+.2f}%", "> +1.0%",
            "ok" if m10 > 1.0 else "warn")
    stops = sum(1 for e in done if e.get("status") == "stopped")
    if done and stops > 3:
        add("Unlocks", "Stop frequency", f"{stops} stopped",
            "<= 3 of 10 (crowding check)", "warn")

    # ---- Trend: profit factor (needs 20 trades like the rest) ----
    if n >= 20:
        r = pd.to_numeric(closed_df["r_multiple"], errors="coerce").dropna()
        gw, gl = float(r[r > 0].sum()), abs(float(r[r <= 0].sum()))
        pf = gw / gl if gl > 0 else float("inf")
        add("Trend", "Profit factor", f"{pf:.2f}", ">= 1.5",
            "ok" if pf >= 1.5 else "warn")

    # ---- Book level: risk-adjusted ratios (gated on equity history) ----
    try:
        eh = pd.read_csv("equity_history.csv")
        eq = eh["total"].astype(float)
        n_days = len(eh)
        if n_days >= 7:
            dd = float(((eq - eq.cummax()) / eq.cummax()).min() * 100)
            add("Book", "Total-equity max DD", f"{dd:.1f}%",
                "> -15% (act at -10%)", "ok" if dd > -10 else "warn")
        else:
            add("Book", "Total-equity max DD",
                f"gated — {n_days}/7 days of history", "needs history", "gated")

        # Sharpe / Sortino / Calmar: need >= 30 daily observations
        if n_days < 31:
            add("Book", "Sharpe / Sortino / Calmar",
                f"gated — {n_days}/31 days of equity history",
                "ratios on tiny samples are noise", "gated")
        else:
            rets = eq.pct_change().dropna()
            mu, sd = float(rets.mean()), float(rets.std())
            sharpe = mu / sd * math.sqrt(365) if sd > 0 else float("nan")
            downside = rets[rets < 0]
            dsd = float(downside.std()) if len(downside) > 1 else float("nan")
            sortino = (mu / dsd * math.sqrt(365)
                       if dsd and not math.isnan(dsd) and dsd > 0 else float("nan"))
            ann_ret = float((eq.iloc[-1] / eq.iloc[0]) ** (365 / n_days) - 1) * 100
            dd_abs = abs(float(((eq - eq.cummax()) / eq.cummax()).min() * 100))
            calmar = ann_ret / dd_abs if dd_abs > 0.01 else float("nan")
            add("Book", "Sharpe (ann., daily equity)", f"{sharpe:.2f}",
                "> 1.0 good, > 1.5 elite", "ok" if sharpe >= 1.0 else "warn")
            if not math.isnan(sortino):
                add("Book", "Sortino (ann.)", f"{sortino:.2f}",
                    "> 1.5 (trend upside-vol is the product)",
                    "ok" if sortino >= 1.5 else "warn")
            if not math.isnan(calmar) and n_days >= 90:
                add("Book", "Calmar (ann ret / max DD)", f"{calmar:.2f}",
                    "> 1.0 (CTA elite); needs 90d+", "ok" if calmar >= 1.0 else "warn")
            elif n_days < 90:
                add("Book", "Calmar", f"gated — {n_days}/90 days",
                    "needs a real drawdown history", "gated")
    except Exception:
        add("Book", "Risk-adjusted ratios", "no equity_history.csv yet",
            "needs history", "gated")

    hr, ho = ops.get("health_runs", 0), ops.get("health_ok", 0)
    if hr:
        pct = ho / hr * 100
        add("Book", "Automation uptime (14d)", f"{ho}/{hr} ({pct:.0f}%)",
            ">= 90%", "ok" if pct >= 90 else "warn")
    n_issues = len(ops.get("acct_issues", []))
    add("Book", "Reconciliation mismatches", str(n_issues), "always 0",
        "ok" if n_issues == 0 else "warn")

    return rows


# ============================================================================
# Dashboard v5 helpers: banner / sparkline / unlock panel / calendar
# ============================================================================

def build_action_banner(state: dict) -> tuple[str, str, str]:
    """Returns (css_class, headline, detail). The whole dashboard in one line."""
    ops = state.get("ops", {})
    if state.get("data_stale"):
        return ("err", "⚠ ACTION NEEDED: market data is STALE",
                f"latest BTC candle is {state['data_stale']}h old — every signal "
                f"below may be wrong. Check VPN / run daily_check manually.")
    if ops.get("exec_halted"):
        return ("err", "⚠ ACTION NEEDED: executor HALTED",
                f"{ops.get('exec_halt_reason','')[:120]} — investigate, then "
                f"delete STOP_TRADING to resume.")
    if ops.get("acct_issues"):
        return ("err", "⚠ ACTION NEEDED: position mismatch",
                "; ".join(ops["acct_issues"])[:160])
    if ops.get("acct_age_h") is not None and ops["acct_age_h"] > 36:
        return ("warn", "CHECK: account sync is stale",
                f"last synced {ops['acct_age_h']:.0f}h ago — nightly run may be failing.")
    return ("ok", "NO ACTION NEEDED", "all systems nominal — the machine has the watch")


def equity_spark_svg(hist: list, width: int = 220, height: int = 40) -> str:
    """Inline SVG sparkline of total equity. No JS, no libraries."""
    if len(hist) < 2:
        return f"<span class='muted'>sparkline after {2-len(hist)} more day(s)</span>"
    vals = [v for _, v in hist][-60:]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    pts = []
    for i, v in enumerate(vals):
        x = i / (len(vals) - 1) * (width - 4) + 2
        y = height - 4 - (v - lo) / rng * (height - 8)
        pts.append(f"{x:.1f},{y:.1f}")
    color = "#00ba7c" if vals[-1] >= vals[0] else "#f4212e"
    return (f"<svg width='{width}' height='{height}'>"
            f"<polyline fill='none' stroke='{color}' stroke-width='1.5' "
            f"points='{' '.join(pts)}'/></svg>")


def today_delta(hist: list) -> tuple:
    """(delta_$, delta_pct, span_label) vs previous recorded day."""
    if len(hist) < 2:
        return (None, None, "")
    prev, cur = hist[-2][1], hist[-1][1]
    return (cur - prev, (cur / prev - 1) * 100 if prev else 0.0,
            f"{hist[-2][0]} → {hist[-1][0]}")


def upcoming_events(ops: dict) -> list[tuple]:
    """Date-sorted (date, label) list of everything scheduled."""
    now = pd.Timestamp.now(tz="UTC")
    ev = [(pd.Timestamp("2026-09-01", tz="UTC"),
           "Carry paper review — go/no-go for $200 live tranche")]
    for e in ops.get("unlock_events", []):
        u = pd.Timestamp(e["unlock_date"], tz="UTC")
        if e.get("status") == "open":
            ev.append((u, f"{e['symbol']} UNLOCK ({e['pct_supply']}% supply) — riding it"))
            ev.append((u + pd.Timedelta(days=4),
                       f"{e['symbol']} paper short closes (T+4) — result to Discord"))
        elif e.get("status") == "pending":
            ev.append((u - pd.Timedelta(days=10),
                       f"{e['symbol']} paper short entry window opens (T-10)"))
            ev.append((u, f"{e['symbol']} UNLOCK ({e['pct_supply']}% supply)"))
    ev = [(d, l) for d, l in ev if d >= now - pd.Timedelta(days=1)]
    return sorted(ev)[:8]


def mc_cone_svg(closed_df, width=680, height=220) -> str:
    """Monte Carlo cone (from mc_cone.json) with live cumulative-R overlaid.
    Inside the shaded cone = noise, no decision warranted. Below p5 = evidence."""
    try:
        cone = json.loads(Path("mc_cone.json").read_text(encoding="utf-8"))
    except Exception:
        return "<span class='muted'>run <code>python mc_cone.py</code> to generate the cone</span>"
    b = cone["bands"]
    n = cone["n_trades"]
    lo = min(min(b["5"]), -2)
    hi = max(max(b["95"]), 2)

    def xy(t, v):
        x = 40 + t / n * (width - 60)
        y = height - 25 - (v - lo) / (hi - lo) * (height - 45)
        return f"{x:.1f},{y:.1f}"

    def band_path(upper, lower):
        pts = [xy(i + 1, v) for i, v in enumerate(upper)]
        pts += [xy(i + 1, v) for i, v in reversed(list(enumerate(lower)))]
        return " ".join(pts)

    zero_y = xy(0, 0).split(",")[1]
    svg = [f"<svg width='{width}' height='{height}'>"]
    svg.append(f"<polygon points='{xy(0,0)} {band_path(b['95'], b['5'])}' "
               f"fill='#1d9bf0' opacity='0.12'/>")
    svg.append(f"<polygon points='{xy(0,0)} {band_path(b['75'], b['25'])}' "
               f"fill='#1d9bf0' opacity='0.18'/>")
    med = " ".join([xy(0, 0)] + [xy(i + 1, v) for i, v in enumerate(b["50"])])
    svg.append(f"<polyline points='{med}' fill='none' stroke='#1d9bf0' "
               f"stroke-width='1' stroke-dasharray='4,3'/>")
    svg.append(f"<line x1='40' y1='{zero_y}' x2='{width-20}' y2='{zero_y}' "
               f"stroke='#536471' stroke-width='0.5'/>")
    # live path
    if closed_df is not None and not closed_df.empty and "r_multiple" in closed_df.columns:
        r = pd.to_numeric(closed_df.sort_values("exit_date")["r_multiple"],
                          errors="coerce").dropna().cumsum()
        if len(r):
            live = " ".join([xy(0, 0)] + [xy(i + 1, v) for i, v in enumerate(r[:n])])
            svg.append(f"<polyline points='{live}' fill='none' stroke='#00ba7c' "
                       f"stroke-width='2'/>")
    # axis labels
    for v in (b["95"][-1], b["50"][-1], 0, b["5"][-1]):
        x, y = xy(n, v).split(",")
        svg.append(f"<text x='{float(x)+3}' y='{float(y)+3}' fill='#8899a6' "
                   f"font-size='10'>{v:+.0f}R</text>")
    svg.append("</svg>")
    return "".join(svg)


def stress_rows(ops: dict) -> list[str]:
    """BTC -30% overnight scenario per open futures position."""
    rows = []
    for p in ops.get("acct", {}).get("futures", {}).get("positions", []):
        mark = p.get("mark_price") or p.get("entry_price", 0)
        amt = p.get("amount", 0)
        if not mark or not amt:
            continue
        shocked = mark * 0.70          # beta~1 assumption: -30% underlying
        pnl = (shocked - mark) * amt   # negative for longs, positive for shorts
        liq = p.get("liq_price", 0)
        breached = (liq > 0 and ((amt > 0 and shocked <= liq)
                                 or (amt < 0 and shocked >= liq)))
        c = "pnl-pos" if pnl >= 0 else "pnl-neg"
        rows.append(f"<tr><td>{p['symbol']}</td><td>{p['side']}</td>"
                    f"<td class='{c}'>{pnl:+.2f}$</td>"
                    f"<td>{p.get('liq_dist_pct','—')}%</td>"
                    f"<td class='{'pnl-neg' if breached else 'pnl-pos'}'>"
                    f"{'LIQUIDATED' if breached else 'survives'}</td></tr>")
    return rows


# ============================================================================
# Ops state (executor / account / carry / automation health)
# ============================================================================

def _read_json(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def collect_ops() -> dict:
    """Gather everything the operator needs to know about the autonomous
    parts of the system: executor, real account, carry sleeve, run health."""
    ops = {}

    # --- executor ---
    cfg = _read_json("executor_config.json")
    est = _read_json("executor_state.json")
    ops["exec_live"] = bool(cfg.get("live", False))
    ops["exec_halted"] = Path("STOP_TRADING").exists()
    ops["exec_halt_reason"] = ""
    if ops["exec_halted"]:
        try:
            ops["exec_halt_reason"] = Path("STOP_TRADING").read_text(encoding="utf-8")[:200]
        except Exception:
            pass
    ops["exec_rails"] = (f"max {cfg.get('max_open_positions', 4)} pos · "
                         f"${cfg.get('max_notional_per_trade', 40):.0f} cap · "
                         f"{cfg.get('daily_loss_limit_pct', 5):.0f}% breaker · "
                         f"{cfg.get('leverage', 3)}x {cfg.get('margin_type', 'ISOLATED').lower()}")
    ops["exec_last_run"] = est.get("last_run", "never")
    ops["exec_n_executed"] = len(est.get("executed_signals", []))

    # --- account (from account_sync) ---
    acct = _read_json("account_state.json")
    ops["acct"] = acct
    ops["acct_age_h"] = None
    if acct.get("synced_at"):
        try:
            ops["acct_age_h"] = (pd.Timestamp.now(tz="UTC")
                                 - pd.Timestamp(acct["synced_at"])).total_seconds() / 3600.0
        except Exception:
            pass
    ops["acct_issues"] = acct.get("cross_check_issues", [])

    # --- carry sleeve ---
    cst = _read_json("paper_carry_state.json")
    ops["carry_open"] = cst.get("open", {})
    ops["carry_last_run"] = cst.get("last_run", "never")
    ops["carry_closed_n"] = 0
    ops["carry_closed_net"] = 0.0
    ops["carry_win_rate"] = None
    log_p = Path("paper_carry_log.csv")
    if log_p.exists():
        try:
            cdf = pd.read_csv(log_p)
            closed = cdf[cdf["action"] == "EXIT"]
            if not closed.empty:
                ops["carry_closed_n"] = len(closed)
                ops["carry_closed_net"] = float(closed["net_bps"].sum())
                ops["carry_win_rate"] = float((closed["net_bps"] > 0).mean())
        except Exception:
            pass
    ops["carry_review_days"] = max(0, (pd.Timestamp("2026-09-01", tz="UTC")
                                       - pd.Timestamp.now(tz="UTC")).days)
    ops["carry_best_bps"] = cst.get("best_trail_bps")
    ops["carry_best_symbol"] = cst.get("best_trail_symbol", "")

    # --- unlock events sleeve ---
    try:
        ops["unlock_events"] = json.loads(
            Path("unlock_events.json").read_text(encoding="utf-8"))
    except Exception:
        ops["unlock_events"] = []

    # --- equity history (today's P&L + sparkline) ---
    ops["eq_hist"] = []
    try:
        eh = pd.read_csv("equity_history.csv")
        ops["eq_hist"] = list(zip(eh["date"].astype(str), eh["total"].astype(float)))
    except Exception:
        pass

    # --- automation health (last 14 days of daily_check completions) ---
    ops["health_runs"] = 0
    ops["health_ok"] = 0
    ops["health_last451"] = "none in window"
    try:
        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=14)
        lines = Path("automation.log").read_text(encoding="utf-8",
                                                 errors="ignore").splitlines()
        last_451 = None
        for ln in lines:
            m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC\]", ln)
            if not m:
                continue
            ts = pd.Timestamp(m.group(1), tz="UTC")
            if ts < cutoff:
                continue
            if "daily_check complete" in ln:
                ops["health_runs"] += 1
                if "FAIL" not in ln:
                    ops["health_ok"] += 1
            if "451" in ln:
                last_451 = ts
        if last_451 is not None:
            ops["health_last451"] = f"{(pd.Timestamp.now(tz='UTC') - last_451).days}d ago"
    except Exception:
        pass

    return ops


def main():
    import os
    print("Fetching market data ...")
    symbol_data = {}
    for s in SYMBOLS:
        try:
            df = fetch_recent(s, bars=300)
            symbol_data[s] = df
            symbol_data[f"_funding_{s}"] = fetch_current_funding_bps(s)
        except Exception as e:
            print(f"  WARN: {s}: {e}")

    if "BTCUSDT" not in symbol_data or symbol_data["BTCUSDT"].empty:
        print("BTCUSDT missing - aborting.")
        return

    print("Fetching Fear & Greed ...")
    fng_value = float("nan")
    try:
        fng_df = fetch_fear_greed_history(limit=30)
        if not fng_df.empty:
            fng_value = float(fng_df["fng_value"].iloc[-1])
    except Exception as e:
        print(f"  WARN: FNG: {e}")

    btc_df = symbol_data["BTCUSDT"]
    btc_rel = {s: (btc_relative_return(symbol_data[s], btc_df)
                   if (s != "BTCUSDT"
                       and isinstance(symbol_data.get(s), pd.DataFrame)
                       and not symbol_data[s].empty
                       and "close" in symbol_data[s].columns) else None)
               for s in SYMBOLS}

    btc_regime = wf3.compute_btc_regime(btc_df)
    macro_on = bool(btc_regime.iloc[-1]) if not btc_regime.empty else False
    days_in_regime = days_in_current_state(btc_regime.tail(400))

    # Macro proximity: SMA200 now vs 20 days ago, in %. Positive slope = ON.
    macro_gap_pct = float("nan")
    try:
        sma = btc_df["close"].rolling(BTC_SMA_PERIOD).mean()
        if len(sma.dropna()) > 20:
            macro_gap_pct = float((sma.iloc[-1] / sma.iloc[-21] - 1) * 100)
    except Exception:
        pass

    fund_input = None
    try:
        fund_input = {}
        for s in SYMBOLS:
            df = symbol_data.get(s)
            if df is None or not isinstance(df, pd.DataFrame) or df.empty:
                continue
            v = symbol_data.get(f"_funding_{s}", 0.0)
            fund_input[s] = pd.DataFrame({"funding_bps_8h_last": [v]*len(df)}, index=df.index)
    except Exception:
        fund_input = None
    regime_df = all_regimes(symbol_data, fund_input)
    if not regime_df.empty:
        lr = regime_df.iloc[-1]
        vol_state = str(lr.get("vol_regime", "NORMAL"))
        corr_state = str(lr.get("corr_regime", "MIXED"))
        funding_state = str(lr.get("funding_regime", "NEUTRAL"))
    else:
        vol_state, corr_state, funding_state = "NORMAL", "MIXED", "NEUTRAL"
    mult_info = describe_multiplier(vol_state, corr_state, funding_state, is_long=True)

    signals = compute_signals(symbol_data, macro_on, fng_value, btc_rel)
    mo = market_overview(symbol_data)

    trades_df = load_live_trades()
    open_df, closed_df = split_open_closed(trades_df) if not trades_df.empty else (pd.DataFrame(), pd.DataFrame())
    open_positions = annotate_open_positions(open_df, symbol_data) if not open_df.empty else []

    total_open_risk = sum(p.get("risk_dollars", 0.0) for p in open_positions)
    closed_pnl = float(closed_df["pnl_net"].sum()) if not closed_df.empty and "pnl_net" in closed_df.columns else 0.0
    equity_now = live_equity(fallback=STARTING_EQUITY + closed_pnl)
    heat_pct = (total_open_risk / equity_now * 100.0) if equity_now > 0 else 0.0

    pipe_pos = pipeline_positions(open_positions)
    open_syms = {p["symbol"] for p in open_positions}
    pipe_watch = pipeline_watchlist(signals, open_syms)

    risk = risk_exposure(open_positions, equity_now, closed_df)
    deployment = load_deployment_state()
    kpis = perf_kpis(closed_df, deployment, btc_df)
    cal = calibration(closed_df)
    recent_alerts = load_recent_alerts(10)
    alert_age = last_alert_age_hours()

    now_sig_map = {s.symbol: s.verdict for s in signals}
    now_state_for_save = {
        "last_run": pd.Timestamp.now(tz="UTC").isoformat(),
        "btc_macro_on": macro_on,
        "signals": now_sig_map,
        "universe": list(SYMBOLS),
        "open_positions": [p["symbol"] for p in open_positions],
        "last_alert_age_h": alert_age,
    }
    last_state = load_last_state()
    changed = what_changed(now_state_for_save, last_state)
    save_last_state(now_state_for_save)

    state = {
        "generated_at": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M UTC"),
        "macro_on": macro_on, "days_in_regime": days_in_regime,
        "regime_states": {"vol": vol_state, "corr": corr_state, "funding": funding_state,
                          "multiplier": mult_info["multiplier"], "reasons": mult_info["reasons"]},
        "market": mo, "fng_value": fng_value,
        "signals": signals,
        "open_positions": open_positions, "n_open": len(open_positions),
        "heat_pct": heat_pct,
        "pipeline_positions": pipe_pos,
        "pipeline_watchlist": pipe_watch,
        "changed": changed,
        "risk": risk,
        "kpis": kpis,
        "cal": cal,
        "deployment": deployment,
        "recent_alerts": recent_alerts,
        "last_alert_age_h": alert_age,
        "discord_set": bool(os.environ.get("DISCORD_WEBHOOK_URL")),
        "macro_gap_pct": macro_gap_pct,
        "rs_set": compute_top_quintile_rs(symbol_data) if USE_REL_STRENGTH else set(),
        "ops": collect_ops(),
    }
    state["metrics_rows"] = build_metrics_rows(closed_df, state["ops"])
    try:
        from analytics import advanced_rows
        state["metrics_rows"] += advanced_rows(closed_df)
    except Exception as e:
        print(f"  WARN analytics: {e}")
    state["closed_df"] = closed_df

    # data staleness alarm: if the latest BTC candle is old, every signal is suspect
    state["data_stale"] = None
    try:
        age_h = (pd.Timestamp.now(tz="UTC") - btc_df.index[-1]).total_seconds() / 3600
        if age_h > 36:
            state["data_stale"] = round(age_h)
    except Exception:
        pass

    # enrich open unlock events with a current price (best effort)
    try:
        from net_utils import fetch_binance_futures
        for e in state["ops"].get("unlock_events", []):
            if e.get("status") == "open":
                try:
                    r = fetch_binance_futures("/fapi/v1/ticker/price",
                                              {"symbol": e["symbol"]})
                    e["cur_px"] = float(r.json()["price"])
                except Exception:
                    pass
    except Exception:
        pass

    html = render_html(state)
    out = Path("dashboard.html")
    out.write_text(html, encoding="utf-8")
    print(f"BTC macro: {'ON' if macro_on else 'OFF'} ({days_in_regime}d)")
    print(f"Open positions: {len(open_positions)}, heat: {heat_pct:.2f}%")
    print(f"Regimes: vol={vol_state}, corr={corr_state}, funding={funding_state} -> x{mult_info['multiplier']:.2f}")
    print(f"\nWrote {out.resolve()}")


if __name__ == "__main__":
    main()
