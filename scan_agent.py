#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Scan Agent (v2) — динамический перебор по universe.csv
- Анализ H4 и D1
- RSI, объёмы, паттерны
- Подтверждённые сигналы пишутся в candidates.csv
"""

import os
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv

from utils.ta import analyze_ticker
from utils.helpers import read_universe, get_next_symbols, get_price_data

load_dotenv(".env")

# === Константы ===
UNIVERSE_CSV = "universe.csv"
CANDIDATES_CSV = "candidates.csv"
MAX_SYMBOLS_PER_CYCLE = int(os.getenv("MAX_SYMBOLS_PER_CYCLE", 100))
TTL_MINUTES = 30

# === Инициализация ===
os.makedirs("state", exist_ok=True)
os.makedirs("logs", exist_ok=True)

# === Загрузка universe ===
universe = read_universe(UNIVERSE_CSV)

if not universe:
    print("❌ Universe пуст — завершение")
    exit(1)

symbols = get_next_symbols(universe, MAX_SYMBOLS_PER_CYCLE)
print(f"📦 Обработка {len(symbols)} тикеров")

# === Загрузка предыдущих кандидатов ===
if os.path.exists(CANDIDATES_CSV):
    prev = pd.read_csv(CANDIDATES_CSV)
    prev_time = datetime.fromtimestamp(os.path.getmtime(CANDIDATES_CSV))
    ttl = datetime.utcnow() - timedelta(minutes=TTL_MINUTES)
    if prev_time > ttl:
        candidates = prev
    else:
        candidates = pd.DataFrame()
else:
    candidates = pd.DataFrame()

# === Обработка тикеров ===
new_signals = []

for sym in symbols:
    asset_class = sym.get("asset_class", "share")
    symbol = sym["ticker"]
    try:
        df = get_price_data(symbol, interval="H4", days=30)
        result = analyze_ticker(df, asset_class)

        if result:
            print(f"✅ Сигнал по {symbol}: {result}")
            row = {
                "symbol": symbol,
                "rsi": result["rsi"],
                "volume_ratio": result["volume_ratio"],
                "pattern": result["pattern"],
                "timestamp": datetime.utcnow().isoformat(),
            }
            new_signals.append(row)
    except Exception as e:
        print(f"⚠️ Ошибка при обработке {symbol}: {e}")

# === Объединение и сохранение ===
if new_signals:
    df_new = pd.DataFrame(new_signals)
    if not candidates.empty:
        candidates = pd.concat([candidates, df_new], ignore_index=True)
    else:
        candidates = df_new

    candidates.to_csv(CANDIDATES_CSV, index=False)
    print(f"💾 Сохранено {len(new_signals)} новых сигналов")
else:
    print("ℹ️ Новых сигналов нет")
