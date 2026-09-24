#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
target_variants_v2.py — второй раунд экспериментов с целью/стопом, поверх
уже задеплоенной капы 4R (utils/ta.py::_structural_target, см.
target_variants.py + STRATEGY.md п.9).

Новые идеи (от пользователя, 2026-09-24):
  1. Свинг по ТЕЛУ свечи (max/min open,close), а не по тени (high/low) —
     тень часто разовый выброс, который рынок не подтверждает; последнее
     high/low ОТКРЫТИЯ ИЛИ ЗАКРЫТИЯ (что больше/меньше) — более
     консервативный, реально "удержанный" уровень.
  2. Капа цели в ПРОЦЕНТАХ ОТ ЦЕНЫ (5-10%), а не только в R — альтернативный,
     по-другому масштабирующийся потолок (не зависит от волатильности/ATR
     конкретного входа).
  3. ATR как ВТОРОЙ, независимый потолок/компонента цели, в комбинации со
     свингами (не вместо них).
  4. Стоп НЕ фиксированный 2×ATR, а структурный — "за нижней волной"
     (ближайший swing low для лонга / swing high для шорта), т.к. текущий
     R автоматически пересчитывается от этого стопа для расчёта R:R цели.
     С разумными границами (не уже 0.5×ATR, не шире 3×ATR — иначе стоп
     либо слишком шумный, либо слишком большой риск на сделку).

Каждая ЦЕЛЬ имеет сигнатуру (df_d1, side, entry, risk) -> (target, label) —
как в target_variants.py (совместимо, есть смысл переиспользовать).
Каждый СТОП имеет сигнатуру (df_d1, df_h1, side, entry, atr_h1) -> (stop, label).
"""

from __future__ import annotations

from typing import Optional, Tuple

import pandas as pd

from utils.ta import Side, SWING_ORDER, atr as _atr_series
from target_variants import _fixed_r, variant_baseline as _v1_baseline_target  # noqa: F401 (переиспользуется для сравнения)


# ---------------------------------------------------------------------------
# Свинг по телу свечи (open/close), а не по тени (high/low)
# ---------------------------------------------------------------------------

def _find_swing_levels_body(df: pd.DataFrame, side: Side, order: int = SWING_ORDER) -> list[float]:
    """Как _find_swing_levels в utils/ta.py, но экстремум ищется по
    max(open,close) для long / min(open,close) для short — "тело" свечи,
    а не тень. Для long считаем ВЫСШЕЕ из открытия/закрытия каждого бара."""
    if side == "long":
        body = df[["open", "close"]].max(axis=1).to_numpy()
    else:
        body = df[["open", "close"]].min(axis=1).to_numpy()
    n = len(df)
    levels: list[float] = []
    for i in range(order, n - order):
        window = body[i - order: i + order + 1]
        if side == "long" and body[i] == window.max():
            levels.append(float(body[i]))
        elif side == "short" and body[i] == window.min():
            levels.append(float(body[i]))
    return levels


def _swing_target_body(
    df_d1: pd.DataFrame, side: Side, entry: float, risk: float, *,
    lookback_days: int, min_rr: float, max_rr: Optional[float] = None,
) -> Tuple[Optional[float], str]:
    lookback = df_d1.tail(lookback_days) if len(df_d1) > lookback_days else df_d1
    levels = _find_swing_levels_body(lookback, side)

    if side == "long":
        candidates = sorted(lvl for lvl in levels if lvl > entry)
    else:
        candidates = sorted((lvl for lvl in levels if lvl < entry), reverse=True)

    if risk > 0:
        for level in candidates:
            rr = abs(level - entry) / risk
            if rr >= min_rr and (max_rr is None or rr <= max_rr):
                return level, "swing_body"
    return None, "none"


# ---------------------------------------------------------------------------
# Варианты ЦЕЛИ
# ---------------------------------------------------------------------------

def target_body_capped_4R(df_d1: pd.DataFrame, side: Side, entry: float, risk: float) -> Tuple[float, str]:
    """Как боевая капа 4R, но свинг по телу свечи, не по тени."""
    target, _ = _swing_target_body(df_d1, side, entry, risk, lookback_days=90, min_rr=1.5, max_rr=4.0)
    if target is not None:
        return target, "swing_body"
    uncapped, _ = _swing_target_body(df_d1, side, entry, risk, lookback_days=90, min_rr=1.5)
    if uncapped is not None:
        return _fixed_r(side, entry, risk, 4.0), "capped_at_4R"
    return _fixed_r(side, entry, risk, 3.0), "fallback_3R"


def _pct_cap_price(side: Side, entry: float, pct: float) -> float:
    return entry * (1 + pct) if side == "long" else entry * (1 - pct)


def target_body_pctcap_7pct(df_d1: pd.DataFrame, side: Side, entry: float, risk: float) -> Tuple[float, str]:
    """Свинг по телу, R:R>=1.5, но капа — не более 7% хода цены от входа
    (вместо капы в R). Fallback (нет подходящего свинга вообще) — 3R, но
    тоже не дальше 7% хода."""
    pct_cap_price = _pct_cap_price(side, entry, 0.07)
    lookback = df_d1.tail(90) if len(df_d1) > 90 else df_d1
    levels = _find_swing_levels_body(lookback, side)
    if side == "long":
        candidates = sorted(lvl for lvl in levels if lvl > entry)
    else:
        candidates = sorted((lvl for lvl in levels if lvl < entry), reverse=True)

    has_qualifying = False
    if risk > 0:
        for level in candidates:
            rr = abs(level - entry) / risk
            if rr >= 1.5:
                has_qualifying = True
                within_pct = (level <= pct_cap_price) if side == "long" else (level >= pct_cap_price)
                if within_pct:
                    return level, "swing_body"
                break  # монотонно дальше -> дальше будет только хуже

    if has_qualifying:
        return pct_cap_price, "capped_at_7pct"

    fallback = _fixed_r(side, entry, risk, 3.0)
    fb_within = (fallback <= pct_cap_price) if side == "long" else (fallback >= pct_cap_price)
    return (fallback if fb_within else pct_cap_price), "fallback_3R_or_7pct"


def target_body_atr_hybrid(df_d1: pd.DataFrame, side: Side, entry: float, risk: float) -> Tuple[float, str]:
    """Свинг по телу, R:R>=1.5, но капа — min(4R, 3×ATR(D1)) — комбинация
    R-капы и дневного ATR как независимого, рыночно-адаптивного потолка."""
    d1_atr = _atr_series(df_d1, 14)
    atr_val = float(d1_atr.iloc[-1]) if len(d1_atr) and pd.notna(d1_atr.iloc[-1]) else None
    atr_cap_price = (entry + 3 * atr_val) if (atr_val and side == "long") else \
                     (entry - 3 * atr_val) if atr_val else None

    lookback = df_d1.tail(90) if len(df_d1) > 90 else df_d1
    levels = _find_swing_levels_body(lookback, side)
    if side == "long":
        candidates = sorted(lvl for lvl in levels if lvl > entry)
    else:
        candidates = sorted((lvl for lvl in levels if lvl < entry), reverse=True)

    def _cap_ok(level: float) -> bool:
        rr_ok = abs(level - entry) / risk <= 4.0 if risk > 0 else True
        atr_ok = True
        if atr_cap_price is not None:
            atr_ok = (level <= atr_cap_price) if side == "long" else (level >= atr_cap_price)
        return rr_ok and atr_ok

    has_qualifying = False
    if risk > 0:
        for level in candidates:
            rr = abs(level - entry) / risk
            if rr >= 1.5:
                has_qualifying = True
                if _cap_ok(level):
                    return level, "swing_body"
                break

    if has_qualifying:
        # берём более БЛИЖНИЙ из двух потолков (R-капа или ATR-капа)
        r_cap_price = _fixed_r(side, entry, risk, 4.0)
        if atr_cap_price is None:
            return r_cap_price, "capped_at_4R"
        closer = min(r_cap_price, atr_cap_price) if side == "long" else max(r_cap_price, atr_cap_price)
        label = "capped_at_4R" if closer == r_cap_price else "capped_at_3xATR_D1"
        return closer, label

    return _fixed_r(side, entry, risk, 3.0), "fallback_3R"


# ---------------------------------------------------------------------------
# Варианты СТОПА (сигнатура: (df_d1, df_h1, side, entry, atr_h1) -> (stop, label))
# ---------------------------------------------------------------------------

STOP_LOOKBACK_DAYS = 20  # окно поиска "нижней/верхней волны" для стопа — короче, чем у цели (90д):
                         # для стопа важна СВЕЖАЯ структура рынка, а не любая историческая


def stop_fixed_2atr(df_d1: pd.DataFrame, df_h1: pd.DataFrame, side: Side, entry: float, atr_h1: float) -> Tuple[float, str]:
    """Боевая логика сейчас — без изменений, для сравнения."""
    stop = entry - atr_h1 * 2 if side == "long" else entry + atr_h1 * 2
    return stop, "fixed_2atr"


def _find_swing_levels_wick(df: pd.DataFrame, side: Side, order: int = SWING_ORDER) -> list[float]:
    """Свинг по тени (high/low) — как в utils.ta._find_swing_levels, но
    ИНВЕРТИРОВАННЫЙ side: для стопа лонга нужен swing LOW (не high)."""
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    n = len(df)
    levels: list[float] = []
    for i in range(order, n - order):
        if side == "long":
            window = lows[i - order: i + order + 1]
            if lows[i] == window.min():
                levels.append(float(lows[i]))
        else:
            window = highs[i - order: i + order + 1]
            if highs[i] == window.max():
                levels.append(float(highs[i]))
    return levels


def stop_structural_wick(df_d1: pd.DataFrame, df_h1: pd.DataFrame, side: Side, entry: float, atr_h1: float) -> Tuple[float, str]:
    """Стоп "за нижней волной" — ближайший swing low (long) / swing high
    (short) на D1 за последние STOP_LOOKBACK_DAYS, с небольшим буфером
    (0.1×ATR за уровнем, чтобы не стоять ровно на хае/лоу). Если такого
    свинга нет (совсем гладкий тренд) — fallback на боевые 2×ATR."""
    lookback = df_d1.tail(STOP_LOOKBACK_DAYS) if len(df_d1) > STOP_LOOKBACK_DAYS else df_d1
    levels = _find_swing_levels_wick(lookback, side)
    buffer = atr_h1 * 0.1 if atr_h1 and atr_h1 > 0 else entry * 0.001

    if side == "long":
        candidates = sorted((lvl for lvl in levels if lvl < entry), reverse=True)  # ближайший снизу
    else:
        candidates = sorted(lvl for lvl in levels if lvl > entry)  # ближайший сверху

    if candidates:
        level = candidates[0]
        stop = level - buffer if side == "long" else level + buffer
        return stop, "swing_structural"

    fallback = entry - atr_h1 * 2 if side == "long" else entry + atr_h1 * 2
    return fallback, "fallback_2atr"


def stop_structural_bounded(df_d1: pd.DataFrame, df_h1: pd.DataFrame, side: Side, entry: float, atr_h1: float) -> Tuple[float, str]:
    """Как stop_structural_wick, но риск (расстояние до стопа) ограничен
    коридором [0.5×ATR, 3×ATR] — слишком тесный структурный стоп (шум)
    расширяется до 0.5×ATR, слишком широкий (редкий, но возможный при
    большом расстоянии до последнего свинга) сужается до 3×ATR."""
    stop, label = stop_structural_wick(df_d1, df_h1, side, entry, atr_h1)
    if label == "fallback_2atr" or not atr_h1 or atr_h1 <= 0:
        return stop, label

    risk = abs(entry - stop)
    min_risk, max_risk = atr_h1 * 0.5, atr_h1 * 3.0
    if risk < min_risk:
        bounded = entry - min_risk if side == "long" else entry + min_risk
        return bounded, "swing_structural_bounded_min"
    if risk > max_risk:
        bounded = entry - max_risk if side == "long" else entry + max_risk
        return bounded, "swing_structural_bounded_max"
    return stop, label


# ---------------------------------------------------------------------------
# Реестр вариантов: каждый — пара (stop_fn, target_fn)
# ---------------------------------------------------------------------------

from target_variants import variant_baseline as target_baseline_uncapped  # noqa: E402
from target_variants import variant_swing_capped_4R as target_wick_capped4R_prod  # noqa: E402 — уже боевая (utils/ta.py)

ALL_COMBOS = {
    "baseline": (stop_fixed_2atr, target_baseline_uncapped),
    "target_body_capped4R": (stop_fixed_2atr, target_body_capped_4R),
    "target_body_pctcap_7pct": (stop_fixed_2atr, target_body_pctcap_7pct),
    "target_body_atr_hybrid": (stop_fixed_2atr, target_body_atr_hybrid),
    "stop_structural_wick": (stop_structural_wick, target_baseline_uncapped),
    "stop_structural_bounded": (stop_structural_bounded, target_baseline_uncapped),
    "combo_structural_stop_body_target": (stop_structural_bounded, target_body_capped_4R),
    "combo_structural_stop_body_pctcap": (stop_structural_bounded, target_body_pctcap_7pct),
    "combo_structural_stop_atr_hybrid": (stop_structural_bounded, target_body_atr_hybrid),
    "combo_structural_stop_PROD_target": (stop_structural_bounded, target_wick_capped4R_prod),
}
