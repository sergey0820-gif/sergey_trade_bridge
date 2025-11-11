import os
import csv
import logging
from datetime import datetime, timedelta, timezone

from tinkoff.invest import Client, CandleInterval
from tinkoff.invest.exceptions import RequestError
from dotenv import load_dotenv

# === Логирование ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)

# === Константы фильтрации ===
MIN_AVG_VOLUME_SHARE = 200_000
MIN_AVG_VOLUME_FUTURE = 2_000
MIN_ATR_PERCENT = 2.0

# === Инициализация ===
load_dotenv()
TOKEN = os.getenv("TINKOFF_TOKEN")
OUTPUT_CSV = "universe.csv"
DAYS = 7  # сокращаем период до 7, чтобы не было INVALID_ARGUMENT


# === Подсчёт ATR ===
def calculate_atr(candles):
    if not candles or len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        high = candles[i].high.units + candles[i].high.nano / 1e9
        low = candles[i].low.units + candles[i].low.nano / 1e9
        prev_close = candles[i - 1].close.units + candles[i - 1].close.nano / 1e9
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    avg_tr = sum(trs) / len(trs)
    first_candle = candles[0]
    close_price = first_candle.close.units + first_candle.close.nano / 1e9
    return (avg_tr / close_price) * 100 if close_price > 0 else 0.0


# === Сбор акций ===
def fetch_shares(client):
    instruments = client.instruments.shares().instruments
    from_time = datetime.now(timezone.utc) - timedelta(days=DAYS)
    to_time = datetime.now(timezone.utc)

    results = []
    for share in instruments:
        if not share.api_trade_available_flag or share.currency != "rub":
            continue
        if share.div_yield_flag:
            logging.info(f"⛔ {share.ticker}: дивиденды — исключено")
            continue

        try:
            candles = client.market_data.get_candles(
                figi=share.figi,
                interval=CandleInterval.CANDLE_INTERVAL_DAY,
                from_=from_time,
                to=to_time,
            )
        except RequestError as e:
            logging.warning(f"Ошибка при получении свечей для {share.ticker}: {e}")
            continue

        if not candles or not candles.candles or len(candles.candles) < 2:
            continue

        avg_volume = sum(c.volume for c in candles.candles) / len(candles.candles)
        if avg_volume < MIN_AVG_VOLUME_SHARE:
            logging.info(f"🔸 {share.ticker}: низкий объём {avg_volume:.0f}")
            continue

        atr = calculate_atr(candles.candles)
        if atr < MIN_ATR_PERCENT:
            logging.info(f"🔸 {share.ticker}: низкий ATR {atr:.2f}%")
            continue

        results.append(
            {
                "figi": share.figi,
                "ticker": share.ticker,
                "name": share.name,
                "avg_volume": round(avg_volume),
                "atr_percent": round(atr, 2),
                "asset_class": "share",
                "class_code": "TQBR",
            }
        )
    return results


# === Сбор фьючерсов ===
def fetch_futures(client):
    instruments = client.instruments.futures().instruments
    from_time = datetime.now(timezone.utc) - timedelta(days=DAYS)
    to_time = datetime.now(timezone.utc)

    results = []
    for fut in instruments:
        if not fut.api_trade_available_flag or fut.currency != "rub":
            continue

        try:
            candles = client.market_data.get_candles(
                figi=fut.figi,
                interval=CandleInterval.CANDLE_INTERVAL_DAY,
                from_=from_time,
                to=to_time,
            )
        except RequestError as e:
            logging.warning(f"Ошибка свечей для {fut.ticker}: {e}")
            continue

        if not candles or not candles.candles or len(candles.candles) < 2:
            continue

        avg_volume = sum(c.volume for c in candles.candles) / len(candles.candles)
        if avg_volume < MIN_AVG_VOLUME_FUTURE:
            logging.info(f"🔸 {fut.ticker}: низкий объём {avg_volume:.0f}")
            continue

        atr = calculate_atr(candles.candles)
        if atr < MIN_ATR_PERCENT:
            logging.info(f"🔸 {fut.ticker}: низкий ATR {atr:.2f}%")
            continue

        results.append(
            {
                "figi": fut.figi,
                "ticker": fut.ticker,
                "name": fut.name,
                "avg_volume": round(avg_volume),
                "atr_percent": round(atr, 2),
                "asset_class": "future",
                "class_code": "SPBFUT",
            }
        )
    return results


# === Основная функция ===
def main():
    logging.info("📊 Сканирование акций...")
    with Client(TOKEN) as client:
        shares = fetch_shares(client)
        logging.info("📉 Сканирование фьючерсов...")
        futures = fetch_futures(client)
        total = shares + futures

        if not total:
            logging.warning("Не найдено подходящих инструментов.")
            return

        with open(OUTPUT_CSV, "w", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "figi",
                    "ticker",
                    "name",
                    "avg_volume",
                    "atr_percent",
                    "asset_class",
                    "class_code",
                ],
            )
            writer.writeheader()
            writer.writerows(total)

        logging.info(f"✅ Сохранено: {len(total)} инструментов → {OUTPUT_CSV}")


# === Точка входа ===
if __name__ == "__main__":
    main()
