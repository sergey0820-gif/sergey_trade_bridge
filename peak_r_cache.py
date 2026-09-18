"""
peak_r_cache.py

Персистентное хранилище пиковой прибыли (в R) по открытой позиции — нужно
для give-back-стопа в dynamic_stop_manager.py: закрыть позицию, если она
хотя бы раз дошла до GIVEBACK_MIN_PEAK_R, а потом откатилась от этого пика
больше чем на GIVEBACK_FRAC — защита от "зашёл уверенно, потом потух,
развернулся". Найдено бэктестом (backtest_ema921.py, 12+ мес, поверх
min_volume_ratio=2.5): портфельный CAGR +996% против +831% без этой
защиты, устойчиво на обеих независимых половинах периода.

По образцу initial_stop_cache.py — тот же паттерн (.state/*.json, ключ —
instrument_uid). Обновляется КАЖДЫЙ раз, когда dynamic_stop_manager.py
видит R выше сохранённого пика, не только при движении стопа. Запись
удаляется, когда give-back реально закрывает позицию — если этого не
делать, старый пик "утечёт" в следующую позицию по этому же uid.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / ".state"
CACHE_PATH = STATE_DIR / "peak_r_prices.json"

CACHE_MAX_AGE_DAYS = 60  # гигиена файла на случай осиротевших записей, не доменная логика


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


def get_peak_r(uid: str) -> float:
    entry = load_cache().get(uid)
    return float(entry.get("peak_r", 0.0)) if entry else 0.0


def update_peak_r(uid: str, current_r: float) -> float:
    """Обновляет пик, если current_r больше сохранённого. Возвращает
    итоговый (возможно не изменившийся) пик — вызывающий код сравнивает
    с ним текущий R, чтобы решить про give-back."""
    cache = load_cache()
    entry = cache.get(uid, {})
    prev = float(entry.get("peak_r", 0.0))
    new_peak = max(prev, current_r)
    if new_peak != prev or uid not in cache:
        cache[uid] = {"peak_r": new_peak, "recorded_at": datetime.now(timezone.utc).isoformat()}
        save_cache(cache)
    return new_peak


def clear_peak_r(uid: str) -> None:
    cache = load_cache()
    if uid in cache:
        del cache[uid]
        save_cache(cache)
