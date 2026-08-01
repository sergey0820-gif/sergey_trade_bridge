#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
telegram_bridge.py — Sergey-Trade 2025 (только уведомления)

Раньше это был постоянно работающий бот (Application.run_polling) с кнопкой
"Place", которая сама вызывала trade_executor.py в обход ВСЕХ проверок
auto_executor.py (дневной риск-гард, лимит маржи, лимит позиций, LLM-барьер —
бот брал кандидатов из candidates.csv/candidates_ai.csv напрямую). Раз все
действия теперь автоматизированы через auto_executor.py, ручное размещение
ордера кнопкой из Telegram убрано — это одноразовый скрипт-уведомитель (как
остальной пайплайн: ai_filter_agent.py -> llm_signal_reviewer.py ->
telegram_bridge.py -> auto_executor.py), без кнопок и без polling.

Читает candidates_llm_approved.csv — тот же финальный файл, который видит
auto_executor.py, — так что уведомление отражает ровно то, что система
считает готовым к входу (или уже вошла бы, будь AUTO_EXECUTE_ENABLED=true).

Ожидаемые env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Опционально: RESEND_COOLDOWN_MIN (антидубли, по умолчанию 240 мин)
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
from telegram import Bot

BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
STATE_DIR = BASE_DIR / "state"
STATE_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "telegram_bridge.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

ENV_PATH = BASE_DIR / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CANDIDATES_LLM_APPROVED = BASE_DIR / "candidates_llm_approved.csv"
STATE_PATH = STATE_DIR / "telegram_state.json"
RESEND_COOLDOWN_MIN = int(os.getenv("RESEND_COOLDOWN_MIN", "240"))


@dataclass
class Candidate:
    ticker: str
    class_code: str
    side: str
    entry: Optional[float]
    stop: Optional[float]
    target: Optional[float]
    rsi_d1: Optional[float]
    rsi_h4: Optional[float]
    volume_ratio: Optional[float]
    pattern: Optional[str]
    timestamp: Optional[str]
    score: Optional[float]
    llm_decision: Optional[str]
    llm_reasoning: Optional[str]


def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def read_candidates() -> List[Candidate]:
    if not CANDIDATES_LLM_APPROVED.exists():
        return []
    with CANDIDATES_LLM_APPROVED.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    out: List[Candidate] = []
    for r in rows:
        out.append(
            Candidate(
                ticker=r.get("ticker", ""),
                class_code=r.get("class_code", ""),
                side=r.get("side", ""),
                entry=_f(r.get("entry")),
                stop=_f(r.get("stop")),
                target=_f(r.get("target")),
                rsi_d1=_f(r.get("rsi_d1")),
                rsi_h4=_f(r.get("rsi_h4")),
                volume_ratio=_f(r.get("volume_ratio")),
                pattern=r.get("pattern") or None,
                timestamp=r.get("timestamp") or None,
                score=_f(r.get("score")),
                llm_decision=r.get("llm_decision") or None,
                llm_reasoning=r.get("llm_reasoning") or None,
            )
        )
    return out


def _candidate_key(c: Candidate) -> str:
    e = f"{c.entry:.4f}" if c.entry is not None else "na"
    return f"{c.ticker}:{c.class_code}:{c.side}:{e}"


def _load_state() -> Dict:
    if not STATE_PATH.exists():
        return {"sent": {}}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as f:
            st = json.load(f)
        st.setdefault("sent", {})
        return st
    except Exception:
        return {"sent": {}}


def _save_state(st: Dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_PATH)


def _cooldown_ok(st: Dict, key: str) -> bool:
    last = st["sent"].get(key)
    if last and (time.time() - last) < RESEND_COOLDOWN_MIN * 60:
        return False
    return True


def _fmt(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.4f}".rstrip("0").rstrip(".")


def build_text(c: Candidate) -> str:
    side_emoji = "🟢 LONG" if c.side == "long" else "🔴 SHORT"
    asset = "future" if c.class_code == "SPBFUT" else "share"
    reasoning = (c.llm_reasoning or "").strip()
    if len(reasoning) > 200:
        reasoning = reasoning[:200] + "…"
    text = (
        f"{side_emoji}  {c.ticker} ({c.class_code}, {asset})\n"
        f"entry: {_fmt(c.entry)} | SL: {_fmt(c.stop)} | TP: {_fmt(c.target)}\n"
        f"RSI D1: {_fmt(c.rsi_d1)} | RSI H1: {_fmt(c.rsi_h4)} | vol_ratio: {_fmt(c.volume_ratio)}\n"
        f"паттерн: {c.pattern or '—'} | score: {_fmt(c.score)}\n"
        f"LLM: {c.llm_decision or '—'}"
    )
    if reasoning:
        text += f"\n   {reasoning}"
    text += f"\nts: {c.timestamp or '—'}"
    return text


async def send_notifications() -> int:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Не заданы TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID в .env")
        return 2

    candidates = read_candidates()
    if not candidates:
        logger.info("candidates_llm_approved.csv пуст или не найден — нечего отправлять")
        return 0

    st = _load_state()
    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    sent = 0
    for c in candidates:
        key = _candidate_key(c)
        if not _cooldown_ok(st, key):
            logger.info("%s: антидубль (уже отправляли недавно) — пропускаю", key)
            continue
        try:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=build_text(c),
                disable_web_page_preview=True,
            )
            st["sent"][key] = time.time()
            sent += 1
            logger.info("Отправлено уведомление: %s", key)
        except Exception as e:
            logger.warning("Не удалось отправить уведомление для %s: %s", key, e)

    if sent:
        _save_state(st)
    logger.info("telegram_bridge: отправлено %d/%d", sent, len(candidates))
    return 0


def main() -> int:
    return asyncio.run(send_notifications())


if __name__ == "__main__":
    sys.exit(main())
