#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Sergey-Trade 2025 — АГЕНТ (async)
Назначение:
- Проверка наличия universe.csv (информационное предупреждение, если пусто)
- Авто-подтверждение сигналов (RSI/Зона/Δ/TTL) в:
    out/live_candidates_public.csv
    out/live_candidates_ki.csv
- Итоговый лог: [agent] done: public=<N> ki=<M>

Важно:
- Скан кандидатов выполняет отдельный сервис (sergey-scan.service).
- Этот агент НИЧЕГО не сканирует заново, он лишь постпроцессит (auto-confirm).
"""

from __future__ import annotations

import os
import csv
import asyncio
from typing import List

from dotenv import load_dotenv

# подключаем авто-подтверждение
from auto_confirm import auto_confirm_csv

# ---------- Конфиг/окружение ----------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
OUT_DIR = os.path.join(BASE_DIR, "out")

UNIVERSE_CSV = os.path.join(OUT_DIR, "universe.csv")
PUBLIC_CSV = os.path.join(OUT_DIR, "live_candidates_public.csv")
KI_CSV = os.path.join(OUT_DIR, "live_candidates_ki.csv")

load_dotenv(os.path.join(BASE_DIR, ".env"))

# ---------- Утилиты ----------


def _csv_rows(path: str) -> List[dict]:
    """Прочитать CSV в список словарей; если файла нет — вернуть []."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _ensure_out_dir() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)


# ---------- Главная ----------


async def main() -> None:
    print("[agent] start")

    _ensure_out_dir()

    # 1) Информационная проверка универса
    uni_rows = _csv_rows(UNIVERSE_CSV)
    if not uni_rows:
        print(
            "[agent] WARN: пустой out/universe.csv — агенту нечего постпроцессить (ждём sergey-scan.service)"
        )

    # 2) Авто-подтверждение (если токен и данные корректны — confirm проставится)
    try:
        await auto_confirm_csv(PUBLIC_CSV)
        await auto_confirm_csv(KI_CSV)
        print("[agent] auto-confirm done")
    except Exception as e:
        # Не валим агент из-за ошибок наружних сервисов — просто логируем
        print("[agent] auto-confirm ERR:", e)

    # 3) Итоговые числа (просто количество строк в CSV после авто-подтверждения)
    public_rows = _csv_rows(PUBLIC_CSV)
    ki_rows = _csv_rows(KI_CSV)

    print(f"[agent] done: public={len(public_rows)} ki={len(ki_rows)}")


if __name__ == "__main__":
    asyncio.run(main())
