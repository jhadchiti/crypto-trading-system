"""
Factor Lab — cross-sectional factor research harness.
=====================================================

Tests documented crypto anomalies against OUR universe, OUR liquidity floor,
and OUR cost model — because published alphas rarely survive those three.

Factors tested (weekly rebalance, long top quintile / short bottom quintile):

  MAX        max single-day return over trailing 30d.
             Literature (Financial Innovation 2021): HIGH MAX outperforms in
             crypto (lottery/attention flows) — opposite of equities.
             We test the spread high-minus-low.

  REVERSAL   trailing 7-day return. Literature: short-term reversal —
             losers outperform winners. We test low-minus-high.

  DOW        day-of-week seasonality on an equal-weight liquid index
             (stats report, not a quintile portfolio).

  HOURLY     (optional, --btc-hourly) BTC hour-of-day seasonality incl. the
             documented 21:00-23:00 UTC concentration.

Method
------
- Universe: cached daily OHLCV (cache/ohlcv/), top --max-symbols by current
  24h volume, eligibility per rebalance = 60d+ history AND trailing 30d avg
  notional >= --min-notional.
- Weekly grid W-MON. Signal computed from data up to rebalance close;
  position held over the following week (no lookahead).
- Costs: 9 bps per side (4 taker + 5 slippage) charged on actual turnover
  of each leg each week.
- OOS = union of the walk_forward FOLDS test windows (same as v6/v7/v8).
- Bootstrap (10k) CI on mean weekly spread return; star if CI excludes 0.

Decision rule (pre-committed):
  A factor is promotable to deeper testing only if its OOS net spread is
  starred AND net annualized spread > 10%. Otherwise: documented rejection.

Usage:
    python factor_lab.py
    python factor_lab.py --max-symbols 100 --min-notional 50000000
    python factor_lab.py --btc-hourly
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd

import mtf_structural_backtest as bt
import walk_forward_v6 as wf6
from walk_forward import FOLDS
from market_data import fetch_all_perp_symbols, filter_by_history, fetch_24h_ticker_all


COST_PER_SIDE_BPS = 9.0        # 4 taker + 5 slippage
N_QUANTILES = 5
MAX_LOOKBACK_D = 30            # MAX factor window
REV_LOOKBACK_D = 7             # reversal window
MIN_HISTORY_D = 60
BOOT_ITERS = 10_000


# ============================================================================
# Data panel
# ============================================================================

def load_panel(max_symbols: int, min_history_days: int) -> dict:
    print("Loading universe ...")
    universe = fetch_all_perp_symbols()
    qualified = filter_by_history(universe, min_history_days)
    ticker = fetch_24h_ticker_all()
    vol_map = dict(zip(ticker["symbol"], ticker["quoteVolume"]))
    syms = qualified["symbol"].tolist()
    syms.sort(key=lambda s: -vol_map.get(s, 0))
    syms = syms[:max_symbols]

    end_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    start_ms = int(pd.Timestamp("2019-09-01", tz="UTC").timestamp() * 1000)

    print("Loading OHLCV (cached) ...")
    data = {}
    for i, sym in enumerate(syms, 1):
        try:
            df = wf6.fetch_ohlcv_cached(sym, start_ms, end_ms)
            if df.empty or len(df) < MIN_HISTORY_D + 10:
                continue
            data[sym] = df
            if i % 25 == 0:
                print(f"  {i}/{len(syms)}")
        except Exception as e:
            print(f"  WARN {sym}: {e}")
    print(f"  loaded {len(data)} symbols")
    return data


def build_matrices(data: dict):
    """Daily close, daily return, and 30d rolling notional matrices."""
    closes = pd.DataFrame({s: d["close"] for s, d in data.items()}).sort_index()
    rets = closes.pct_change()
    notional = pd.DataFrame({s: (d["close"] * d["volume"]).rolling(30).mean()
                             for s, d in data.items()}).sort_index()
    return closes, rets, notional


# ============================================================================
# Weekly rebalance engine
# ============================================================================

def weekly_spread_returns(closes: pd.DataFrame, rets: pd.DataFrame,
                          notional: pd.DataFrame, signal: pd.DataFrame,
                          min_notional: float, long_high: bool,
                          n_q: int = N_QUANTILES,
                          beta_neutral: bool = False) -> pd.Series:
    """
    Generic engine: at each W-MON rebalance date t, rank eligible symbols by
    signal.loc[t], hold top/bottom quantile over (t, t+1week], net of
    turnover costs. Returns weekly NET spread return series.

    long_high=True  -> long top quantile, short bottom (MAX per literature)
    long_high=False -> long bottom, short top (reversal per literature)

    beta_neutral=True (Frazzini-Pedersen construction, for BAB): treats the
    signal as a beta estimate and scales each leg by 1/|leg mean beta|, so
    the spread isolates alpha instead of net market exposure. Without this,
    a low-minus-high-beta portfolio carries large negative net beta and its
    return is dominated by the market drift.
    """
    # Weekly panels: LAST daily value in each week. The bar labeled t spans
    # week t; its value is the close on that week's final trading day. The
    # signal panel is resampled identically, so signal.loc[t] uses data
    # through the SAME final day the position is opened on — fresh signal,
    # no lookahead: the position's return is over the FOLLOWING week.
    wk_close = closes.resample("W-MON", label="left", closed="left").last()
    wk_ret = wk_close.pct_change().shift(-1)   # return over the FOLLOWING week
    wk_sig = signal.resample("W-MON", label="left", closed="left").last()
    wk_not = notional.resample("W-MON", label="left", closed="left").last()

    out_dates, out_rets = [], []
    prev_long: set = set()
    prev_short: set = set()

    for t in wk_close.index[:-1]:
        # eligibility and signal as of the week's final day
        try:
            sig_row = wk_sig.loc[t]
            not_row = wk_not.loc[t]
        except KeyError:
            continue
        elig = sig_row.dropna().index.intersection(
            not_row[not_row >= min_notional].dropna().index)
        if len(elig) < n_q * 2:
            prev_long, prev_short = set(), set()
            continue

        s = sig_row[elig].sort_values()
        q = max(1, len(s) // n_q)
        low_set = set(s.index[:q])
        high_set = set(s.index[-q:])
        long_set = high_set if long_high else low_set
        short_set = low_set if long_high else high_set

        r = wk_ret.loc[t]
        long_r = r[list(long_set)].dropna()
        short_r = r[list(short_set)].dropna()
        if long_r.empty or short_r.empty:
            prev_long, prev_short = set(), set()
            continue
        if beta_neutral:
            # scale each leg by 1/|mean beta| (signal IS the beta estimate)
            b_long = float(sig_row[list(long_set)].mean())
            b_short = float(sig_row[list(short_set)].mean())
            b_long = max(abs(b_long), 0.2)
            b_short = max(abs(b_short), 0.2)
            gross = long_r.mean() / b_long - short_r.mean() / b_short
        else:
            gross = long_r.mean() - short_r.mean()

        # turnover cost: fraction of each leg replaced this week
        def turn(cur, prev):
            if not cur:
                return 0.0
            return len(cur - prev) / len(cur)
        turnover = (turn(long_set, prev_long) + turn(short_set, prev_short)) / 2
        # each replaced name costs a round trip on both entry and exit legs:
        # 2 sides x cost_per_side, on the turned-over fraction, both legs
        cost = 2 * (COST_PER_SIDE_BPS / 10_000.0) * turnover * 2

        out_dates.append(t)
        out_rets.append(gross - cost)
        prev_long, prev_short = long_set, short_set

    return pd.Series(out_rets, index=pd.DatetimeIndex(out_dates))


# ============================================================================
# Factor signals
# ============================================================================

def signal_max(rets: pd.DataFrame) -> pd.DataFrame:
    """Max single-day return over trailing 30 days."""
    return rets.rolling(MAX_LOOKBACK_D).max()


def signal_reversal(closes: pd.DataFrame) -> pd.DataFrame:
    """Trailing 7-day return."""
    return closes.pct_change(REV_LOOKBACK_D)


def signal_anchor(closes: pd.DataFrame) -> pd.DataFrame:
    """
    52-week-high anchoring: close / trailing 365d max close.
    Literature (J. Banking & Finance 2025): coins NEAR their 52w high
    outperform — anchoring makes investors underreact near the high.
    Long high-nearness -> long_high=True.
    """
    return closes / closes.rolling(365, min_periods=180).max()


def signal_bab(rets: pd.DataFrame, btc_col: str = "BTCUSDT",
               window: int = 90) -> pd.DataFrame:
    """
    Rolling beta vs BTC over trailing 90d. BAB literature: long LOW beta,
    short HIGH beta -> long_high=False.
    """
    if btc_col not in rets.columns:
        return pd.DataFrame(index=rets.index)
    btc = rets[btc_col]
    btc_var = btc.rolling(window).var()
    betas = {}
    for c in rets.columns:
        if c == btc_col:
            continue
        betas[c] = rets[c].rolling(window).cov(btc) / btc_var
    return pd.DataFrame(betas)


# ============================================================================
# Evaluation
# ============================================================================

def in_oos(dates: pd.DatetimeIndex) -> pd.Series:
    """Bool mask: date falls inside any FOLDS test window."""
    mask = pd.Series(False, index=dates)
    for (_, _, _, te_start, te_end) in FOLDS:
        s = pd.Timestamp(te_start, tz="UTC")
        e = pd.Timestamp(te_end, tz="UTC")
        mask |= (dates >= s) & (dates <= e)
    return mask


def bootstrap_ci(x: np.ndarray, iters: int = BOOT_ITERS):
    if len(x) < 8:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(42)
    means = np.array([rng.choice(x, len(x), replace=True).mean()
                      for _ in range(iters)])
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def report_factor(name: str, weekly: pd.Series) -> dict:
    if weekly.empty:
        print(f"\n{name}: no weekly returns generated")
        return {}
    oos_mask = in_oos(weekly.index)
    full = weekly.values
    oos = weekly[oos_mask].values

    def stats(x):
        if len(x) == 0:
            return dict(n=0, ann=float("nan"), sh=float("nan"),
                        lb=float("nan"), ub=float("nan"), star=False)
        ann = float(np.mean(x)) * 52
        sh = float(np.mean(x) / np.std(x) * math.sqrt(52)) if np.std(x) > 0 else float("nan")
        lb, ub = bootstrap_ci(np.asarray(x))
        return dict(n=len(x), ann=ann, sh=sh, lb=lb, ub=ub,
                    star=(not math.isnan(lb)) and lb > 0)

    f, o = stats(full), stats(oos)
    print(f"\n=== {name} ===")
    print(f"  FULL  n={f['n']:<4} ann={f['ann']:+7.1%}  sharpe={f['sh']:+5.2f}  "
          f"weeklyCI=[{f['lb']:+.4f},{f['ub']:+.4f}] {'*' if f['star'] else ''}")
    print(f"  OOS   n={o['n']:<4} ann={o['ann']:+7.1%}  sharpe={o['sh']:+5.2f}  "
          f"weeklyCI=[{o['lb']:+.4f},{o['ub']:+.4f}] {'*' if o['star'] else ''}")
    weekly.to_csv(f"factor_lab_{name.lower()}_weekly.csv")
    return {"name": name, **{f"full_{k}": v for k, v in f.items()},
            **{f"oos_{k}": v for k, v in o.items()}}


# ============================================================================
# Day-of-week + hourly seasonality
# ============================================================================

def dow_report(rets: pd.DataFrame, notional: pd.DataFrame, min_notional: float):
    """Equal-weight liquid index, mean return by weekday."""
    liquid = rets.where(notional >= min_notional)
    idx_ret = liquid.mean(axis=1).dropna()
    print("\n=== DOW (equal-weight liquid index, daily mean return) ===")
    oos_mask = in_oos(idx_ret.index)
    rows = []
    for dow in range(7):
        d_full = idx_ret[idx_ret.index.dayofweek == dow]
        d_oos = idx_ret[(idx_ret.index.dayofweek == dow) & oos_mask]
        name = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dow]
        t_full = (d_full.mean() / (d_full.std() / math.sqrt(len(d_full)))
                  if len(d_full) > 10 and d_full.std() > 0 else float("nan"))
        print(f"  {name}: full mean={d_full.mean():+.4%} (t={t_full:+.2f}, n={len(d_full)})"
              f"   oos mean={d_oos.mean():+.4%} (n={len(d_oos)})")
        rows.append({"dow": name, "full_mean": d_full.mean(),
                     "full_t": t_full, "oos_mean": d_oos.mean()})
    pd.DataFrame(rows).to_csv("factor_lab_dow.csv", index=False)
    print("  NOTE: |t| < 2 on OOS = noise. Do not trade this without a starred t.")


def btc_hourly_report():
    """Fetch BTC 1h klines and report mean return by UTC hour."""
    print("\nFetching BTC 1h klines (full history — one-time, ~60 calls) ...")
    end_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    start_ms = int(pd.Timestamp("2019-09-01", tz="UTC").timestamp() * 1000)
    cache = Path("cache/ohlcv_1h_BTCUSDT.csv")
    if cache.exists():
        df = pd.read_csv(cache, parse_dates=["date"], index_col="date")
        df.index = pd.to_datetime(df.index, utc=True)
    else:
        df = bt.fetch_binance_klines("BTCUSDT", "1h", start_ms, end_ms)
        if not df.empty:
            df.to_csv(cache)
    if df.empty:
        print("  fetch failed"); return
    r = df["close"].pct_change().dropna()
    oos_mask = in_oos(r.index)
    print("\n=== BTC HOURLY (UTC) mean return by hour ===")
    rows = []
    for h in range(24):
        h_full = r[r.index.hour == h]
        h_oos = r[(r.index.hour == h) & oos_mask]
        t = (h_full.mean() / (h_full.std() / math.sqrt(len(h_full)))
             if len(h_full) > 50 and h_full.std() > 0 else float("nan"))
        flag = " <== documented window" if h in (21, 22) else ""
        print(f"  {h:02d}:00  full={h_full.mean():+.4%} (t={t:+.2f})  "
              f"oos={h_oos.mean():+.4%}{flag}")
        rows.append({"hour": h, "full_mean": h_full.mean(), "full_t": t,
                     "oos_mean": h_oos.mean()})
    pd.DataFrame(rows).to_csv("factor_lab_btc_hourly.csv", index=False)


# ============================================================================
# Main
# ============================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-symbols", type=int, default=100)
    ap.add_argument("--min-history-days", type=int, default=365)
    ap.add_argument("--min-notional", type=float, default=50_000_000)
    ap.add_argument("--btc-hourly", action="store_true")
    args = ap.parse_args()

    data = load_panel(args.max_symbols, args.min_history_days)
    if len(data) < 20:
        print("Too few symbols loaded — aborting."); return
    closes, rets, notional = build_matrices(data)

    results = []

    # MAX: literature says HIGH max outperforms in crypto -> long high
    weekly_max = weekly_spread_returns(
        closes, rets, notional, signal_max(rets),
        args.min_notional, long_high=True)
    results.append(report_factor("MAX", weekly_max))

    # REVERSAL: literature says losers outperform -> long low (past return)
    weekly_rev = weekly_spread_returns(
        closes, rets, notional, signal_reversal(closes),
        args.min_notional, long_high=False)
    results.append(report_factor("REVERSAL", weekly_rev))

    # ANCHOR: nearness to 52w high predicts returns -> long high
    weekly_anchor = weekly_spread_returns(
        closes, rets, notional, signal_anchor(closes),
        args.min_notional, long_high=True)
    results.append(report_factor("ANCHOR", weekly_anchor))

    # BAB: long low-beta, short high-beta, BETA-NEUTRAL legs (Frazzini-Pedersen)
    weekly_bab = weekly_spread_returns(
        closes, rets, notional, signal_bab(rets),
        args.min_notional, long_high=False, beta_neutral=True)
    results.append(report_factor("BAB", weekly_bab))

    # Factor correlation matrix (diversification check)
    named = {"MAX": weekly_max, "REV": weekly_rev,
             "ANCHOR": weekly_anchor, "BAB": weekly_bab}
    named = {k: v for k, v in named.items() if not v.empty}
    if len(named) >= 2:
        joined = pd.concat([v.rename(k) for k, v in named.items()], axis=1).dropna()
        if len(joined) > 10:
            print("\nFactor weekly-return correlation matrix:")
            print(joined.corr().round(2).to_string())

    # Day of week
    dow_report(rets, notional, args.min_notional)

    # Hourly (optional)
    if args.btc_hourly:
        btc_hourly_report()

    # Decision rule
    print("\n=== DECISION RULE (pre-committed) ===")
    print("  Promote a factor to deeper testing ONLY if OOS is starred AND")
    print("  OOS net annualized spread > 10%. Otherwise documented rejection.")
    for r in results:
        if not r:
            continue
        ok = r.get("oos_star") and r.get("oos_ann", 0) > 0.10
        print(f"  [{'PROMOTE' if ok else 'REJECT'}] {r['name']}: "
              f"OOS ann {r.get('oos_ann', float('nan')):+.1%}, "
              f"starred={bool(r.get('oos_star'))}")

    pd.DataFrame([r for r in results if r]).to_csv("factor_lab_results.csv", index=False)
    print("\nWrote factor_lab_results.csv + per-factor weekly CSVs.")


if __name__ == "__main__":
    main()
