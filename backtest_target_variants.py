#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
backtest_target_variants.py

Сравнение вариантов расчёта цели (take-profit) для стратегии EMA9/21+RSI50.
Повод: наблюдение с реального счёта — цель часто ставится на "последний
пик" (swing high/low на D1), который на графике оказывается очень далеко от
входа, и цена до него практически никогда не доходит. Варианты и их мотивация
описаны в target_variants.py.

Данные (D1/H1 через кэш data_cache/candles/, при необходимости — Tinkoff API)
и сигналы (analyze_trade_setup: EMA9/21 + RSI50, стоп 2×ATR) — ТЕ ЖЕ, что в
backtest_ema921.py (эта его инфраструктура и переиспользуется напрямую).
Единственное, что варьируется между прогонами — способ расчёта target.

ВАЖНО: варианты симулируются НЕЗАВИСИМО (каждый — свой проход по барам своего
тикера), а не на общей "траектории" — потому что момент выхода зависит от
target, а значит и момент СЛЕДУЮЩЕГО входа (нет открытой позиции -> ищем
новый сетап) у каждого варианта свой. Это дороже по CPU (N проходов вместо
одного), но честно отражает, как вариант вёл бы себя в реальности, и не
требует повторной загрузки свечей (самая дорогая часть) — она случается один
раз на тикер, а не один раз на (тикер, вариант).

Использование:
  python backtest_target_variants.py --months 12 --max-tickers 60
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
        logging.FileHandler(LOGS_DIR / "backtest_target_variants.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

sys.path.insert(0, str(BASE_DIR))
from utils.ta import analyze_trade_setup  # noqa: E402
from dynamic_stop_manager import compute_new_sl_price  # noqa: E402
from config import COMMISSION_BPS_ROUNDTRIP  # noqa: E402
from target_variants import ALL_VARIANTS  # noqa: E402
from backtest_ema921 import (  # noqa: E402
    DYN_ACTIVATE_R, DYN_TRAIL_START_R, DYN_TRAIL_GAP_R,
    WARMUP_H1_BARS, MIN_D1_BARS, MIN_H1_BARS,
    fetch_d1, fetch_h1, load_universe, build_instrument_cache,
)

ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH if ENV_PATH.exists() else None)


@dataclass
class VariantTrade:
    variant: str
    ticker: str
    class_code: str
    side: str
    entry_time: object
    entry: float
    initial_stop: float
    target: float
    target_label: str
    exit_time: Optional[object] = None
    exit_price: Optional[float] = None
    exit_reason: str = ""
    r_multiple: float = 0.0
    bars_held: int = 0


def simulate_ticker_variant(
    variant_name: str, target_fn, ticker: str, class_code: str, df_d1, df_h1,
) -> List[VariantTrade]:
    trades: List[VariantTrade] = []
    if len(df_h1) < WARMUP_H1_BARS + MIN_H1_BARS:
        return trades

    open_trade: Optional[VariantTrade] = None
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
        if not setup.side or not setup.entry or not setup.stop:
            continue

        risk = abs(setup.entry - setup.stop)
        target, target_label = target_fn(d1_slice, setup.side, setup.entry, risk)
        if target is None:
            continue

        open_trade = VariantTrade(
            variant=variant_name, ticker=ticker, class_code=class_code, side=setup.side,
            entry_time=bar_time, entry=setup.entry, initial_stop=setup.stop,
            target=target, target_label=target_label,
        )
        current_stop = setup.stop
        open_entry_idx = i

    return trades


def compute_stats(closed: List[VariantTrade]) -> dict:
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
        "n": len(closed),
        "win_rate": win_rate,
        "avg_r_win": avg_r_win,
        "avg_r_loss": avg_r_loss,
        "expectancy": expectancy,
        "total_r": total_r,
        "max_dd": max_dd,
        "by_reason": by_reason,
        "avg_bars_held": avg_bars,
    }


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Сравнение вариантов расчёта цели (take-profit)")
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

    variants = ALL_VARIANTS
    if args.variants:
        wanted = set(args.variants.split(","))
        variants = {k: v for k, v in ALL_VARIANTS.items() if k in wanted}

    warmup_days = 120
    total_days = args.months * 30 + warmup_days
    universe = load_universe(Path(args.universe_csv), args.max_tickers)
    logger.info("Бэктест вариантов цели: %d тикеров, %d месяцев, варианты: %s",
                len(universe), args.months, ", ".join(variants.keys()))

    all_trades: List[VariantTrade] = []

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

            ticker_trade_counts = []
            for variant_name, target_fn in variants.items():
                trades = simulate_ticker_variant(variant_name, target_fn, ticker, class_code, df_d1, df_h1)
                all_trades.extend(trades)
                ticker_trade_counts.append(f"{variant_name}={len(trades)}")

            logger.info("[%d/%d] %s:%s — %s", idx, len(universe), ticker, class_code,
                        ", ".join(ticker_trade_counts))
            time.sleep(0.1)

    # ---- сохраняем все сделки всех вариантов в один CSV ----
    trades_path = OUT_DIR / "backtest_target_variants_trades.csv"
    with trades_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["variant", "ticker", "class_code", "side", "entry_time", "entry", "stop",
                    "target", "target_label", "exit_time", "exit_price", "exit_reason",
                    "r_multiple", "bars_held"])
        for t in all_trades:
            w.writerow([t.variant, t.ticker, t.class_code, t.side, t.entry_time, t.entry,
                        t.initial_stop, t.target, t.target_label, t.exit_time, t.exit_price,
                        t.exit_reason, round(t.r_multiple, 4), t.bars_held])

    # ---- отчёт по каждому варианту ----
    lines = [f"\n{'=' * 78}", f"СРАВНЕНИЕ ВАРИАНТОВ ЦЕЛИ — {len(universe)} тикеров, {args.months} мес.", "=" * 78]
    header = f"{'вариант':<22}{'n':>5}{'win%':>7}{'avg_R_win':>11}{'avg_R_loss':>12}{'expect':>9}{'total_R':>9}{'maxDD':>8}{'bars':>7}"
    lines.append(header)
    lines.append("-" * len(header))

    summary_rows = []
    for variant_name in variants.keys():
        closed = [t for t in all_trades if t.variant == variant_name and t.exit_time is not None]
        stats = compute_stats(closed)
        if not stats:
            lines.append(f"{variant_name:<22}{'—':>5} (нет закрытых сделок)")
            continue
        summary_rows.append((variant_name, stats))
        lines.append(
            f"{variant_name:<22}{stats['n']:>5}{stats['win_rate']:>6.1f}%"
            f"{stats['avg_r_win']:>+11.2f}{stats['avg_r_loss']:>+12.2f}"
            f"{stats['expectancy']:>+9.3f}{stats['total_r']:>+9.1f}{stats['max_dd']:>8.1f}"
            f"{stats['avg_bars_held']:>7.0f}"
        )

    lines.append("-" * len(header))
    lines.append("Причины выхода (доля от закрытых сделок):")
    for variant_name, stats in summary_rows:
        reasons = ", ".join(f"{k}={v}({v/stats['n']*100:.0f}%)" for k, v in stats["by_reason"].items())
        lines.append(f"  {variant_name:<22} {reasons}")

    lines.append("=" * 78)
    lines.append(f"Сделки сохранены: {trades_path}")
    summary = "\n".join(lines)
    logger.info(summary)
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
