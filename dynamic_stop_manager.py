#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dynamic_stop_manager.py

Динамическое управление стопами (SL) для проекта Sergey-Trade 2025.

⚠️ ВАЖНО:
- Скрипт НИКОГДА не создаёт новые стопы "с нуля".
- Работает только с уже существующими SL (STOP_ORDER_TYPE_MARKET), двигая их ближе к цене.
- TP не трогает.
- По умолчанию работает в режиме dry-run (только логирование). Для включения реального
  движения стопов используйте DYNAMIC_STOPS_APPLY=1 в .env.

Логика:
1. Берём портфель (shares/futures).
2. Для каждой позиции:
   - определяем направление (лонг/шорт);
   - находим текущий SL по stop_orders;
   - считаем R (прибыль / изначальный риск);
   - при R >= DYN_ACTIVATE_R двигаем SL в безубыток (entry);
   - при R >= DYN_TRAIL_START_R включаем трейлинг:
     * лонг: SL подтягиваем вверх за ценой;
     * шорт: SL подтягиваем вниз за ценой.
3. Двигаем SL только "в сторону уменьшения риска":
   - для лонга: SL не опускаем ниже старого;
   - для шорта: SL не поднимаем выше старого.
"""

import logging
import math
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from tinkoff.invest import (
    Client,
    InstrumentIdType,
    InvestError,
    Quotation,
    StopOrder,
    StopOrderDirection,
    StopOrderExpirationType,
    StopOrderType,
)

# -----------------------------------------------------------------------------
# Настройки логирования
# -----------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

LOG_FILE = LOGS_DIR / "dynamic_stop_manager.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

load_dotenv()


# -----------------------------------------------------------------------------
# Утилиты
# -----------------------------------------------------------------------------


def quotation_to_float(q: Quotation) -> float:
    return q.units + q.nano / 1_000_000_000 if q is not None else 0.0


def float_to_quotation(value: float) -> Quotation:
    units = int(math.floor(value))
    nano = int(round((value - units) * 1_000_000_000))
    if nano >= 1_000_000_000:
        units += 1
        nano -= 1_000_000_000
    return Quotation(units=units, nano=nano)


# -----------------------------------------------------------------------------
# Основная логика
# -----------------------------------------------------------------------------


def load_config():
    """Загрузка конфигурации из .env с разумными дефолтами."""
    enabled = os.environ.get("DYNAMIC_STOPS_ENABLED", "0") == "1"
    apply_changes = os.environ.get("DYNAMIC_STOPS_APPLY", "0") == "1"

    def _f(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError:
            logger.warning(
                "Некорректное значение %s=%r в окружении, использую %.3f по умолчанию",
                name,
                raw,
                default,
            )
            return default

    cfg = {
        "enabled": enabled,
        "apply_changes": apply_changes,
        "activate_r": _f("DYN_ACTIVATE_R", 1.0),
        "trail_start_r": _f("DYN_TRAIL_START_R", 2.0),
        "trail_gap_r": _f("DYN_TRAIL_GAP_R", 0.5),
    }
    return cfg


def get_portfolio_positions(client, account_id: str):
    """Получаем список позиций из портфеля (shares, futures и т.п.)."""
    operations = client.operations
    resp = operations.get_portfolio(account_id=account_id)
    positions = resp.positions  # type: ignore[attr-defined]

    result = []
    for p in positions:
        # интересуют только акции и фьючерсы
        instr_type = getattr(p, "instrument_type", "")
        if instr_type not in ("share", "future"):
            continue

        instrument_uid = getattr(p, "instrument_uid", "") or getattr(p, "instrument_id", "")
        if not instrument_uid:
            continue

        qty_q: Quotation = getattr(p, "quantity", None)
        qty = quotation_to_float(qty_q) if qty_q is not None else 0.0
        if abs(qty) < 0.001:
            continue

        avg_price_mv = getattr(p, "average_position_price", None) or getattr(
            p, "average_position_price_fifo", None
        )
        if avg_price_mv is None:
            # Без средней цены нам нечего считать R, пропускаем
            logger.warning(
                "Позиция без средней цены: uid=%s instrument_type=%s, пропускаю",
                instrument_uid,
                instr_type,
            )
            continue

        entry = avg_price_mv.units + avg_price_mv.nano / 1_000_000_000

        direction = "long" if qty > 0 else "short"

        result.append(
            {
                "instrument_uid": instrument_uid,
                "instrument_type": instr_type,
                "qty": abs(qty),
                "entry": entry,
                "direction": direction,
            }
        )

    logger.info("📊 Динамик: найдено позиций для обработки: %s", len(result))
    return result


def get_instrument_info_map(client, uids: List[str]):
    """Для списка uid получаем тикер, class_code, шаг цены."""
    instruments = client.instruments
    info_map: Dict[str, Dict] = {}

    for uid in uids:
        try:
            resp = instruments.get_instrument_by(
                id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id=uid
            )
            inst = resp.instrument  # type: ignore[attr-defined]
        except Exception as e:
            logger.warning("Не удалось получить инструмент по uid=%s: %s", uid, e)
            continue

        ticker = getattr(inst, "ticker", uid)
        class_code = getattr(inst, "class_code", "")
        mpi_q: Quotation = getattr(inst, "min_price_increment", None)
        step = quotation_to_float(mpi_q) if mpi_q is not None else 0.01

        info_map[uid] = {
            "ticker": ticker,
            "class_code": class_code,
            "min_step": step,
        }

    return info_map


def get_last_price_map(client, uids: List[str]) -> Dict[str, float]:
    market_data = client.market_data
    if not uids:
        return {}
    resp = market_data.get_last_prices(instrument_id=uids)
    result: Dict[str, float] = {}
    for lp in resp.last_prices:
        result[lp.instrument_uid] = quotation_to_float(lp.price)
    return result


def get_stop_orders_map(client, account_id: str) -> Dict[str, List[StopOrder]]:
    stop_orders_service = client.stop_orders
    resp = stop_orders_service.get_stop_orders(account_id=account_id)
    result: Dict[str, List[StopOrder]] = {}
    for so in resp.stop_orders:
        uid = so.instrument_uid
        result.setdefault(uid, []).append(so)
    logger.info("📊 Динамик: всего активных стоп-заявок: %s", len(resp.stop_orders))
    return result


def split_sl_tp_for_position(
    position_dir: str, current_price: float, orders: List[StopOrder]
) -> Tuple[Optional[StopOrder], Optional[StopOrder]]:
    """
    Грубое разделение SL / TP для позиции.
    Для лонга:
      - SL: SELL ниже текущей цены
      - TP: SELL выше текущей цены
    Для шорта:
      - SL: BUY выше текущей
      - TP: BUY ниже текущей
    Берём ближайший по цене к текущей.
    """
    sl_candidates: List[Tuple[StopOrder, float]] = []
    tp_candidates: List[Tuple[StopOrder, float]] = []

    for o in orders:
        price = quotation_to_float(o.stop_price)

        if position_dir == "long":
            if o.direction == StopOrderDirection.STOP_ORDER_DIRECTION_SELL:
                if price < current_price:
                    sl_candidates.append((o, price))
                elif price > current_price:
                    tp_candidates.append((o, price))
        else:  # short
            if o.direction == StopOrderDirection.STOP_ORDER_DIRECTION_BUY:
                if price > current_price:
                    sl_candidates.append((o, price))
                elif price < current_price:
                    tp_candidates.append((o, price))

    sl_order = None
    tp_order = None

    if sl_candidates:
        # ближе всех к цене
        sl_order = sorted(sl_candidates, key=lambda x: abs(x[1] - current_price))[0][0]
    if tp_candidates:
        tp_order = sorted(tp_candidates, key=lambda x: abs(x[1] - current_price))[0][0]

    return sl_order, tp_order


def compute_new_sl_price(
    direction: str,
    entry: float,
    current: float,
    old_sl: float,
    min_step: float,
    activate_r: float,
    trail_start_r: float,
    trail_gap_r: float,
) -> Optional[float]:
    """
    Рассчитываем новый уровень SL.

    Возвращает:
      - float (новая цена SL), если есть смысл двигать;
      - None, если менять не нужно.
    """
    if direction not in ("long", "short"):
        return None

    # базовые величины
    if direction == "long":
        if entry <= old_sl:
            # Некорректный SL (выше или равен entry) — не двигаем
            return None
        risk_per_unit = entry - old_sl
        profit = current - entry
    else:  # short
        if entry >= old_sl:
            return None
        risk_per_unit = old_sl - entry
        profit = entry - current

    if risk_per_unit <= 0:
        return None

    R = profit / risk_per_unit

    if R < activate_r:
        # До активации ничего не делаем
        return None

    # Стадия 1: перевод в безубыток (entry)
    if (direction == "long" and old_sl < entry <= current) or (
        direction == "short" and old_sl > entry >= current
    ):
        # Если старый SL ещё "хуже", чем entry — цель: подтянуть к entry
        target_be = entry
        candidate = target_be
    else:
        candidate = old_sl

    # Стадия 2: трейлинг за ценой при большом профите
    if R >= trail_start_r:
        gap = risk_per_unit * trail_gap_r
        # минимальный зазор в 2 шага цены, чтобы SL не прилипал к рынку
        min_gap = max(min_step * 2, risk_per_unit * 0.1)
        gap = max(gap, min_gap)

        if direction == "long":
            trail_candidate = current - gap
            if trail_candidate > candidate:
                candidate = trail_candidate
        else:
            trail_candidate = current + gap
            if trail_candidate < candidate:
                candidate = trail_candidate

    # Не допускаем "расширение" риска
    if direction == "long":
        if candidate <= old_sl:
            return None
        if candidate >= current:
            # SL не может быть выше текущей цены
            candidate = current - min_step
    else:
        if candidate >= old_sl:
            return None
        if candidate <= current:
            candidate = current + min_step

    # Приводим к шагу цены
    if min_step <= 0:
        return None

    if direction == "long":
        # округляем вниз (чтобы не улететь выше, чем можно)
        grid_price = math.floor(candidate / min_step) * min_step
        if grid_price <= old_sl:
            return None
        return grid_price
    else:
        # short: округляем вверх
        grid_price = math.ceil(candidate / min_step) * min_step
        if grid_price >= old_sl:
            return None
        return grid_price


def apply_new_sl(
    client,
    account_id: str,
    position_info: Dict,
    instr_info: Dict,
    current_price: float,
    sl_order: StopOrder,
    new_sl_price: float,
    apply_changes: bool,
):
    """
    Применяем новый SL:
    - в режиме dry-run только логируем;
    - при apply_changes=True — отменяем старый SL и ставим новый.
    """
    ticker = instr_info["ticker"]
    class_code = instr_info["class_code"]
    direction = position_info["direction"]
    uid = position_info["instrument_uid"]
    qty = int(position_info["qty"])

    old_sl_price = quotation_to_float(sl_order.stop_price)

    logger.info(
        "🔁 Динамик: %s (%s) %s | current=%.4f, old_SL=%.4f, new_SL=%.4f, qty=%s",
        ticker,
        class_code,
        "ЛОНГ" if direction == "long" else "ШОРТ",
        current_price,
        old_sl_price,
        new_sl_price,
        qty,
    )

    if not apply_changes:
        logger.info("📝 DYNAMIC_STOPS_APPLY=0 — изменения НЕ применены (dry-run).")
        return

    stop_orders_service = client.stop_orders

    # 1) Отменяем старый SL
    try:
        stop_orders_service.cancel_stop_order(
            account_id=account_id,
            stop_order_id=sl_order.stop_order_id,
        )
        logger.info(
            "❌ Старый SL отменён: stop_order_id=%s", sl_order.stop_order_id
        )
    except InvestError as e:
        logger.error("Ошибка при отмене SL (%s): %s", sl_order.stop_order_id, e)
        return
    except Exception as e:
        logger.error("Неожиданная ошибка при отмене SL: %s", e)
        return

    # 2) Ставим новый SL (STOP_ORDER_TYPE_MARKET, GOOD_TILL_CANCELLED)
    try:
        direction_enum = (
            StopOrderDirection.STOP_ORDER_DIRECTION_SELL
            if direction == "long"
            else StopOrderDirection.STOP_ORDER_DIRECTION_BUY
        )
        stop_price_q = float_to_quotation(new_sl_price)

        resp = stop_orders_service.post_stop_order(
            account_id=account_id,
            instrument_id=uid,
            quantity=qty,
            stop_price=stop_price_q,
            price=float_to_quotation(0.0),  # для MARKET можно 0
            direction=direction_enum,
            expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCELLED,
            stop_order_type=StopOrderType.STOP_ORDER_TYPE_MARKET,
        )
        logger.info(
            "✅ Новый SL выставлен: %s (%s) stop_price=%.4f, stop_order_id=%s",
            ticker,
            class_code,
            new_sl_price,
            resp.stop_order_id,
        )
    except InvestError as e:
        logger.error("Ошибка при постановке нового SL: %s", e)
    except Exception as e:
        logger.error("Неожиданная ошибка при постановке нового SL: %s", e)


# -----------------------------------------------------------------------------
# main
# -----------------------------------------------------------------------------


def main():
    cfg = load_config()

    if not cfg["enabled"]:
        logger.info("🚦 dynamic_stop_manager: отключён (DYNAMIC_STOPS_ENABLED != 1), выхожу.")
        return 0

    token = os.environ.get("TINKOFF_TOKEN")
    account_id = os.environ.get("TINKOFF_ACCOUNT_ID")

    if not token or not account_id:
        logger.error("❌ Не заданы TINKOFF_TOKEN или TINKOFF_ACCOUNT_ID в окружении.")
        return 1

    logger.info(
        "🚦 Запуск dynamic_stop_manager.py (apply_changes=%s, activate_r=%.2f, trail_start_r=%.2f, trail_gap_r=%.2f)",
        cfg["apply_changes"],
        cfg["activate_r"],
        cfg["trail_start_r"],
        cfg["trail_gap_r"],
    )

    with Client(token) as client:
        # 1) Позиции
        positions = get_portfolio_positions(client, account_id)
        if not positions:
            logger.info("📭 Нет позиций для обработки, выхожу.")
            return 0

        uids = [p["instrument_uid"] for p in positions]

        # 2) Информация по инструментам (тикер, class_code, шаг)
        instr_info_map = get_instrument_info_map(client, uids)

        # 3) Текущие цены
        last_price_map = get_last_price_map(client, uids)

        # 4) Стоп-заявки
        stop_orders_map = get_stop_orders_map(client, account_id)

        # 5) Основной цикл по позициям
        for pos in positions:
            uid = pos["instrument_uid"]
            direction = pos["direction"]
            entry = pos["entry"]

            info = instr_info_map.get(uid)
            if not info:
                logger.warning(
                    "Нет информации об инструменте для uid=%s, пропускаю позицию.", uid
                )
                continue

            ticker = info["ticker"]
            class_code = info["class_code"]
            min_step = info["min_step"]

            current = last_price_map.get(uid)
            if not current:
                logger.warning(
                    "Нет текущей цены для %s (%s), пропускаю.", ticker, class_code
                )
                continue

            orders = stop_orders_map.get(uid, [])
            if not orders:
                logger.info(
                    "ℹ️ Для %s (%s) нет ни одной стоп-заявки, динамик ничего не делает.",
                    ticker,
                    class_code,
                )
                continue

            sl_order, tp_order = split_sl_tp_for_position(direction, current, orders)

            if sl_order is None:
                logger.info(
                    "ℹ️ Для %s (%s) не найден SL, динамик ничего не делает.",
                    ticker,
                    class_code,
                )
                continue

            old_sl = quotation_to_float(sl_order.stop_price)
            new_sl = compute_new_sl_price(
                direction=direction,
                entry=entry,
                current=current,
                old_sl=old_sl,
                min_step=min_step,
                activate_r=cfg["activate_r"],
                trail_start_r=cfg["trail_start_r"],
                trail_gap_r=cfg["trail_gap_r"],
            )

            if new_sl is None:
                logger.info(
                    "ℹ️ %s (%s): условий для движения SL нет (entry=%.4f, current=%.4f, old_SL=%.4f).",
                    ticker,
                    class_code,
                    entry,
                    current,
                    old_sl,
                )
                continue

            apply_new_sl(
                client=client,
                account_id=account_id,
                position_info=pos,
                instr_info=info,
                current_price=current,
                sl_order=sl_order,
                new_sl_price=new_sl,
                apply_changes=cfg["apply_changes"],
            )

    logger.info("🏁 dynamic_stop_manager.py завершён.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

