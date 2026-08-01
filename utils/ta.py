""" utils/ta.py
Обновленный модуль технического анализа для Sergey-Trade 2025.
Реализовано согласно Стратегии: EMA 9/21 + RSI 50.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Any, Literal, Tuple
import numpy as np
import pandas as pd

# Типы данных для удобства
Side = Literal["long", "short"]
Mode = Literal["trend", "pullback", "breakout"]

# === Параметры структурной цели (вместо фиксированного 3R) ===
SWING_LOOKBACK_DAYS = 90  # окно поиска swing high/low на D1
SWING_ORDER = 3  # фрактал: свинг должен быть экстремумом среди ±SWING_ORDER баров
MIN_TARGET_RR = 1.5  # минимальный R:R, чтобы взять структурный уровень как цель

@dataclass
class TradeSetup:
    mode: Optional[Mode]
    side: Optional[Side]
    entry: Optional[float]
    stop: Optional[float]
    target: Optional[float]
    rsi_d1: Optional[float]
    rsi_h4: Optional[float]  # По факту используется H1
    volume_ratio_d1: Optional[float]
    reason: str

# -----------------------------
# Индикаторы
# -----------------------------

def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()

def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    ema_up = up.ewm(com=period - 1, adjust=False).mean()
    ema_down = down.ewm(com=period - 1, adjust=False).mean()
    rs = ema_up / ema_down
    return 100 - (100 / (1 + rs))

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, 
                    (high - prev_close).abs(), 
                    (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

# -----------------------------
# Вспомогательные функции (утилиты)
# -----------------------------

def _volume_ratio(df: pd.DataFrame, window: int = 20) -> float:
    if "volume" not in df.columns or df["volume"].empty:
        return 1.0
    vol = df["volume"]
    w = min(window, max(3, len(vol)))
    ma = vol.rolling(w, min_periods=1).mean()
    den = float(ma.iloc[-1]) if len(ma) else 0.0
    if den <= 0: return 1.0
    return float(vol.iloc[-1]) / den

def _rr_ok(entry: float, stop: float, target: float, min_rr: float = 2.0) -> bool:
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0: return False
    return (reward / risk) >= min_rr


def _find_swing_levels(df: pd.DataFrame, side: Side, order: int = SWING_ORDER) -> list[float]:
    """
    Фракталы Вильямса: свинг-хай (для long) / свинг-лоу (для short) — экстремум
    среди ±order соседних баров. Последние `order` баров не могут быть
    подтверждены (не хватает данных справа) и не рассматриваются.
    """
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    n = len(df)
    levels: list[float] = []
    for i in range(order, n - order):
        if side == "long":
            window = highs[i - order : i + order + 1]
            if highs[i] == window.max():
                levels.append(float(highs[i]))
        else:
            window = lows[i - order : i + order + 1]
            if lows[i] == window.min():
                levels.append(float(lows[i]))
    return levels


def _structural_target(
    df_d1: pd.DataFrame, side: Side, entry: float, risk: float
) -> Tuple[float, str]:
    """
    Цель — ближайший swing high/low на D1 (за последние SWING_LOOKBACK_DAYS),
    который даёт R:R >= MIN_TARGET_RR. Если ближайший свинг даёт R:R ниже
    порога — ищем следующий, более дальний. Если ни один свинг в окне не
    подходит (или свингов вообще нет) — откатываемся на старую логику 3R.

    Возвращает (target_price, source), где source — "swing" или "fallback_3R".
    """
    lookback = df_d1.tail(SWING_LOOKBACK_DAYS) if len(df_d1) > SWING_LOOKBACK_DAYS else df_d1
    levels = _find_swing_levels(lookback, side)

    if side == "long":
        candidates = sorted(lvl for lvl in levels if lvl > entry)  # ближе -> дальше
    else:
        candidates = sorted((lvl for lvl in levels if lvl < entry), reverse=True)

    if risk > 0:
        for level in candidates:
            rr = abs(level - entry) / risk
            if rr >= MIN_TARGET_RR:
                return level, "swing"

    fallback = entry + (risk * 3) if side == "long" else entry - (risk * 3)
    return fallback, "fallback_3R"

# ---------------------------------
# ОСНОВНОЙ АНАЛИЗ (Стратегия 9/21)
# ---------------------------------

def analyze_trade_setup(df_d1: pd.DataFrame, df_h4: pd.DataFrame) -> TradeSetup:
    """
    Основная логика поиска сигналов:
    1. Тренд на D1 подтвержден RSI > 50 (для лонга) или RSI < 50 (для шорта).
    2. Вход на H1 при пересечении EMA 9 и EMA 21.
    """
    if df_d1.empty or df_h4.empty or len(df_h4) < 22:
        return TradeSetup(None, None, None, None, None, None, None, 1.0, "недостаточно данных")

    # Считаем индикаторы для H1 (точки входа)
    e9_h1 = ema(df_h4["close"], 9)
    e21_h1 = ema(df_h4["close"], 21)
    r_h1 = rsi(df_h4["close"], 14)
    a_h1 = atr(df_h4, 14)
    
    # Считаем фильтр тренда на D1
    r_d1 = rsi(df_d1["close"], 14)
    
    # Последние значения
    curr_e9, prev_e9 = e9_h1.iloc[-1], e9_h1.iloc[-2]
    curr_e21, prev_e21 = e21_h1.iloc[-1], e21_h1.iloc[-2]
    curr_rsi_h1 = r_h1.iloc[-1]
    curr_rsi_d1 = r_d1.iloc[-1]
    
    side: Optional[Side] = None
    reason = "сетап не найден"

    # Условие LONG: пересечение снизу вверх + фильтр RSI
    if curr_e9 > curr_e21 and prev_e9 <= prev_e21:
        if curr_rsi_h1 > 50 and curr_rsi_d1 > 50:
            side = "long"
            reason = "EMA 9/21 Cross Up + RSI > 50"

    # Условие SHORT: пересечение сверху вниз + фильтр RSI
    elif curr_e9 < curr_e21 and prev_e9 >= prev_e21:
        if curr_rsi_h1 < 50 and curr_rsi_d1 < 50:
            side = "short"
            reason = "EMA 9/21 Cross Down + RSI < 50"

    if not side:
        return TradeSetup(None, None, None, None, None, curr_rsi_d1, curr_rsi_h1, _volume_ratio(df_d1), reason)

    # Расчет цен
    entry = df_h4["close"].iloc[-1]
    atr_val = a_h1.iloc[-1]

    # Стоп-лосс на 2 ATR (дальше его подхватывает dynamic_stop_manager.py —
    # переводит в безубыток на +1R, трейлит с +2R)
    stop = entry - (atr_val * 2) if side == "long" else entry + (atr_val * 2)
    dist = abs(entry - stop)

    # Цель — ближайший структурный уровень (swing high/low на D1) с R:R >= 1.5,
    # иначе следующий более дальний свинг, иначе fallback на фиксированные 3R
    target, target_source = _structural_target(df_d1, side, entry, dist)
    reason = f"{reason} | target={target_source}"

    return TradeSetup(
        mode="trend",
        side=side,
        entry=entry,
        stop=stop,
        target=target,
        rsi_d1=curr_rsi_d1,
        rsi_h4=curr_rsi_h1,
        volume_ratio_d1=_volume_ratio(df_d1),
        reason=reason
    )

