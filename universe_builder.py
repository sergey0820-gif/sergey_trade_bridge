import os
import csv
import logging
import time
from datetime import datetime, timedelta, timezone

from tinkoff.invest import Client, CandleInterval
from tinkoff.invest.exceptions import RequestError
from dotenv import load_dotenv

from config import COMMISSION_BPS_ROUNDTRIP

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# === Логирование ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)

# Отдельный alert-лог (не зависит от того, как cron перенаправляет
# stdout/stderr) — пишется явно из кода при исчерпании retry, чтобы
# сбой не остался незамеченным, если MAILTO="" и никто не смотрит логи.
ALERT_LOG_PATH = os.path.join(BASE_DIR, "logs", "universe_alerts.log")


def _write_alert(message: str) -> None:
    os.makedirs(os.path.dirname(ALERT_LOG_PATH), exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with open(ALERT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{ts} ERROR universe_builder: {message}\n")

# === Константы фильтрации ===
MIN_AVG_VOLUME_SHARE = 200_000
MIN_AVG_VOLUME_FUTURE = 2_000
MIN_ATR_PERCENT = 2.0
MIN_TURNOVER_RUB = 5_000_000  # понижено с 10 млн — по факту на 10 млн проходило
# 190 инструментов, на 5 млн — 241 (+51), без ухода в совсем неликвид (см. обсуждение)
DIVIDEND_CUTOFF_WINDOW_DAYS = 7  # STRATEGY.md п.1: исключать ±7 дней от отсечки
MAX_COMMISSION_BPS = 50  # STRATEGY.md п.1: комиссия > 0.5% (50 bps) — исключать
MIN_DAYS_TO_EXPIRATION = 15  # не торговать фьючерс, если до экспирации меньше

# Живая стратегия не торгует ничем крипто-привязанным на TQBR/SPBFUT (решение
# зафиксировано при отборе пула для склейки непрерывных фьючерсов). Раньше
# отсекалось только внешним запретом брокера ("Only for qualified investors"
# на BTU6, 2026-08-03) — не собственным фильтром, то есть переставало бы
# защищать при смене статуса/политики брокера. Ключевые слова подобраны по
# фактическому списку basicAsset/name на SPBFUT (проверено по полной выгрузке
# client.instruments.futures() 2026-08-12): "Индекс Bitcoin"/BTU6.../"Индекс
# Ethereum"/EHU6.../"ETHA" (Ethereum-ETF, ловится по name)/ETU6.../"Индекс
# Ripple"/XRU6.../"Индекс Solana"/S3U6.../"Bitcoin-фонд IBIT"/IBU6.../
# "BTCUSDT"+"SOLUSDT"+"XRPUSDT"+"TRXUSDT"+"ETHUSDT" (перпетуалы) — итого 11
# базовых активов, 49 тикеров на момент проверки. Список keyword-based (не
# точный список тикеров), чтобы ловить новые контракты той же природы
# автоматически, а не требовать ручного обновления на каждую новую серию.
CRYPTO_BASIC_ASSET_KEYWORDS = (
    "bitcoin", "ethereum", "ripple", "solana", "litecoin", "dogecoin",
    "crypto", "крипто", "биткоин", "эфириум", "usdt",
)


def is_crypto_linked(basic_asset: str, ticker: str = "", name: str = "") -> bool:
    haystack = f"{basic_asset} {ticker} {name}".lower()
    return any(kw in haystack for kw in CRYPTO_BASIC_ASSET_KEYWORDS)

# Retry для instruments.shares()/futures() — 2026-08-11: поймали
# перемежающийся self-signed cert в цепочке TLS у Tinkoff API (похоже на
# неполный роллаут их же фикса от 2026-08-03), который проходит за
# несколько минут. Раньше эти два вызова падали необработанным исключением
# и молча оставляли universe.csv вчерашним (MAILTO="" в crontab — ни один
# сбой не долетал до человека).
INSTRUMENTS_RETRY_ATTEMPTS = 5
INSTRUMENTS_RETRY_DELAY_SECONDS = 60


def _fetch_with_retry(fn, label: str, attempts: int = INSTRUMENTS_RETRY_ATTEMPTS,
                       delay: int = INSTRUMENTS_RETRY_DELAY_SECONDS):
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except RequestError as e:
            last_exc = e
            logging.warning(f"⚠️ {label}: попытка {attempt}/{attempts} не удалась: {e}")
            if attempt < attempts:
                time.sleep(delay)
    _write_alert(f"{label}: все {attempts} попыток исчерпаны (интервал {delay}с), последняя ошибка: {last_exc}")
    raise last_exc

# TODO: фильтр по датам публикации отчётности (±7 дней) из STRATEGY.md п.1
# НЕ реализован: client.instruments.get_asset_reports(...) падает с внутренней
# ошибкой SDK (tinkoff-investments==0.2.0b117) при любом варианте вызова
# (instrument_id kwarg -> TypeError; GetAssetReportsRequest(instrument_id=...)
# -> RequestError NOT_FOUND; по figi/uid -> AttributeError на 'seconds').
# Похоже на баг самого SDK, а не ошибку вызова — не пытаться "починить" наивно.
# Согласовано с владельцем: реализуем только дивидендный фильтр (см. ниже).

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


# === Проверка дивидендной отсечки в окне ±N дней ===
def has_dividend_cutoff_nearby(client, figi: str, ticker: str) -> bool:
    now = datetime.now(timezone.utc)
    window_from = now - timedelta(days=DIVIDEND_CUTOFF_WINDOW_DAYS)
    window_to = now + timedelta(days=DIVIDEND_CUTOFF_WINDOW_DAYS)
    try:
        resp = client.instruments.get_dividends(
            figi=figi, from_=window_from, to=window_to
        )
    except RequestError as e:
        logging.warning(f"Ошибка при получении дивидендов для {ticker}: {e}")
        return False

    for div in resp.dividends:
        cutoff = getattr(div, "last_buy_date", None)
        if cutoff and window_from <= cutoff <= window_to:
            return True
    return False


# === Сбор акций ===
def fetch_shares(client):
    instruments = _fetch_with_retry(
        lambda: client.instruments.shares().instruments, "client.instruments.shares()"
    )
    from_time = datetime.now(timezone.utc) - timedelta(days=DAYS)
    to_time = datetime.now(timezone.utc)

    results = []
    for share in instruments:
        if not share.api_trade_available_flag or share.currency != "rub":
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

        avg_close = sum(
            c.close.units + c.close.nano / 1e9 for c in candles.candles
        ) / len(candles.candles)
        turnover_rub = avg_volume * avg_close
        if turnover_rub < MIN_TURNOVER_RUB:
            logging.info(f"🔸 {share.ticker}: низкий оборот {turnover_rub:.0f} ₽")
            continue

        atr = calculate_atr(candles.candles)
        if atr < MIN_ATR_PERCENT:
            logging.info(f"🔸 {share.ticker}: низкий ATR {atr:.2f}%")
            continue

        if share.div_yield_flag and has_dividend_cutoff_nearby(
            client, share.figi, share.ticker
        ):
            logging.info(
                f"⛔ {share.ticker}: дивидендная отсечка в пределах "
                f"±{DIVIDEND_CUTOFF_WINDOW_DAYS} дней — исключено"
            )
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
    instruments = _fetch_with_retry(
        lambda: client.instruments.futures().instruments, "client.instruments.futures()"
    )
    from_time = datetime.now(timezone.utc) - timedelta(days=DAYS)
    to_time = datetime.now(timezone.utc)

    results = []
    for fut in instruments:
        if is_crypto_linked(fut.basic_asset, fut.ticker, fut.name):
            logging.info(f"🔸 {fut.ticker}: крипто-привязанный инструмент (basic_asset={fut.basic_asset!r}) — исключён по политике")
            continue

        if not fut.api_trade_available_flag or fut.currency != "rub":
            continue

        days_to_expiration = (fut.expiration_date - to_time).days
        if days_to_expiration < MIN_DAYS_TO_EXPIRATION:
            logging.info(
                f"🔸 {fut.ticker}: до экспирации {days_to_expiration} дн. "
                f"(< {MIN_DAYS_TO_EXPIRATION}) — тонкая ликвидность у ролла, пропуск"
            )
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

        avg_close = sum(
            c.close.units + c.close.nano / 1e9 for c in candles.candles
        ) / len(candles.candles)
        turnover_rub = avg_volume * avg_close
        if turnover_rub < MIN_TURNOVER_RUB:
            logging.info(f"🔸 {fut.ticker}: низкий оборот {turnover_rub:.0f} ₽")
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
def _universe_csv_age_str() -> str:
    if not os.path.exists(OUTPUT_CSV):
        return "universe.csv отсутствует вообще"
    age_hours = (time.time() - os.path.getmtime(OUTPUT_CSV)) / 3600
    return f"текущий universe.csv не обновлён, возраст ~{age_hours:.1f}ч"


def main() -> int:
    if COMMISSION_BPS_ROUNDTRIP > MAX_COMMISSION_BPS:
        logging.warning(
            f"⚠️ Комиссия по тарифу ({COMMISSION_BPS_ROUNDTRIP} bps) превышает "
            f"лимит {MAX_COMMISSION_BPS} bps (0.5%) — universe не собирается."
        )
        return 0

    try:
        logging.info("📊 Сканирование акций...")
        with Client(TOKEN) as client:
            shares = fetch_shares(client)
            logging.info("📉 Сканирование фьючерсов...")
            futures = fetch_futures(client)
    except RequestError as e:
        msg = f"instruments.shares()/futures() не удались после retry — {_universe_csv_age_str()}. Ошибка: {e}"
        logging.error(f"❌ {msg}")
        _write_alert(msg)
        return 1

    total = shares + futures
    if not total:
        msg = f"Не найдено подходящих инструментов — {_universe_csv_age_str()}"
        logging.warning(f"⚠️ {msg}")
        _write_alert(msg)
        return 1

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
    return 0


# === Точка входа ===
if __name__ == "__main__":
    import sys
    sys.exit(main())
