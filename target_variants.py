#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
target_variants.py — альтернативные способы расчёта цели (take-profit) для
стратегии EMA9/21 + RSI50, для сравнения с боевой логикой в utils/ta.py
(_structural_target: ближайший swing-уровень с R:R >= 1.5, иначе fallback 3R).

Причина: наблюдение с реального счёта — цель часто ставится на "последний
пик" (swing high/low), который оказывается ОЧЕНЬ далеко от входа, и цена до
него практически никогда не доходит. Это прямое следствие текущей логики:
если ближайший свинг даёт R:R < 1.5, он ОТБРАСЫВАЕТСЯ и алгоритм берёт
СЛЕДУЮЩИЙ, более дальний — то есть чем ближе рынок к балансу (свинги густо
расположены, но недалеко от входа), тем дальше улетает выбранная цель.

Каждая функция имеет сигнатуру (df_d1, side, entry, risk) -> (target, label)
и не имеет побочных эффектов — не используется в боевом коде, только здесь
и в backtest_target_variants.py.
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

import pandas as pd

from utils.ta import Side, SWING_ORDER, _find_swing_levels  # noqa: E402 — переиспользуем боевую логику фракталов


def _swing_target(
    df_d1: pd.DataFrame, side: Side, entry: float, risk: float, *,
    lookback_days: int, min_rr: float, max_rr: Optional[float] = None,
) -> Tuple[Optional[float], str]:
    """Общий движок для swing-based вариантов: ближайший swing с min_rr <= R:R (<= max_rr, если задан)."""
    lookback = df_d1.tail(lookback_days) if len(df_d1) > lookback_days else df_d1
    levels = _find_swing_levels(lookback, side, order=SWING_ORDER)

    if side == "long":
        candidates = sorted(lvl for lvl in levels if lvl > entry)
    else:
        candidates = sorted((lvl for lvl in levels if lvl < entry), reverse=True)

    if risk > 0:
        for level in candidates:
            rr = abs(level - entry) / risk
            if rr >= min_rr and (max_rr is None or rr <= max_rr):
                return level, "swing"

    return None, "none"


def _fixed_r(side: Side, entry: float, risk: float, r_mult: float) -> float:
    return entry + risk * r_mult if side == "long" else entry - risk * r_mult


# ---------------------------------------------------------------------------
# Варианты
# ---------------------------------------------------------------------------

def variant_baseline(df_d1: pd.DataFrame, side: Side, entry: float, risk: float) -> Tuple[float, str]:
    """Боевая логика сейчас: ближайший swing (90 дней) с R:R >= 1.5, иначе фикс. 3R."""
    target, label = _swing_target(df_d1, side, entry, risk, lookback_days=90, min_rr=1.5)
    if target is not None:
        return target, "swing"
    return _fixed_r(side, entry, risk, 3.0), "fallback_3R"


def variant_fixed_2R(df_d1: pd.DataFrame, side: Side, entry: float, risk: float) -> Tuple[float, str]:
    """Полностью игнорируем структуру — фиксированная цель 2R (совпадает с trail_start_r трейлинга)."""
    return _fixed_r(side, entry, risk, 2.0), "fixed_2R"


def variant_fixed_1_5R(df_d1: pd.DataFrame, side: Side, entry: float, risk: float) -> Tuple[float, str]:
    """Фиксированная цель 1.5R (порог MIN_TARGET_RR боевой логики, но как цель, а не минимум)."""
    return _fixed_r(side, entry, risk, 1.5), "fixed_1.5R"


def variant_swing_capped_4R(df_d1: pd.DataFrame, side: Side, entry: float, risk: float) -> Tuple[float, str]:
    """Как baseline, но если ближайший качественный свинг дальше 4R — берём фикс. 4R вместо него."""
    target, label = _swing_target(df_d1, side, entry, risk, lookback_days=90, min_rr=1.5, max_rr=4.0)
    if target is not None:
        return target, "swing_capped"
    # ищем ближайший свинг вообще (даже за пределами 4R) — если единственный кандидат ушёл за потолок,
    # это тот самый случай "далёкий пик", который и нужно обрезать
    uncapped, _ = _swing_target(df_d1, side, entry, risk, lookback_days=90, min_rr=1.5)
    if uncapped is not None:
        return _fixed_r(side, entry, risk, 4.0), "capped_at_4R"
    return _fixed_r(side, entry, risk, 3.0), "fallback_3R"


def variant_swing_recent_30d(df_d1: pd.DataFrame, side: Side, entry: float, risk: float) -> Tuple[float, str]:
    """Как baseline, но окно поиска свинга — 30 дней вместо 90 (отсекаем старые, протухшие пики)."""
    target, label = _swing_target(df_d1, side, entry, risk, lookback_days=30, min_rr=1.5)
    if target is not None:
        return target, "swing_recent"
    return _fixed_r(side, entry, risk, 3.0), "fallback_3R"


def variant_swing_nearest_any_rr(df_d1: pd.DataFrame, side: Side, entry: float, risk: float) -> Tuple[float, str]:
    """Берём БЛИЖАЙШИЙ свинг вообще (минимальный порог 0.5R, чтобы не целиться в шум рядом со входом),
    не отбрасываем его из-за низкого R:R — противоположность текущей логике."""
    target, label = _swing_target(df_d1, side, entry, risk, lookback_days=90, min_rr=0.5)
    if target is not None:
        return target, "swing_nearest"
    return _fixed_r(side, entry, risk, 3.0), "fallback_3R"


def variant_no_target(df_d1: pd.DataFrame, side: Side, entry: float, risk: float) -> Tuple[float, str]:
    """Без цели вообще — выход только по (трейлинг-)стопу. inf/-inf гарантированно никогда не бьётся."""
    return (math.inf if side == "long" else -math.inf), "no_target"


ALL_VARIANTS = {
    "baseline": variant_baseline,
    "fixed_2R": variant_fixed_2R,
    "fixed_1.5R": variant_fixed_1_5R,
    "swing_capped_4R": variant_swing_capped_4R,
    "swing_recent_30d": variant_swing_recent_30d,
    "swing_nearest_any_rr": variant_swing_nearest_any_rr,
    "no_target": variant_no_target,
}
