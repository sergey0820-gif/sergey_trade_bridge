#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
stops_guard.py
- Читает out/pending_stops.csv (очередь на постановку SL/TP)
- Выставляет стоп-заявки через Tinkoff Invest API
- Помечает строки как PLACED и перезаписывает CSV
- Логи: logs/stops_guard.log

Ожидаемые колонки (лишние игнорируются):
ts,ticker,figi,side,lots,entry,stop,target,status,sl_order_id,tp_order_id
"""

import os
import csv
import time
import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import grpc
from dotenv import load_dotenv
from tinkoff.invest import (
    Client,
    Quotation,
    StopOrderDirection,
    StopOrderType,
    StopOrderExpirationType,
)
from tinkoff.invest.exceptions import RequestError

PENDING_FILE = "out/pending_stops.csv"
LOG_FILE = "logs/stops_guard.log"
SLEEP_SEC = int(os.getenv("STOPS_GUARD_INTERVAL", "8"))  # пауза между проходами
DRY = os.getenv("DRY_RUN", "false").lower() == "true"

# Логирование
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)

# ВАЖНО: правильная константа без “…LED”
EXPIRATION = StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL


def to_q(value: Decimal | float | str) -> Quotation:
    """Без зависимости от tinkoff.invest.utils."""
    d = Decimal(str(value)).quantize(Decimal("0.000000001"), rounding=ROUND_HALF_UP)
    sign = -1 if d < 0 else 1
    d = abs(d)
    units = int(d.to_integral_value(rounding=ROUND_HALF_UP))
    nano = int((d - Decimal(units)) * Decimal(10**9))
    if sign < 0:
        if units > 0:
            units = -units
        else:
            nano = -nano
    return Quotation(units=units, nano=nano)


def read_pending_rows(path: str):
    if not os.path.exists(path):
        return [], []
    with open(path, "r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        rows = list(r)
        return rows, r.fieldnames


def write_pending_rows(path: str, rows, fieldnames):
    must = [
        "ts",
        "ticker",
        "figi",
        "side",
        "lots",
        "entry",
        "stop",
        "target",
        "status",
        "sl_order_id",
        "tp_order_id",
        "error",
    ]
    fieldnames = list(fieldnames or [])
    for k in must:
        if k not in fieldnames:
            fieldnames.append(k)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def s_int(x, default=0) -> int:
    try:
        return int(str(x).strip())
    except Exception:
        return default


def s_dec(x, default: Optional[Decimal] = None) -> Optional[Decimal]:
    try:
        return Decimal(str(x))
    except Exception:
        return default


def place_one_stop(
    cli: Client,
    account_id: str,
    figi: str,
    direction: StopOrderDirection,
    stop_type: StopOrderType,
    quantity: int,
    price_dec: Decimal,
    stop_price_dec: Decimal,
) -> str:
    """Унифицированный вызов для SL/TP (актуальная сигнатура SDK)."""
    if DRY:
        logging.info(
            "[DRY] stop %s %s qty=%s price=%s stop=%s figi=%s",
            stop_type.name,
            direction.name,
            quantity,
            price_dec,
            stop_price_dec,
            figi,
        )
        return "dry-order-id"

    resp = cli.stop_orders.post_stop_order(
        account_id=account_id,
        figi=figi,
        quantity=quantity,
        price=to_q(price_dec),
        stop_price=to_q(stop_price_dec),
        direction=direction,
        stop_order_type=stop_type,
        expiration_type=EXPIRATION,
    )
    return resp.stop_order_id


def main():
    load_dotenv(".env")
    token = os.getenv("TINKOFF_TOKEN") or os.getenv("TINKOFF_INVEST_TOKEN")
    account_id = os.getenv("TINKOFF_ACCOUNT_ID")
    if not token or not account_id:
        logging.error(
            "Нет токена/аккаунта: TINKOFF_TOKEN / TINKOFF_INVEST_TOKEN и TINKOFF_ACCOUNT_ID"
        )
        return

    logging.info("stops_guard: started (dry=%s, interval=%ss)", DRY, SLEEP_SEC)

    while True:
        try:
            rows, fieldnames = read_pending_rows(PENDING_FILE)
            if not rows:
                time.sleep(SLEEP_SEC)
                continue

            changed = False

            with Client(token) as cli:
                for r in rows:
                    status = (r.get("status") or "").strip().upper()
                    if status in ("PLACED", "DONE", "CANCELLED"):
                        continue

                    figi = (r.get("figi") or "").strip()
                    side = (r.get("side") or "BUY").strip().upper()
                    lots = s_int(r.get("lots"), 0)
                    stop_dec = s_dec(r.get("stop"))
                    tp_dec = s_dec(r.get("target"))
                    # entry может пригодиться позже
                    # entry_dec = s_dec(r.get("entry"))

                    if not figi or lots <= 0 or stop_dec is None:
                        r["error"] = "invalid row: figi/lots/stop"
                        r["status"] = "ERROR"
                        changed = True
                        continue

                    direction = (
                        StopOrderDirection.STOP_ORDER_DIRECTION_SELL
                        if side == "BUY"
                        else StopOrderDirection.STOP_ORDER_DIRECTION_BUY
                    )

                    # SL
                    if not (r.get("sl_order_id") or "").strip():
                        try:
                            exec_price = stop_dec
                            trig_price = stop_dec
                            sl_id = place_one_stop(
                                cli,
                                account_id,
                                figi,
                                direction,
                                StopOrderType.STOP_ORDER_TYPE_STOP_LOSS,
                                lots,
                                exec_price,
                                trig_price,
                            )
                            r["sl_order_id"] = sl_id
                            logging.info(
                                "[OK] SL placed: figi=%s lots=%s stop=%s id=%s",
                                figi,
                                lots,
                                stop_dec,
                                sl_id,
                            )
                            changed = True
                        except (RequestError, grpc.RpcError, Exception) as e:
                            r["error"] = f"SL error: {e}"
                            logging.error("SL error for %s: %s", figi, e)

                    # TP (если указан)
                    if tp_dec is not None and not (r.get("tp_order_id") or "").strip():
                        try:
                            exec_price = tp_dec
                            trig_price = tp_dec
                            tp_id = place_one_stop(
                                cli,
                                account_id,
                                figi,
                                direction,
                                StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT,
                                lots,
                                exec_price,
                                trig_price,
                            )
                            r["tp_order_id"] = tp_id
                            logging.info(
                                "[OK] TP placed: figi=%s lots=%s target=%s id=%s",
                                figi,
                                lots,
                                tp_dec,
                                tp_id,
                            )
                            changed = True
                        except (RequestError, grpc.RpcError, Exception) as e:
                            r["error"] = f"TP error: {e}"
                            logging.error("TP error for %s: %s", figi, e)

                    if r.get("sl_order_id"):
                        r["status"] = "PLACED"
                        changed = True

            if changed:
                write_pending_rows(PENDING_FILE, rows, fieldnames)

        except Exception as e:
            logging.error("stops_guard loop error: %s", e)

        time.sleep(SLEEP_SEC)


if __name__ == "__main__":
    main()
