#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trade_executor.py

Скрипт для входа в позицию (лонг/шорт) через Tinkoff Invest
и заполнения pending_stops.csv для последующей постановки SL/TP.

Интерфейс:
  --ticker
  --class_code
  --side        (buy/long, sell/short)
  --entry
  --stop
  --target
  [--qty]       (необязательно, если не указан — считается от риска)
  [--dry-run]   (только расчёты и запись в логи, без реального ордера)

Возможности:
  * Расчёт направления сделки и объёма позиции.
  * Нормализация уровней: для лонга всегда SL < entry < TP,
    для шорта всегда TP < entry < SL (даже если входные числа перепутаны).
  * Запись уровней в pending_stops.csv в формате:
        ticker,class_code,stop_price,target_price
  * Подробное логирование в logs/trade_executor.log.
"""

import argparse
import csv
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from dotenv import load_dotenv
from tinkoff.invest import (
    Client,
    InvestError,
    MoneyValue,
    OrderDirection,
    OrderType,
    Quotation,
)
from tinkoff.invest.services import InstrumentsService, OrdersService

# -------------------------
# Настройки путей и логов
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

PENDING_STOPS_FILE = BASE_DIR / "pending_stops.csv"
REJECTED_DIR = BASE_DIR / "orders" / "rejected"

# -------------------------
# Загрузка .env
# -------------------------

ENV_PATH = BASE_DIR / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    load_dotenv()

TINKOFF_TOKEN = os.getenv("TINKOFF_TOKEN")
TINKOFF_ACCOUNT_ID = os.getenv("TINKOFF_ACCOUNT_ID")
CAPITAL_STR = os.getenv("CAPITAL", "0")
RISK_PER_TRADE_STR = os.getenv("RISK_PER_TRADE", "0.01")

try:
    CAPITAL = float(CAPITAL_STR)
except ValueError:
    CAPITAL = 0.0
    logger.warning("CAPITAL в .env не число: %s", CAPITAL_STR)

try:
    RISK_PER_TRADE = float(RISK_PER_TRADE_STR)
except ValueError:
    RISK_PER_TRADE = 0.01
    logger.warning("RISK_PER_TRADE в .env не число: %s", RISK_PER_TRADE_STR)

# STRATEGY.md п.2: авто-выбор market/limit по отклонению цены от сигнала.
# Полный порог — тот же, что уже используется postprocess_candidates.py для
# отсева протухших сигналов; порог для "бить рынком" — вдвое строже по умолчанию.
ENTRY_DEVIATION_MAX_PCT = float(os.getenv("ENTRY_DEVIATION_MAX_PCT", "0.5"))
AUTO_MARKET_MAX_DEV_PCT = float(
    os.getenv("AUTO_MARKET_MAX_DEV_PCT", str(ENTRY_DEVIATION_MAX_PCT / 2))
)


# -------------------------
# Утилиты для котировок
# -------------------------


def quotation_to_float(q: Quotation) -> float:
    """Перевод Quotation -> float."""
    return q.units + q.nano / 1_000_000_000


def float_to_quotation(value: float) -> Quotation:
    """Перевод float -> Quotation с адекватной нормализацией nano."""
    units = int(math.floor(value))
    nano = int(round((value - units) * 1_000_000_000))
    if nano >= 1_000_000_000:
        units += 1
        nano -= 1_000_000_000
    return Quotation(units=units, nano=nano)


# -------------------------
# Поиск инструмента
# -------------------------


def find_instrument(
    instruments: InstrumentsService,
    ticker: str,
    class_code: str,
) -> Optional[object]:
    """
    Унифицированный поиск инструмента по ticker+class_code.

    Возвращает объект инструмента (share/future), либо None.
    Мы не завязываемся на конкретный тип, дальше используем:
      .uid, .figi, .lot, .name
    если они есть.
    """
    ticker = ticker.strip().upper()
    class_code = class_code.strip().upper()

    # Простая эвристика: фьючерсы — на SPBFUT, остальное считаем акциями.
    is_future = class_code.startswith("SPB") or class_code.endswith("FUT") or class_code.endswith("Z5") or class_code.endswith("H6")

    try:
        if is_future:
            # Получаем список фьючерсов один раз и ищем по тикеру
            futures = instruments.futures().instruments
            for f in futures:
                if f.ticker.upper() == ticker:
                    return f
        else:
            shares = instruments.shares().instruments
            for s in shares:
                if s.ticker.upper() == ticker and s.class_code.upper() == class_code:
                    return s
    except InvestError as e:
        logger.error("Ошибка instruments при поиске %s (%s): %s", ticker, class_code, e)
        return None
    except Exception as e:
        logger.exception("Неожиданная ошибка при поиске инструмента %s (%s): %s", ticker, class_code, e)
        return None

    logger.warning("Инструмент не найден: %s (%s)", ticker, class_code)
    return None


# -------------------------
# Проверка уже открытой позиции
# -------------------------


def has_open_position(client: Client, account_id: str, instrument_uid: str) -> bool:
    """
    Проверка через client.operations.get_positions (тот же вызов, что
    positions_guard.py/stop_manager.py используют для построения карты позиций).
    Возвращает True, если по instrument_uid уже есть ненулевой баланс.
    """
    try:
        resp = client.operations.get_positions(account_id=account_id)
    except Exception as e:
        logger.exception("Не удалось получить позиции для проверки открытых: %s", e)
        # Не блокируем вход из-за временной ошибки API — но громко предупреждаем.
        return False

    for sec in resp.securities:
        if sec.instrument_uid == instrument_uid and int(sec.balance) != 0:
            return True
    for fut in resp.futures:
        if fut.instrument_uid == instrument_uid and int(fut.balance) != 0:
            return True
    return False


def write_rejected_order(
    ticker: str,
    class_code: str,
    side: str,
    entry: float,
    stop_price: float,
    target_price: float,
    risk_pct: float,
    reason: str,
) -> None:
    """
    Записывает отказ в orders/rejected/ в формате, уже используемом в репо
    (см. orders/rejected/*.json): ts, ticker, class, direction, entry, stop,
    target, risk_pct — плюс поле reason для диагностики.
    """
    REJECTED_DIR.mkdir(parents=True, exist_ok=True)
    fname = REJECTED_DIR / f"{ticker}_{int(time.time())}.json"
    data = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ticker": ticker,
        "class": "future" if class_code.strip().upper() == "SPBFUT" else "stock",
        "direction": "long" if side.lower() in ("long", "buy") else "short",
        "entry": entry,
        "stop": stop_price,
        "target": target_price,
        "risk_pct": risk_pct,
        "reason": reason,
    }
    try:
        fname.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        logger.info("Записан отказ во входе: %s (%s)", fname, reason)
    except Exception as e:
        logger.exception("Не удалось записать файл отказа %s: %s", fname, e)


# -------------------------
# Немедленная постановка стопа после входа
# -------------------------


def place_immediate_stops(
    ticker: str,
    class_code: str,
    side: str,
    quantity_lots: int,
    stop_price: float,
    target_price: float,
) -> None:
    """
    STRATEGY.md п.3: ставим SL/TP сразу после исполнения входа, не дожидаясь
    отдельного периодического stop_manager.py. stop_manager.py дедуплицирует
    по активным стоп-заявкам (get_active_stop_uids), поэтому задвоения не
    будет: если эта немедленная постановка не удалась, запись в
    pending_stops.csv остаётся резервным механизмом.
    """
    from trade_utils.price_helper import place_stop_order

    direction = "SELL" if side.lower() in ("long", "buy") else "BUY"

    for stop_type, price in (("STOP_LOSS", stop_price), ("TAKE_PROFIT", target_price)):
        try:
            place_stop_order(
                ticker=ticker,
                class_code=class_code,
                quantity_lots=quantity_lots,
                direction=direction,
                stop_order_type=stop_type,
                stop_price=price,
            )
            logger.info("Немедленный %s выставлен для %s (%s)", stop_type, ticker, class_code)
        except Exception as e:
            logger.warning(
                "Не удалось немедленно выставить %s для %s (%s): %s "
                "(резерв — stop_manager.py по pending_stops.csv)",
                stop_type,
                ticker,
                class_code,
                e,
            )


# -------------------------
# Авто-выбор типа ордера (market/limit)
# -------------------------


def decide_order_type(
    market_data,
    instrument,
    entry: float,
    requested_type: str,
) -> tuple[Optional[str], Optional[float]]:
    """
    Возвращает (order_type, limit_price).
    order_type: "market" | "limit" | None (None => сигнал протух, не входить).
    """
    if requested_type in ("market", "limit"):
        return requested_type, entry if requested_type == "limit" else None

    # requested_type == "auto"
    try:
        resp = market_data.get_last_prices(figi=[instrument.figi])
        last_price = quotation_to_float(resp.last_prices[0].price)
    except Exception as e:
        logger.error(
            "Не удалось получить текущую цену для авто-выбора типа ордера, "
            "бью рынком по умолчанию: %s",
            e,
        )
        return "market", None

    dev_pct = abs(last_price / entry - 1.0) * 100.0 if entry else 0.0
    logger.info(
        "Авто-выбор типа ордера: last=%.4f entry=%.4f dev=%.3f%% "
        "(market<=%.3f%%, limit<=%.3f%%)",
        last_price,
        entry,
        dev_pct,
        AUTO_MARKET_MAX_DEV_PCT,
        ENTRY_DEVIATION_MAX_PCT,
    )

    if dev_pct <= AUTO_MARKET_MAX_DEV_PCT:
        return "market", None
    if dev_pct <= ENTRY_DEVIATION_MAX_PCT:
        return "limit", entry

    logger.warning(
        "Сигнал протух: отклонение %.3f%% больше допустимого %.3f%% — вход отменён",
        dev_pct,
        ENTRY_DEVIATION_MAX_PCT,
    )
    return None, None


# -------------------------
# Нормализация стопа и тейка
# -------------------------


def normalize_stops_for_side(
    side: str,
    entry: float,
    stop_raw: float,
    target_raw: float,
) -> tuple[float, float]:
    """
    Нормализует стоп и тейк под сторону сделки.

    ЛОНГ:
        SL < entry < TP
    ШОРТ:
        TP < entry < SL

    Если входные значения перепутаны (например, stop выше entry),
    функция расставит их в правильном порядке относительно entry.
    """
    side_l = side.lower()

    prices = sorted([entry, stop_raw, target_raw])

    if side_l in ("long", "buy"):
        # ЛОНГ: SL = самая нижняя, TP = самая верхняя
        stop_price = prices[0]
        target_price = prices[-1]
    elif side_l in ("short", "sell"):
        # ШОРТ: SL = самая верхняя, TP = самая нижняя
        stop_price = prices[-1]
        target_price = prices[0]
    else:
        # Fallback — ничего не нормализуем, но логируем
        logger.warning(
            "Неожиданная side=%s, использую сырые уровни stop=%s, target=%s",
            side,
            stop_raw,
            target_raw,
        )
        stop_price = stop_raw
        target_price = target_raw

    logger.info(
        "Нормализация стопов: side=%s entry=%.4f stop_raw=%.4f target_raw=%.4f => SL=%.4f TP=%.4f",
        side,
        entry,
        stop_raw,
        target_raw,
        stop_price,
        target_price,
    )
    return stop_price, target_price


# -------------------------
# Работа с pending_stops.csv
# -------------------------


def write_pending_stop_row(
    ticker: str,
    class_code: str,
    stop_price: float,
    target_price: float,
) -> None:
    """
    Запись (обновление) строки в pending_stops.csv.

    Формат файла:
        ticker,class_code,stop_price,target_price

    На каждый тикер + class_code — одна строка:
    старые записи по тому же инструменту перезаписываются.
    """
    rows: list[dict] = []

    if PENDING_STOPS_FILE.exists():
        try:
            with PENDING_STOPS_FILE.open("r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if (
                        row.get("ticker", "").strip().upper() == ticker.strip().upper()
                        and row.get("class_code", "").strip().upper() == class_code.strip().upper()
                    ):
                        # Пропускаем старую запись по этому инструменту
                        continue
                    rows.append(row)
        except Exception as e:
            logger.error("Ошибка чтения %s: %s", PENDING_STOPS_FILE, e)

    # Добавляем актуальную строку
    rows.append(
        {
            "ticker": ticker.strip().upper(),
            "class_code": class_code.strip().upper(),
            "stop_price": f"{stop_price:.6f}",
            "target_price": f"{target_price:.6f}",
        }
    )

    # Перезаписываем файл
    try:
        with PENDING_STOPS_FILE.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["ticker", "class_code", "stop_price", "target_price"],
            )
            writer.writeheader()
            writer.writerows(rows)
        logger.info(
            "pending_stops.csv обновлён: %s (%s) SL=%.4f TP=%.4f",
            ticker,
            class_code,
            stop_price,
            target_price,
        )
    except Exception as e:
        logger.exception("Не удалось записать %s: %s", PENDING_STOPS_FILE, e)


# -------------------------
# Размещение ордера
# -------------------------


def place_order(
    orders: OrdersService,
    account_id: str,
    instrument,
    side: str,
    quantity: int,
    entry_price: float,
    order_type: str = "market",
    limit_price: Optional[float] = None,
    dry_run: bool = False,
) -> tuple[Optional[str], int]:
    """
    Размещение ордера (market или limit).
    Возвращает (order_id, lots_executed). При dry-run — (order_id, quantity).
    При ошибке — (None, 0).
    """
    side_l = side.lower()
    if side_l in ("long", "buy"):
        direction = OrderDirection.ORDER_DIRECTION_BUY
    elif side_l in ("short", "sell"):
        direction = OrderDirection.ORDER_DIRECTION_SELL
    else:
        raise ValueError(f"Неизвестная сторона сделки: {side}")

    tinkoff_order_type = (
        OrderType.ORDER_TYPE_LIMIT if order_type == "limit" else OrderType.ORDER_TYPE_MARKET
    )

    order_id = str(uuid4())

    logger.info(
        "Размещение ордера: %s %s x %s type=%s price=%s (dry_run=%s)",
        "BUY" if direction == OrderDirection.ORDER_DIRECTION_BUY else "SELL",
        quantity,
        getattr(instrument, "ticker", "UNKNOWN"),
        order_type,
        limit_price if limit_price is not None else entry_price,
        dry_run,
    )

    if dry_run:
        logger.info("dry-run режим: ордер не отправляется в Tinkoff.")
        return order_id, quantity

    try:
        call_kwargs = dict(
            instrument_id=getattr(instrument, "uid", instrument.figi),
            quantity=quantity,
            direction=direction,
            order_type=tinkoff_order_type,
            account_id=account_id,
            order_id=order_id,
        )
        if tinkoff_order_type == OrderType.ORDER_TYPE_LIMIT:
            call_kwargs["price"] = float_to_quotation(
                limit_price if limit_price is not None else entry_price
            )

        resp = orders.post_order(**call_kwargs)
        logger.info(
            "Ордер отправлен: order_id=%s, executed_order_price=%s, lots_executed=%s",
            resp.order_id,
            resp.executed_order_price,
            resp.lots_executed,
        )
        return resp.order_id, resp.lots_executed
    except InvestError as e:
        logger.error("InvestError при размещении ордера: %s", e)
        return None, 0
    except Exception as e:
        logger.exception("Неожиданная ошибка при размещении ордера: %s", e)
        return None, 0


# -------------------------
# Расчёт количества лотов
# -------------------------


def calc_quantity_from_risk(
    entry: float,
    stop_price: float,
    lot_size: int,
    capital: float,
    risk_per_trade: float,
) -> int:
    """
    Расчёт количества лотов от риска:
        риск_в_руб = capital * risk_per_trade
        R = |entry - stop_price|
        стоимость_лота ≈ entry * lot_size
        qty_lots = риск_в_руб / (R * lot_size)
    """
    entry = float(entry)
    stop_price = float(stop_price)
    R = abs(entry - stop_price)

    if R <= 0 or lot_size <= 0:
        logger.warning(
            "Невозможно посчитать объём от риска (R=%.6f, lot_size=%s), возвращаю 1 лот",
            R,
            lot_size,
        )
        return 1

    risk_rub = capital * risk_per_trade
    if risk_rub <= 0:
        logger.warning(
            "CAPITAL или RISK_PER_TRADE некорректны (capital=%.2f, risk=%.4f), возвращаю 1 лот",
            capital,
            risk_per_trade,
        )
        return 1

    # Сколько рублей теряем на 1 лот до стопа
    rub_per_lot = R * lot_size
    qty_float = risk_rub / rub_per_lot
    qty = max(1, int(qty_float))

    logger.info(
        "Расчёт объёма: capital=%.2f risk=%.4f R=%.4f lot_size=%s => qty≈%.2f -> %s лотов",
        capital,
        risk_per_trade,
        R,
        lot_size,
        qty_float,
        qty,
    )
    return qty


# -------------------------
# CLI и main()
# -------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Executor сделки через Tinkoff Invest")

    parser.add_argument("--ticker", required=True, help="Тикер инструмента (например, SBER)")
    parser.add_argument("--class_code", required=True, help="Класс (TQBR, SPBFUT и т.п.)")
    parser.add_argument(
        "--side",
        required=True,
        help="Сторона: long/buy или short/sell",
    )
    parser.add_argument("--entry", required=True, type=float, help="Цена входа")
    parser.add_argument("--stop", required=True, type=float, help="Исходный уровень стоп-лосса")
    parser.add_argument("--target", required=True, type=float, help="Исходный уровень тейк-профита")
    parser.add_argument(
        "--qty",
        type=int,
        default=None,
        help="Количество лотов (если не задано — считается от риска)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Тестовый запуск без отправки ордеров в Tinkoff",
    )
    parser.add_argument(
        "--order-type",
        choices=["market", "limit", "auto"],
        default="auto",
        help="Тип ордера: market, limit (по --entry) или auto (по отклонению цены)",
    )

    return parser.parse_args()


def main() -> None:
    if not TINKOFF_TOKEN or not TINKOFF_ACCOUNT_ID:
        logger.error("Не заданы TINKOFF_TOKEN или TINKOFF_ACCOUNT_ID в .env")
        sys.exit(1)

    args = parse_args()

    ticker = args.ticker.strip().upper()
    class_code = args.class_code.strip().upper()
    side = args.side.strip()
    entry = float(args.entry)
    stop_raw = float(args.stop)
    target_raw = float(args.target)
    dry_run = bool(args.dry_run)

    # Нормализуем стоп и тейк под сторону (SL/TP)
    stop_price, target_price = normalize_stops_for_side(
        side=side,
        entry=entry,
        stop_raw=stop_raw,
        target_raw=target_raw,
    )

    with Client(TINKOFF_TOKEN) as client:
        instruments = client.instruments
        orders = client.orders
        market_data = client.market_data

        instrument = find_instrument(instruments, ticker, class_code)
        if instrument is None:
            logger.error("Инструмент не найден, сделка прервана.")
            sys.exit(1)

        instrument_uid = getattr(instrument, "uid", None)
        if instrument_uid and has_open_position(client, TINKOFF_ACCOUNT_ID, instrument_uid):
            logger.error(
                "Уже есть открытая позиция по %s (%s) — вход отменён.", ticker, class_code
            )
            write_rejected_order(
                ticker=ticker,
                class_code=class_code,
                side=side,
                entry=entry,
                stop_price=stop_price,
                target_price=target_price,
                risk_pct=RISK_PER_TRADE * 100,
                reason="already_open_position",
            )
            sys.exit(1)

        lot_size = getattr(instrument, "lot", 1) or 1

        if args.qty is not None and args.qty > 0:
            quantity = int(args.qty)
            logger.info("Использую qty из аргумента: %s лотов", quantity)
        else:
            quantity = calc_quantity_from_risk(
                entry=entry,
                stop_price=stop_price,
                lot_size=lot_size,
                capital=CAPITAL,
                risk_per_trade=RISK_PER_TRADE,
            )

        order_choice, limit_price = decide_order_type(
            market_data=market_data,
            instrument=instrument,
            entry=entry,
            requested_type=args.order_type,
        )
        if order_choice is None:
            logger.error("Вход отменён: сигнал протух (превышено допустимое отклонение цены).")
            sys.exit(2)

        order_id, lots_executed = place_order(
            orders=orders,
            account_id=TINKOFF_ACCOUNT_ID,
            instrument=instrument,
            side=side,
            quantity=quantity,
            entry_price=entry,
            order_type=order_choice,
            limit_price=limit_price,
            dry_run=dry_run,
        )

        if order_id is None and not dry_run:
            logger.error("Ордер не был размещён, pending_stops записывать не буду.")
            sys.exit(1)

        if order_id is not None and not dry_run:
            place_immediate_stops(
                ticker=ticker,
                class_code=class_code,
                side=side,
                quantity_lots=lots_executed or quantity,
                stop_price=stop_price,
                target_price=target_price,
            )

    # Записываем/обновляем pending_stops.csv уже после успешного (или dry-run) размещения
    write_pending_stop_row(
        ticker=ticker,
        class_code=class_code,
        stop_price=stop_price,
        target_price=target_price,
    )

    logger.info(
        "trade_executor завершён: %s (%s) side=%s entry=%.4f SL=%.4f TP=%.4f qty=%s dry_run=%s",
        ticker,
        class_code,
        side,
        entry,
        stop_price,
        target_price,
        quantity,
        dry_run,
    )


if __name__ == "__main__":
    main()

