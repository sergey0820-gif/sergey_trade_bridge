#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
backtest_target_variants_v2.py — второй раунд сравнения (стоп, цель) —
пары из target_variants_v2.py::ALL_COMBOS. В отличие от
backtest_target_variants.py (только цель варьируется, стоп всегда боевые
2×ATR), здесь варьируется и стоп: analyze_trade_setup используется ТОЛЬКО
для обнаружения сигнала (side/entry), стоп и цель считаются заново парой
(stop_fn, target_fn) для каждого варианта — ATR(H1,14) пересчитывается
отдельно (той же формулой utils.ta.atr, что и в бою), т.к.
analyze_trade_setup не отдаёt его наружу.

Использование:
  python backtest_target_variants_v2.py --months 12 --max-tickers 60
"""

from __future__ import annotations

import csv
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
from tinkoff.invest import Client

BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
OUT_DIR = BASE_DIR / "out"
OUT_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "backtest_target_variants_v2.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

sys.path.insert(0, str(BASE_DIR))
from utils.ta import analyze_trade_setup, atr as _atr_series  # noqa: E402
from dynamic_stop_manager import compute_new_sl_price  # noqa: E402
from config import COMMISSION_BPS_ROUNDTRIP  # noqa: E402
from target_variants_v2 import ALL_COMBOS  # noqa: E402
from backtest_ema921 import (  # noqa: E402
    DYN_ACTIVATE_R, DYN_TRAIL_START_R, DYN_TRAIL_GAP_R,
    WARMUP_H1_BARS, MIN_D1_BARS, MIN_H1_BARS,
    fetch_d1, fetch_h1, load_universe, build_instrument_cache,
)

ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH if ENV_PATH.exists() else None)


@dataclass
class ComboTrade:
    variant: str
    ticker: str
    class_code: str
    side: str
    entry_time: object
    entry: float
    initial_stop: float
    stop_label: str
    target: float
    target_label: str
    exit_time: Optional[object] = None
    exit_price: Optional[float] = None
    exit_reason: str = ""
    r_multiple: float = 0.0
    bars_held: int = 0


def simulate_ticker_combo(
    variant_name: str, stop_fn, target_fn, ticker: str, class_code: str, df_d1, df_h1,
) -> List[ComboTrade]:
    trades: List[ComboTrade] = []
    if len(df_h1) < WARMUP_H1_BARS + MIN_H1_BARS:
        return trades

    open_trade: Optional[ComboTrade] = None
    current_stop = 0.0
    open_entry_idx = 0

    for i in range(WARMUP_H1_BARS, len(df_h1)):
        bar = df_h1.iloc[i]
        bar_time = bar["time"]

        if open_trade is not None:
            side = open_trade.side
            min_step = max(open_trade.entry * 0.0001, 0.01)
            risk_per_unit = abs(open_trade.entry - open_trade.initial_stop)

            hit_stop = (bar["low"] <= current_stop) if side == "long" else (bar["high"] >= current_stop)
            hit_target = (bar["high"] >= open_trade.target) if side == "long" else (bar["low"] <= open_trade.target)

            if hit_stop:
                exit_price, reason = current_stop, "stop"
            elif hit_target:
                exit_price, reason = open_trade.target, "target"
            else:
                exit_price, reason = None, ""

            if exit_price is not None:
                raw_r = (exit_price - open_trade.entry) / risk_per_unit if side == "long" else (open_trade.entry - exit_price) / risk_per_unit
                cost_r = (open_trade.entry * (COMMISSION_BPS_ROUNDTRIP / 10000.0)) / risk_per_unit
                open_trade.exit_time = bar_time
                open_trade.exit_price = exit_price
                open_trade.exit_reason = reason
                open_trade.r_multiple = raw_r - cost_r
                open_trade.bars_held = i - open_entry_idx
                trades.append(open_trade)
                open_trade = None
                continue

            new_sl = compute_new_sl_price(
                direction=side, entry=open_trade.entry, current=bar["close"],
                old_sl=current_stop, min_step=min_step,
                activate_r=DYN_ACTIVATE_R, trail_start_r=DYN_TRAIL_START_R,
                trail_gap_r=DYN_TRAIL_GAP_R,
            )
            if new_sl is not None:
                current_stop = new_sl
            continue

        d1_slice = df_d1[df_d1["time"] <= bar_time]
        h1_slice = df_h1.iloc[: i + 1]
        if len(d1_slice) < MIN_D1_BARS or len(h1_slice) < MIN_H1_BARS:
            continue

        setup = analyze_trade_setup(d1_slice, h1_slice)
        if not setup.side or not setup.entry:
            continue

        atr_h1_series = _atr_series(h1_slice, 14)
        atr_h1 = float(atr_h1_series.iloc[-1]) if len(atr_h1_series) and atr_h1_series.notna().iloc[-1] else None
        if not atr_h1 or atr_h1 <= 0:
            continue

        entry = setup.entry
        stop, stop_label = stop_fn(d1_slice, h1_slice, setup.side, entry, atr_h1)
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        target, target_label = target_fn(d1_slice, setup.side, entry, risk)
        if target is None:
            continue

        open_trade = ComboTrade(
            variant=variant_name, ticker=ticker, class_code=class_code, side=setup.side,
            entry_time=bar_time, entry=entry, initial_stop=stop, stop_label=stop_label,
            target=target, target_label=target_label,
        )
        current_stop = stop
        open_entry_idx = i

    return trades


def compute_stats(closed: List[ComboTrade]) -> dict:
    if not closed:
        return {}
    wins = [t for t in closed if t.r_multiple > 0]
    losses = [t for t in closed if t.r_multiple <= 0]
    win_rate = len(wins) / len(closed) * 100
    avg_r_win = sum(t.r_multiple for t in wins) / len(wins) if wins else 0.0
    avg_r_loss = sum(t.r_multiple for t in losses) / len(losses) if losses else 0.0
    expectancy = sum(t.r_multiple for t in closed) / len(closed)
    total_r = sum(t.r_multiple for t in closed)

    closed_sorted = sorted(closed, key=lambda t: t.entry_time)
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for t in closed_sorted:
        cum += t.r_multiple
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    by_reason: Dict[str, int] = {}
    for t in closed:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1

    avg_bars = sum(t.bars_held for t in closed) / len(closed)

    return {
        "n": len(closed), "win_rate": win_rate, "avg_r_win": avg_r_win,
        "avg_r_loss": avg_r_loss, "expectancy": expectancy, "total_r": total_r,
        "max_dd": max_dd, "by_reason": by_reason, "avg_bars_held": avg_bars,
    }


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Сравнение вариантов (стоп, цель) — v2")
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--max-tickers", type=int, default=60)
    ap.add_argument("--universe-csv", default=str(BASE_DIR / "universe.csv"))
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--variants", default="", help="через запятую, пусто = все")
    args = ap.parse_args()

    import os
    token = os.getenv("TINKOFF_TOKEN")
    if not token:
        logger.error("Не задан TINKOFF_TOKEN в .env")
        return 2

    combos = ALL_COMBOS
    if args.variants:
        wanted = set(args.variants.split(","))
        combos = {k: v for k, v in ALL_COMBOS.items() if k in wanted}

    warmup_days = 120
    total_days = args.months * 30 + warmup_days
    universe = load_universe(Path(args.universe_csv), args.max_tickers)
    logger.info("Бэктест v2 (стоп+цель): %d тикеров, %d месяцев, варианты: %s",
                len(universe), args.months, ", ".join(combos.keys()))

    all_trades: List[ComboTrade] = []

    with Client(token) as client:
        cache = build_instrument_cache(client)
        for idx, (ticker, class_code) in enumerate(universe, 1):
            figi = cache.get((ticker, class_code))
            if not figi:
                continue
            try:
                df_d1 = fetch_d1(client, figi, total_days, ticker=ticker, class_code=class_code,
                                  use_cache=not args.no_cache)
                df_h1 = fetch_h1(client, figi, total_days, ticker=ticker, class_code=class_code,
                                  use_cache=not args.no_cache)
            except Exception as e:
                logger.warning("[%d/%d] %s: ошибка загрузки свечей: %s", idx, len(universe), ticker, e)
                continue

            counts = []
            for variant_name, (stop_fn, target_fn) in combos.items():
                trades = simulate_ticker_combo(variant_name, stop_fn, target_fn, ticker, class_code, df_d1, df_h1)
                all_trades.extend(trades)
                counts.append(f"{variant_name}={len(trades)}")

            logger.info("[%d/%d] %s:%s — %s", idx, len(universe), ticker, class_code, ", ".join(counts))
            time.sleep(0.1)

    trades_path = OUT_DIR / "backtest_target_variants_v2_trades.csv"
    with trades_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["variant", "ticker", "class_code", "side", "entry_time", "entry", "stop",
                    "stop_label", "target", "target_label", "exit_time", "exit_price",
                    "exit_reason", "r_multiple", "bars_held"])
        for t in all_trades:
            w.writerow([t.variant, t.ticker, t.class_code, t.side, t.entry_time, t.entry,
                        t.initial_stop, t.stop_label, t.target, t.target_label, t.exit_time,
                        t.exit_price, t.exit_reason, round(t.r_multiple, 4), t.bars_held])

    years = args.months / 12.0
    risk_per_trade_pct = 2.0

    lines = [f"\n{'=' * 90}", f"СРАВНЕНИЕ (СТОП, ЦЕЛЬ) v2 — {len(universe)} тикеров, {args.months} мес.", "=" * 90]
    header = (f"{'вариант':<34}{'n':>5}{'win%':>7}{'avg_R_win':>11}{'avg_R_loss':>12}"
              f"{'expect':>9}{'total_R':>9}{'maxDD':>8}{'bars':>6}{'год.дох%':>10}")
    lines.append(header)
    lines.append("-" * len(header))

    for variant_name in combos.keys():
        closed = [t for t in all_trades if t.variant == variant_name and t.exit_time is not None]
        stats = compute_stats(closed)
        if not stats:
            lines.append(f"{variant_name:<34}{'—':>5} (нет закрытых сделок)")
            continue
        trades_per_year = stats["n"] / years if years > 0 else 0
        annual_pct = trades_per_year * stats["expectancy"] * risk_per_trade_pct
        lines.append(
            f"{variant_name:<34}{stats['n']:>5}{stats['win_rate']:>6.1f}%"
            f"{stats['avg_r_win']:>+11.2f}{stats['avg_r_loss']:>+12.2f}"
            f"{stats['expectancy']:>+9.3f}{stats['total_r']:>+9.1f}{stats['max_dd']:>8.1f}"
            f"{stats['avg_bars_held']:>6.0f}{annual_pct:>+9.1f}%"
        )

    lines.append("-" * len(header))
    lines.append(f"(год.дох% — наивная оценка: сделок/год × экспектация × {risk_per_trade_pct:.0f}% риска на сделку,")
    lines.append(" без сложного процента и лимита позиций/маржи — см. STRATEGY.md п.4)")
    lines.append("=" * 90)
    lines.append(f"Сделки сохранены: {trades_path}")
    summary = "\n".join(lines)
    logger.info(summary)
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
