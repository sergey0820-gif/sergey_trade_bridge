#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trade_executor.py

Скрипт для входа в позицию (лонг/шорт) через Tinkoff Invest
и заполнения pending_stops.csv для последующей постановки SL/TP.

Интерфейс совместим с предыдущей версией:
  --ticker
  --class_code
  --side        (buy/long, sell/short)
  --entry
  --stop
  --target

Новые возможности:
  * Проверка торгового статуса через GetTradingStatus.
  * Если рыночные заявки запрещены, но лимитные разрешены — fallback на лимитную заявку.
  * Понятные логи по кодам ошибок 30042/30052 и др.
  * Режим --dry-run для безопасного теста.
"""

import argparse
import csv
import logging
import math
import os
import sys
from pathlib import Path
from typing import Optional
from uuid import uuid4

from tinkoff.invest import (
    Client,
    InvestError,
    MoneyValue,
    OrderDirection,
    OrderType,
    Quotation,
)
from tinkoff.invest.services import InstrumentsService, MarketDataService, OrdersService

# -------------------------
# Настройки логирования
# -------------------------

BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

LOG_FILE = LOGS_DIR / "trade_executor.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# -------------------------
# Утилиты для котировок
# -------------------------


def quotation_to_float(q: Quotation) -> float:
    return q.units + q.nano / 1_000_000_000


def float_to_quotation(value: float) -> Quotation:
    units = int(math.floor(value))
    nano = int(round((value - units) * 1_000_000_000))
    # Защита от переполнения nano после округления
    if nano >= 1_000_000_000:
        units += 1
        nano -= 1_000_000_000
    return Quotation(units=units, nano=nano)


def money_to_float(m: MoneyValue) -> float:
    return m.units + m.nano / 1_000_000_000


# -------------------------
# Поиск инструмента
# -------------------------


def find_instrument(
    instruments: InstrumentsService, ticker: str, class_code: str
):
    """
    Ищем инструмент по тикеру и class_code через find_instrument.
    Возвращаем объект-инструмент (share/future/etc.) либо падаем с логом.
    """
    res = instruments.find_instrument(query=ticker)
    if not res.instruments:
        logger.error("FindInstrument: ничего не найдено по тикеру %s", ticker)
        raise RuntimeError(f"Инструмент {ticker} не найден")

    # Фильтруем по class_code, если возможно
    candidates = [
        inst for inst in res.instruments if getattr(inst, "class_code", "") == class_code
    ]
    if not candidates:
        # Если по class_code нет, берём первый попавшийся, но логируем
        inst = res.instruments[0]
        logger.warning(
            "FindInstrument: по class_code=%s ничего не найдено, использую первый инструмент: "
            "ticker=%s, class_code=%s",
            class_code,
            getattr(inst, "ticker", "?"),
            getattr(inst, "class_code", "?"),
        )
        return inst

    if len(candidates) > 1:
        logger.warning(
            "FindInstrument: найдено несколько инструментов для %s/%s, беру первый",
            ticker,
            class_code,
        )

    return candidates[0]


# -------------------------
# Расчёт количества лотов
# -------------------------


def calc_quantity(
    *,
    entry: float,
    stop: float,
    lot: int,
    capital: float,
    risk_per_trade: float,
    side: str,
) -> int:
    """
    Расчёт количества лотов по риску:
      risk_amount = capital * risk_per_trade
      risk_per_lot = |entry - stop| * lot
      qty = floor(risk_amount / risk_per_lot)

    Если что-то не так — возвращаем хотя бы 1 лот.
    """

    price_diff = abs(entry - stop)
    if price_diff <= 0:
        logger.warning(
            "Некорректные уровни entry/stop (entry=%.4f, stop=%.4f), выставляю 1 лот.",
            entry,
            stop,
        )
        return 1

    risk_amount = capital * risk_per_trade
    risk_per_lot = price_diff * lot

    if risk_per_lot <= 0:
        logger.warning(
            "Некорректный риск на лот (diff=%.4f, lot=%s), выставляю 1 лот.",
            price_diff,
            lot,
        )
        return 1

    qty_float = risk_amount / risk_per_lot
    qty = int(qty_float)

    if qty < 1:
        logger.warning(
            "Расчётный размер позиции < 1 лота (qty=%.4f). Выставляю 1 лот.", qty_float
        )
        qty = 1

    return qty


# -------------------------
# Работа с торговым статусом
# -------------------------


def get_trading_status(
    market_data: MarketDataService, instrument_uid: str, ticker: str
):
    """
    Получаем статус торгов для инструмента:
      trading_status, market_order_available_flag, limit_order_available_flag, api_trade_available_flag

    В версии tinkoff-invest 0.2.0b59 метод принимает только instrument_id (UID),
    параметра instrument_id_type нет.
    """
    resp = market_data.get_trading_status(instrument_id=instrument_uid)
    logger.info(
        "TradingStatus %s: trading_status=%s, market_order_available=%s, "
        "limit_order_available=%s, api_trade_available=%s",
        ticker,
        resp.trading_status,
        resp.market_order_available_flag,
        resp.limit_order_available_flag,
        resp.api_trade_available_flag,
    )
    return resp


# -------------------------
# Постановка ордера (MARKET / LIMIT)
# -------------------------


def place_order_with_fallback(
    orders: OrdersService,
    market_data: MarketDataService,
    account_id: str,
    *,
    instrument_uid: str,
    ticker: str,
    class_code: str,
    direction: OrderDirection,
    qty: int,
    entry: float,
    stop: float,
    target: float,
    lot: int,
    instrument_type: str,
    slippage_pct: float,
    dry_run: bool,
):
    """
    1) Проверяем торговый статус через GetTradingStatus.
    2) Если market_order_available=True — ставим MARKET.
    3) Если только limit_order_available=True — ставим LIMIT по last_price ± slippage_pct.
    4) Обрабатываем InvestError с понятными логами по кодам.
    """

    status = get_trading_status(market_data, instrument_uid, ticker)

    if not status.api_trade_available_flag:
        logger.error(
            "PostOrder: торговля по API недоступна для %s (%s), api_trade_available_flag=False",
            ticker,
            class_code,
        )
        return None

    # Если dry-run — просто логируем и выходим перед отправкой ордера
    if dry_run:
        logger.info(
            "DRY-RUN: заявка НЕ отправлена. ticker=%s, class_code=%s, side=%s, qty=%s, entry=%.4f",
            ticker,
            class_code,
            "BUY" if direction == OrderDirection.ORDER_DIRECTION_BUY else "SELL",
            qty,
            entry,
        )
        return None

    order_id = str(uuid4())
    side_text = "Лонг" if direction == OrderDirection.ORDER_DIRECTION_BUY else "Шорт"

    def log_success(resp, entry_price_used: float, mode: str):
        total_value = entry_price_used * lot * qty
        logger.info(
            "✅ Вход выполнен (%s): <b>%s</b> (%s) — %s | Лотов: <b>%s</b>  Лотность: <code>%s</code> | "
            "Entry: <code>%.4f</code>  Stop: <code>%.4f</code>  Target: <code>%.4f</code> | "
            "Сумма (оценка): <b>%s</b> | order_id: <code>%s</code>",
            mode,
            ticker,
            class_code,
            side_text,
            qty,
            lot,
            entry_price_used,
            stop,
            target,
            f"{total_value:,.2f}",
            resp.order_id,
        )

    try:
        # --- Пытаемся MARKET, если можно ---
        if status.market_order_available_flag:
            resp = orders.post_order(
                account_id=account_id,
                instrument_id=instrument_uid,
                quantity=qty,
                direction=direction,
                order_type=OrderType.ORDER_TYPE_MARKET,
                order_id=order_id,
            )
            log_success(resp, entry, "MARKET")
            return resp

        # --- MARKET нельзя, пробуем LIMIT, если можно ---
        if status.limit_order_available_flag:
            last_prices = market_data.get_last_prices(instrument_id=[instrument_uid])
            if not last_prices.last_prices:
                logger.error(
                    "PostOrder: не удалось получить last_price для %s (%s), LIMIT не выставлен.",
                    ticker,
                    class_code,
                )
                return None

            last_price = quotation_to_float(last_prices.last_prices[0].price)
            if direction == OrderDirection.ORDER_DIRECTION_BUY:
                limit_price_value = last_price * (1 + slippage_pct)
            else:
                limit_price_value = last_price * (1 - slippage_pct)

            limit_price = float_to_quotation(limit_price_value)

            resp = orders.post_order(
                account_id=account_id,
                instrument_id=instrument_uid,
                quantity=qty,
                direction=direction,
                order_type=OrderType.ORDER_TYPE_LIMIT,
                price=limit_price,
                order_id=order_id,
            )
            log_success(resp, limit_price_value, "LIMIT")
            return resp

        # Ни market, ни limit не доступны
        logger.error(
            "PostOrder: ни рыночные, ни лимитные заявки недоступны для %s (%s). "
            "market_order_available_flag=%s, limit_order_available_flag=%s",
            ticker,
            class_code,
            status.market_order_available_flag,
            status.limit_order_available_flag,
        )
        return None

    except InvestError as e:
        # Пытаемся вытащить код и tracking_id
        code: Optional[str] = None
        tracking_id: Optional[str] = None
        if hasattr(e, "metadata") and isinstance(e.metadata, dict):
            code = e.metadata.get("code")
            tracking_id = e.metadata.get("tracking_id")

        logger.error(
            "FAILED to post order: %s (%s)", code or "UNKNOWN", tracking_id or "no_tracking_id"
        )

        # Более читаемые сообщения по основным кодам
        if code == "30042":
            logger.error(
                "❌ Недостаточно активов для маржинальной сделки (код 30042). "
                "Проверь маржинальные показатели счёта (GetMarginAttributes) или уменьшай размер позиции."
            )
        elif code == "30052":
            logger.error(
                "❌ Инструмент запрещён для торговли через API (код 30052). "
                "Торговля возможна только через приложение/терминал."
            )
        else:
            logger.error("Подробнее об ошибке: %s", e)

        return None


# -------------------------
# pending_stops.csv
# -------------------------


def append_pending_stop(
    csv_path: Path, ticker: str, class_code: str, stop_price: float, target_price: float
):
    """
    Добавляем строку в pending_stops.csv формата:
    ticker,class_code,stop_price,target_price
    """
    file_exists = csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["ticker", "class_code", "stop_price", "target_price"])
        writer.writerow([ticker, class_code, f"{stop_price:.4f}", f"{target_price:.4f}"])
    logger.info(
        "pending_stops.csv: добавлена строка %s,%s,stop=%.4f,target=%.4f",
        ticker,
        class_code,
        stop_price,
        target_price,
    )


# -------------------------
# main
# -------------------------


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Исполнитель сделок Sergey-Trade 2025")
    parser.add_argument("--ticker", required=True, help="Тикер инструмента (например, SBER)")
    parser.add_argument(
        "--class_code",
        required=True,
        help="Код класса инструмента (например, TQBR, SPBFUT)",
    )
    parser.add_argument(
        "--side",
        required=True,
        help="Направление сделки: buy/long или sell/short",
    )
    parser.add_argument(
        "--entry",
        required=True,
        type=float,
        help="Цена входа",
    )
    parser.add_argument(
        "--stop",
        required=True,
        type=float,
        help="StopLoss уровень",
    )
    parser.add_argument(
        "--target",
        required=True,
        type=float,
        help="TakeProfit уровень",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Не отправлять заявку, только расчёт и логирование (для теста вручную)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    ticker = args.ticker.upper()
    class_code = args.class_code.upper()
    side_raw = args.side.lower().strip()
    entry = float(args.entry)
    stop = float(args.stop)
    target = float(args.target)
    dry_run = args.dry_run

    # Направление сделки
    if side_raw in ("buy", "long", "l"):
        direction = OrderDirection.ORDER_DIRECTION_BUY
    elif side_raw in ("sell", "short", "s"):
        direction = OrderDirection.ORDER_DIRECTION_SELL
    else:
        logger.error("Неизвестное направление side=%s. Ожидается buy/long или sell/short.", side_raw)
        return 1

    # Переменные окружения
    token = os.environ.get("TINKOFF_TOKEN")
    account_id = os.environ.get("TINKOFF_ACCOUNT_ID")

    if not token or not account_id:
        logger.error("Не заданы TINKOFF_TOKEN или TINKOFF_ACCOUNT_ID в окружении.")
        return 1

    capital_str = os.environ.get("CAPITAL", "100000")
    risk_str = os.environ.get("RISK_PER_TRADE", "0.01")
    slippage_str = os.environ.get("ORDER_SLIPPAGE_PCT", "0.001")  # 0.1%

    try:
        capital = float(capital_str)
    except ValueError:
        logger.warning(
            "CAPITAL имеет некорректное значение (%s), использую 100000 по умолчанию.", capital_str
        )
        capital = 100000.0

    try:
        risk_per_trade = float(risk_str)
    except ValueError:
        logger.warning(
            "RISK_PER_TRADE имеет некорректное значение (%s), использую 0.01 по умолчанию.",
            risk_str,
        )
        risk_per_trade = 0.01

    try:
        slippage_pct = float(slippage_str)
    except ValueError:
        logger.warning(
            "ORDER_SLIPPAGE_PCT имеет некорректное значение (%s), использую 0.001 по умолчанию.",
            slippage_str,
        )
        slippage_pct = 0.001

    pending_stops_path = BASE_DIR / "pending_stops.csv"

    logger.info(
        "Запуск trade_executor: ticker=%s, class_code=%s, side=%s, entry=%.4f, stop=%.4f, target=%.4f, "
        "capital=%.2f, risk_per_trade=%.4f, slippage_pct=%.4f, dry_run=%s",
        ticker,
        class_code,
        side_raw,
        entry,
        stop,
        target,
        capital,
        risk_per_trade,
        slippage_pct,
        dry_run,
    )

    with Client(token) as client:
        instruments = client.instruments
        market_data = client.market_data
        orders = client.orders

        # 1) Находим инструмент
        inst = find_instrument(instruments, ticker, class_code)
        instrument_uid = inst.uid
        lot = getattr(inst, "lot", 1)
        instrument_type = getattr(inst, "instrument_type", "unknown")

        # Логируем тип инструмента
        if class_code.startswith("SPB") or class_code == "SPBFUT":
            logger.info("FutureBy")
        else:
            logger.info("ShareBy")

        # 2) Считаем количество лотов
        qty = calc_quantity(
            entry=entry,
            stop=stop,
            lot=lot,
            capital=capital,
            risk_per_trade=risk_per_trade,
            side=side_raw,
        )

        # 3) Выставляем заявку (MARKET или LIMIT) с fallback
        resp = place_order_with_fallback(
            orders=orders,
            market_data=market_data,
            account_id=account_id,
            instrument_uid=instrument_uid,
            ticker=ticker,
            class_code=class_code,
            direction=direction,
            qty=qty,
            entry=entry,
            stop=stop,
            target=target,
            lot=lot,
            instrument_type=instrument_type,
            slippage_pct=slippage_pct,
            dry_run=dry_run,
        )

        if resp is None:
            logger.error("Заявка не была выставлена, pending_stops.csv не обновлён.")
            return 1

        # 4) Добавляем строку в pending_stops.csv
        append_pending_stop(
            pending_stops_path,
            ticker=ticker,
            class_code=class_code,
            stop_price=stop,
            target_price=target,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

