"""
csv_helper.py — модуль для работы с файлом pending_stops.csv:
- чтение
- проверка обязательных колонок
- нормализация значений
"""

import os
import logging
import pandas as pd

PENDING_FILE = "pending_stops.csv"
REQUIRED_COLUMNS = ["ticker", "class_code", "stop_price", "target_price"]


def load_pending_stops(file_path: str = PENDING_FILE) -> pd.DataFrame:
    """
    Загружает CSV‑файл, проверяет наличие обязательных колонок,
    нормализует значения и возвращает DataFrame.
    Если файл не найден или структура неверная — возвращает пустой DataFrame.
    """
    if not os.path.exists(file_path):
        logging.error(f"Файл {file_path} не найден.")
        return pd.DataFrame()

    try:
        df = pd.read_csv(file_path)
    except Exception as e:
        logging.error(f"Ошибка чтения файла {file_path}: {e}")
        return pd.DataFrame()

    # Проверка наличия всех обязательных колонок
    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        logging.error(f"В файле {file_path} отсутствуют столбцы: {missing}")
        return pd.DataFrame()

    # Нормализация: заменяем NaN или None на пустую строку
    df = df.fillna("")

    # Приведение типов: stop_price и target_price → числа, если возможно
    for col in ["stop_price", "target_price"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def validate_row(row: pd.Series) -> bool:
    """
    Проверяет корректность одной строки:
    - ticker и class_code — непустые строки
    - stop_price и target_price — не NaN
    Возвращает True если строка валидна, иначе False.
    """
    ticker = str(row["ticker"]).strip()
    class_code = str(row["class_code"]).strip()
    stop_price = row["stop_price"]
    target_price = row["target_price"]

    if not ticker:
        logging.warning("Строка пропущена: пустой ticker.")
        return False
    if not class_code:
        logging.warning(f"Строка пропущена для {ticker}: пустой class_code.")
        return False
    if pd.isna(stop_price):
        logging.warning(f"Строка пропущена для {ticker}: stop_price не задан.")
        return False
    if pd.isna(target_price):
        logging.warning(f"Строка пропущена для {ticker}: target_price не задан.")
        return False

    return True
