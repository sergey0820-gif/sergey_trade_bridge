#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Sergey-Trade bridge: снимок аккаунта Тинькофф (позиции, активные заявки, операции сегодня)
и выгрузка в CSV/JSON, с бережной работой по лимитам и совместимостью разных версий SDK.
"""

import os
import csv
import json
import time
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

# Tinkoff Invest SDK
from tinkoff.invest import Client, CandleInterval, OperationState
from tinkoff.invest.exceptions import RequestError

BASE_DIR = os.path.dirname(__file__)
OUT_DIR = os.path.join(BASE_DIR, "out")
os.makedirs(OUT_DIR, exist_ok=True)

# ---------- ENV ----------
load_dotenv(os.path.join(BASE_DIR, ".env"))
TOKEN = os.getenv("TINKOFF_TOKEN") or os.getenv("TINKOFF_INVEST_TOKEN")  # на всякий
ACCOUNT_ID = os.getenv("TINKOFF_ACCOUNT_ID")


# ---------- UTIL ----------
def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def money_to_float(mv) -> float:
    """Универсальный парсер MoneyValue/Quotation -> float."""
    if mv is None:
        return 0.0
    units = getattr(mv, "units", 0) or 0
    nano = getattr(mv, "nano", 0) or 0
    return float(units) + float(nano) / 1e9


def operation_fee_to_float(op) -> float:
    """
    Комиссия операции в float. В разных версиях SDK поле могло называться:
      - op.commission (MoneyValue)
      - op.commission_value
      - op.fee
      - op.broker_commission
    Если ничего нет — вернём 0.0
    """
    for attr in ("commission", "commission_value", "fee", "broker_commission"):
        val = getattr(op, attr, None)
        if val is not None:
            return money_to_float(val)
    return 0.0


def call_with_backoff(fn, *args, **kwargs):
    """Вызов метода SDK с мягким бэкоффом при RESOURCE_EXHAUSTED."""
    max_tries = 6
    delay = 0.5
    for i in range(max_tries):
        try:
            return fn(*args, **kwargs)
        except RequestError as e:
            code = getattr(e, "status_code", None)
            # код 8 == RESOURCE_EXHAUSTED
            if getattr(code, "value", None) == 8:
                meta = getattr(e, "metadata", None)
                reset = 1.0
                if meta and getattr(meta, "ratelimit_reset", None):
                    try:
                        reset = float(meta.ratelimit_reset)
                    except Exception:
                        pass
                time.sleep(max(delay, reset))
                delay *= 1.8
                continue
            raise


# ---------- FETCHERS ----------
def fetch_portfolio_positions(client, account_id):
    """Портфельные позиции + текущие рыночные цены (где доступны)."""
    res = call_with_backoff(client.operations.get_portfolio, account_id=account_id)
    positions = getattr(res, "positions", []) or []
    rows = []
    ts = now_utc_iso()

    for p in positions:
        figi = getattr(p, "figi", "")
        name = getattr(p, "instrument_uid", "") or figi
        qty = money_to_float(getattr(p, "quantity", None))
        avg = money_to_float(getattr(p, "average_position_price", None))
        cur = getattr(getattr(p, "average_position_price", None), "currency", "") or ""
        # Текущая рыночная оценка/цена
        current_price = money_to_float(getattr(p, "current_price", None))
        rows.append(
            {
                "ts": ts,
                "ticker": getattr(p, "ticker", "") or figi,
                "figi": figi,
                "class": getattr(p, "instrument_type", ""),
                "qty": qty,
                "avg_price": avg,
                "currency": cur,
                "market_price": current_price,
                "name": name,
            }
        )
    return rows


def fetch_active_orders(client, account_id):
    """Активные (размещённые) заявки."""
    try:
        res = call_with_backoff(client.orders.get_orders, account_id=account_id)
        orders = getattr(res, "orders", []) if hasattr(res, "orders") else res
    except Exception:
        orders = []

    ts = now_utc_iso()
    rows = []
    for o in orders or []:
        rows.append(
            {
                "ts": ts,
                "order_id": getattr(o, "order_id", ""),
                "ticker": getattr(o, "ticker", "") or getattr(o, "figi", ""),
                "figi": getattr(o, "figi", ""),
                "direction": getattr(o, "direction", ""),
                "price": money_to_float(getattr(o, "initial_order_price", None))
                or money_to_float(getattr(o, "price", None)),
                "lots": getattr(o, "lots_requested", 0) or getattr(o, "lots", 0),
                "status": getattr(o, "execution_report_status", ""),
            }
        )
    return rows


def fetch_operations_today(client, account_id):
    """Возвращает список OperationItem/Operation за сегодня (EXECUTED), с поддержкой обеих версий API."""
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end = datetime.now(timezone.utc)
    # пробуем новый курсорный API
    try:
        resp = call_with_backoff(
            client.operations.get_operations_by_cursor,
            account_id=account_id,
            from_=start,
            to=end,
            state=OperationState.OPERATION_STATE_EXECUTED,
            limit=1000,
        )
        items = list(getattr(resp, "items", []) or [])
        while getattr(resp, "has_next", False):
            resp = call_with_backoff(
                client.operations.get_operations_by_cursor,
                account_id=account_id,
                cursor=getattr(resp, "next_cursor", ""),
                limit=1000,
            )
            items.extend(getattr(resp, "items", []) or [])
        return items
    except Exception:
        pass
    # фоллбэк на старый API
    try:
        resp = call_with_backoff(
            client.operations.get_operations,
            account_id=account_id,
            from_=start,
            to=end,
            state=OperationState.OPERATION_STATE_EXECUTED,
        )
        return getattr(resp, "operations", []) or []
    except Exception:
        return []


# ---------- I/O ----------
def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main():
    if not TOKEN or not ACCOUNT_ID:
        raise RuntimeError(
            "❌ Нет TINKOFF_TOKEN (или TINKOFF_INVEST_TOKEN) и/или TINKOFF_ACCOUNT_ID в .env"
        )

    with Client(TOKEN) as client:
        # Позиции
        pos_rows = fetch_portfolio_positions(client, ACCOUNT_ID)
        write_csv(
            os.path.join(OUT_DIR, "positions.csv"),
            [
                "ts",
                "ticker",
                "figi",
                "class",
                "qty",
                "avg_price",
                "currency",
                "market_price",
                "name",
            ],
            pos_rows,
        )

        # Активные заявки
        ord_rows = fetch_active_orders(client, ACCOUNT_ID)
        write_csv(
            os.path.join(OUT_DIR, "orders.csv"),
            [
                "ts",
                "order_id",
                "ticker",
                "figi",
                "direction",
                "price",
                "lots",
                "status",
            ],
            ord_rows,
        )

        # Операции за сегодня
        ops_api_list = fetch_operations_today(client, ACCOUNT_ID)
        ops_rows = []
        for op in ops_api_list:
            dt = getattr(op, "date", None)
            ops_rows.append(
                {
                    "ts": dt.isoformat() if dt else "",
                    "id": getattr(op, "id", ""),
                    "name": getattr(op, "instrument_name", "")
                    or getattr(op, "figi", ""),
                    "type": getattr(op, "operation_type", ""),
                    "currency": getattr(op, "currency", "") or "",
                    "payment": money_to_float(getattr(op, "payment", None)),
                    "price": money_to_float(getattr(op, "price", None)),
                    "qty": getattr(op, "quantity", 0)
                    or getattr(op, "quantity_executed", 0)
                    or 0,
                    "fee": operation_fee_to_float(op),
                }
            )
        write_csv(
            os.path.join(OUT_DIR, "operations_today.csv"),
            ["ts", "id", "name", "type", "currency", "payment", "price", "qty", "fee"],
            ops_rows,
        )

        # Снимок
        snapshot = {
            "generated_at": now_utc_iso(),
            "counts": {
                "positions": len(pos_rows),
                "orders": len(ord_rows),
                "operations_today": len(ops_rows),
            },
            "files": {
                "positions": os.path.join(OUT_DIR, "positions.csv"),
                "orders": os.path.join(OUT_DIR, "orders.csv"),
                "operations_today": os.path.join(OUT_DIR, "operations_today.csv"),
            },
        }
        with open(os.path.join(OUT_DIR, "snapshot.json"), "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)

        print(json.dumps(snapshot, ensure_ascii=False))


if __name__ == "__main__":
    main()
