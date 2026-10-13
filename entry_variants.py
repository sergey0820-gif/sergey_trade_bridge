#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
entry_variants.py — альтернативные ТРИГГЕРЫ входа ("войти раньше, на
начале роста"), 2026-10-12. В отличие от всех экспериментов этой сессии
до сих пор (капа цели, структурный стоп, momentum-fade, gap-фильтр) —
они все были ДОБАВКАМИ поверх одного и того же триггера (пересечение
EMA9/21 на H1). Здесь меняется сам триггер — когда именно считается, что
сигнал есть.

Стоп и цель для ВСЕХ вариантов — одна и та же логика (_structural_stop/
_structural_target из utils/ta.py), чтобы сравнение было честным: разница
в результатах — только от момента входа, не от риск-менеджмента.

Каждый вариант — функция (df_d1, df_h4) -> (side, entry, reason) или
(None, None, "") если сигнала нет. side/entry — как в TradeSetup.
"""

from __future__ import annotations

from typing import Optional, Tuple

import pandas as pd

from utils.ta import Side, ema, rsi


def _d1_rsi_trend_ok(df_d1: pd.DataFrame, side: Side) -> bool:
    r_d1 = rsi(df_d1["close"], 14)
    if len(r_d1) < 1 or pd.isna(r_d1.iloc[-1]):
        return False
    return r_d1.iloc[-1] > 50 if side == "long" else r_d1.iloc[-1] < 50


# ---------------------------------------------------------------------------
# 1) baseline — текущая боевая логика (EMA9/21 H1 cross + RSI H1 + RSI D1)
# ---------------------------------------------------------------------------

def variant_baseline(df_d1: pd.DataFrame, df_h4: pd.DataFrame) -> Tuple[Optional[Side], Optional[float], str]:
    if len(df_h4) < 22:
        return None, None, ""
    e9, e21 = ema(df_h4["close"], 9), ema(df_h4["close"], 21)
    r_h1 = rsi(df_h4["close"], 14)
    curr_e9, prev_e9 = e9.iloc[-1], e9.iloc[-2]
    curr_e21, prev_e21 = e21.iloc[-1], e21.iloc[-2]
    curr_rsi_h1 = r_h1.iloc[-1]
    entry = df_h4["close"].iloc[-1]

    if curr_e9 > curr_e21 and prev_e9 <= prev_e21 and curr_rsi_h1 > 50 and _d1_rsi_trend_ok(df_d1, "long"):
        return "long", entry, "ema9/21_cross_h1"
    if curr_e9 < curr_e21 and prev_e9 >= prev_e21 and curr_rsi_h1 < 50 and _d1_rsi_trend_ok(df_d1, "short"):
        return "short", entry, "ema9/21_cross_h1"
    return None, None, ""


# ---------------------------------------------------------------------------
# 2) fast_ema_5_13 — та же механика, быстрее пара (меньше запаздывание)
# ---------------------------------------------------------------------------

def variant_fast_ema(df_d1: pd.DataFrame, df_h4: pd.DataFrame) -> Tuple[Optional[Side], Optional[float], str]:
    if len(df_h4) < 14:
        return None, None, ""
    e5, e13 = ema(df_h4["close"], 5), ema(df_h4["close"], 13)
    r_h1 = rsi(df_h4["close"], 14)
    curr_e5, prev_e5 = e5.iloc[-1], e5.iloc[-2]
    curr_e13, prev_e13 = e13.iloc[-1], e13.iloc[-2]
    curr_rsi_h1 = r_h1.iloc[-1]
    entry = df_h4["close"].iloc[-1]

    if curr_e5 > curr_e13 and prev_e5 <= prev_e13 and curr_rsi_h1 > 50 and _d1_rsi_trend_ok(df_d1, "long"):
        return "long", entry, "ema5/13_cross_h1"
    if curr_e5 < curr_e13 and prev_e5 >= prev_e13 and curr_rsi_h1 < 50 and _d1_rsi_trend_ok(df_d1, "short"):
        return "short", entry, "ema5/13_cross_h1"
    return None, None, ""


# ---------------------------------------------------------------------------
# 3)/4) breakout_Nbar — пробой диапазона последних N H1-баров (Donchian)
# ---------------------------------------------------------------------------

def _breakout(df_d1: pd.DataFrame, df_h4: pd.DataFrame, n: int) -> Tuple[Optional[Side], Optional[float], str]:
    if len(df_h4) < n + 2:
        return None, None, ""
    highs = df_h4["high"]
    lows = df_h4["low"]
    closes = df_h4["close"]
    # диапазон ДО текущего бара (не включая его — иначе пробой самого себя)
    range_high = highs.iloc[-(n + 1):-1].max()
    range_low = lows.iloc[-(n + 1):-1].min()
    entry = closes.iloc[-1]

    if entry > range_high and _d1_rsi_trend_ok(df_d1, "long"):
        return "long", entry, f"breakout_{n}bar_high"
    if entry < range_low and _d1_rsi_trend_ok(df_d1, "short"):
        return "short", entry, f"breakout_{n}bar_low"
    return None, None, ""


def variant_breakout_10bar(df_d1: pd.DataFrame, df_h4: pd.DataFrame) -> Tuple[Optional[Side], Optional[float], str]:
    return _breakout(df_d1, df_h4, 10)


def variant_breakout_20bar(df_d1: pd.DataFrame, df_h4: pd.DataFrame) -> Tuple[Optional[Side], Optional[float], str]:
    return _breakout(df_d1, df_h4, 20)


# ---------------------------------------------------------------------------
# 5) price_above_ema9 — цена пересекает ОДНУ быструю EMA (раньше, шумнее)
# ---------------------------------------------------------------------------

def variant_price_above_ema9(df_d1: pd.DataFrame, df_h4: pd.DataFrame) -> Tuple[Optional[Side], Optional[float], str]:
    if len(df_h4) < 10:
        return None, None, ""
    e9 = ema(df_h4["close"], 9)
    closes = df_h4["close"]
    curr_close, prev_close = closes.iloc[-1], closes.iloc[-2]
    curr_e9, prev_e9 = e9.iloc[-1], e9.iloc[-2]

    if curr_close > curr_e9 and prev_close <= prev_e9 and _d1_rsi_trend_ok(df_d1, "long"):
        return "long", curr_close, "price_cross_above_ema9"
    if curr_close < curr_e9 and prev_close >= prev_e9 and _d1_rsi_trend_ok(df_d1, "short"):
        return "short", curr_close, "price_cross_below_ema9"
    return None, None, ""


# ---------------------------------------------------------------------------
# 6) macd_cross — MACD(12,26) пересекает сигнальную линию(9)
# ---------------------------------------------------------------------------

def variant_macd_cross(df_d1: pd.DataFrame, df_h4: pd.DataFrame) -> Tuple[Optional[Side], Optional[float], str]:
    if len(df_h4) < 35:
        return None, None, ""
    closes = df_h4["close"]
    macd_line = ema(closes, 12) - ema(closes, 26)
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    curr_macd, prev_macd = macd_line.iloc[-1], macd_line.iloc[-2]
    curr_sig, prev_sig = signal_line.iloc[-1], signal_line.iloc[-2]
    entry = closes.iloc[-1]

    if curr_macd > curr_sig and prev_macd <= prev_sig and _d1_rsi_trend_ok(df_d1, "long"):
        return "long", entry, "macd_cross_up"
    if curr_macd < curr_sig and prev_macd >= prev_sig and _d1_rsi_trend_ok(df_d1, "short"):
        return "short", entry, "macd_cross_down"
    return None, None, ""


# ---------------------------------------------------------------------------
# 7) breakout_10bar + объём — пробой диапазона, подтверждённый объёмом
# ---------------------------------------------------------------------------

def variant_breakout_10bar_volume(df_d1: pd.DataFrame, df_h4: pd.DataFrame) -> Tuple[Optional[Side], Optional[float], str]:
    side, entry, reason = _breakout(df_d1, df_h4, 10)
    if side is None:
        return None, None, ""
    if "volume" not in df_d1.columns or df_d1["volume"].empty:
        return side, entry, reason + "+vol"
    vol = df_d1["volume"]
    w = min(20, max(3, len(vol)))
    ma = vol.rolling(w, min_periods=1).mean()
    den = float(ma.iloc[-1]) if len(ma) else 0.0
    vol_ratio = float(vol.iloc[-1]) / den if den > 0 else 1.0
    if vol_ratio >= 1.0:
        return side, entry, reason + "+vol"
    return None, None, ""


ALL_ENTRY_VARIANTS = {
    "baseline": variant_baseline,
    "fast_ema_5_13": variant_fast_ema,
    "breakout_10bar": variant_breakout_10bar,
    "breakout_20bar": variant_breakout_20bar,
    "price_above_ema9": variant_price_above_ema9,
    "macd_cross": variant_macd_cross,
    "breakout_10bar_volume": variant_breakout_10bar_volume,
}
