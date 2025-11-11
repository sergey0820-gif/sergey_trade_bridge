import pandas as pd
import pandas_ta as ta
import logging


def analyze_ticker_live(df: pd.DataFrame, asset_class: str):
    try:
        if df.empty or len(df) < 10:
            return None

        rsi_d1 = ta.rsi(df["close"], length=14).iloc[-1]
        rsi_h4 = ta.rsi(df["close"], length=14).iloc[-2]

        volume = df["volume"].iloc[-1]
        volume_avg = df["volume"].rolling(window=5).mean().iloc[-2]
        volume_ratio = volume / volume_avg if volume_avg else 0

        # Упрощённый фильтр по RSI
        if not (30 < rsi_d1 < 70 and 30 < rsi_h4 < 70):
            return None

        # Упрощённый фильтр по объёму
        if volume_ratio < 0.8:
            return None

        # Паттерны
        last_candle = df.iloc[-1]
        prev_candle = df.iloc[-2]

        is_hammer = last_candle["high"] - last_candle["close"] > 2 * (
            last_candle["open"] - last_candle["low"]
        )
        is_engulfing = (
            (last_candle["open"] < last_candle["close"])
            and (prev_candle["open"] > prev_candle["close"])
            and (last_candle["open"] < prev_candle["close"])
            and (last_candle["close"] > prev_candle["open"])
        )

        pattern = None
        if is_hammer:
            pattern = "hammer"
        elif is_engulfing:
            pattern = "engulfing"
        else:
            return None  # Ни один паттерн не найден

        entry = df["close"].iloc[-1]
        stop = entry * 0.98
        target = entry * 1.04
        side = "buy" if rsi_d1 < 50 else "short"

        return {
            "side": side,
            "entry": round(entry, 2),
            "stop": round(stop, 2),
            "target": round(target, 2),
            "rsi_d1": round(rsi_d1, 2),
            "rsi_h4": round(rsi_h4, 2),
            "volume_ratio": round(volume_ratio, 2),
            "pattern": pattern,
        }
    except Exception as e:
        logging.warning(f"Ошибка в analyze_ticker_live(): {e}")
        return None
