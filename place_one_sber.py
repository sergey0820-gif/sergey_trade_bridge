#!/usr/bin/env python3
import os, traceback
from uuid import uuid4
from dotenv import load_dotenv
from tinkoff.invest import Client, OrderDirection, OrderType, InstrumentIdType
from trade_utils.orders import post_order_safe_sync

def main():
    load_dotenv()
    token = os.getenv("TINKOFF_TOKEN")
    account = os.getenv("TINKOFF_ACCOUNT_ID")
    if not token or not account:
        print("❌ .env: нет TINKOFF_TOKEN/TINKOFF_ACCOUNT_ID"); return

    ticker = os.getenv("TICKER", "SBER")
    class_code = os.getenv("CLASS_CODE", "TQBR")
    qty = int(os.getenv("QTY", "1"))
    allow_place = os.getenv("ALLOW_PLACE", "false").lower() in ("1","true","yes","y")

    with Client(token) as client:
        instr = client.instruments.get_instrument_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
            class_code=class_code,
            id=ticker
        ).instrument
        uid = instr.uid
        figi = instr.figi

        try:
            lp = client.market_data.get_last_prices(figi=[figi]).last_prices
            if lp:
                p = lp[0].price
                last = p.units + p.nano/1e9
                print(f"Last price ≈ {last:.2f} RUB")
        except Exception as e:
            print("warn: last price not available:", e)

        if not allow_place:
            print(f"[DRY] Готов поставить MARKET BUY {qty} лот(ов) {ticker}. Включи ALLOW_PLACE=true в .env для реальной постановки.")
            return

        print("Placing MARKET BUY…")
        resp = post_order_safe_sync(
            client,
            account_id=account,
            instrument_id=uid,  # в v2 разрешён FIGI или instrument_uid
            quantity=qty,
            direction=OrderDirection.ORDER_DIRECTION_BUY,
            order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=str(uuid4()),
        )
        print("Order response:", resp)

if __name__ == "__main__":
    main()
