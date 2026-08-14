"""
initial_stop_cache.py

Персистентное хранилище исходного (на момент первой успешной постановки)
стоп-лосса на позицию — по образцу .state/*.json кэшей, уже используемых
в проекте (stop_alerts.json, llm_reviewed_signals.json).

Зачем: dynamic_stop_manager.py::compute_new_sl_price() считал риск
(risk_per_unit) от entry и ТЕКУЩЕГО живого SL (полученного с биржи каждый
раз заново) — как только SL хоть раз подвинут в безубыток
(old_sl == entry), это ложно срабатывает как "некорректные данные" и
обрывает функцию раньше, чем считается R, — трейлинг дальше безубытка
становится невозможен (см. STRATEGY.md, "Открытые вопросы" п.8б).
Фикс требует знать ИСХОДНЫЙ SL отдельно от текущего — а его сейчас нигде
не хранят: pending_stops.csv — временная очередь, строка из неё удаляется
сразу после успешной постановки стопа (см. stop_manager.py).

Пишется: stop_manager.py — сразу после успешной постановки SL по позиции
(единственное место в пайплайне, где исходный стоп достоверно известен).
Читается: dynamic_stop_manager.py — перед расчётом нового SL.

Ключ — instrument_uid. Новая успешная постановка SL для того же uid
(будь то действительно новая позиция или повторная защита позиции,
потерявшей стоп) естественным образом перезаписывает старое значение —
отдельного механизма очистки при закрытии позиции не требуется.
Возрастная чистка (CACHE_MAX_AGE_DAYS) — только гигиена файла на случай
осиротевших записей (по образцу REVIEWED_CACHE_MAX_AGE_HOURS в
llm_signal_reviewer.py), не логика домена.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / ".state"
CACHE_PATH = STATE_DIR / "initial_stop_prices.json"

# Свечи/трейлинг-сделки этой стратегии живут часы-недели (не минуты) — запас
# с большим избытком, чтобы не потерять данные для честно ещё открытой
# долгой позиции; просто гигиена файла, не доменное ограничение.
CACHE_MAX_AGE_DAYS = 60


def load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=CACHE_MAX_AGE_DAYS)
    pruned = {}
    for uid, entry in cache.items():
        ts = entry.get("recorded_at")
        try:
            if ts and datetime.fromisoformat(ts) >= cutoff:
                pruned[uid] = entry
        except Exception:
            continue
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(pruned, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CACHE_PATH)


def record_initial_sl(uid: str, stop_price: float, direction: str) -> None:
    """Вызывается сразу после подтверждённой успешной постановки SL."""
    cache = load_cache()
    cache[uid] = {
        "initial_sl": stop_price,
        "direction": direction,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    save_cache(cache)


def get_initial_sl(uid: str) -> Optional[float]:
    cache = load_cache()
    entry = cache.get(uid)
    if entry is None:
        return None
    return entry.get("initial_sl")
