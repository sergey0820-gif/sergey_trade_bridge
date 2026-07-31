#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
auto_executor.py

STRATEGY.md п.2/п.5: автоматический мост candidates_llm_approved.csv →
trade_executor.py, без Telegram/Sheets/подтверждения человеком.

Это самое рискованное звено всего плана (вход без человека), поэтому оно
спрятано за отдельным master-переключателем AUTO_EXECUTE_ENABLED (env,
по умолчанию false) — в дополнение к уже существующим DRY_RUN/ALLOW_PLACE.
Без AUTO_EXECUTE_ENABLED=true скрипт только логирует, что бы он сделал.

Один проход:
  1. check_daily_risk() — если дневной лимит убытка достигнут, новые входы
     не открываются (существующие позиции остаются под защитой стопов).
  2. Читаем candidates_llm_approved.csv (выход llm_signal_reviewer.py —
     кандидат уже прошёл и формальные правила ai_filter_agent.py, и
     независимую LLM-проверку), оставляем decision=PASS/verdict=approve
     как defense-in-depth (см. STRATEGY.md "LLM-проверка сигналов").
  3. Отбрасываем протухшие по времени кандидаты (AUTO_EXECUTE_CANDIDATE_TTL_SEC).
  4. Отбрасываем кандидатов с уже открытой позицией (по uid из портфеля;
     trade_executor.py делает тот же чек ещё раз как окончательный барьер).
  5. Ограничение MAX_OPEN_POSITIONS одновременных позиций (без currency).
  6. Для оставшихся — вызываем trade_executor.py --order-type auto подпроцессом.
  7. Успешные входы дописываются в logs/orders_log.csv.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from tinkoff.invest import Client

from daily_risk_guard import check_daily_risk

BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

CANDIDATES_LLM_APPROVED = BASE_DIR / "candidates_llm_approved.csv"
ORDERS_LOG = LOGS_DIR / "orders_log.csv"
ORDERS_LOG_HEADER = [
    "ts", "ticker", "class_code", "side", "qty", "lot_size", "price_used", "sum_total", "order_id",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "auto_executor.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

ENV_PATH = BASE_DIR / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    load_dotenv()

TINKOFF_TOKEN = os.getenv("TINKOFF_TOKEN")
TINKOFF_ACCOUNT_ID = os.getenv("TINKOFF_ACCOUNT_ID")


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


AUTO_EXECUTE_ENABLED = _env_bool("AUTO_EXECUTE_ENABLED", False)
DRY_RUN = _env_bool("DRY_RUN", False)
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "10"))
CANDIDATE_TTL_SEC = int(os.getenv("AUTO_EXECUTE_CANDIDATE_TTL_SEC", "900"))


def pick_python() -> str:
    exe = Path(sys.executable)
    if str(exe).startswith(str(BASE_DIR / ".venv")):
        return str(exe)
    for cand in (BASE_DIR / ".venv/bin/python", BASE_DIR / "venv/bin/python"):
        if cand.exists():
            return str(cand)
    return str(exe)


def read_approved_candidates() -> list[dict]:
    """
    Читает candidates_llm_approved.csv — выход llm_signal_reviewer.py, который
    уже отфильтровал только кандидатов, прошедших И формальные правила
    ai_filter_agent.py, И независимую LLM-проверку (llm_decision=approve).
    Фильтр по decision/verdict ниже — defense-in-depth (колонки пробрасываются
    насквозь из candidates_ai.csv), а не единственный барьер.
    """
    if not CANDIDATES_LLM_APPROVED.exists():
        logger.info("candidates_llm_approved.csv не найден — нечего исполнять")
        return []
    with CANDIDATES_LLM_APPROVED.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    approved = [
        r for r in rows
        if (r.get("decision") or "").strip().upper() == "PASS"
        and (r.get("verdict") or "").strip().lower() == "approve"
    ]
    logger.info("candidates_llm_approved.csv: всего=%d, approve=%d", len(rows), len(approved))
    return approved


def is_fresh(row: dict) -> bool:
    ts_raw = (row.get("timestamp") or "").strip()
    if not ts_raw:
        return False
    try:
        ts = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        logger.warning("Не удалось разобрать timestamp=%r для %s — пропускаю", ts_raw, row.get("ticker"))
        return False
    age_sec = (datetime.now() - ts).total_seconds()
    return 0 <= age_sec <= CANDIDATE_TTL_SEC


def fetch_portfolio_summary(client: Client, account_id: str) -> tuple[set[str], int]:
    """
    Возвращает (open_uids, open_count) — open_count не считает currency-остатки
    (иначе кэш на счету засчитывается как "открытая позиция").
    """
    pf = client.operations.get_portfolio(account_id=account_id)
    open_uids: set[str] = set()
    open_count = 0
    for p in pf.positions:
        q = p.quantity
        if (getattr(q, "units", 0) or 0) == 0 and (getattr(q, "nano", 0) or 0) == 0:
            continue
        if p.instrument_uid:
            open_uids.add(p.instrument_uid)
        if p.instrument_type != "currency":
            open_count += 1
    return open_uids, open_count


def append_orders_log(ticker: str, class_code: str, side: str, qty: str, order_id: str, price_used: str) -> None:
    is_new = not ORDERS_LOG.exists()
    with ORDERS_LOG.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(ORDERS_LOG_HEADER)
        w.writerow([
            datetime.now(timezone.utc).isoformat(),
            ticker, class_code, side, qty, "", price_used, "", order_id,
        ])


def run_trade_executor(row: dict) -> int:
    """
    Вызывает trade_executor.py подпроцессом. Возвращает returncode:
      0 — вход выполнен (или dry-run), 1 — отказ/ошибка, 2 — сигнал протух.
    """
    py = pick_python()
    cmd = [
        py, str(BASE_DIR / "trade_executor.py"),
        "--ticker", row["ticker"],
        "--class_code", row["class_code"],
        "--side", row["side"],
        "--entry", row["entry"],
        "--stop", row["stop"],
        "--target", row["target"],
        "--order-type", "auto",
    ]
    if DRY_RUN:
        cmd.append("--dry-run")

    logger.info("RUN ▷ %s", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True)
    if proc.stdout:
        logger.info("trade_executor stdout:\n%s", proc.stdout.strip())
    if proc.stderr:
        logger.info("trade_executor stderr:\n%s", proc.stderr.strip())

    if proc.returncode == 0:
        m_order = re.search(r"order_id=([\w-]+)", proc.stdout)
        m_lots = re.search(r"lots_executed=(\d+)", proc.stdout)
        order_id = m_order.group(1) if m_order else ""
        qty = m_lots.group(1) if m_lots else ""
        append_orders_log(
            ticker=row["ticker"],
            class_code=row["class_code"],
            side=row["side"],
            qty=qty,
            order_id=order_id,
            price_used=row["entry"],
        )
    return proc.returncode


def run_once() -> None:
    if not TINKOFF_TOKEN or not TINKOFF_ACCOUNT_ID:
        logger.error("Не заданы TINKOFF_TOKEN или TINKOFF_ACCOUNT_ID в .env")
        return

    with Client(TINKOFF_TOKEN) as client:
        risk = check_daily_risk(client, TINKOFF_ACCOUNT_ID)
        if risk.halted:
            logger.warning(
                "Дневной лимит убытка достигнут (yield=%.2f%% <= -%.2f%%) — новые входы заблокированы.",
                risk.yield_pct, risk.limit_pct,
            )
            return

        approved = read_approved_candidates()
        if not approved:
            return

        fresh = [r for r in approved if is_fresh(r)]
        stale_count = len(approved) - len(fresh)
        if stale_count:
            logger.info("Отброшено протухших по времени кандидатов: %d", stale_count)
        if not fresh:
            return

        open_uids, open_count = fetch_portfolio_summary(client, TINKOFF_ACCOUNT_ID)
        logger.info("Открытых позиций сейчас: %d (лимит %d)", open_count, MAX_OPEN_POSITIONS)

    if not AUTO_EXECUTE_ENABLED:
        logger.info(
            "AUTO_EXECUTE_ENABLED=false — только логирую намерения, ничего не размещаю (%d кандидатов прошли бы дальше)",
            len(fresh),
        )
        for r in fresh:
            skip_reason = "already_open_position" if r.get("uid") in open_uids else None
            logger.info(
                "[would-execute] %s (%s) side=%s entry=%s stop=%s target=%s%s",
                r["ticker"], r["class_code"], r["side"], r["entry"], r["stop"], r["target"],
                f" -- SKIP: {skip_reason}" if skip_reason else "",
            )
        return

    placed_this_run = 0
    for r in fresh:
        ticker = r["ticker"]
        uid = (r.get("uid") or "").strip()

        if uid and uid in open_uids:
            logger.info("%s: уже есть открытая позиция — пропускаю", ticker)
            continue

        if open_count + placed_this_run >= MAX_OPEN_POSITIONS:
            logger.info(
                "%s: достигнут лимит одновременных позиций (%d) — пропускаю оставшихся кандидатов этого прохода",
                ticker, MAX_OPEN_POSITIONS,
            )
            break

        try:
            rc = run_trade_executor(r)
        except Exception as e:
            logger.exception("%s: ошибка при вызове trade_executor.py: %s", ticker, e)
            continue

        if rc == 0:
            placed_this_run += 1
            logger.info("%s: вход выполнен", ticker)
        elif rc == 2:
            logger.info("%s: сигнал протух (auto order-type отклонил вход)", ticker)
        else:
            logger.warning("%s: trade_executor.py вернул код %d — вход не выполнен", ticker, rc)

    logger.info("auto_executor: проход завершён, входов открыто=%d", placed_this_run)


def main() -> int:
    logger.info(
        "Запуск auto_executor.py AUTO_EXECUTE_ENABLED=%s DRY_RUN=%s MAX_OPEN_POSITIONS=%s",
        AUTO_EXECUTE_ENABLED, DRY_RUN, MAX_OPEN_POSITIONS,
    )
    run_once()
    return 0


if __name__ == "__main__":
    sys.exit(main())
