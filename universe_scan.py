"""
Universe Scanner — run the Donchian+BTC-macro strategy across the entire
Binance USD-M perp universe.
=========================================================================

Answers: "is our 8-symbol universe lucky, representative, or anomalously bad?"

Methodology:
  1. Enumerate all active USDT perps with >= 2y history (~150 symbols).
  2. Fetch Daily OHLCV for each (cached to ./cache/ohlcv/).
  3. Compute BTC macro regime once.
  4. Run the SAME strategy on each symbol using the same fixed config.
  5. Aggregate per-symbol metrics: n_trades, win_rate, expectancy_R, trade_Sharpe,
     bootstrap CI lower bound (the "is it real" test).
  6. Generate distribution HTML report showing:
       - Distribution shape (Sharpe / expectancy histograms)
       - % of symbols with starred bootstrap CI
       - Top 20 and bottom 20 symbols
       - Where our current 8 sit in the distribution
       - Cross-section: volume rank vs Sharpe (does liquidity predict edge?)

DO NOT use this to pick "the top 10 symbols" — that's overfitting. Use it to
verify the edge generalizes across the universe.

Usage:
    python universe_scan.py                  # full run, ~20 min first time
    python universe_scan.py --max-symbols 30 # quick smoke run
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import replace
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

import mtf_structural_backtest as bt
import donchian_baseline as dc
import donchian_v2 as d2
import walk_forward_v3 as wf3
from funding import fetch_funding, align_funding_to_bars
from walk_forward_v2 import adx
from market_data import (
    fetch_all_perp_symbols, filter_by_history, fetch_24h_ticker_all,
    _ensure_cache,
)


CURRENT_8_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
                     "AVAXUSDT", "LINKUSDT", "DOGEUSDT", "XRPUSDT")

FROZEN_N_ENTRY = 55
FROZEN_N_EXIT = 20
RISK_PER_TRADE = 0.0075


# ============================================================================
# OHLCV cache
# ============================================================================

def fetch_ohlcv_cached(symbol: str, start_ms: int, end_ms: int,
                       min_bars: int = 1500) -> pd.DataFrame:
    """Cache-aware fetch. Re-fetches if cache is stale OR too short."""
    cache_dir = _ensure_cache("ohlcv")
    cache_file = cache_dir / f"{symbol}.csv"
    if cache_file.exists():
        df = pd.read_csv(cache_file, parse_dates=["date"], index_col="date")
        df.index = pd.to_datetime(df.index, utc=True)
        # require recent AND sufficiently deep history
        recent_ok = (not df.empty and
                     df.index[-1] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=2))
        depth_ok = len(df) >= min_bars
        if recent_ok and depth_ok:
            return df
        # otherwise fall through and re-fetch

    df = bt.fetch_binance_klines(symbol, "1d", start_ms, end_ms)
    if not df.empty:
        df.to_csv(cache_file)
        time.sleep(0.15)
    return df


# ============================================================================
# Strategy backtest (v3 btc_only logic, simplified for cross-section scan)
# ============================================================================

def backtest_one_symbol(symbol: str, df: pd.DataFrame,
                        funding_carry: Optional[pd.Series],
                        funding_8h_last: Optional[pd.Series],
                        btc_regime: pd.Series) -> list[bt.Trade]:
    """Same logic as walk_forward_v3 btc_only variant, single-symbol."""
    dcfg = replace(dc.DCFG, n_entry=FROZEN_N_ENTRY, n_exit=FROZEN_N_EXIT)
    d = dc.build_donchian(df, dcfg)
    trades: list[bt.Trade] = []
    pos: Optional[bt.Position] = None
    equity = 12_500.0
    rt_cost_bps = 2 * (dcfg.taker_fee_bps + dcfg.slippage_bps)
    sizing_cfg = bt.Config(risk_per_trade=dcfg.risk_per_trade,
                            vol_target_annual=dcfg.vol_target_annual)

    for i, (date, row) in enumerate(d.iterrows()):
        if pos is not None:
            pos.bars_held += 1
            bar_bps = 0.0
            if funding_carry is not None:
                try: bar_bps = float(funding_carry.loc[date])
                except KeyError: pass
            funding_drag = (bar_bps / 10000.0) * abs(pos.size) * row["close"]
            equity -= funding_drag if pos.side > 0 else -funding_drag

            exit_reason = None; exit_price = None
            if pos.side > 0 and row["low"] <= pos.stop:
                exit_price = pos.stop; exit_reason = "atr_stop"
            elif pos.side < 0 and row["high"] >= pos.stop:
                exit_price = pos.stop; exit_reason = "atr_stop"
            if exit_reason is None:
                if pos.side > 0 and not math.isnan(row["exit_low"]) and row["close"] < row["exit_low"]:
                    exit_price = row["close"]; exit_reason = "channel_exit"
                elif pos.side < 0 and not math.isnan(row["exit_high"]) and row["close"] > row["exit_high"]:
                    exit_price = row["close"]; exit_reason = "channel_exit"
            if exit_reason is None and pos.bars_held >= dcfg.time_stop_bars:
                exit_price = row["close"]; exit_reason = "time_stop"
            if exit_reason is not None:
                cost = (rt_cost_bps / 10000.0) * abs(pos.size) * exit_price
                gross = (exit_price - pos.entry_price) * pos.size
                net = gross - cost
                equity += net
                r = net / pos.risk_dollars if pos.risk_dollars > 0 else 0.0
                trades.append(bt.Trade(
                    symbol=symbol, side=pos.side,
                    entry_date=pos.entry_date, exit_date=date,
                    entry_price=pos.entry_price, exit_price=exit_price,
                    size=pos.size, pnl_gross=gross, pnl_net=net,
                    r_multiple=r, exit_reason=exit_reason, bars_held=pos.bars_held,
                ))
                pos = None

        if pos is None and not math.isnan(row["atr"]):
            fund_8h = 0.0
            if funding_8h_last is not None:
                try: fund_8h = float(funding_8h_last.loc[date])
                except KeyError: pass
            allow_long = fund_8h <= d2.FUNDING_FILTER_MAX_BPS_8H
            allow_short = fund_8h >= -d2.FUNDING_FILTER_MAX_BPS_8H

            try:
                macro_ok = bool(btc_regime.loc[date])
            except KeyError:
                macro_ok = False
            if not macro_ok:
                allow_long = False; allow_short = False

            long_break = (not math.isnan(row["entry_high"]) and row["close"] > row["entry_high"])
            short_break = (not math.isnan(row["entry_low"]) and row["close"] < row["entry_low"])

            if allow_long and long_break:
                entry = row["close"]; stop = entry - dcfg.atr_stop_mult * row["atr"]
                size = bt._size_position(equity, entry, stop, row["atr"], sizing_cfg)
                if size > 0:
                    pos = bt.Position(symbol=symbol, side=+1,
                                      entry_date=date, entry_price=entry,
                                      size=size, stop=stop, initial_stop=stop,
                                      risk_dollars=size * (entry - stop),
                                      high_since_entry=entry, low_since_entry=entry)
            elif allow_short and short_break:
                entry = row["close"]; stop = entry + dcfg.atr_stop_mult * row["atr"]
                size = bt._size_position(equity, entry, stop, row["atr"], sizing_cfg)
                if size > 0:
                    pos = bt.Position(symbol=symbol, side=-1,
                                      entry_date=date, entry_price=entry,
                                      size=-size, stop=stop, initial_stop=stop,
                                      risk_dollars=size * (stop - entry),
                                      high_since_entry=entry, low_since_entry=entry)
    return trades


# ============================================================================
# Metrics + bootstrap
# ============================================================================

def metrics_for_symbol(symbol: str, trades: list[bt.Trade]) -> dict:
    if not trades:
        return {"symbol": symbol, "n_trades": 0, "win_rate": float("nan"),
                "expectancy_r": float("nan"), "trade_sharpe": float("nan"),
                "ci_lower": float("nan"), "ci_upper": float("nan"),
                "is_starred": False, "ann_R": 0.0}
    rs = np.array([t.r_multiple for t in trades])
    # bootstrap
    rng = np.random.default_rng(42)
    n_iter = 2000   # smaller per-symbol to keep scan fast
    draws = np.empty(n_iter)
    for i in range(n_iter):
        idx = rng.integers(0, len(rs), size=len(rs))
        draws[i] = rs[idx].mean()
    ci_lo, ci_hi = float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))
    years = max((trades[-1].exit_date - trades[0].entry_date).days / 365.25, 1e-9)
    return {
        "symbol": symbol,
        "n_trades": int(len(rs)),
        "win_rate": float((rs > 0).mean()),
        "expectancy_r": float(rs.mean()),
        "trade_sharpe": float(rs.mean() / rs.std() * math.sqrt(30)) if rs.std() > 0 else 0.0,
        "ci_lower": ci_lo, "ci_upper": ci_hi,
        "is_starred": ci_lo > 0,
        "ann_R": float(rs.sum() / years),
        "total_R": float(rs.sum()),
    }


# ============================================================================
# Driver
# ============================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-symbols", type=int, default=200,
                    help="limit number of symbols for quick runs")
    ap.add_argument("--min-history-days", type=int, default=730,
                    help="minimum listing age to qualify")
    ap.add_argument("--out", default="universe_scan_report.html")
    args = ap.parse_args()

    print("Fetching universe ...")
    universe = fetch_all_perp_symbols()
    qualified = filter_by_history(universe, min_history_days=args.min_history_days)
    print(f"  total active USDT perps: {len(universe)}")
    print(f"  qualified by history (>= {args.min_history_days}d): {len(qualified)}")

    # Get current liquidity
    print("\nFetching 24h ticker (for liquidity ranking) ...")
    ticker = fetch_24h_ticker_all()
    ticker_map = dict(zip(ticker["symbol"], ticker["quoteVolume"]))

    symbols = qualified["symbol"].tolist()
    # rank by current volume so we scan most-liquid first
    symbols.sort(key=lambda s: -ticker_map.get(s, 0))
    symbols = symbols[:args.max_symbols]
    print(f"  scanning {len(symbols)} symbols (top by current 24h volume)")

    # Fetch FULL history (back to 2019-09) for fair comparison with walk_forward_v3.
    # min_history_days remains a *filter* on which symbols are eligible (>=2y),
    # but we still fetch all available data for those that qualify.
    end_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    start_ms = int(pd.Timestamp("2019-09-01", tz="UTC").timestamp() * 1000)

    print("\nFetching/loading OHLCV per symbol (cached) ...")
    data = {}
    funding_by_symbol = {}
    skipped = []
    for i, sym in enumerate(symbols, 1):
        try:
            df = fetch_ohlcv_cached(sym, start_ms, end_ms)
            if df.empty or len(df) < args.min_history_days * 0.9:
                skipped.append(sym); continue
            data[sym] = df
            # funding (cached lightly)
            ev = fetch_funding(sym, start_ms, end_ms)
            if not ev.empty:
                funding_by_symbol[sym] = align_funding_to_bars(ev, df.index, 1440)
            if i % 20 == 0:
                print(f"  {i}/{len(symbols)} symbols loaded")
        except Exception as e:
            skipped.append(sym)
            print(f"  WARN {sym}: {e}")
    print(f"  ready: {len(data)}, skipped: {len(skipped)}")

    if "BTCUSDT" not in data:
        print("BTCUSDT missing — cannot compute macro regime.")
        return
    btc_regime = wf3.compute_btc_regime(data["BTCUSDT"])

    print("\nRunning strategy on each symbol ...")
    rows = []
    for i, (sym, df) in enumerate(data.items(), 1):
        fb = funding_by_symbol.get(sym)
        fund_carry = fb["funding_bps_in_bar"] if fb is not None else None
        fund_last = fb["funding_bps_8h_last"] if fb is not None else None
        trades = backtest_one_symbol(sym, df, fund_carry, fund_last, btc_regime)
        m = metrics_for_symbol(sym, trades)
        m["volume_24h"] = ticker_map.get(sym, 0)
        rows.append(m)
        if i % 25 == 0:
            print(f"  scored {i}/{len(data)}")

    df_metrics = pd.DataFrame(rows).sort_values("trade_sharpe", ascending=False)
    df_metrics.to_csv("universe_scan_metrics.csv", index=False)

    # Distribution stats
    valid = df_metrics[df_metrics["n_trades"] >= 10].copy()
    n_total = len(valid)
    n_starred = int(valid["is_starred"].sum())
    pct_starred = (n_starred / n_total * 100.0) if n_total > 0 else 0.0
    pct_positive_exp = (float((valid["expectancy_r"] > 0).mean()) * 100.0) if n_total > 0 else 0.0
    median_sharpe = float(valid["trade_sharpe"].median()) if n_total > 0 else float("nan")
    median_exp = float(valid["expectancy_r"].median()) if n_total > 0 else float("nan")

    print(f"\n=== UNIVERSE SCAN RESULTS ===")
    print(f"  symbols scored (n>=10): {n_total}")
    print(f"  % positive expectancy:  {pct_positive_exp:.1f}%")
    print(f"  % starred CI (edge significant): {pct_starred:.1f}%")
    print(f"  median trade Sharpe:    {median_sharpe:+.3f}")
    print(f"  median expectancy_R:    {median_exp:+.3f}")

    # Where our 8 sit
    print("\n  Current 8 symbols' ranks (out of valid):")
    valid_sorted = valid.sort_values("trade_sharpe", ascending=False).reset_index(drop=True)
    for s in CURRENT_8_SYMBOLS:
        rank_row = valid_sorted[valid_sorted["symbol"] == s]
        if len(rank_row):
            rank = int(rank_row.index[0]) + 1
            r = rank_row.iloc[0]
            print(f"    {s:<10}  rank {rank:>3}/{n_total}  "
                  f"Sharpe {r['trade_sharpe']:+.2f}  "
                  f"exp {r['expectancy_r']:+.2f}R  "
                  f"{'*' if r['is_starred'] else ' '}")
        else:
            print(f"    {s:<10}  NOT IN SCAN")

    print("\n  Top 15 by Sharpe (and not in current 8):")
    candidates = valid_sorted[~valid_sorted["symbol"].isin(CURRENT_8_SYMBOLS)].head(15)
    for _, r in candidates.iterrows():
        print(f"    {r['symbol']:<14}  n={int(r['n_trades']):>3}  "
              f"Sh={r['trade_sharpe']:+.2f}  exp={r['expectancy_r']:+.2f}R  "
              f"ann_R={r['ann_R']:+.1f}  vol_24h=${r['volume_24h']/1e6:.1f}M  "
              f"{'*' if r['is_starred'] else ' '}")

    # Write HTML
    write_report(args.out, df_metrics, valid, pct_starred, pct_positive_exp,
                 median_sharpe, median_exp, CURRENT_8_SYMBOLS)
    print(f"\nWrote {args.out} and universe_scan_metrics.csv")


def write_report(out_path, df, valid, pct_starred, pct_pos_exp,
                 med_sh, med_exp, current_8):
    sharpes = valid["trade_sharpe"].dropna().tolist()
    exps = valid["expectancy_r"].dropna().tolist()

    def make_hist(vals, n_bins=24):
        if not vals:
            return {"bins": [], "counts": []}
        arr = np.asarray(vals)
        counts, edges = np.histogram(arr, bins=n_bins)
        centers = [(edges[i] + edges[i+1]) / 2 for i in range(len(edges) - 1)]
        return {"bins": [f"{c:.2f}" for c in centers], "counts": [int(c) for c in counts]}

    h_sh = make_hist(sharpes)
    h_exp = make_hist(exps)

    df_sorted = valid.sort_values("trade_sharpe", ascending=False)
    rows_html = []
    for _, r in df_sorted.iterrows():
        is_ours = r["symbol"] in current_8
        cls = "row-ours" if is_ours else ""
        star = "*" if r["is_starred"] else ""
        rows_html.append(
            f"<tr class='{cls}'><td>{r['symbol']}</td>"
            f"<td>{int(r['n_trades'])}</td>"
            f"<td>{r['win_rate']*100:.1f}%</td>"
            f"<td>{r['expectancy_r']:+.3f}R</td>"
            f"<td>{r['trade_sharpe']:+.2f}</td>"
            f"<td>{r['ci_lower']:+.2f}, {r['ci_upper']:+.2f} {star}</td>"
            f"<td>${r['volume_24h']/1e6:,.1f}M</td></tr>"
        )
    table_html = "".join(rows_html)

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Universe Scan</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 20px;
       background: #0f1419; color: #e7e9ea; }}
h1 {{ font-size: 18px; }}
.kpi-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 16px 0; }}
.kpi {{ background: #15202b; border: 1px solid #253341; border-radius: 8px; padding: 14px; }}
.kpi-label {{ font-size: 10px; color: #8899a6; text-transform: uppercase; }}
.kpi-value {{ font-size: 22px; font-weight: 600; margin-top: 4px; }}
.card {{ background: #15202b; border: 1px solid #253341; border-radius: 8px; padding: 14px; margin-bottom: 16px; }}
.card h2 {{ font-size: 13px; color: #8899a6; margin: 0 0 12px 0;
            text-transform: uppercase; letter-spacing: 0.05em; }}
.row-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
th {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #253341; color: #8899a6; }}
td {{ padding: 5px 8px; border-bottom: 1px solid #1a2330; }}
tr.row-ours td {{ background: rgba(29,155,240,0.1); }}
tr:hover td {{ background: #1a2330; }}
.interp {{ font-size: 13px; color: #8899a6; margin-top: 12px; line-height: 1.5; }}
</style></head><body>
<h1>Universe Scan - Donchian (55/20) + BTC-macro across {len(valid)} symbols</h1>
<div class="kpi-grid">
  <div class="kpi"><div class="kpi-label">Symbols scored</div><div class="kpi-value">{len(valid)}</div></div>
  <div class="kpi"><div class="kpi-label">% positive expectancy</div><div class="kpi-value">{pct_pos_exp:.0f}%</div></div>
  <div class="kpi"><div class="kpi-label">% starred edge (CI&gt;0)</div><div class="kpi-value">{pct_starred:.0f}%</div></div>
  <div class="kpi"><div class="kpi-label">Median Sharpe</div><div class="kpi-value">{med_sh:+.2f}</div></div>
</div>

<div class="row-2">
  <div class="card"><h2>Per-symbol trade Sharpe distribution</h2>
    <canvas id="hSh" height="170"></canvas></div>
  <div class="card"><h2>Per-symbol expectancy (R) distribution</h2>
    <canvas id="hExp" height="170"></canvas></div>
</div>

<div class="card">
  <h2>All scored symbols (current 8 highlighted)</h2>
  <table>
    <thead><tr><th>symbol</th><th>n trades</th><th>win rate</th><th>expectancy</th>
      <th>trade Sharpe</th><th>95% CI on expectancy</th><th>24h volume</th></tr></thead>
    <tbody>{table_html}</tbody>
  </table>
  <div class="interp">
    Highlighted rows = your current 8-symbol universe.
    * = bootstrap 95% CI on expectancy excludes zero (statistically significant edge).
    <br>If &gt; 40% of symbols are starred &rarr; edge is universal across crypto.
    <br>If 20-40% &rarr; partial edge, worth investigating what differentiates winners.
    <br>If &lt; 20% &rarr; edge is fragile / lucky, reconsider methodology.
  </div>
</div>

<script>
const hSh = {json.dumps(h_sh)};
const hExp = {json.dumps(h_exp)};
function opts(yfmt) {{
  return {{ responsive: true,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{ x: {{ ticks: {{ color: '#8899a6' }}, grid: {{ color: '#1a2330' }} }},
               y: {{ ticks: {{ color: '#8899a6', callback: yfmt }}, grid: {{ color: '#1a2330' }} }} }},
  }};
}}
new Chart(document.getElementById('hSh'), {{
  type: 'bar', data: {{ labels: hSh.bins, datasets: [{{ data: hSh.counts,
    backgroundColor: hSh.bins.map(b => parseFloat(b) >= 0 ? '#00ba7c' : '#f4212e') }}] }},
  options: opts(v => v.toString()),
}});
new Chart(document.getElementById('hExp'), {{
  type: 'bar', data: {{ labels: hExp.bins, datasets: [{{ data: hExp.counts,
    backgroundColor: hExp.bins.map(b => parseFloat(b) >= 0 ? '#00ba7c' : '#f4212e') }}] }},
  options: opts(v => v.toString()),
}});
</script>
</body></html>"""

    Path(out_path).write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
