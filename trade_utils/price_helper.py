# -*- coding: utf-8 -*-
"""
price_helper.py — утилиты работы с ценами и стоп-заявками для Sergey-Trade 2025.

Требования по проекту:
- Используем библиотеку tinkoff-investments v0.2.0b59
- Для SL/TP используем только post_stop_order() из StopOrdersService
- Направление (long/short) и количество берём из фактической позиции
- Проверка отклонений от текущей цены не требуется
- Типы стопов: STOP_LOSS и TAKE_PROFIT
- Срок действия: GOOD_TILL_CANCEL (GTC)

Примечание по "рыночности":
В Invest API для стоп-заявок нет отдельного типа "MARKET". Согласно документации,
стоп-заявки имеют типы STOP_LOSS / TAKE_PROFIT / STOP_LIMIT, а срок действия задаётся
через StopOrderExpirationType. Мы выставляем цену заявки равной стоп-цене.
Сами перечисления и вызов post_stop_order задокументированы в официальных материалах
(см. StopOrdersService и PostStopOrderRequest). 
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional, Tuple, Union

from tinkoff.invest import (
    Client,
    StopOrderDirection,
    StopOrderType,
    StopOrderExpirationType,
)
from tinkoff.invest.utils import decimal_to_quotation, quotation_to_decimal


def get_last_price(client: Client, instrument_uid: str) -> Decimal:
    """
    Получить последнюю цену инструмента по instrument_uid.
    Возвращает Decimal.

    Использует MarketDataService.get_last_prices (по uid).
    """
    resp = client.market_data.get_last_prices(instrument_id=[instrument_uid])
    if not resp.last_prices:
        raise RuntimeError(f"Нет last_price для uid={instrument_uid}")
    q = resp.last_prices[0].price  # Quotation
    return quotation_to_decimal(q)


def _as_direction(value: Union[str, StopOrderDirection]) -> StopOrderDirection:
    """Привести строку 'buy'/'sell' либо enum к StopOrderDirection."""
    if isinstance(value, StopOrderDirection):
        return value
    v = str(value).strip().lower()
    if v in ("buy", "long", "close_short"):
        return StopOrderDirection.STOP_ORDER_DIRECTION_BUY
    if v in ("sell", "short", "close_long"):
        return StopOrderDirection.STOP_ORDER_DIRECTION_SELL
    raise ValueError(f"Unknown direction: {value!r}")


def _as_type(value: str) -> StopOrderType:
    """Привести 'sl'/'stop_loss'/'tp'/'take_profit' к StopOrderType."""
    v = str(value).strip().lower()
    if v in ("sl", "stop_loss", "stoploss"):
        return StopOrderType.STOP_ORDER_TYPE_STOP_LOSS
    if v in ("tp", "take_profit", "takeprofit"):
        return StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT
    raise ValueError(f"Unknown stop kind: {value!r}")


def place_stop_order(
    client: Client,
    *,
    account_id: str,
    instrument_uid: str,
    quantity: int,
    direction: Union[str, StopOrderDirection],
    stop_price: Union[Decimal, float, str],
    kind: str,  # 'sl' | 'tp'
    expiration_type: StopOrderExpirationType = StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
) -> str:
    """
    Разместить стоп-заявку (SL или TP) через StopOrdersService.post_stop_order().

    ВАЖНО:
    - В Invest API нет отдельного "MARKET" для стоп-заявок; используем STOP_LOSS/TAKE_PROFIT,
      передавая price == stop_price. Это соответствует официальной спецификации.
    - Для GOOD_TILL_CANCEL дата истечения не требуется.

    Возвращает stop_order_id (str).
    """
    if quantity <= 0:
        raise ValueError("quantity должен быть > 0")

    stop_type = _as_type(kind)
    dir_enum = _as_direction(direction)

    # Преобразуем числа в Quotation
    stop_price_dec = Decimal(str(stop_price))
    price_q = decimal_to_quotation(stop_price_dec)      # price
    stop_price_q = decimal_to_quotation(stop_price_dec) # stop_price

    logging.info(
        f"🧾 post_stop_order: uid={instrument_uid}, qty={quantity}, "
        f"dir={dir_enum.name}, kind={stop_type.name}, stop={stop_price_dec}"
    )

    resp = client.stop_orders.post_stop_order(
        account_id=account_id,
        instrument_id=instrument_uid,  # именно instrument_id (uid), не figi
        quantity=quantity,
        price=price_q,
        stop_price=stop_price_q,
        direction=dir_enum,
        stop_order_type=stop_type,
        expiration_type=expiration_type,
    )

    # По спецификации вернётся идентификатор стоп-заявки
    stop_id = resp.stop_order_id
    logging.info(f"✅ Стоп-заявка размещена: stop_order_id={stop_id}")
    return stop_id

