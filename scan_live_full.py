# scan_live_full.py

import os
import pandas as pd
import logging
from datetime import datetime
from dotenv import load_dotenv
from utils.helpers import get_candles
from utils.ta import analyze_ticker_live

load_dotenv()

# Настройка логирования
LOG_FILE = "logs/scan_live_full.log"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)

def log(msg):
    print(msg)
    logging.info(msg)

def log_warning(msg):
    print(msg)
    logging.warning(msg)

def main():
    log("⚙️ Запуск scan_live_full.py")

    # Загрузка universe.csv
    universe_path = "universe.csv"
    if not os.path.exists(universe_path):
        log_warning("Файл universe.csv не найден")
        return

    universe = pd.read_csv(universe_path)
    if universe.empty:
        log_warning("Файл universe.csv пуст")
        return

    results = []

    for _, row in universe.iterrows():
        ticker = row["ticker"]
        class_code = row["class_code"]
        asset_class = row["asset_class"]

        log(f"🔍 Анализ {ticker} ({class_code})...")

        try:
            df = get_candles(
                ticker=ticker,
                class_code=class_code,
                interval="day",
                days=30  # ✅ передаём только один раз
            )
            if df is None or df.empty:
                log_warning(f"⚠️ Нет данных для {ticker}")
                continue

            result = analyze_ticker_live(df, asset_class)

            if result:
                result.update({
                    "ticker": ticker,
                    "class_code": class_code,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                results.append(result)

        except Exception as e:
            log_warning(f"⚠️ Ошибка при обработке {ticker}: {e}")

    # Сохраняем результаты
    if results:
        df_result = pd.DataFrame(results)
        df_result.to_csv("candidates.csv", index=False)
        log(f"✅ Сигналы сохранены: {len(df_result)} инструментов")
    else:
        log("❌ Сигналы не найдены")

if __name__ == "__main__":
    main()

