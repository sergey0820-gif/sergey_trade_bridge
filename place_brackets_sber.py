# ~/sergey_trade_bridge/place_brackets_sber.py
import os, sys, math, traceback
from uuid import uuid4
from dotenv import load_dotenv

load_dotenv(".env")

TOKEN = os.getenv("TINKOFF_TOKEN") or os.getenv("TINKOFF_INVEST_TOKEN")
ACC   = os.getenv("TINKOFF_ACCOUNT_ID")
ALLOW_PLACE = os.getenv("ALLOW_PLACE", "false").lower() in ("1","true","yes","on")

TICKER = "SBER"
CLASS  = "TQBR"
QTY    = 1      # сколько закрывать в стоп-ордерах (в лотах)
SL_PCT = 0.01   # 1% вниз
TP_PCT = 0.02   # 2% вверх

def to_q(x: float):
    from tinkoff.invest import Quotation
    sign = -1 if x < 0 else 1
    x = abs(x)
    units = int(math.floor(x))
    nano  = int(round((x - units) * 1_000_000_000))
    if nano == 1_000_000_000:
        units += 1
        nano = 0
    units *= sign
    return Quotation(units=units, nano=nano)

def find_figi(client, ticker, class_code):
    from tinkoff.invest import InstrumentIdType
    # 1) через get_instrument_by (по тикеру)
    try:
        inst = client.instruments.get_instrument_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
            id=ticker
        ).instrument
        if inst and getattr(inst, "class_code", "") == class_code:
            return inst.figi, getattr(inst, "lot", 1) or 1
    except Exception:
        pass
    # 2) резерв: через список акций
    shares = client.instruments.shares().instruments
    for s in shares:
        if getattr(s, "ticker", "") == ticker and getattr(s, "class_code", "") == class_code:
            return s.figi, getattr(s, "lot", 1) or 1
    return None, None

def last_price(client, figi):
    lp = client.market_data.get_last_prices(figi=[figi]).last_prices
    if not lp:
        return None
    p = lp[0].price
    return p.units + p.nano/1e9

def place_brackets():
    if not TOKEN or not ACC:
        print("ERROR: нет TINKOFF_TOKEN/TINKOFF_INVEST_TOKEN или TINKOFF_ACCOUNT_ID в .env")
        sys.exit(1)

    from tinkoff.invest import (
        Client,
        OrderDirection,
        StopOrderType,
        StopOrderExpirationType,
    )

    print("LIVE mode (постановка стопов)" if ALLOW_PLACE else "Dry-run mode (ALLOW_PLACE!=true)")

    with Client(TOKEN) as client:
        # FIGI
        figi, lot = find_figi(client, TICKER, CLASS)
        if not figi:
            print("ERROR: FIGI для SBER/TQBR не найден")
            sys.exit(2)
        print(f"FOUND: {TICKER} {CLASS} figi={figi} lot={lot}")

        # текущая цена как ориентир для уровней
        px = last_price(client, figi)
        if not px:
            print("WARN: нет last price — уровни считаем от 300.00")
            px = 300.00
        print(f"Last ≈ {px:.2f}")

        entry  = px
        sl_lvl = round(entry * (1 - SL_PCT), 2)
        tp_lvl = round(entry * (1 + TP_PCT), 2)
        print(f"Levels → SL: {sl_lvl:.2f}  |  TP: {tp_lvl:.2f}  (qty={QTY})")

        if not ALLOW_PLACE:
            print("[DRY] Ставили бы два стоп-ордера SELL: STOP_LOSS и TAKE_PROFIT")
            return

        # STOP LOSS (sell), stop_limit: триггер=stop_price, лимит=price (ставим равно)
        try:
            resp_sl = client.stop_orders.post_stop_order(
                account_id=ACC,
                figi=figi,
                quantity=QTY,
                price=to_q(sl_lvl),            # лимит-цена
                stop_price=to_q(sl_lvl),       # триггер
                direction=OrderDirection.ORDER_DIRECTION_SELL,
                stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LOSS,
                expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
            )
            print("✅ SL placed:", resp_sl)
        except TypeError as te:
            print("TypeError SL (попробуйте instrument_id=):", te)
            try:
                resp_sl = client.stop_orders.post_stop_order(
                    account_id=ACC,
                    instrument_id=figi,
                    quantity=QTY,
                    price=to_q(sl_lvl),
                    stop_price=to_q(sl_lvl),
                    direction=OrderDirection.ORDER_DIRECTION_SELL,
                    stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LOSS,
                    expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
                )
                print("✅ SL placed (instrument_id):", resp_sl)
            except Exception:
                print("SL error:")
                traceback.print_exc()
        except Exception:
            print("SL error:")
            traceback.print_exc()

        # TAKE PROFIT (sell)
        try:
            resp_tp = client.stop_orders.post_stop_order(
                account_id=ACC,
                figi=figi,
                quantity=QTY,
                price=to_q(tp_lvl),
                stop_price=to_q(tp_lvl),
                direction=OrderDirection.ORDER_DIRECTION_SELL,
                stop_order_type=StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT,
                expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
            )
            print("✅ TP placed:", resp_tp)
        except TypeError as te:
            print("TypeError TP (попробуйте instrument_id=):", te)
            try:
                resp_tp = client.stop_orders.post_stop_order(
                    account_id=ACC,
                    instrument_id=figi,
                    quantity=QTY,
                    price=to_q(tp_lvl),
                    stop_price=to_q(tp_lvl),
                    direction=OrderDirection.ORDER_DIRECTION_SELL,
                    stop_order_type=StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT,
                    expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
                )
                print("✅ TP placed (instrument_id):", resp_tp)
            except Exception:
                print("TP error:")
                traceback.print_exc()
        except Exception:
            print("TP error:")
            traceback.print_exc()

if __name__ == "__main__":
    place_brackets()
