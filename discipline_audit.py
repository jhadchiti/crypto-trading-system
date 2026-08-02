"""
Cost of Discipline — nightly audit of every blocked breakout.
==============================================================

Answers, with receipts, the recurring question: "the dashboard shows missed
opportunities — is the macro filter costing me money?"

Method: simulate taking EVERY 55d breakout (both directions) during the
current macro-OFF streak with standard exits (2xATR stop, 20d channel, ~costs)
— deliberately generous to the missed-opportunity claim (no RS gate, no
funding gate). Writes discipline_audit.json for the dashboard.

Selection-bias truth this panel encodes: stopped-out losers vanish from your
screen; open winners stay visible and running. The closed-trade total is the
honest ledger of what taking blocked signals actually earns.

Runs nightly via daily_check (advisory). Dormant while macro is ON.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

import secrets_local  # noqa: F401
import donchian_baseline as dc
import walk_forward_v3 as wf3
import walk_forward_v6 as wf6

OUT = Path("discipline_audit.json")
COST_R = 0.02   # round-trip costs expressed in R (approx)


def current_off_start(regime: pd.Series):
    """Start date of the current macro-OFF streak, or None if macro is ON."""
    if regime.empty or bool(regime.iloc[-1]):
        return None
    flip_idx = None
    vals = regime.values
    for i in range(len(vals) - 1, -1, -1):
        if vals[i]:
            flip_idx = i + 1
            break
    return regime.index[flip_idx] if flip_idx is not None else regime.index[0]


def main():
    end_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    start_ms = int(pd.Timestamp("2019-09-01", tz="UTC").timestamp() * 1000)

    btc = wf6.fetch_ohlcv_cached("BTCUSDT", start_ms, end_ms)
    if btc.empty:
        print("no BTC data"); return
    regime = wf3.compute_btc_regime(btc)
    off_start = current_off_start(regime)
    if off_start is None:
        OUT.write_text(json.dumps({"active": False,
                                   "note": "macro ON — discipline audit dormant"}),
                       encoding="utf-8")
        print("macro ON — audit dormant")
        return

    uni = json.loads(Path("active_universe.json").read_text())["universe"]
    trades, open_tr = [], []

    for sym in uni:
        try:
            df = wf6.fetch_ohlcv_cached(sym, start_ms, end_ms)
        except Exception:
            continue
        if df.empty or len(df) < 120:
            continue
        d = dc.build_donchian(df, dc.DCFG)
        pos = None
        for date, row in d.iterrows():
            if date < off_start:
                continue
            if pos is not None:
                reason = exit_px = None
                if pos["side"] > 0 and row["low"] <= pos["stop"]:
                    exit_px, reason = pos["stop"], "stop"
                elif pos["side"] < 0 and row["high"] >= pos["stop"]:
                    exit_px, reason = pos["stop"], "stop"
                elif pos["side"] > 0 and not math.isnan(row["exit_low"]) and row["close"] < row["exit_low"]:
                    exit_px, reason = row["close"], "channel"
                elif pos["side"] < 0 and not math.isnan(row["exit_high"]) and row["close"] > row["exit_high"]:
                    exit_px, reason = row["close"], "channel"
                if reason:
                    risk = abs(pos["entry"] - pos["stop"])
                    r = (exit_px - pos["entry"]) / risk * pos["side"] - COST_R
                    trades.append({"side": pos["side"], "r": r})
                    pos = None
            try:
                macro_off = not bool(regime.loc[date])
            except KeyError:
                continue
            if pos is None and macro_off and not math.isnan(row["atr"]) and row["atr"] > 0:
                if not math.isnan(row["entry_high"]) and row["close"] > row["entry_high"]:
                    pos = {"side": 1, "entry": row["close"],
                           "stop": row["close"] - 2 * row["atr"], "date": date}
                elif not math.isnan(row["entry_low"]) and row["close"] < row["entry_low"]:
                    pos = {"side": -1, "entry": row["close"],
                           "stop": row["close"] + 2 * row["atr"], "date": date}
        if pos is not None:
            last = d.iloc[-1]
            risk = abs(pos["entry"] - pos["stop"])
            open_tr.append({"sym": sym,
                            "side": "LONG" if pos["side"] > 0 else "SHORT",
                            "since": str(pos["date"].date()),
                            "r": round((last["close"] - pos["entry"]) / risk * pos["side"], 2)})

    tr = pd.DataFrame(trades)
    days = (pd.Timestamp.now(tz="UTC") - off_start).days
    # dollars at current sizing
    equity = 111.76
    try:
        equity = float(json.loads(Path("account_state.json").read_text())
                       .get("total_equity_usd", equity))
    except Exception:
        pass

    result = {
        "active": True,
        "off_start": str(off_start.date()),
        "days": days,
        "n_closed": int(len(tr)),
        "closed_total_r": round(float(tr["r"].sum()), 1) if len(tr) else 0.0,
        "closed_win": round(float((tr["r"] > 0).mean()), 2) if len(tr) else None,
        "longs_r": round(float(tr[tr["side"] > 0]["r"].sum()), 1) if len(tr) else 0.0,
        "shorts_r": round(float(tr[tr["side"] < 0]["r"].sum()), 1) if len(tr) else 0.0,
        "dollars_if_taken": round(float(tr["r"].sum()) * equity * 0.0075, 2) if len(tr) else 0.0,
        "earn_alt": round(equity * 0.017 * days / 365, 2),
        "open_hypotheticals": sorted(open_tr, key=lambda x: -x["r"])[:6],
        "open_total_r": round(sum(t["r"] for t in open_tr), 1),
        "generated": pd.Timestamp.now(tz="UTC").isoformat(),
    }
    OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"blocked-signal audit: {result['n_closed']} closed = "
          f"{result['closed_total_r']:+.1f}R ({result['dollars_if_taken']:+.2f}$), "
          f"{len(open_tr)} open hypotheticals = {result['open_total_r']:+.1f}R")


if __name__ == "__main__":
    main()
