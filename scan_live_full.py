#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
scan_live_full.py

Полный скан рынка по universe.csv:
- Для каждого тикера берём D1 и H1 свечи через Tinkoff Invest;
- Вызываем utils.ta.analyze_trade_setup(df_d1, df_h4);
- Если найден сетап (trend / reversal) с нормальным R:R → пишем в candidates.csv.

Структура candidates.csv:
ticker,class_code,side,entry,stop,target,rsi_d1,rsi_h4,volume_ratio,pattern,timestamp
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List

import pandas as pd
from dotenv import load_dotenv
from tinkoff.invest import CandleInterval, Client

from utils.ta import analyze_trade_setup

# ---------------------------------
# Логирование
# ---------------------------------

LOG_PATH = os.path.join("logs", "scan_live_full.log")
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ---------------------------------
# Вспомогательные функции
# ---------------------------------


def load_universe(path: str = "universe.csv", limit: int | None = None) -> pd.DataFrame:
    if not os.path.exists(path):
        raise FileNotFoundError(f"universe.csv не найден по пути: {path}")

    df = pd.read_csv(path)
    # ожидаем минимум: ticker, class_code
    required_cols = {"ticker", "class_code"}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"В {path} нет колонок: {missing}")

    if limit is not None:
        df = df.head(limit)
    return df


def candles_to_df(candles) -> pd.DataFrame:
    """
    Превращаем список свечей Tinkoff в DataFrame с колонками:
    time, open, high, low, close, volume
    """
    rows = []
    for c in candles:
        rows.append(
            {
                "time": c.time.replace(tzinfo=timezone.utc),
                "open": c.open.units + c.open.nano / 1e9,
                "high": c.high.units + c.high.nano / 1e9,
                "low": c.low.units + c.low.nano / 1e9,
                "close": c.close.units + c.close.nano / 1e9,
                "volume": c.volume,
            }
        )
    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df.sort_values("time", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def build_instrument_cache(client: Client) -> dict[tuple[str, str], str]:
    """
    Строим кэш (ticker, class_code) -> figi ОДИН раз за запуск, по акциям и фьючерсам.
    Раньше get_figi_by_ticker дергал client.instruments.shares() на каждый тикер
    и вообще не смотрел futures() — это и упирало в RESOURCE_EXHAUSTED, и роняло
    резолв всех SPBFUT-инструментов.
    """
    cache: dict[tuple[str, str], str] = {}
    try:
        for s in client.instruments.shares().instruments:
            cache[(s.ticker, s.class_code)] = s.figi
    except Exception as e:
        logger.error("Ошибка при загрузке списка акций: %s", e)
    try:
        for f in client.instruments.futures().instruments:
            cache[(f.ticker, f.class_code)] = f.figi
    except Exception as e:
        logger.error("Ошибка при загрузке списка фьючерсов: %s", e)
    return cache


def get_figi_by_ticker(
    client: Client,
    ticker: str,
    class_code: str,
    cache: dict[tuple[str, str], str] | None = None,
) -> str | None:
    """
    Ищем FIGI по тикеру/классу. Если передан cache (см. build_instrument_cache),
    используем его без обращений к API; иначе — старое поведение (один запрос shares()).
    """
    if cache is not None:
        return cache.get((ticker, class_code))

    try:
        shares = client.instruments.shares().instruments
        for s in shares:
            if s.ticker == ticker and s.class_code == class_code:
                return s.figi
        return None
    except Exception as e:
        logger.error(f"Ошибка при поиске FIGI для {ticker}: {e}")
        return None


def fetch_candles(
    client: Client,
    figi: str,
    interval: CandleInterval,
    days: int,
) -> List:
    """
    Загружаем свечи за указанное число дней.
    """
    now = datetime.now(timezone.utc)
    _from = now - timedelta(days=days)
    candles = client.market_data.get_candles(
        figi=figi,
        from_=_from,
        to=now,
        interval=interval,
    ).candles
    return candles


# ---------------------------------
# Основная логика сканирования
# ---------------------------------


def scan_market(mode: str, max_tickers: int | None, notify: bool = False) -> None:
    """
    Сканирует universe.csv и формирует candidates.csv по новой логике.
    """
    load_dotenv()
    token = os.getenv("TINKOFF_TOKEN")
    if not token:
        raise RuntimeError("Не задан TINKOFF_TOKEN в .env")

    universe = load_universe(limit=max_tickers)
    logger.info("Начало сканирования: тикеров в universe: %s", len(universe))

    out_rows = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # сколько дней истории берём
    if mode == "morning":
        days_d1 = 260  # ~1 год
        days_h4 = 60   # используем H1 вместо H4, но глубину оставим
    else:  # intraday / default
        days_d1 = 260
        days_h4 = 60

    with Client(token) as client:
        instrument_cache = build_instrument_cache(client)
        logger.info("Кэш инструментов построен: %s штук", len(instrument_cache))

        for idx, row in universe.iterrows():
            ticker = str(row["ticker"])
            class_code = str(row["class_code"])

            try:
                figi = get_figi_by_ticker(client, ticker, class_code, cache=instrument_cache)
                if not figi:
                    logger.warning("Пропускаю %s:%s — не найден FIGI", ticker, class_code)
                    continue

                d1_candles = fetch_candles(
                    client, figi, CandleInterval.CANDLE_INTERVAL_DAY, days_d1
                )
                h4_candles = fetch_candles(
                    client, figi, CandleInterval.CANDLE_INTERVAL_HOUR, days_h4
                )

                df_d1 = candles_to_df(d1_candles)
                df_h4 = candles_to_df(h4_candles)

                if df_d1.empty or df_h4.empty:
                    logger.info("Нет свечей для %s:%s, пропускаю", ticker, class_code)
                    continue

                # min_volume_ratio=1.0: требовать объём не ниже своей скользящей
                # средней — по бэктесту на 12 мес/60 тикерах это главный фактор,
                # поднявший экспектацию с +0.04R до +0.32R на сделку
                setup = analyze_trade_setup(df_d1, df_h4, min_volume_ratio=1.0)

                if not setup.side or not setup.entry or not setup.stop or not setup.target:
                    # сетап не найден / невалиден
                    continue

                if setup.side == "short" and class_code == "TQBR":
                    # STRATEGY.md п.1: шорт допустим только по фьючерсам,
                    # акции напрямую не шортим
                    logger.info(
                        "⛔ %s:%s short по акции — пропускаю (SHORT только для фьючерсов)",
                        ticker,
                        class_code,
                    )
                    continue

                pattern = setup.mode or "unknown"

                logger.info(
                    "✅ %s:%s %s entry=%.4f SL=%.4f TP=%.4f [%s]",
                    ticker,
                    class_code,
                    setup.side,
                    setup.entry,
                    setup.stop,
                    setup.target,
                    pattern,
                )

                out_rows.append(
                    [
                        ticker,
                        class_code,
                        setup.side,
                        f"{setup.entry:.4f}",
                        f"{setup.stop:.4f}",
                        f"{setup.target:.4f}",
                        f"{setup.rsi_d1:.2f}" if setup.rsi_d1 is not None else "",
                        f"{setup.rsi_h4:.2f}" if setup.rsi_h4 is not None else "",
                        f"{setup.volume_ratio_d1:.2f}"
                        if setup.volume_ratio_d1 is not None
                        else "",
                        pattern,
                        now_str,
                    ]
                )

            except Exception as e:
                logger.exception("Ошибка при обработке %s:%s: %s", ticker, class_code, e)
                continue

    # пишем candidates.csv
    out_path = "candidates.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "ticker",
                "class_code",
                "side",
                "entry",
                "stop",
                "target",
                "rsi_d1",
                "rsi_h4",
                "volume_ratio",
                "pattern",
                "timestamp",
            ]
        )
        writer.writerows(out_rows)

    logger.info("Сигналов найдено: %s", len(out_rows))
    logger.info("candidates.csv обновлён.")

    if notify:
        logger.info("notify=ON (отправка в Telegram реализуется через telegram_bridge)")


# ---------------------------------
# CLI
# ---------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sergey-Trade 2025 — full live scan")
    parser.add_argument(
        "--mode",
        choices=["morning", "intraday"],
        default="morning",
        help="режим сканирования (morning / intraday)",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help="максимум тикеров из universe.csv",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="флаг: добавить уведомление (фактически только в логах; Telegram — через отдельный скрипт)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logger.info(
        "Запуск scan_live_full.py mode=%s max=%s notify=%s",
        args.mode,
        args.max,
        args.notify,
    )
    scan_market(mode=args.mode, max_tickers=args.max, notify=args.notify)


if __name__ == "__main__":
    main()

