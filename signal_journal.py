#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
signal_journal.py

Общий журнал судьбы каждого сигнала по всему пайплайну
(ai_filter_agent.py -> llm_signal_reviewer.py -> auto_executor.py) —
append-only, в отличие от candidates.csv/candidates_ai.csv/
candidates_llm_approved.csv, которые перезаписываются каждый прогон и не
хранят историю.

Каждый сигнал журналируется РОВНО ОДИН раз — на той стадии, где он
остановился (final_status):
  rejected_by_rules      — не прошёл score_candidate() в ai_filter_agent.py
  borderline_by_rules    — прошёл, но decision=BORDERLINE (не approve)
  rejected_by_llm        — прошёл правила, но llm_signal_reviewer.py отклонил
  skipped_stale          — прошёл ИИ, но протух по времени (auto_executor.py)
  skipped_already_open   — уже есть открытая позиция по этому инструменту
  skipped_position_limit — упёрлись в MAX_OPEN_POSITIONS в этом проходе
  skipped_margin_limit   — упёрлись в MAX_MARGIN_UTILIZATION в этом проходе
  skipped_crypto_filter  — независимая (defense-in-depth) проверка
                            is_crypto_linked() в auto_executor.py отсекла
                            крипто-привязанный фьючерс перед исполнением —
                            второй слой поверх universe_builder.py
  would_execute          — прошёл всё, но AUTO_EXECUTE_ENABLED=false (реальный
                            вход не делался — текущее состояние системы)
  executed                — реально размещён ордер
  skipped_stale_at_executor / skipped_executor_error — trade_executor.py
                            отказал на своём отдельном барьере

monthly_signal_review.py читает журнал целиком и досчитывает
outcome/outcome_r_multiple по факту того, что произошло с ценой дальше —
это единственный способ понять, была ли реальная разница между тем, что
одобрено, и тем, что отклонено (в т.ч. без включения реальной торговли).
"""

from __future__ import annotations

import csv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
JOURNAL_PATH = BASE_DIR / "logs" / "signal_journal.csv"

COLUMNS = [
    "ts", "ticker", "class_code", "side", "entry", "stop", "target",
    "rsi_d1", "rsi_h4", "volume_ratio", "pattern",
    "rules_score", "rules_decision", "rules_reasons",
    "llm_decision", "llm_reasoning",
    "final_status", "final_reason",
    "outcome", "outcome_r_multiple", "outcome_exit_price", "outcome_exit_time", "outcome_checked_at",
]


def append_journal_row(row: dict) -> None:
    JOURNAL_PATH.parent.mkdir(exist_ok=True)
    is_new = not JOURNAL_PATH.exists()
    with JOURNAL_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow({c: row.get(c, "") for c in COLUMNS})
