from __future__ import annotations
from uuid import uuid4
from decimal import Decimal, getcontext
from typing import Optional

from tinkoff.invest import (
    OrderType, OrderDirection,
    InstrumentIdType,
)
from tinkoff.invest.utils import quotation_to_decimal

from trade_utils.uid_resolver import _resolve_uid

getcontext().prec = 28  # безопасная точность для финансовых расчётов

async def _is_api_tradable(c, uid: str) -> bool:
    """
    Быстрая проверка: инструмент вообще разрешён к торговле по API.
    """
    r = await c.instruments.get_instrument_by(
        id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
        class_code="",
        id=uid,
    )
    inst = getattr(r, "instrument", None)
    return bool(inst and getattr(inst, "api_trade_available_flag", False))

async def _trading_status_ok(c, uid: str) -> bool:
    """
    Мягкая проверка текущего статуса торгов:
    пробуем MarketDataService.GetTradingStatus, если метод/сигнатура изменится —
    не падаем, а просто пропускаем проверку.
    """
    try:
        # большинство клиентов поддерживают прямой вызов с instrument_id=uid
        ts = await c.market_data.get_trading_status(instrument_id=uid)
        # допускаем нормальную торговлю/торговлю (названия enum могут различаться в билдах)
        name = getattr(ts.trading_status, "name", str(ts.trading_status))
        return any(k in name for k in ("NORMAL", "TRADING"))
    except Exception:
        return True  # не препятствуем, если проверка недоступна

async def post_order_safe(
    c,
    account_id: str,
    ticker: str,
    class_code: Optional[str],
    qty_lots: int,
    direction: OrderDirection,
    order_type: OrderType,
    dry_run: bool = False,
):
    """
    Единственная точка постановки заявок:
    - резолвит UID;
    - проверяет api_trade_available_flag;
    - мягко проверяет торговый статус;
    - передаёт instrument_id=UID и order_id=UUID
    """
    if qty_lots <= 0:
        raise ValueError("qty_lots must be > 0")

    uid = await _resolve_uid(c, ticker, class_code)
    if not uid:
        raise RuntimeError(f"Не нашли UID для {ticker}/{class_code}")

    if not await _is_api_tradable(c, uid):
        raise RuntimeError(f"Инструмент {ticker} не разрешён для торговли по API")

    if not await _trading_status_ok(c, uid):
        raise RuntimeError(f"Сейчас нет нормального статуса торгов для {ticker}")

    if dry_run:
        return {"dry_run": True, "instrument_uid": uid, "qty_lots": int(qty_lots)}

    # Ключ идемпотентности — UUID, instrument_id — UID/FIGI (без указания типа),
    # как требует API PostOrder.
    # order_id: UID/UUID до 36 символов (идемпотентность). instrument_id: FIGI или Instrument_uid.
    # см. оф. доки.
    return await c.orders.post_order(
        account_id=account_id,
        instrument_id=uid,
        order_id=str(uuid4()),
        direction=direction,
        order_type=order_type,
        quantity=int(qty_lots),
    )

# Compatibility wrapper for sync code
def post_order_safe_sync(*args, **kwargs):
    import asyncio
    # NB: asyncio.run() нельзя вызывать, если цикл уже запущен.
    # В обычных скриптах это безопасно.
    return asyncio.run(post_order_safe(*args, **kwargs))
