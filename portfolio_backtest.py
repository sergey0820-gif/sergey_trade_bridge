#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
portfolio_backtest.py

Портфельная симуляция поверх уже готовых сделок из backtest_ema921.py.

backtest_ema921.py считает каждый тикер независимо — как будто у каждого
сигнала есть отдельный неограниченный капитал. Реальный счёт так не работает:
капитал общий, сделки на 60 тикерах конкурируют за него, действуют
MAX_OPEN_POSITIONS (лимит одновременных позиций) и MAX_MARGIN_UTILIZATION
(лимит доли капитала под маржой), и прибыль/убыток одной сделки меняет размер
следующей (сложный процент). Этот скрипт берёт CSV со сделками одного или
нескольких прогонов backtest_ema921.py (--tag) и проигрывает их как единый
портфель с этими ограничениями, чтобы получить честную (а не "потолочную")
оценку годовой доходности.

Как считается размер позиции и маржа:
- risk_rub = equity_на_момент_входа * RISK_PER_TRADE (сложный процент —
  считается от текущего, не от стартового капитала)
- notional = risk_rub / risk_pct, где risk_pct = |entry-stop|/entry
- margin_needed = notional / LEVERAGE (допущение x5, как обсуждали ранее —
  реальные ставки риска по инструментам не смоделированы)
- Позиция открывается, только если после неё margin_used <= equity * MAX_MARGIN_UTILIZATION
  и одновременных позиций < MAX_OPEN_POSITIONS; иначе сигнал пропускается
  ("skipped") — это и есть тот эффект, который наивная оценка "сделок/год ×
  экспектация" в backtest_ema921.py игнорирует.
- При нескольких сигналах на один и тот же час — приоритет по большему
  запланированному R:R (target/stop) как грубая замена скорингу
  ai_filter_agent.py, которого в этом бэктесте нет.

Ограничение (честно): просадка считается только по РЕАЛИЗОВАННОМУ капиталу
(в моменты закрытия сделок), без mark-to-market по floating-позициям — в
реальности внутридневная просадка может быть заметно глубже.

Использование:
  python portfolio_backtest.py
  python portfolio_backtest.py --capital 15000 --leverage 5
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
OUT_DIR = BASE_DIR / "out"
OUT_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "portfolio_backtest.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH if ENV_PATH.exists() else None)

# Сценарии = прогоны backtest_ema921.py, уже сделанные в этой сессии
DEFAULT_SCENARIOS = {
    "без фильтров": OUT_DIR / "backtest_ema921_trades_true_baseline.csv",
    "фильтр объёма": OUT_DIR / "backtest_ema921_trades_baseline.csv",
    "объём+тренд": OUT_DIR / "backtest_ema921_trades_vol_trend.csv",
    "объём+тренд+зазор EMA": OUT_DIR / "backtest_ema921_trades_vol_trend_gap.csv",
}


def load_trades(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["entry_time", "exit_time"])
    df = df.dropna(subset=["exit_time"]).copy()
    df["risk_pct"] = (df.entry - df.stop).abs() / df.entry
    df["rr"] = (df.target - df.entry).abs() / (df.entry - df.stop).abs()
    df = df[df.risk_pct > 0]
    return df.sort_values(["entry_time", "rr"], ascending=[True, False]).reset_index(drop=True)


def simulate_portfolio(
    trades: pd.DataFrame,
    capital: float,
    risk_per_trade: float,
    max_positions: int,
    max_margin_utilization: float,
    leverage: float,
) -> Dict:
    equity = capital
    active: List[Dict] = []  # {"exit_time", "risk_rub", "margin", "r_multiple"}
    equity_curve: List[Tuple[pd.Timestamp, float]] = [(trades.entry_time.min(), equity)]
    taken = 0
    skipped_positions = 0
    skipped_margin = 0

    def close_due(before_time: pd.Timestamp) -> None:
        nonlocal equity
        due = sorted([p for p in active if p["exit_time"] <= before_time], key=lambda p: p["exit_time"])
        for p in due:
            equity += p["r_multiple"] * p["risk_rub"]
            active.remove(p)
            equity_curve.append((p["exit_time"], equity))

    for row in trades.itertuples(index=False):
        close_due(row.entry_time)

        if len(active) >= max_positions:
            skipped_positions += 1
            continue

        margin_used = sum(p["margin"] for p in active)
        risk_rub = equity * risk_per_trade
        notional = risk_rub / row.risk_pct
        margin_needed = notional / leverage

        if margin_used + margin_needed > equity * max_margin_utilization:
            skipped_margin += 1
            continue

        active.append({
            "exit_time": row.exit_time, "risk_rub": risk_rub,
            "margin": margin_needed, "r_multiple": row.r_multiple,
        })
        taken += 1

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
    span_days = (trades.exit_time.max() - trades.entry_time.min()).days
    years = span_days / 365.25 if span_days > 0 else 1.0
    cagr_pct = ((final_equity / capital) ** (1 / years) - 1) * 100 if final_equity > 0 else -100.0

    return {
        "total_candidates": len(trades),
        "taken": taken,
        "skipped_positions": skipped_positions,
        "skipped_margin": skipped_margin,
        "final_equity": final_equity,
        "return_pct": (final_equity / capital - 1) * 100,
        "cagr_pct": cagr_pct,
        "max_dd_pct": max_dd_pct,
        "years": years,
        "curve": curve,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Портфельная симуляция поверх сделок backtest_ema921.py")
    ap.add_argument("--capital", type=float, default=float(os.getenv("CAPITAL", "15000")),
                     help="стартовый капитал, руб (по умолчанию — CAPITAL из .env)")
    ap.add_argument("--risk-per-trade", type=float, default=float(os.getenv("RISK_PER_TRADE", "0.02")),
                     help="риск на сделку, доля капитала (по умолчанию — RISK_PER_TRADE из .env)")
    ap.add_argument("--max-positions", type=int, default=int(os.getenv("MAX_OPEN_POSITIONS", "20")))
    ap.add_argument("--max-margin-utilization", type=float, default=float(os.getenv("MAX_MARGIN_UTILIZATION", "0.8")))
    ap.add_argument("--leverage", type=float, default=5.0,
                     help="допущение по плечу для оценки маржи (реальные ставки риска по инструментам не смоделированы)")
    args = ap.parse_args()

    logger.info(
        "Портфельная симуляция: capital=%.0f risk_per_trade=%.1f%% max_positions=%d max_margin=%.0f%% leverage=x%.0f",
        args.capital, args.risk_per_trade * 100, args.max_positions, args.max_margin_utilization * 100, args.leverage,
    )

    rows = []
    for name, path in DEFAULT_SCENARIOS.items():
        if not path.exists():
            logger.warning("Пропуск сценария '%s' — файл не найден: %s", name, path)
            continue
        trades = load_trades(path)
        result = simulate_portfolio(
            trades, args.capital, args.risk_per_trade,
            args.max_positions, args.max_margin_utilization, args.leverage,
        )
        curve_path = OUT_DIR / f"portfolio_equity_{name.replace(' ', '_').replace('+', 'n')}.csv"
        pd.DataFrame(result["curve"], columns=["time", "equity"]).to_csv(curve_path, index=False)

        logger.info(
            "[%s] кандидатов=%d взято=%d (пропущено: лимит_позиций=%d лимит_маржи=%d) "
            "итог=%.0f₽ (%+.1f%%) CAGR=%+.1f%% макс.просадка(реализованная)=%.1f%%",
            name, result["total_candidates"], result["taken"],
            result["skipped_positions"], result["skipped_margin"],
            result["final_equity"], result["return_pct"], result["cagr_pct"], result["max_dd_pct"],
        )
        rows.append({"сценарий": name, **{k: v for k, v in result.items() if k != "curve"}})

    if not rows:
        logger.error("Ни один сценарий не найден — сначала прогони backtest_ema921.py с нужными --tag")
        return 2

    summary = pd.DataFrame(rows)
    print("\n" + "=" * 100)
    print(f"ПОРТФЕЛЬНАЯ СИМУЛЯЦИЯ — старт {args.capital:.0f}₽, риск {args.risk_per_trade*100:.0f}%/сделку, "
          f"лимит {args.max_positions} позиций, маржа <= {args.max_margin_utilization*100:.0f}%, плечо x{args.leverage:.0f}")
    print("=" * 100)
    for _, r in summary.iterrows():
        print(
            f"{r['сценарий']:<28} взято {r['taken']:>4}/{r['total_candidates']:<4} "
            f"(пропущено поз={r['skipped_positions']:>4} маржа={r['skipped_margin']:>4})  "
            f"итог={r['final_equity']:>12,.0f}₽  доход={r['return_pct']:>+9.1f}%  "
            f"CAGR={r['cagr_pct']:>+8.1f}%  просадка={r['max_dd_pct']:>5.1f}%"
        )
    print("=" * 100)
    summary_path = OUT_DIR / "portfolio_backtest_summary.csv"
    summary.drop(columns=["curve"], errors="ignore").to_csv(summary_path, index=False)
    print(f"Сводка сохранена: {summary_path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
