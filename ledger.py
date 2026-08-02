"""
Sleeve Ledger — per-sleeve P&L attribution.
============================================

Answers "which sleeve made the money?" — impossible to reconstruct later if
not tracked from day one.

Method (residual attribution, the standard desk approach):
    total_equity = inception + deposits + trend_PnL + carry_PnL + unlock_PnL
                   + yield_and_residual
  - trend realized  : sum of pnl_net in live_trades.csv
  - trend unrealized: futures uPnL from account_state.json
  - carry / unlock  : live trade logs when those sleeves go live (paper = $0)
  - yield_and_residual: whatever ΔEquity the above doesn't explain —
    dominated by Simple Earn interest; also absorbs fees/dust. If this line
    ever goes meaningfully NEGATIVE, money is leaking somewhere unexplained
    -> investigate immediately.

DEPOSITS MUST BE LOGGED or attribution breaks:
    python ledger.py deposit 50          # after depositing $50 to Binance
    python ledger.py withdraw 20
    python ledger.py show

State: sleeve_ledger.json (gitignored — personal financial data).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

LEDGER = Path("sleeve_ledger.json")
INCEPTION_EQUITY = 111.76          # verified 2026-07-31 post LTC->USDT
INCEPTION_DATE = "2026-07-31"


def load() -> dict:
    if LEDGER.exists():
        try:
            return json.loads(LEDGER.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"inception_equity": INCEPTION_EQUITY,
            "inception_date": INCEPTION_DATE,
            "flows": []}   # [{date, amount}] +deposit / -withdrawal


def save(d: dict) -> None:
    LEDGER.write_text(json.dumps(d, indent=2), encoding="utf-8")


def record_flow(amount: float) -> None:
    d = load()
    d["flows"].append({"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                       "amount": round(amount, 2)})
    save(d)
    total = sum(f["amount"] for f in d["flows"])
    print(f"recorded {amount:+.2f}. Net deposits to date: {total:+.2f}")


def attribution() -> dict:
    """Compute per-sleeve P&L. Returns dict of rows + integrity fields."""
    d = load()
    deposits = sum(f["amount"] for f in d["flows"])

    # trend realized (live_trades.csv)
    trend_realized = 0.0
    n_trades = 0
    try:
        lt = pd.read_csv("live_trades.csv")
        if not lt.empty and "pnl_net" in lt.columns:
            closed = lt[lt["exit_date"].notna() & (lt["exit_date"] != "")]
            trend_realized = float(pd.to_numeric(closed["pnl_net"],
                                                 errors="coerce").fillna(0).sum())
            n_trades = len(closed)
    except Exception:
        pass

    # trend unrealized + current equity (account_state.json)
    trend_unrealized = 0.0
    equity = None
    try:
        acct = json.loads(Path("account_state.json").read_text(encoding="utf-8"))
        trend_unrealized = float(acct.get("futures", {}).get("unrealized_pnl", 0))
        equity = float(acct.get("total_equity_usd", 0))
    except Exception:
        pass

    carry_live = 0.0     # populated when carry goes live (own trade log)
    unlock_live = 0.0    # populated if unlock sleeve is ever promoted

    explained = trend_realized + trend_unrealized + carry_live + unlock_live
    yield_resid = None
    if equity is not None:
        yield_resid = equity - d["inception_equity"] - deposits - explained

    return {
        "equity": equity,
        "inception": d["inception_equity"],
        "deposits": deposits,
        "trend_realized": trend_realized,
        "trend_unrealized": trend_unrealized,
        "n_trades": n_trades,
        "carry_live": carry_live,
        "unlock_live": unlock_live,
        "yield_resid": yield_resid,
    }


def project_milestones(annual_return: float = 0.15) -> dict:
    """Project equity forward at the recent deposit pace + assumed return.
    Returns milestone dates for $1k / $10k / $25k. Conservative by design."""
    a = attribution()
    equity = a["equity"] or INCEPTION_EQUITY
    d = load()
    # deposit pace: trailing 90d of flows, monthly rate
    now = pd.Timestamp.now(tz="UTC")
    recent = [f["amount"] for f in d["flows"]
              if pd.Timestamp(f["date"], tz="UTC") > now - pd.Timedelta(days=90)]
    monthly = sum(recent) / 3.0
    r_m = (1 + annual_return) ** (1 / 12) - 1
    eq = equity
    milestones = {}
    for m in range(1, 121):
        eq = eq * (1 + r_m) + monthly
        for target in (1_000, 10_000, 25_000):
            if target not in milestones and eq >= target:
                milestones[target] = (now + pd.DateOffset(months=m)).strftime("%Y-%m")
    return {"equity": equity, "monthly_pace": monthly,
            "assumed_return": annual_return, "milestones": milestones}


def show() -> None:
    a = attribution()
    print(f"inception {INCEPTION_DATE}: ${a['inception']:.2f}")
    print(f"net deposits:        {a['deposits']:+.2f}")
    print(f"trend realized:      {a['trend_realized']:+.2f}  ({a['n_trades']} closed trades)")
    print(f"trend unrealized:    {a['trend_unrealized']:+.2f}")
    print(f"carry (live):        {a['carry_live']:+.2f}")
    print(f"unlocks (live):      {a['unlock_live']:+.2f}")
    if a["yield_resid"] is not None:
        print(f"yield + residual:    {a['yield_resid']:+.2f}")
        print(f"current equity:      ${a['equity']:.2f}")
        if a["yield_resid"] < -1.0:
            print("!! residual is negative beyond rounding — money is leaking "
                  "somewhere unexplained. Investigate.")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "show":
        show()
    elif args[0] == "deposit" and len(args) == 2:
        record_flow(abs(float(args[1])))
    elif args[0] == "withdraw" and len(args) == 2:
        record_flow(-abs(float(args[1])))
    else:
        print(__doc__)
