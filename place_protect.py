#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import argparse
from dotenv import load_dotenv

from tinkoff.invest import (
    Client,
    Quotation,
    OrderDirection,
    StopOrderDirection,
    StopOrderType,
    StopOrderExpirationType,
    PostOrderRequest,
    PostStopOrderRequest,
    OrderType,
    InstrumentIdType,
    RequestError,
)

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))

TOKEN = os.getenv("TINKOFF_TOKEN") or os.getenv("TINKOFF_INVEST_TOKEN")
ACCOUNT_ID = os.getenv("TINKOFF_ACCOUNT_ID", "")
ALLOW_PLACE = os.getenv("ALLOW_PLACE", "false").lower() == "true"


def q(v: float) -> Quotation:
    units = int(v)
    nano = int(round((v - units) * 1_000_000_000))
    return Quotation(units=units, nano=nano)


def resolve_instrument(c: Client, ticker: str, class_code: str):
    return c.instruments.get_instrument_by(
        id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
        class_code=class_code,
        id=ticker,
    ).instrument


def place_sl_tp_for_long(
    c: Client, figi: str, lots: int, stop_price: float, take_price: float
):
    """
    Для LONG:
      - SL: STOP_ORDER_TYPE_STOP_LOSS (stop_price = стоп-триггер, price = рыночная 0)
      - TP: STOP_ORDER_TYPE_TAKE_PROFIT (stop_price = триггер, price = лимит исполнения = take_price)
    На Мосбирже фьючерсы позволяют вешать стоп-заявки без открытой позиции.
    """
    # STOP-LOSS
    req_sl = PostStopOrderRequest(
        figi=figi,
        quantity=lots,
        price=q(
            0.0
        ),  # для стоп-лосса можно 0 (рыночное исполнение), или =stop_price для stop-limit
        stop_price=q(stop_price),
        direction=StopOrderDirection.STOP_ORDER_DIRECTION_SELL,
        account_id=ACCOUNT_ID,
        expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
        stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LOSS,
    )
    sl_resp = c.stop_orders.post_stop_order(req_sl)

    # TAKE-PROFIT (стоп-условие = take_price, лимит = take_price)
    req_tp = PostStopOrderRequest(
        figi=figi,
        quantity=lots,
        price=q(take_price),
        stop_price=q(take_price),
        direction=StopOrderDirection.STOP_ORDER_DIRECTION_SELL,
        account_id=ACCOUNT_ID,
        expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
        stop_order_type=StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT,
    )
    tp_resp = c.stop_orders.post_stop_order(req_tp)
    return sl_resp, tp_resp


def main():
    ap = argparse.ArgumentParser(description="Повесить SL/TP к позиции вручную")
    ap.add_argument("--ticker", required=True, help="Напр. ASZ5")
    ap.add_argument("--class", dest="cls", required=True, help="Напр. SPBFUT")
    ap.add_argument("--lots", type=int, required=True, help="Сколько лотов защищать")
    ap.add_argument("--stop", type=float, required=True, help="Цена стоп-триггера")
    ap.add_argument("--target", type=float, required=True, help="Цена тейк-профита")
    args = ap.parse_args()

    if not TOKEN or not ACCOUNT_ID:
        print("ERR: нет TOKEN/ACCOUNT_ID в .env")
        return 2
    if not ALLOW_PLACE:
        print(
            "DRY: ALLOW_PLACE=false — защита не будет размещена. Поставь ALLOW_PLACE=true в .env"
        )
        return 0

    with Client(TOKEN) as c:
        try:
            ins = resolve_instrument(c, args.ticker, args.cls)
            figi = ins.figi
        except RequestError as e:
            print("ERR: не нашли инструмент:", e)
            return 1

        try:
            sl, tp = place_sl_tp_for_long(c, figi, args.lots, args.stop, args.target)
            print(f"OK: SL id={sl.stop_order_id}, TP id={tp.stop_order_id}")
        except RequestError as e:
            print("ERR: размещение SL/TP:", e)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
