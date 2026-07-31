#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
daily_risk_guard.py

STRATEGY.md п.4: дневной лимит убытка. Читает готовую метрику
daily_yield_relative из портфеля (Tinkoff считает и обнуляет её сам на новый
торговый день) и при достижении -DAILY_LOSS_LIMIT_PCT (по умолчанию 3%)
взводит флаг остановки новых входов на день — существующие позиции продолжают
жить под защитой stop_manager.py/stops_guard.py, эта проверка их не трогает.

Использование:
  - как библиотека: check_daily_risk(client, account_id) -> RiskStatus,
    вызывается из auto_executor.py перед каждым проходом.
  - как cron-скрипт: python daily_risk_guard.py (см. run_daily_risk_guard.sh),
    просто логирует текущее состояние и шлёт Telegram-алерт при первом халте
    за день.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from tinkoff.invest import Client
from tinkoff.invest.utils import quotation_to_decimal

BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
STATE_DIR = BASE_DIR / ".state"
STATE_PATH = STATE_DIR / "daily_risk_halt.json"

LOGS_DIR.mkdir(exist_ok=True)
STATE_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "daily_risk_guard.log", encoding="utf-8"),
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

DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "3.0"))

# Торговый день — по МСК (UTC+3, без переходов на летнее/зимнее время в РФ).
MSK = timezone(timedelta(hours=3))


@dataclass
class RiskStatus:
    halted: bool
    yield_pct: float
    limit_pct: float
    checked_at: str
    trade_date: str


def _today_msk() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d")


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("Не удалось прочитать %s: %s", STATE_PATH, e)
        return {}


def _save_state(state: dict) -> None:
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _send_telegram_alert(yield_pct: float) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы — алерт не отправлен")
        return

    text = (
        "🛑 Дневной лимит убытка достигнут\n"
        f"daily_yield_relative = {yield_pct:.2f}%\n"
        f"лимит = -{DAILY_LOSS_LIMIT_PCT:.2f}%\n"
        "Новые входы заблокированы до конца торгового дня. "
        "Открытые позиции остаются под защитой стопов."
    )
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=5,
        )
        if resp.status_code != 200:
            logger.warning("Ошибка Telegram-алерта: %s %s", resp.status_code, resp.text)
    except Exception as e:
        logger.warning("Исключение при отправке Telegram-алерта: %s", e)


def check_daily_risk(client: Client, account_id: str) -> RiskStatus:
    """
    Читает daily_yield_relative из портфеля и обновляет .state/daily_risk_halt.json.
    Halt всегда считается по живому запросу к API, а не по локальному файлу —
    файл нужен только для анти-спама Telegram-алерта (не более одного в день).
    """
    pf = client.operations.get_portfolio(account_id=account_id)
    yield_pct = float(quotation_to_decimal(pf.daily_yield_relative))

    halted = yield_pct <= -DAILY_LOSS_LIMIT_PCT
    today = _today_msk()
    now_iso = datetime.now(timezone.utc).isoformat()

    state = _load_state()
    # Если хранимая дата отличается от текущей — это новый торговый день,
    # локальный анти-спам флаг сбрасывается (сам halt всегда пересчитывается заново).
    if state.get("trade_date") != today:
        state = {"trade_date": today, "alert_sent": False}

    if halted and not state.get("alert_sent", False):
        logger.warning(
            "Дневной лимит убытка достигнут: yield=%.2f%% <= -%.2f%% — отправляю алерт",
            yield_pct,
            DAILY_LOSS_LIMIT_PCT,
        )
        _send_telegram_alert(yield_pct)
        state["alert_sent"] = True

    state["trade_date"] = today
    state["halted"] = halted
    state["yield_pct"] = yield_pct
    state["checked_at"] = now_iso
    _save_state(state)

    return RiskStatus(
        halted=halted,
        yield_pct=yield_pct,
        limit_pct=DAILY_LOSS_LIMIT_PCT,
        checked_at=now_iso,
        trade_date=today,
    )


def main() -> int:
    if not TINKOFF_TOKEN or not TINKOFF_ACCOUNT_ID:
        logger.error("Не заданы TINKOFF_TOKEN или TINKOFF_ACCOUNT_ID в .env")
        return 2

    with Client(TINKOFF_TOKEN) as client:
        status = check_daily_risk(client, TINKOFF_ACCOUNT_ID)

    logger.info(
        "daily_risk_guard: date=%s yield=%.2f%% limit=-%.2f%% halted=%s",
        status.trade_date,
        status.yield_pct,
        status.limit_pct,
        status.halted,
    )
    return 1 if status.halted else 0


if __name__ == "__main__":
    sys.exit(main())
