"""
Delta-Neutral Funding Carry — Backtest Harness
==============================================

Implements the strategy in FUNDING_CARRY_SPEC.md against full historical
Binance USD-M funding data.

Position: long spot + short perp, notional N per leg. Delta-neutral.
Income:   funding rate x N per 8h event while short perp and funding > 0.
Costs:    ~48bps of notional round trip (4 executions, fees + slippage).

Entry:  trailing 9-event (3-day) mean funding >= ENTRY_BPS (10 bps/8h)
Exit:   trailing mean <= EXIT_BPS (3), single event < -5 bps, or age > 90d

Portfolio: capital split equally across up to K=3 concurrent positions.
Idle capital earns SIMPLE_EARN_APY as the benchmark alternative.

Outputs:
  funding_carry_episodes.csv   every episode (symbol, dates, gross, net, ...)
  funding_carry_summary.csv    per-symbol aggregates
  console report + decision-rule verdict

Usage:
    python funding_carry_backtest.py                     # default universe
    python funding_carry_backtest.py --max-symbols 50
    python funding_carry_backtest.py --entry-bps 12 --exit-bps 4
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from funding import fetch_funding
from market_data import fetch_all_perp_symbols, filter_by_history, fetch_24h_ticker_all


# ============================================================================
# Config (defaults match FUNDING_CARRY_SPEC.md)
# ============================================================================

ENTRY_BPS = 10.0          # trailing mean funding to enter (bps per 8h)
EXIT_BPS = 3.0            # trailing mean funding to exit
HARD_EXIT_BPS = -5.0      # single-event hard exit
TRAIL_EVENTS = 9          # 9 events = 3 days
MAX_AGE_DAYS = 90
MAX_CONCURRENT = 3        # K
LEVERAGE = 2.0            # perp leverage (capital = N + N/L)

SPOT_FEE_BPS = 10.0       # per side
PERP_FEE_BPS = 4.0        # per side
SLIPPAGE_BPS = 5.0        # per leg per side
# round trip: (spot fee + slip) x2 + (perp fee + slip) x2
RT_COST_BPS = 2 * (SPOT_FEE_BPS + SLIPPAGE_BPS) + 2 * (PERP_FEE_BPS + SLIPPAGE_BPS)

SIMPLE_EARN_APY = 0.04    # benchmark for idle capital

CACHE_DIR = Path("cache/funding_full")


# ============================================================================
# Data
# ============================================================================

def fetch_funding_cached(symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Full funding history with a local CSV cache (funding history is
    append-only, so cache is refreshed only if stale > 2 days)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = CACHE_DIR / f"{symbol}.csv"
    if f.exists():
        df = pd.read_csv(f, parse_dates=["funding_time"])
        df["funding_time"] = pd.to_datetime(df["funding_time"], utc=True)
        if not df.empty and df["funding_time"].iloc[-1] >= (
                pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=2)):
            return df
    df = fetch_funding(symbol, start_ms, end_ms)
    if not df.empty:
        df.to_csv(f, index=False)
    return df


# ============================================================================
# Per-symbol episode simulation
# ============================================================================

@dataclass
class Episode:
    symbol: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    n_events: int
    gross_bps: float          # funding collected, bps of notional
    net_bps: float            # after round-trip costs
    exit_reason: str

    @property
    def days(self) -> float:
        return (self.exit_time - self.entry_time).total_seconds() / 86400.0


def simulate_symbol(symbol: str, events: pd.DataFrame,
                    entry_bps: float, exit_bps: float,
                    hard_exit_bps: float, trail: int,
                    max_age_days: int) -> list[Episode]:
    """
    Run the entry/exit state machine over a symbol's 8h funding events.

    Funding sign convention: positive rate = longs pay shorts. We are SHORT
    the perp, so positive funding is income to us.
    """
    if events.empty or len(events) < trail + 1:
        return []

    ev = events.copy().reset_index(drop=True)
    ev["bps"] = ev["funding_rate"] * 10_000.0
    ev["trail_mean"] = ev["bps"].rolling(trail).mean()

    episodes: list[Episode] = []
    in_pos = False
    entry_i = 0
    accrued = 0.0

    for i in range(len(ev)):
        row = ev.iloc[i]
        tm = row["trail_mean"]
        if not in_pos:
            if not math.isnan(tm) and tm >= entry_bps:
                in_pos = True
                entry_i = i
                accrued = 0.0
        else:
            # collect this event's funding (we hold through it)
            accrued += row["bps"]
            age_days = (row["funding_time"] - ev.iloc[entry_i]["funding_time"]
                        ).total_seconds() / 86400.0
            reason = None
            if row["bps"] < hard_exit_bps:
                reason = "hard_flip"
            elif not math.isnan(tm) and tm <= exit_bps:
                reason = "funding_decay"
            elif age_days >= max_age_days:
                reason = "time_stop"
            if reason is not None:
                episodes.append(Episode(
                    symbol=symbol,
                    entry_time=ev.iloc[entry_i]["funding_time"],
                    exit_time=row["funding_time"],
                    n_events=i - entry_i + 1,
                    gross_bps=accrued,
                    net_bps=accrued - RT_COST_BPS,
                    exit_reason=reason,
                ))
                in_pos = False

    # close any open episode at data end
    if in_pos:
        last = ev.iloc[-1]
        episodes.append(Episode(
            symbol=symbol,
            entry_time=ev.iloc[entry_i]["funding_time"],
            exit_time=last["funding_time"],
            n_events=len(ev) - entry_i,
            gross_bps=accrued,
            net_bps=accrued - RT_COST_BPS,
            exit_reason="data_end",
        ))
    return episodes


# ============================================================================
# Portfolio simulation (8h grid, K slots, equal capital)
# ============================================================================

def simulate_portfolio(episodes: list[Episode], leverage: float,
                       max_concurrent: int, simple_earn_apy: float,
                       ) -> dict:
    """
    Walk an 8h time grid. At each step, active episodes occupy up to K slots
    (first-come, first-served by entry time). Capital per slot = 1/K of total.

    Returns dict with the portfolio equity curve (on 1.0 starting capital)
    and summary stats. Capital efficiency: return on capital = bps of
    notional x (N / capital) where capital per position = N * (1 + 1/L)
    => notional_per_capital = 1 / (1 + 1/L).
    """
    if not episodes:
        return {}

    notional_per_capital = 1.0 / (1.0 + 1.0 / leverage)

    eps = sorted(episodes, key=lambda e: e.entry_time)
    t0 = min(e.entry_time for e in eps)
    t1 = max(e.exit_time for e in eps)
    grid = pd.date_range(t0, t1, freq="8h", tz="UTC")
    if len(grid) < 2:
        return {}

    # For accrual, spread each episode's NET bps evenly over its events.
    per_event_net = {id(e): e.net_bps / max(e.n_events, 1) for e in eps}

    active: list = []
    queue = list(eps)
    equity = 1.0
    curve = []
    slots_used_total = 0

    earn_per_8h = (1 + simple_earn_apy) ** (8.0 / (24 * 365)) - 1

    for t in grid:
        # release finished
        active = [e for e in active if e.exit_time > t]
        # admit new (respect K)
        while queue and queue[0].entry_time <= t and len(active) < max_concurrent:
            active.append(queue.pop(0))
        # skip queued episodes that started but never got a slot before ending
        while queue and queue[0].entry_time <= t and len(active) >= max_concurrent:
            if queue[0].exit_time <= t:
                queue.pop(0)
            else:
                break

        k = len(active)
        slots_used_total += k
        # accrue funding on active slots
        step_ret = 0.0
        for e in active:
            slot_capital_frac = 1.0 / max_concurrent
            step_ret += (per_event_net[id(e)] / 10_000.0) * notional_per_capital * slot_capital_frac
        # idle capital earns Simple Earn
        idle_frac = 1.0 - k / max_concurrent
        step_ret += idle_frac * earn_per_8h

        equity *= (1 + step_ret)
        curve.append((t, equity))

    curve_df = pd.DataFrame(curve, columns=["time", "equity"]).set_index("time")
    years = (grid[-1] - grid[0]).total_seconds() / (365.25 * 86400)
    apy = (curve_df["equity"].iloc[-1]) ** (1 / max(years, 1e-9)) - 1

    # worst 30-day window
    eq = curve_df["equity"]
    window = 90  # 90 x 8h = 30 days
    roll_ret = eq / eq.shift(window) - 1
    worst_30d = float(roll_ret.min()) if len(roll_ret.dropna()) else float("nan")

    deployment = slots_used_total / (len(grid) * max_concurrent)

    return {
        "curve": curve_df,
        "apy": float(apy),
        "worst_30d": worst_30d,
        "deployment": float(deployment),
        "years": float(years),
    }


# ============================================================================
# Main
# ============================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-symbols", type=int, default=60)
    ap.add_argument("--min-history-days", type=int, default=365)
    ap.add_argument("--entry-bps", type=float, default=ENTRY_BPS)
    ap.add_argument("--exit-bps", type=float, default=EXIT_BPS)
    ap.add_argument("--hard-exit-bps", type=float, default=HARD_EXIT_BPS)
    ap.add_argument("--leverage", type=float, default=LEVERAGE)
    args = ap.parse_args()

    print(f"Round-trip cost model: {RT_COST_BPS:.0f} bps of notional")
    print(f"Entry {args.entry_bps} bps/8h (3d mean), exit {args.exit_bps}, "
          f"hard-exit single event < {args.hard_exit_bps}")
    print(f"Leverage {args.leverage}x -> notional/capital = "
          f"{1/(1+1/args.leverage):.3f}\n")

    print("Loading universe ...")
    universe = fetch_all_perp_symbols()
    qualified = filter_by_history(universe, args.min_history_days)
    ticker = fetch_24h_ticker_all()
    vol_map = dict(zip(ticker["symbol"], ticker["quoteVolume"]))
    syms = qualified["symbol"].tolist()
    syms.sort(key=lambda s: -vol_map.get(s, 0))
    syms = syms[:args.max_symbols]
    print(f"  {len(syms)} symbols (top by 24h volume)\n")

    end_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    start_ms = int(pd.Timestamp("2019-09-01", tz="UTC").timestamp() * 1000)

    all_eps: list[Episode] = []
    print("Fetching funding history + simulating (cached after first run) ...")
    for i, sym in enumerate(syms, 1):
        try:
            ev = fetch_funding_cached(sym, start_ms, end_ms)
            eps = simulate_symbol(sym, ev, args.entry_bps, args.exit_bps,
                                  args.hard_exit_bps, TRAIL_EVENTS, MAX_AGE_DAYS)
            all_eps.extend(eps)
            if i % 10 == 0:
                print(f"  {i}/{len(syms)}  ({len(all_eps)} episodes so far)")
        except Exception as e:
            print(f"  WARN {sym}: {e}")

    if not all_eps:
        print("\nNo episodes found. Funding never met the entry threshold — "
              "the carry opportunity may not exist at these parameters.")
        return

    ep_df = pd.DataFrame([{
        "symbol": e.symbol, "entry_time": e.entry_time, "exit_time": e.exit_time,
        "days": e.days, "n_events": e.n_events,
        "gross_bps": e.gross_bps, "net_bps": e.net_bps,
        "exit_reason": e.exit_reason,
    } for e in all_eps]).sort_values("entry_time")
    ep_df.to_csv("funding_carry_episodes.csv", index=False)

    # ---- Episode-level stats ----
    n = len(ep_df)
    win = float((ep_df["net_bps"] > 0).mean())
    med_days = float(ep_df["days"].median())
    print(f"\n=== EPISODES ===")
    print(f"  total: {n}   win rate (net>0): {win:.1%}   median length: {med_days:.1f}d")
    print(f"  net bps/episode: mean {ep_df['net_bps'].mean():+.0f}, "
          f"median {ep_df['net_bps'].median():+.0f}, "
          f"worst {ep_df['net_bps'].min():+.0f}, best {ep_df['net_bps'].max():+.0f}")
    print(f"  exit reasons: {ep_df['exit_reason'].value_counts().to_dict()}")

    per_sym = ep_df.groupby("symbol").agg(
        episodes=("net_bps", "size"), net_bps_total=("net_bps", "sum"),
        win_rate=("net_bps", lambda x: (x > 0).mean()),
    ).sort_values("net_bps_total", ascending=False)
    per_sym.to_csv("funding_carry_summary.csv")
    print(f"\n  top symbols by total net bps:")
    print(per_sym.head(10).to_string())

    # ---- Portfolio sim ----
    port = simulate_portfolio(all_eps, args.leverage, MAX_CONCURRENT, SIMPLE_EARN_APY)
    if port:
        print(f"\n=== PORTFOLIO (K={MAX_CONCURRENT}, {args.leverage}x, "
              f"idle capital at {SIMPLE_EARN_APY:.0%} Simple Earn) ===")
        print(f"  span: {port['years']:.1f} years")
        print(f"  net APY on capital:   {port['apy']:+.2%}")
        print(f"  worst 30-day window:  {port['worst_30d']:+.2%}")
        print(f"  avg slot deployment:  {port['deployment']:.1%}")
        print(f"  benchmark (pure Simple Earn): +{SIMPLE_EARN_APY:.0%}")

        # ---- Decision rule (FUNDING_CARRY_SPEC.md §8) ----
        print(f"\n=== DECISION RULE (spec §8) ===")
        checks = [
            ("Net APY > 7%",            port["apy"] > 0.07),
            ("Episode win rate > 70%",  win > 0.70),
            ("Worst 30d > -2%",         (not math.isnan(port["worst_30d"])) and port["worst_30d"] > -0.02),
            (f"Episodes >= 30 (n={n})", n >= 30),
        ]
        for label, ok in checks:
            print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        verdict = all(ok for _, ok in checks)
        print(f"\n  VERDICT: {'PROCEED TO PAPER TRADING' if verdict else 'DO NOT SHIP — stay in Simple Earn'}")

    print("\nWrote funding_carry_episodes.csv, funding_carry_summary.csv")


if __name__ == "__main__":
    main()
