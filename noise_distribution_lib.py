"""
noise_distribution_lib.py — переиспользуемая методика: N прогонов бэктеста
с шумом ТОЛЬКО на разбиении связок одинакового entry_time (без изменения
параметров, без будущей информации), обёрнутая в run_with_noise(). Основа —
tie_break_noise_sensitivity.py (2026-08-13).
"""
import sys

import numpy as np
import pandas as pd

REPO_DIR = "/Users/sergeychikunov/sergey_trade_bridge_dev"
sys.path.insert(0, REPO_DIR)
SCRATCH = "/private/tmp/claude-501/-Users-sergeychikunov-sergey-trade-bridge-dev/0e7f3142-941d-403a-8c88-09b5294a1110/scratchpad"
sys.path.insert(0, SCRATCH)

from wide_margin_financing_v2 import daily_margin_fee, SHARES_CLASS_CODE  # noqa: E402
from portfolio_backtest import compute_sharpe  # noqa: E402

TRADES_PATH = f"{SCRATCH}/wide_backtest_out/wide_backtest_trades.csv"
MAX_POSITIONS = 20
LEVERAGE = 5.0

_TRADES_CACHE = {}


def load_block_trades_base(block: str) -> pd.DataFrame:
    if block in _TRADES_CACHE:
        return _TRADES_CACHE[block]
    df = pd.read_csv(TRADES_PATH, parse_dates=["entry_time", "exit_time"])
    df = df[df["block"] == block].copy()
    df["risk_pct"] = (df.entry - df.stop).abs() / df.entry
    df["rr"] = (df.target - df.entry).abs() / (df.entry - df.stop).abs()
    df = df[df.risk_pct > 0].reset_index(drop=True)
    _TRADES_CACHE[block] = df
    return df


def order_with_tie_noise(trades: pd.DataFrame, seed: int) -> pd.DataFrame:
    rng = np.random.RandomState(seed)
    parts = []
    for _, g in trades.groupby("entry_time", sort=True):
        idx = g.index.to_numpy().copy()
        rng.shuffle(idx)
        parts.append(g.loc[idx])
    return pd.concat(parts, ignore_index=True)


def simulate_one(trades_ordered: pd.DataFrame, capital: float, risk_per_trade: float, margin_cap: float) -> dict:
    equity = capital
    active = []
    equity_curve = [(trades_ordered.entry_time.min(), equity)]
    total_candidates = 0
    n_margin_rejected_profitable = 0
    n_profitable_total = 0
    margin_util_samples = []

    def close_due(before_time):
        nonlocal equity
        due = sorted([p for p in active if p["exit_time"] <= before_time], key=lambda p: p["exit_time"])
        for p in due:
            equity += p["r_multiple"] * p["risk_rub"]
            active.remove(p)
            equity_curve.append((p["exit_time"], equity))

    tz = trades_ordered.entry_time.dt.tz
    start_day = trades_ordered.entry_time.min().normalize()
    end_day = trades_ordered.exit_time.max().normalize()
    all_days = pd.date_range(start_day, end_day, freq="D", tz=tz)
    rows = list(trades_ordered.itertuples(index=False))
    idx = 0
    n_rows = len(rows)

    for day in all_days:
        day_end = day + pd.Timedelta(days=1)
        while idx < n_rows and rows[idx].entry_time < day_end:
            row = rows[idx]
            close_due(row.entry_time)
            total_candidates += 1
            is_profitable = row.r_multiple > 0
            if is_profitable:
                n_profitable_total += 1

            margin_used = sum(p["margin"] for p in active)
            margin_util_samples.append(margin_used / equity if equity > 0 else 1.0)
            risk_rub = equity * risk_per_trade
            notional = risk_rub / row.risk_pct
            margin_needed = notional / LEVERAGE

            rejected_for_margin = False
            if len(active) >= MAX_POSITIONS:
                pass  # skipped_positions, не считается margin-отказом
            elif margin_used + margin_needed > equity * margin_cap:
                rejected_for_margin = True
            else:
                active.append({
                    "exit_time": row.exit_time, "risk_rub": risk_rub, "r_multiple": row.r_multiple,
                    "margin": margin_needed, "notional": notional, "class_code": row.class_code,
                    "side": row.side, "entry_date": row.entry_time.normalize(),
                })
            if rejected_for_margin and is_profitable:
                n_margin_rejected_profitable += 1
            idx += 1

        close_due(day_end)

        overnight = [p for p in active if p["entry_date"] <= day]
        if overnight:
            long_shares = [p for p in overnight if p["class_code"] == SHARES_CLASS_CODE and p["side"] == "long"]
            short_shares = [p for p in overnight if p["class_code"] == SHARES_CLASS_CODE and p["side"] == "short"]
            futures_pos = [p for p in overnight if p["class_code"] != SHARES_CLASS_CODE]
            total_long_notional = sum(p["notional"] for p in long_shares)
            total_short_notional = sum(p["notional"] for p in short_shares)
            long_debt = max(0.0, total_long_notional - equity)
            short_debt = total_short_notional
            margin_used_today = sum(p["margin"] for p in overnight)
            futures_margin_today = sum(p["margin"] for p in futures_pos)
            shortfall_vs_cap = max(0.0, margin_used_today - equity * margin_cap)
            futures_go_deficit = min(shortfall_vs_cap, futures_margin_today)
            total_uncovered_today = long_debt + short_debt + futures_go_deficit
            if total_uncovered_today > 0:
                equity -= daily_margin_fee(total_uncovered_today)

    if active:
        close_due(max(p["exit_time"] for p in active))

    curve = sorted(equity_curve, key=lambda x: x[0])
    peak = capital
    max_dd_pct = 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        if peak > 0:
            max_dd_pct = max(max_dd_pct, (peak - eq) / peak * 100)
    final_equity = curve[-1][1]
    span_days = (trades_ordered.exit_time.max() - trades_ordered.entry_time.min()).days
    years = span_days / 365.25 if span_days > 0 else 1.0
    cagr_pct = ((final_equity / capital) ** (1 / years) - 1) * 100 if final_equity > 0 else -100.0
    sharpe = compute_sharpe(curve)

    pct_profitable_missed_to_margin = (
        n_margin_rejected_profitable / n_profitable_total * 100 if n_profitable_total else 0.0
    )
    avg_margin_util_pct = (
        sum(margin_util_samples) / len(margin_util_samples) * 100 if margin_util_samples else 0.0
    )

    return {
        "return_pct": (final_equity / capital - 1) * 100, "cagr_pct": cagr_pct,
        "sharpe": sharpe, "max_dd_pct": max_dd_pct,
        "pct_profitable_missed_to_margin": pct_profitable_missed_to_margin,
        "avg_margin_util_pct": avg_margin_util_pct,
    }


def run_with_noise(block: str, capital: float, risk_per_trade: float, margin_cap: float,
                    n_runs: int = 30, seed_base: int = 0) -> pd.DataFrame:
    """N прогонов с шумом ТОЛЬКО на разбиении связок entry_time. Возвращает
    DataFrame с одной строкой на прогон (return_pct/cagr_pct/sharpe/max_dd_pct/
    pct_profitable_missed_to_margin/avg_margin_util_pct)."""
    base = load_block_trades_base(block)
    rows = []
    for i in range(n_runs):
        seed = seed_base + i
        ordered = order_with_tie_noise(base, seed)
        r = simulate_one(ordered, capital, risk_per_trade, margin_cap)
        rows.append({"seed": seed, **r})
    return pd.DataFrame(rows)


def summarize(df: pd.DataFrame) -> dict:
    out = {}
    for metric in ["return_pct", "cagr_pct", "sharpe", "max_dd_pct",
                    "pct_profitable_missed_to_margin", "avg_margin_util_pct"]:
        out[f"{metric}_p10"] = df[metric].quantile(0.10)
        out[f"{metric}_median"] = df[metric].median()
        out[f"{metric}_p90"] = df[metric].quantile(0.90)
    out["pct_runs_positive_return"] = (df["return_pct"] > 0).mean() * 100
    out["n_runs"] = len(df)
    return out
