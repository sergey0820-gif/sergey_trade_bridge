#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
monthly_signal_review.py — "работа над ошибками" по signal_journal.csv

signal_journal.py пишет ОДНУ строку на каждый сигнал — там, где он
остановился в пайплайне (rejected_by_rules / borderline_by_rules /
rejected_by_llm / skipped_* / would_execute / executed). Но CSV не знает,
чем бы это закончилось — этот скрипт досчитывает outcome по факту будущих
цен, ОДИНАКОВО для всех статусов (тем же алгоритмом трейлинг-стопа, что
реально использует dynamic_stop_manager.py), чтобы честно сравнить:
одобренные сигналы были лучше отклонённых, или нет.

Это работает независимо от того, включена ли реальная торговля
(AUTO_EXECUTE_ENABLED) — мы не берём фактический P&L со счёта, а смотрим,
что сделала бы цена, если бы мы вошли по записанным entry/stop/target.

Использование:
  python monthly_signal_review.py                 # весь ещё не досчитанный журнал
  python monthly_signal_review.py --month 2026-08  # только сигналы за август
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv
from tinkoff.invest import CandleInterval, Client

BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
OUT_DIR = BASE_DIR / "out"
OUT_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "monthly_signal_review.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

sys.path.insert(0, str(BASE_DIR))
from backtest_ema921 import candles_to_df  # noqa: E402
from dynamic_stop_manager import compute_new_sl_price  # noqa: E402
from signal_journal import JOURNAL_PATH, COLUMNS  # noqa: E402
from config import COMMISSION_BPS_ROUNDTRIP  # noqa: E402

ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH if ENV_PATH.exists() else None)

DYN_ACTIVATE_R = 1.0
DYN_TRAIL_START_R = 2.0
DYN_TRAIL_GAP_R = 0.5
MAX_LOOKAHEAD_DAYS = 30  # если ни стоп, ни цель не сработали за это время — outcome=timeout_unresolved
MIN_AGE_HOURS = 6  # не проверяем совсем свежие сигналы — им ещё не дали шанс дойти до цели/стопа


def fetch_forward_h1(client: Client, figi: str, start: datetime, end: datetime) -> pd.DataFrame:
    resp = client.market_data.get_candles(
        figi=figi, interval=CandleInterval.CANDLE_INTERVAL_HOUR, from_=start, to=end,
    )
    return candles_to_df(resp.candles)


def resolve_outcome(client: Client, figi: str, row: dict) -> Optional[dict]:
    ts = pd.Timestamp(row["ts"]).tz_localize("UTC") if pd.Timestamp(row["ts"]).tzinfo is None else pd.Timestamp(row["ts"])
    now = datetime.now(timezone.utc)
    if (now - ts).total_seconds() < MIN_AGE_HOURS * 3600:
        return None  # рано проверять

    end = min(now, ts + timedelta(days=MAX_LOOKAHEAD_DAYS))
    df_h1 = fetch_forward_h1(client, figi, ts, end)
    if df_h1.empty:
        return None

    side = row["side"]
    entry = float(row["entry"])
    stop = float(row["stop"])
    target = float(row["target"])
    risk_per_unit = abs(entry - stop)
    if risk_per_unit <= 0:
        return {"outcome": "bad_data", "outcome_r_multiple": 0, "outcome_exit_price": "", "outcome_exit_time": ""}

    current_stop = stop
    min_step = max(entry * 0.0001, 0.01)

    for _, bar in df_h1.iterrows():
        hit_stop = (bar["low"] <= current_stop) if side == "long" else (bar["high"] >= current_stop)
        hit_target = (bar["high"] >= target) if side == "long" else (bar["low"] <= target)

        if hit_stop:
            exit_price = current_stop
            raw_r = (exit_price - entry) / risk_per_unit if side == "long" else (entry - exit_price) / risk_per_unit
            cost_r = (entry * (COMMISSION_BPS_ROUNDTRIP / 10000.0)) / risk_per_unit
            return {"outcome": "stop", "outcome_r_multiple": round(raw_r - cost_r, 4),
                    "outcome_exit_price": exit_price, "outcome_exit_time": bar["time"]}
        if hit_target:
            exit_price = target
            raw_r = (exit_price - entry) / risk_per_unit if side == "long" else (entry - exit_price) / risk_per_unit
            cost_r = (entry * (COMMISSION_BPS_ROUNDTRIP / 10000.0)) / risk_per_unit
            return {"outcome": "target", "outcome_r_multiple": round(raw_r - cost_r, 4),
                    "outcome_exit_price": exit_price, "outcome_exit_time": bar["time"]}

        new_sl = compute_new_sl_price(
            direction=side, entry=entry, current=bar["close"], old_sl=current_stop, min_step=min_step,
            activate_r=DYN_ACTIVATE_R, trail_start_r=DYN_TRAIL_START_R, trail_gap_r=DYN_TRAIL_GAP_R,
        )
        if new_sl is not None:
            current_stop = new_sl

    if end >= now:
        return None  # окно наблюдения ещё не истекло (MAX_LOOKAHEAD_DAYS не прошло) и не резолвнулось — ждём
    return {"outcome": "timeout_unresolved", "outcome_r_multiple": 0, "outcome_exit_price": "", "outcome_exit_time": ""}


def build_instrument_cache(client: Client) -> dict:
    cache = {}
    for s in client.instruments.shares().instruments:
        cache[(s.ticker, s.class_code)] = s.figi
    for f in client.instruments.futures().instruments:
        cache[(f.ticker, f.class_code)] = f.figi
    return cache


def main() -> int:
    ap = argparse.ArgumentParser(description="Ежемесячный разбор журнала сигналов")
    ap.add_argument("--month", default=None, help="YYYY-MM — только сигналы за этот месяц (по умолчанию — весь журнал)")
    args = ap.parse_args()

    token = os.getenv("TINKOFF_TOKEN")
    if not token:
        logger.error("Не задан TINKOFF_TOKEN")
        return 2

    if not JOURNAL_PATH.exists():
        logger.info("signal_journal.csv не найден — нечего разбирать")
        return 0

    with JOURNAL_PATH.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    if args.month:
        rows = [r for r in rows if str(r.get("ts", "")).startswith(args.month)]

    to_resolve = [r for r in rows if not (r.get("outcome") or "").strip() and r.get("ts")]
    logger.info("Всего строк: %d, без досчитанного outcome: %d", len(rows), len(to_resolve))

    if to_resolve:
        with Client(token) as client:
            cache = build_instrument_cache(client)
            for i, row in enumerate(to_resolve, 1):
                figi = cache.get((row["ticker"], row["class_code"]))
                if not figi:
                    continue
                try:
                    result = resolve_outcome(client, figi, row)
                except Exception as e:
                    logger.warning("%s: ошибка при досчёте outcome: %s", row["ticker"], e)
                    continue
                if result:
                    row.update(result)
                    row["outcome_checked_at"] = datetime.now(timezone.utc).isoformat()
                    logger.info("[%d/%d] %s %s -> %s (%.2fR)", i, len(to_resolve), row["ticker"],
                                row.get("final_status"), result["outcome"], float(result["outcome_r_multiple"] or 0))
                time.sleep(0.1)

        # переписываем журнал целиком с досчитанными outcome
        by_key = {(r["ts"], r["ticker"], r["side"], r["entry"]): r for r in to_resolve}
        for r in rows:
            key = (r["ts"], r["ticker"], r["side"], r["entry"])
            if key in by_key:
                r.update(by_key[key])
        with JOURNAL_PATH.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

    # ---- отчёт ----
    df = pd.DataFrame(rows)
    resolved = df[df["outcome"].isin(["stop", "target"])].copy()
    if resolved.empty:
        print("Нет ни одной сделки с досчитанным исходом (stop/target) — рано подводить итоги.")
        return 0

    resolved["outcome_r_multiple"] = resolved["outcome_r_multiple"].astype(float)

    period = args.month or "весь журнал"
    print(f"\n{'=' * 70}\nРАЗБОР ПОЛЁТОВ — {period}\n{'=' * 70}")
    for status, g in resolved.groupby("final_status"):
        wr = (g.outcome_r_multiple > 0).mean() * 100
        exp = g.outcome_r_multiple.mean()
        print(f"{status:<24} n={len(g):>4}  win_rate={wr:>5.1f}%  expectancy={exp:>+7.3f}R")
    print("-" * 70)
    wr = (resolved.outcome_r_multiple > 0).mean() * 100
    exp = resolved.outcome_r_multiple.mean()
    print(f"{'ВСЕГО':<24} n={len(resolved):>4}  win_rate={wr:>5.1f}%  expectancy={exp:>+7.3f}R")
    print("=" * 70)

    out_path = OUT_DIR / f"signal_review_{args.month or 'all'}.csv"
    df.to_csv(out_path, index=False)
    print(f"Полная таблица сохранена: {out_path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
