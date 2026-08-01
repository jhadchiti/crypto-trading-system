"""
Comprehensive Strategy Tearsheet Generator.
============================================

Reads any trades.csv (from backtest, walk-forward, or live trading) and
produces strategy_report.html — a single self-contained HTML page with the
full performance evaluation a professional desk would use.

Metric categories produced:
  1. Executive KPIs:        CAGR, Max DD, Sharpe, Sortino, Calmar, win rate, expectancy
  2. Returns:               total, monthly distribution, % positive months, excess vs BTC
  3. Risk:                  vol, VaR/CVaR, downside dev, worst rolling 12-month, drawdown details
  4. Risk-adjusted:         Sharpe (annualized), Sortino, Calmar, MAR, tail ratio
  5. Trade quality:         hit rate, avg win/loss, expectancy, profit factor,
                            largest win/loss, max consecutive streaks, top-10 concentration
  6. Statistical:           bootstrap CI on expectancy, P(edge>0), IS vs OOS degradation
  7. Regime conditioning:   performance by BTC regime, vol regime, funding regime
  8. Benchmark comparison:  strategy vs BTC buy-and-hold over same period

Plots (rendered via Chart.js, no external deps beyond CDN):
  - Equity curve vs BTC buy-hold
  - Drawdown underwater plot
  - R-multiple distribution histogram
  - Monthly returns histogram
  - Monte Carlo equity range (1000 resampled paths, 5/25/50/75/95 percentile bands)

Usage:
    python strategy_report.py                            # default: trades.csv
    python strategy_report.py --file walkforward_v4_baseline_trades.csv
    python strategy_report.py --equity 100000 --risk 0.0075
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

import mtf_structural_backtest as bt


# ============================================================================
# DEFAULTS
# ============================================================================

DEFAULT_STARTING_EQUITY = 1_000.0  # matches dashboard.py; override with --equity
DEFAULT_RISK_PER_TRADE = 0.0075
DEFAULT_BENCHMARK = "BTCUSDT"
IS_OOS_CUT = "2024-01-01"
TRADING_DAYS_PER_YEAR = 365  # crypto trades 365


# ============================================================================
# DATA LOADING
# ============================================================================

def load_trades(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    for col in ("entry_date", "exit_date"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
    # only closed trades
    if "exit_date" in df.columns:
        df = df[df["exit_date"].notna()].copy()
    df = df.sort_values("exit_date").reset_index(drop=True)
    return df


def build_equity_curve(trades: pd.DataFrame, starting_equity: float) -> pd.Series:
    """Equity curve indexed by exit_date, forward-filled to daily grid."""
    if trades.empty:
        return pd.Series(dtype=float)
    eq = starting_equity + trades["pnl_net"].cumsum()
    eq.index = trades["exit_date"]
    # collapse multiple trades same day to last value
    eq = eq.groupby(eq.index).last()
    # forward-fill onto daily grid for benchmark comparison
    daily_idx = pd.date_range(eq.index.min().normalize(),
                              eq.index.max().normalize(), freq="D", tz="UTC")
    return eq.reindex(daily_idx, method="ffill").bfill()


def fetch_benchmark(symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    return bt.fetch_binance_klines(symbol, "1d", start_ms, end_ms)


def benchmark_equity(bench_df: pd.DataFrame, starting_equity: float,
                     equity_index: pd.DatetimeIndex) -> pd.Series:
    if bench_df.empty:
        return pd.Series(index=equity_index, dtype=float)
    bench = bench_df["close"]
    bench_norm = bench / bench.iloc[0]
    eq = bench_norm * starting_equity
    eq = eq.reindex(equity_index, method="ffill").bfill()
    return eq


# ============================================================================
# METRICS — RETURNS
# ============================================================================

def monthly_returns(equity: pd.Series) -> pd.Series:
    return equity.resample("ME").last().pct_change().dropna()


def cagr(equity: pd.Series) -> float:
    if equity.empty or equity.iloc[0] <= 0:
        return float("nan")
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    if years <= 0:
        return float("nan")
    final, start = float(equity.iloc[-1]), float(equity.iloc[0])
    if final <= 0:
        return float("nan")
    return (final / start) ** (1.0 / years) - 1.0


def total_return(equity: pd.Series) -> float:
    if equity.empty:
        return float("nan")
    return float(equity.iloc[-1] / equity.iloc[0] - 1.0)


# ============================================================================
# METRICS — RISK
# ============================================================================

def annualized_volatility(equity: pd.Series) -> float:
    daily = equity.pct_change().dropna()
    if len(daily) < 2:
        return float("nan")
    return float(daily.std() * math.sqrt(TRADING_DAYS_PER_YEAR))


def drawdown_series(equity: pd.Series) -> pd.Series:
    peak = equity.cummax()
    return equity / peak - 1.0


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return float("nan")
    return float(drawdown_series(equity).min())


def avg_drawdown(equity: pd.Series) -> float:
    dd = drawdown_series(equity)
    underwater = dd[dd < 0]
    return float(underwater.mean()) if len(underwater) else 0.0


def max_drawdown_duration_days(equity: pd.Series) -> int:
    """Longest run of bars where equity is below the prior peak."""
    if equity.empty:
        return 0
    peak = equity.cummax()
    under = (equity < peak).astype(int)
    # run-length encode
    longest = cur = 0
    for v in under:
        if v:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 0
    return int(longest)


def pct_time_underwater(equity: pd.Series) -> float:
    if equity.empty:
        return float("nan")
    return float((drawdown_series(equity) < 0).mean()) * 100.0


def var_cvar(equity: pd.Series, q: float = 0.05) -> tuple[float, float]:
    daily = equity.pct_change().dropna()
    if len(daily) < 20:
        return float("nan"), float("nan")
    var = float(np.percentile(daily, q * 100))
    tail = daily[daily <= var]
    cvar = float(tail.mean()) if len(tail) else float("nan")
    return var, cvar


def downside_deviation(equity: pd.Series) -> float:
    daily = equity.pct_change().dropna()
    neg = daily[daily < 0]
    if len(neg) < 2:
        return float("nan")
    return float(neg.std() * math.sqrt(TRADING_DAYS_PER_YEAR))


def worst_rolling_12m(equity: pd.Series) -> float:
    if equity.empty:
        return float("nan")
    monthly = equity.resample("ME").last()
    if len(monthly) < 13:
        return float("nan")
    rolling = monthly.pct_change(12).dropna()
    return float(rolling.min()) if len(rolling) else float("nan")


# ============================================================================
# METRICS — RISK-ADJUSTED
# ============================================================================

def sharpe_ratio(equity: pd.Series, rf: float = 0.0) -> float:
    daily = equity.pct_change().dropna()
    if len(daily) < 2 or daily.std() == 0:
        return float("nan")
    excess = daily - rf / TRADING_DAYS_PER_YEAR
    return float(excess.mean() / daily.std() * math.sqrt(TRADING_DAYS_PER_YEAR))


def sortino_ratio(equity: pd.Series, rf: float = 0.0) -> float:
    daily = equity.pct_change().dropna()
    if len(daily) < 2:
        return float("nan")
    excess = daily - rf / TRADING_DAYS_PER_YEAR
    neg = daily[daily < 0]
    if len(neg) < 2 or neg.std() == 0:
        return float("nan")
    return float(excess.mean() / neg.std() * math.sqrt(TRADING_DAYS_PER_YEAR))


def calmar_ratio(equity: pd.Series) -> float:
    c = cagr(equity); dd = max_drawdown(equity)
    if math.isnan(c) or math.isnan(dd) or dd >= 0:
        return float("nan")
    return c / abs(dd)


def tail_ratio(equity: pd.Series) -> float:
    daily = equity.pct_change().dropna()
    if len(daily) < 20:
        return float("nan")
    p95 = np.percentile(daily, 95); p5 = np.percentile(daily, 5)
    if p5 == 0:
        return float("nan")
    return float(abs(p95 / p5))


# ============================================================================
# METRICS — TRADE QUALITY
# ============================================================================

def trade_metrics(trades: pd.DataFrame) -> dict:
    if trades.empty or "r_multiple" not in trades.columns:
        return {}
    rs = trades["r_multiple"].astype(float).to_numpy()
    pnls = trades["pnl_net"].astype(float).to_numpy()

    wins = rs[rs > 0]; losses = rs[rs <= 0]
    win_rate = float(np.mean(rs > 0))
    avg_win_r = float(wins.mean()) if len(wins) else 0.0
    avg_loss_r = float(losses.mean()) if len(losses) else 0.0
    expectancy_r = float(rs.mean())
    win_loss_ratio = (avg_win_r / abs(avg_loss_r)) if avg_loss_r != 0 else float("nan")

    gross_wins = float(pnls[pnls > 0].sum())
    gross_losses = float(-pnls[pnls < 0].sum())
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else float("inf")

    # consecutive streaks
    max_cons_win = max_cons_loss = cur_w = cur_l = 0
    for r in rs:
        if r > 0:
            cur_w += 1; cur_l = 0
            max_cons_win = max(max_cons_win, cur_w)
        else:
            cur_l += 1; cur_w = 0
            max_cons_loss = max(max_cons_loss, cur_l)

    # concentration: % of profit from top 10 trades
    pnl_sorted = np.sort(pnls)[::-1]
    top10_sum = float(pnl_sorted[:10].sum()) if len(pnl_sorted) else 0.0
    total_pos = float(pnls[pnls > 0].sum()) if (pnls > 0).any() else 1.0
    top10_concentration = (top10_sum / total_pos * 100.0) if total_pos > 0 else float("nan")

    return {
        "n_trades": int(len(rs)),
        "win_rate": win_rate,
        "avg_win_r": avg_win_r,
        "avg_loss_r": avg_loss_r,
        "expectancy_r": expectancy_r,
        "win_loss_ratio": win_loss_ratio,
        "profit_factor": profit_factor,
        "largest_win_r": float(rs.max()),
        "largest_loss_r": float(rs.min()),
        "max_consec_wins": int(max_cons_win),
        "max_consec_losses": int(max_cons_loss),
        "top10_concentration_pct": top10_concentration,
        "avg_bars_held": float(trades["bars_held"].mean()) if "bars_held" in trades.columns else float("nan"),
        "trades_per_year": (len(rs) /
                            max((trades["exit_date"].max() - trades["entry_date"].min()).days / 365.25, 1e-9)),
    }


# ============================================================================
# METRICS — STATISTICAL
# ============================================================================

def bootstrap_expectancy(rs: np.ndarray, n_iter: int = 10000,
                         seed: int = 42) -> tuple[float, float, float, float]:
    """Returns (mean, lo, hi, p_positive)."""
    if len(rs) == 0:
        return float("nan"), float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    draws = np.empty(n_iter)
    n = len(rs)
    for i in range(n_iter):
        idx = rng.integers(0, n, size=n)
        draws[i] = rs[idx].mean()
    return (float(draws.mean()), float(np.percentile(draws, 2.5)),
            float(np.percentile(draws, 97.5)), float((draws > 0).mean()))


def is_oos_degradation(trades: pd.DataFrame, cut_iso: str) -> dict:
    cut = pd.Timestamp(cut_iso, tz="UTC")
    is_df = trades[trades["exit_date"] < cut]
    oos_df = trades[trades["exit_date"] >= cut]
    is_exp = float(is_df["r_multiple"].mean()) if len(is_df) else float("nan")
    oos_exp = float(oos_df["r_multiple"].mean()) if len(oos_df) else float("nan")
    return {
        "is_n": len(is_df), "oos_n": len(oos_df),
        "is_expectancy_r": is_exp, "oos_expectancy_r": oos_exp,
        "degradation_factor": (oos_exp / is_exp) if (is_exp and not math.isnan(is_exp) and is_exp != 0) else float("nan"),
    }


# ============================================================================
# METRICS — REGIME
# ============================================================================

def regime_breakdown(trades: pd.DataFrame, btc_df: pd.DataFrame) -> dict:
    """Bucket trades by BTC regime at entry date."""
    if btc_df.empty or trades.empty:
        return {}
    sma200 = btc_df["close"].rolling(200).mean()
    is_bull = (sma200 > sma200.shift(20)).fillna(False)
    vol20 = btc_df["close"].pct_change().rolling(20).std()
    vol_median = vol20.median()
    is_high_vol = (vol20 > vol_median).fillna(False)

    results = {}
    for label, mask in [("bull", is_bull), ("bear", ~is_bull),
                         ("high_vol", is_high_vol), ("low_vol", ~is_high_vol)]:
        regime_dates = mask[mask].index.normalize()
        entry_dates = trades["entry_date"].dt.normalize()
        in_regime = entry_dates.isin(regime_dates)
        sub = trades[in_regime]
        if len(sub) == 0:
            results[label] = {"n": 0, "expectancy_r": float("nan"), "win_rate": float("nan")}
        else:
            rs = sub["r_multiple"].astype(float).to_numpy()
            results[label] = {
                "n": int(len(rs)),
                "expectancy_r": float(rs.mean()),
                "win_rate": float((rs > 0).mean()),
            }
    return results


# ============================================================================
# MONTE CARLO
# ============================================================================

def monte_carlo_paths(rs: np.ndarray, starting_equity: float,
                      risk_per_trade: float, n_paths: int = 1000,
                      seed: int = 42) -> dict:
    """
    Resample R-multiples with replacement and produce equity paths.
    Returns percentile bands {5, 25, 50, 75, 95} as lists.
    """
    if len(rs) == 0:
        return {p: [] for p in (5, 25, 50, 75, 95)}
    rng = np.random.default_rng(seed)
    n = len(rs)
    paths = np.empty((n_paths, n))
    for i in range(n_paths):
        idx = rng.integers(0, n, size=n)
        sampled = rs[idx]
        # cumulative R, convert to equity
        cum_pnl = np.cumsum(sampled) * risk_per_trade * starting_equity
        paths[i, :] = starting_equity + cum_pnl
    return {p: [float(x) for x in np.percentile(paths, p, axis=0)]
            for p in (5, 25, 50, 75, 95)}


# ============================================================================
# COMPARISON
# ============================================================================

def compare_to_benchmark(strategy_eq: pd.Series,
                         benchmark_eq: pd.Series) -> dict:
    if strategy_eq.empty or benchmark_eq.empty:
        return {}
    s_cagr = cagr(strategy_eq); b_cagr = cagr(benchmark_eq)
    s_sh = sharpe_ratio(strategy_eq); b_sh = sharpe_ratio(benchmark_eq)
    s_dd = max_drawdown(strategy_eq); b_dd = max_drawdown(benchmark_eq)
    # daily-return correlation
    s_ret = strategy_eq.pct_change().dropna()
    b_ret = benchmark_eq.pct_change().dropna()
    common = s_ret.index.intersection(b_ret.index)
    corr = float(s_ret.loc[common].corr(b_ret.loc[common])) if len(common) > 20 else float("nan")
    return {
        "strategy_cagr": s_cagr, "benchmark_cagr": b_cagr,
        "excess_cagr": s_cagr - b_cagr if (not math.isnan(s_cagr) and not math.isnan(b_cagr)) else float("nan"),
        "strategy_sharpe": s_sh, "benchmark_sharpe": b_sh,
        "strategy_max_dd": s_dd, "benchmark_max_dd": b_dd,
        "correlation": corr,
    }


# ============================================================================
# HTML RENDERING
# ============================================================================

def fmt_pct(x, signed=True, decimals=2):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x*100:+.{decimals}f}%" if signed else f"{x*100:.{decimals}f}%"


def fmt_num(x, decimals=2, signed=False):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    if math.isinf(x):
        return "∞"
    spec = f"{'+' if signed else ''}.{decimals}f"
    return format(x, spec)


def fmt_money(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"${x:,.0f}"


def render_report(report: dict) -> str:
    K = report["kpis"]; R = report["returns"]; RK = report["risk"]
    RA = report["risk_adjusted"]; T = report["trades"]; S = report["stats"]
    REG = report["regimes"]; BCH = report["benchmark"]; MC = report["monte_carlo"]

    # equity curve data
    eq_dates = report["equity_dates"]; eq_values = report["equity_values"]
    bench_values = report["benchmark_values"]
    dd_values = report["drawdown_values"]
    r_histogram = report["r_histogram"]
    monthly_hist = report["monthly_histogram"]
    mc_p5  = MC.get(5, []); mc_p25 = MC.get(25, [])
    mc_p50 = MC.get(50, []); mc_p75 = MC.get(75, [])
    mc_p95 = MC.get(95, [])
    mc_xs = list(range(len(mc_p50)))

    def kpi_card(label, value, sub=""):
        return f"""
        <div class="kpi">
          <div class="kpi-label">{label}</div>
          <div class="kpi-value">{value}</div>
          {f'<div class="kpi-sub">{sub}</div>' if sub else ''}
        </div>
        """

    kpis_html = "".join([
        kpi_card("CAGR", fmt_pct(K["cagr"]), "annualized"),
        kpi_card("Max DD", fmt_pct(K["max_dd"], decimals=1), "worst peak-to-trough"),
        kpi_card("Sharpe", fmt_num(K["sharpe"]), "annualized"),
        kpi_card("Sortino", fmt_num(K["sortino"])),
        kpi_card("Calmar", fmt_num(K["calmar"]), "CAGR / |Max DD|"),
        kpi_card("Win Rate", fmt_pct(K["win_rate"], signed=False, decimals=1)),
        kpi_card("Expectancy", fmt_num(K["expectancy_r"], 2, True) + "R", "per trade"),
        kpi_card("Profit Factor", fmt_num(K["profit_factor"])),
    ])

    def two_col(label, value):
        return f"<tr><td>{label}</td><td>{value}</td></tr>"

    returns_html = "\n".join([
        two_col("CAGR",                       fmt_pct(R["cagr"])),
        two_col("Total return",               fmt_pct(R["total_return"])),
        two_col("Best month",                 fmt_pct(R["best_month"])),
        two_col("Worst month",                fmt_pct(R["worst_month"])),
        two_col("Median month",               fmt_pct(R["median_month"])),
        two_col("% profitable months",        fmt_pct(R["pct_positive_months"], signed=False)),
        two_col("Best year (rolling 12mo)",   fmt_pct(R["best_12mo"])),
        two_col("Worst year (rolling 12mo)",  fmt_pct(R["worst_12mo"])),
    ])

    risk_html = "\n".join([
        two_col("Annualized volatility",      fmt_pct(RK["ann_vol"], signed=False)),
        two_col("Max drawdown",               fmt_pct(RK["max_dd"])),
        two_col("Avg drawdown",               fmt_pct(RK["avg_dd"])),
        two_col("Longest underwater",         f"{RK['max_dd_duration_days']} days"),
        two_col("% time underwater",          f"{RK['pct_time_underwater']:.1f}%"),
        two_col("Daily VaR (95%)",            fmt_pct(RK["var_95"])),
        two_col("Daily CVaR (95%)",           fmt_pct(RK["cvar_95"])),
        two_col("Downside deviation (ann)",   fmt_pct(RK["downside_dev"], signed=False)),
    ])

    ra_html = "\n".join([
        two_col("Sharpe ratio",               fmt_num(RA["sharpe"])),
        two_col("Sortino ratio",              fmt_num(RA["sortino"])),
        two_col("Calmar ratio",               fmt_num(RA["calmar"])),
        two_col("Tail ratio (p95/|p5|)",      fmt_num(RA["tail_ratio"])),
    ])

    trade_html = "\n".join([
        two_col("Trades",                     f"{T['n_trades']:,}"),
        two_col("Trades / year",              fmt_num(T["trades_per_year"], 1)),
        two_col("Win rate",                   fmt_pct(T["win_rate"], signed=False, decimals=1)),
        two_col("Avg win",                    fmt_num(T["avg_win_r"], 2, True) + "R"),
        two_col("Avg loss",                   fmt_num(T["avg_loss_r"], 2, True) + "R"),
        two_col("Win/loss ratio",             fmt_num(T["win_loss_ratio"])),
        two_col("Expectancy",                 fmt_num(T["expectancy_r"], 2, True) + "R"),
        two_col("Profit factor",              fmt_num(T["profit_factor"])),
        two_col("Largest win",                fmt_num(T["largest_win_r"], 2, True) + "R"),
        two_col("Largest loss",               fmt_num(T["largest_loss_r"], 2, True) + "R"),
        two_col("Max consecutive wins",       f"{T['max_consec_wins']}"),
        two_col("Max consecutive losses",     f"{T['max_consec_losses']}"),
        two_col("Top-10 trades % of profit",  f"{T['top10_concentration_pct']:.1f}%"),
        two_col("Avg holding (bars)",         fmt_num(T["avg_bars_held"], 1)),
    ])

    stats_flag = "*" if S["bootstrap_ci_lo"] > 0 else ""
    stats_html = "\n".join([
        two_col("Bootstrap mean expectancy",  fmt_num(S["bootstrap_mean"], 3, True) + "R"),
        two_col(f"95% CI on expectancy {stats_flag}",
                f"[{fmt_num(S['bootstrap_ci_lo'], 3, True)}, {fmt_num(S['bootstrap_ci_hi'], 3, True)}]R"),
        two_col("P(expectancy > 0)",          fmt_pct(S["p_positive"], signed=False, decimals=1)),
        two_col("In-sample n",                f"{S['is_n']}"),
        two_col("In-sample expectancy",       fmt_num(S["is_expectancy_r"], 3, True) + "R"),
        two_col("Out-of-sample n",            f"{S['oos_n']}"),
        two_col("Out-of-sample expectancy",   fmt_num(S["oos_expectancy_r"], 3, True) + "R"),
        two_col("OOS / IS degradation factor", fmt_num(S["degradation_factor"], 2)),
    ])

    def regime_row(label, d):
        if not d: return ""
        return (f"<tr><td>{label}</td>"
                f"<td>{d['n']}</td>"
                f"<td>{fmt_num(d['expectancy_r'], 3, True)}R</td>"
                f"<td>{fmt_pct(d['win_rate'], signed=False, decimals=1)}</td></tr>")
    regime_html = "\n".join([
        regime_row("BTC bull (SMA200 rising)",   REG.get("bull")),
        regime_row("BTC bear (SMA200 falling)",  REG.get("bear")),
        regime_row("High BTC vol",               REG.get("high_vol")),
        regime_row("Low BTC vol",                REG.get("low_vol")),
    ])

    bench_html = ""
    if BCH:
        bench_html = "\n".join([
            two_col("Strategy CAGR",       fmt_pct(BCH["strategy_cagr"])),
            two_col(f"{DEFAULT_BENCHMARK} CAGR", fmt_pct(BCH["benchmark_cagr"])),
            two_col("Excess CAGR",         fmt_pct(BCH["excess_cagr"])),
            two_col("Strategy Sharpe",     fmt_num(BCH["strategy_sharpe"])),
            two_col(f"{DEFAULT_BENCHMARK} Sharpe", fmt_num(BCH["benchmark_sharpe"])),
            two_col("Strategy Max DD",     fmt_pct(BCH["strategy_max_dd"], decimals=1)),
            two_col(f"{DEFAULT_BENCHMARK} Max DD", fmt_pct(BCH["benchmark_max_dd"], decimals=1)),
            two_col("Daily-return correlation", fmt_num(BCH["correlation"])),
        ])

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Strategy Tearsheet — {report["file"]}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  body {{ font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
         margin: 0; padding: 24px; background: #0f1419; color: #e7e9ea; }}
  h1 {{ font-size: 20px; margin: 0; font-weight: 600; }}
  .sub {{ color: #8899a6; font-size: 12px; margin-bottom: 20px; }}
  .grid {{ display: grid; gap: 16px; }}
  .card {{ background: #15202b; border: 1px solid #253341; border-radius: 8px; padding: 16px; }}
  .card h2 {{ font-size: 12px; margin: 0 0 12px 0; color: #8899a6;
             text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }}
  .kpi-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 16px; }}
  .kpi {{ background: #15202b; border: 1px solid #253341; border-radius: 8px;
         padding: 12px; }}
  .kpi-label {{ font-size: 10px; color: #8899a6; text-transform: uppercase; letter-spacing: 0.05em; }}
  .kpi-value {{ font-size: 22px; font-weight: 600; margin-top: 4px; }}
  .kpi-sub {{ font-size: 11px; color: #536471; margin-top: 2px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  td {{ padding: 6px 10px; border-bottom: 1px solid #1a2330; }}
  td:first-child {{ color: #8899a6; }}
  td:last-child {{ text-align: right; font-weight: 500; }}
  th {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid #253341;
       color: #8899a6; font-weight: 500; }}
  canvas {{ background: #0f1419; }}
  .row-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  .row-3 {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; }}
  .meta {{ font-size: 11px; color: #536471; margin-top: 16px; text-align: right; }}
  .star {{ color: #00ba7c; font-weight: 700; }}
</style></head>
<body>

<h1>Strategy Tearsheet</h1>
<div class="sub">File: <b>{report["file"]}</b> ·
   Period: {report["period_start"]} → {report["period_end"]} ·
   {T["n_trades"]} trades ·
   Starting equity: {fmt_money(report["starting_equity"])} ·
   Risk/trade: {report["risk_per_trade"]*100:.2f}%</div>

<div class="kpi-grid">
{kpis_html}
</div>

<div class="grid">

  <!-- EQUITY + DRAWDOWN -->
  <div class="card">
    <h2>Equity Curve · Strategy vs {DEFAULT_BENCHMARK} Buy-and-Hold</h2>
    <canvas id="equityChart" height="220"></canvas>
  </div>

  <div class="card">
    <h2>Drawdown (underwater)</h2>
    <canvas id="ddChart" height="120"></canvas>
  </div>

  <!-- METRIC TABLES -->
  <div class="row-3">
    <div class="card">
      <h2>Returns</h2>
      <table>{returns_html}</table>
    </div>
    <div class="card">
      <h2>Risk</h2>
      <table>{risk_html}</table>
    </div>
    <div class="card">
      <h2>Risk-Adjusted</h2>
      <table>{ra_html}</table>
    </div>
  </div>

  <div class="row-2">
    <div class="card">
      <h2>Trade Quality</h2>
      <table>{trade_html}</table>
    </div>
    <div class="card">
      <h2>Statistical Significance</h2>
      <table>{stats_html}</table>
      <div style="font-size:11px;color:#536471;margin-top:8px;">
        * = 95% CI excludes zero
      </div>
    </div>
  </div>

  <div class="row-2">
    <div class="card">
      <h2>Regime Conditioning</h2>
      <table>
        <thead><tr><th>regime</th><th>n</th><th>exp R</th><th>win %</th></tr></thead>
        <tbody>{regime_html}</tbody>
      </table>
    </div>
    <div class="card">
      <h2>Benchmark Comparison ({DEFAULT_BENCHMARK})</h2>
      <table>{bench_html or '<tr><td colspan="2" style="color:#536471">benchmark unavailable</td></tr>'}</table>
    </div>
  </div>

  <!-- DISTRIBUTIONS -->
  <div class="row-2">
    <div class="card">
      <h2>R-Multiple Distribution</h2>
      <canvas id="rHistChart" height="180"></canvas>
    </div>
    <div class="card">
      <h2>Monthly Returns Distribution</h2>
      <canvas id="monthlyChart" height="180"></canvas>
    </div>
  </div>

  <!-- MONTE CARLO -->
  <div class="card">
    <h2>Monte Carlo Equity Range (1000 resampled paths)</h2>
    <canvas id="mcChart" height="220"></canvas>
    <div style="font-size:11px;color:#536471;margin-top:8px;">
      Shaded bands: 5th-95th and 25th-75th percentiles. Solid line: median.
      Shows what the same edge could have produced under different trade orderings.
    </div>
  </div>

</div>

<div class="meta">Generated {report["generated_at"]}</div>

<script>
const eqDates = {json.dumps(eq_dates)};
const eqValues = {json.dumps(eq_values)};
const benchValues = {json.dumps(bench_values)};
const ddValues = {json.dumps(dd_values)};
const rHist = {json.dumps(r_histogram)};
const monthlyHist = {json.dumps(monthly_hist)};
const mcXs = {json.dumps(mc_xs)};
const mcP5 = {json.dumps(mc_p5)};
const mcP25 = {json.dumps(mc_p25)};
const mcP50 = {json.dumps(mc_p50)};
const mcP75 = {json.dumps(mc_p75)};
const mcP95 = {json.dumps(mc_p95)};

function chartOpts(yfmt) {{
  return {{
    responsive: true,
    plugins: {{ legend: {{ labels: {{ color: '#e7e9ea' }} }} }},
    scales: {{
      x: {{ ticks: {{ color: '#8899a6', maxRotation: 0, autoSkip: true, maxTicksLimit: 8 }}, grid: {{ color: '#1a2330' }} }},
      y: {{ ticks: {{ color: '#8899a6', callback: yfmt }}, grid: {{ color: '#1a2330' }} }},
    }},
  }};
}}

if (eqDates.length) {{
  new Chart(document.getElementById('equityChart'), {{
    type: 'line', data: {{ labels: eqDates, datasets: [
      {{ label: 'Strategy', data: eqValues, borderColor: '#1d9bf0',
        backgroundColor: 'rgba(29,155,240,0.08)', fill: true, tension: 0.1, pointRadius: 0, borderWidth: 1.5 }},
      {{ label: '{DEFAULT_BENCHMARK} buy-hold', data: benchValues, borderColor: '#f7931a',
        backgroundColor: 'rgba(247,147,26,0.0)', fill: false, tension: 0.1, pointRadius: 0, borderWidth: 1.5 }},
    ]}}, options: chartOpts(v => '$' + Number(v).toLocaleString()),
  }});

  new Chart(document.getElementById('ddChart'), {{
    type: 'line', data: {{ labels: eqDates, datasets: [
      {{ label: 'Drawdown', data: ddValues.map(v => v*100), borderColor: '#f4212e',
        backgroundColor: 'rgba(244,33,46,0.25)', fill: true, tension: 0.1, pointRadius: 0, borderWidth: 1 }},
    ]}}, options: chartOpts(v => v.toFixed(1) + '%'),
  }});
}}

if (rHist.bins && rHist.bins.length) {{
  new Chart(document.getElementById('rHistChart'), {{
    type: 'bar', data: {{ labels: rHist.bins, datasets: [
      {{ label: 'trades', data: rHist.counts,
        backgroundColor: rHist.bins.map(b => parseFloat(b) >= 0 ? '#00ba7c' : '#f4212e') }}
    ]}}, options: chartOpts(v => v.toString()),
  }});
}}

if (monthlyHist.bins && monthlyHist.bins.length) {{
  new Chart(document.getElementById('monthlyChart'), {{
    type: 'bar', data: {{ labels: monthlyHist.bins, datasets: [
      {{ label: 'months', data: monthlyHist.counts,
        backgroundColor: monthlyHist.bins.map(b => parseFloat(b) >= 0 ? '#00ba7c' : '#f4212e') }}
    ]}}, options: chartOpts(v => v.toString()),
  }});
}}

if (mcP50.length) {{
  new Chart(document.getElementById('mcChart'), {{
    type: 'line', data: {{ labels: mcXs, datasets: [
      {{ label: '95th pct', data: mcP95, borderColor: 'rgba(0,186,124,0.5)',
        backgroundColor: 'rgba(0,186,124,0.1)', fill: '+1', pointRadius: 0, borderWidth: 0.5 }},
      {{ label: '75th pct', data: mcP75, borderColor: 'rgba(0,186,124,0.7)',
        backgroundColor: 'rgba(0,186,124,0.15)', fill: '+1', pointRadius: 0, borderWidth: 0.5 }},
      {{ label: 'Median',   data: mcP50, borderColor: '#1d9bf0',
        backgroundColor: 'transparent', fill: false, pointRadius: 0, borderWidth: 2 }},
      {{ label: '25th pct', data: mcP25, borderColor: 'rgba(244,33,46,0.7)',
        backgroundColor: 'rgba(244,33,46,0.15)', fill: '+1', pointRadius: 0, borderWidth: 0.5 }},
      {{ label: '5th pct',  data: mcP5,  borderColor: 'rgba(244,33,46,0.5)',
        backgroundColor: 'transparent', fill: false, pointRadius: 0, borderWidth: 0.5 }},
    ]}}, options: {{
      responsive: true,
      plugins: {{ legend: {{ labels: {{ color: '#e7e9ea' }} }} }},
      scales: {{
        x: {{ ticks: {{ color: '#8899a6' }}, grid: {{ color: '#1a2330' }}, title: {{ display: true, text: 'trade #', color: '#8899a6' }} }},
        y: {{ ticks: {{ color: '#8899a6', callback: v => '$' + Number(v).toLocaleString() }}, grid: {{ color: '#1a2330' }} }},
      }},
    }},
  }});
}}
</script>
</body></html>
"""


# ============================================================================
# DRIVER
# ============================================================================

def histogram(data, n_bins: int = 20) -> dict:
    if not len(data):
        return {"bins": [], "counts": []}
    arr = np.asarray(data)
    counts, edges = np.histogram(arr, bins=n_bins)
    centers = [(edges[i] + edges[i+1]) / 2 for i in range(len(edges) - 1)]
    return {"bins": [f"{c:.2f}" for c in centers], "counts": [int(c) for c in counts]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="trades.csv")
    ap.add_argument("--equity", type=float, default=DEFAULT_STARTING_EQUITY)
    ap.add_argument("--risk", type=float, default=DEFAULT_RISK_PER_TRADE)
    ap.add_argument("--out", default="strategy_report.html")
    ap.add_argument("--no-benchmark", action="store_true",
                    help="skip benchmark fetch (no internet / for testing)")
    args = ap.parse_args()

    trades = load_trades(args.file)
    if trades.empty:
        print(f"No trades found in {args.file}")
        return
    print(f"Loaded {len(trades)} trades from {args.file}")

    equity = build_equity_curve(trades, args.equity)
    print(f"Equity curve: {len(equity)} daily points, "
          f"{equity.index[0].date()} → {equity.index[-1].date()}")

    # benchmark
    bench_eq = pd.Series(index=equity.index, dtype=float)
    bench_metrics = {}
    if not args.no_benchmark:
        print(f"Fetching {DEFAULT_BENCHMARK} benchmark ...")
        try:
            bench_df = fetch_benchmark(DEFAULT_BENCHMARK, equity.index[0], equity.index[-1])
            bench_eq = benchmark_equity(bench_df, args.equity, equity.index)
            bench_metrics = compare_to_benchmark(equity, bench_eq)
        except Exception as e:
            print(f"  WARN: benchmark fetch failed: {e}")

    # regime breakdown
    regimes = {}
    if not args.no_benchmark:
        try:
            btc_df = fetch_benchmark(DEFAULT_BENCHMARK, equity.index[0], equity.index[-1])
            regimes = regime_breakdown(trades, btc_df)
        except Exception:
            pass

    # all metrics
    R = {
        "cagr": cagr(equity),
        "total_return": total_return(equity),
        "best_month": float(monthly_returns(equity).max()) if len(monthly_returns(equity)) else float("nan"),
        "worst_month": float(monthly_returns(equity).min()) if len(monthly_returns(equity)) else float("nan"),
        "median_month": float(monthly_returns(equity).median()) if len(monthly_returns(equity)) else float("nan"),
        "pct_positive_months": float((monthly_returns(equity) > 0).mean()) if len(monthly_returns(equity)) else float("nan"),
        "best_12mo": float(equity.resample("ME").last().pct_change(12).max()) if len(equity) > 365 else float("nan"),
        "worst_12mo": worst_rolling_12m(equity),
    }
    var95, cvar95 = var_cvar(equity)
    RK = {
        "ann_vol": annualized_volatility(equity),
        "max_dd": max_drawdown(equity),
        "avg_dd": avg_drawdown(equity),
        "max_dd_duration_days": max_drawdown_duration_days(equity),
        "pct_time_underwater": pct_time_underwater(equity),
        "var_95": var95, "cvar_95": cvar95,
        "downside_dev": downside_deviation(equity),
    }
    RA = {
        "sharpe": sharpe_ratio(equity),
        "sortino": sortino_ratio(equity),
        "calmar": calmar_ratio(equity),
        "tail_ratio": tail_ratio(equity),
    }
    T = trade_metrics(trades)

    rs = trades["r_multiple"].astype(float).to_numpy()
    boot_mean, boot_lo, boot_hi, p_pos = bootstrap_expectancy(rs)
    is_oos = is_oos_degradation(trades, IS_OOS_CUT)
    S = {
        "bootstrap_mean": boot_mean,
        "bootstrap_ci_lo": boot_lo,
        "bootstrap_ci_hi": boot_hi,
        "p_positive": p_pos,
        **is_oos,
    }

    MC = monte_carlo_paths(rs, args.equity, args.risk)

    K = {
        "cagr": R["cagr"], "max_dd": RK["max_dd"],
        "sharpe": RA["sharpe"], "sortino": RA["sortino"], "calmar": RA["calmar"],
        "win_rate": T["win_rate"], "expectancy_r": T["expectancy_r"],
        "profit_factor": T["profit_factor"],
    }

    dd = drawdown_series(equity)
    report = {
        "file": args.file,
        "period_start": equity.index[0].strftime("%Y-%m-%d"),
        "period_end": equity.index[-1].strftime("%Y-%m-%d"),
        "starting_equity": args.equity,
        "risk_per_trade": args.risk,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "kpis": K, "returns": R, "risk": RK, "risk_adjusted": RA,
        "trades": T, "stats": S, "regimes": regimes,
        "benchmark": bench_metrics,
        "monte_carlo": MC,
        "equity_dates": [d.strftime("%Y-%m-%d") for d in equity.index],
        "equity_values": [float(x) for x in equity.values],
        "benchmark_values": [float(x) if pd.notna(x) else None for x in bench_eq.values] if not bench_eq.empty else [],
        "drawdown_values": [float(x) for x in dd.values],
        "r_histogram": histogram(rs, n_bins=24),
        "monthly_histogram": histogram(monthly_returns(equity).values, n_bins=20),
    }

    html = render_report(report)
    Path(args.out).write_text(html, encoding="utf-8")
    print(f"\nWrote {Path(args.out).resolve()}")


if __name__ == "__main__":
    main()
