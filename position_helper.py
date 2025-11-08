"""
position_helper.py — модуль для работы с портфелем:
- поиск инструмента (figi/uid) по тикеру + class_code
- получение фактической позиции (qty, направление) по счёту
"""

import logging
from typing import Optional, Tuple
from decimal import Decimal
from tinkoff.invest import Client, InstrumentIdType, StopOrderDirection
from tinkoff.invest.utils import quotation_to_decimal

def get_instrument_uid(client: Client, ticker: str, class_code: str) -> Optional[str]:
    """
    Возвращает instrument_uid (или figi) для заданного ticker + class_code.
    Возвращает None, если не найден.
    """
    try:
        resp = client.instruments.get_instrument_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
            id=ticker,
            class_code=class_code,
        )
        inst = resp.instrument
        logging.info(f"🔍 Инструмент найден: {ticker} → uid={inst.uid}")
        return inst.uid
    except Exception as e:
        logging.error(f"🚨 Ошибка при get_instrument_uid для {ticker}: {e}")
        return None

def find_position(positions: list, instrument_uid: str) -> Optional[object]:
    """
    Ищет среди списка positions объект, соответствующий instrument_uid.
    Возвращает объект позиции или None.
    """
    for p in positions:
        if getattr(p, "instrument_uid", None) == instrument_uid \
           and getattr(p, "quantity", None) and p.quantity.units > 0:
            return p
    return None

def get_position_details(position_obj: object) -> Tuple[int, StopOrderDirection]:
    """
    Из объекта позиции извлекает:
     - qty (int)
     - direction (StopOrderDirection) — SELL если LONG, BUY если SHORT
    Для понимания LONG/SHORT сравнивает average_position_price и current_price.
    """
    try:
        qty = int(position_obj.quantity.units)
    except Exception as e:
        logging.error(f"🚨 Ошибка при чтении qty позиции: {e}")
        raise

    try:
        avg_price = float(quotation_to_decimal(position_obj.average_position_price))
        current_price = float(quotation_to_decimal(position_obj.current_price))
    except Exception as e:
        logging.error(f"⚠️ Не удалось определить цены для направления: {e}")
        # по умолчанию считаем SELL
        return qty, StopOrderDirection.STOP_ORDER_DIRECTION_SELL

    if current_price > avg_price:
        return qty, StopOrderDirection.STOP_ORDER_DIRECTION_SELL
    else:
        return qty, StopOrderDirection.STOP_ORDER_DIRECTION_BUY

