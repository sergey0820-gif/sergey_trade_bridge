from trade_utils.orders import post_order_safe
from uuid import uuid4
# trade_executor.py

import argparse
import os
import logging
from datetime import datetime
from dotenv import load_dotenv
from tinkoff.invest import Client, OrderDirection, OrderType

# Загрузка переменных среды
load_dotenv()
TOKEN = os.getenv("TINKOFF_TOKEN")
ACCOUNT_ID = os.getenv("TINKOFF_ACCOUNT_ID")
CAPITAL = float(os.getenv("CAPITAL", 100000))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", 0.01))

# Логирование
logging.basicConfig(
    filename="logs/trade_executor.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

def find_instrument(client, ticker, class_code):
    instruments = client.instruments
    if class_code == "TQBR":
        items = instruments.shares().instruments
    elif class_code == "SPBFUT":
        items = instruments.futures().instruments
    else:
        return None
    for item in items:
        if item.ticker == ticker and item.class_code == class_code:
            return item
    return None

def calculate_quantity(entry, stop, lot):
    risk_amount = CAPITAL * RISK_PER_TRADE
    loss_per_unit = abs(entry - stop)
    if loss_per_unit == 0:
        return 0
    qty = int(risk_amount / loss_per_unit)
    return max(1, (qty // lot) * lot)

def place_order(ticker, class_code, side, entry, stop, target):
    with Client(TOKEN) as client:
        instrument = find_instrument(client, ticker, class_code)
        if not instrument:
            raise Exception(f"Инструмент не найден: {ticker} {class_code}")

        figi = instrument.figi
        lot = instrument.lot
        quantity = calculate_quantity(entry, stop, lot)
        if quantity == 0:
            raise Exception("Недопустимо малый риск или равен 0")

        direction = OrderDirection.ORDER_DIRECTION_BUY if side == "buy" else OrderDirection.ORDER_DIRECTION_SELL

        order = await post_order_safe(client, 
            figi=figi,
            quantity=quantity,
            direction=direction,
            account_id=ACCOUNT_ID,
            order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=str(uuid4())))).uuid4())).uuid4())).strftime('%H%M%S')}"
        )

        # Сохраняем в pending_stops.csv
        os.makedirs("out", exist_ok=True)
        with open("out/pending_stops.csv", "a") as f:
            f.write(f"{order.order_id},{ticker},{figi},{quantity},{stop},{target},"
                    f"{entry},{datetime.now().isoformat()},{side},{entry},open,,,\n")

        logging.info(f"✅ Размещена заявка: {ticker} {side} x{quantity} по рынку")
        print(f"✅ Заявка размещена: {ticker} {side} x{quantity}")

# === Точка входа ===
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Размещение заявки")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--class_code", required=True, choices=["TQBR", "SPBFUT"])
    parser.add_argument("--side", required=True, choices=["buy", "sell"])
    parser.add_argument("--entry", type=float, required=True)
    parser.add_argument("--stop", type=float, required=True)
    parser.add_argument("--target", type=float, required=True)

    args = parser.parse_args()

    try:
        place_order(
            ticker=args.ticker,
            class_code=args.class_code,
            side=args.side,
            entry=args.entry,
            stop=args.stop,
            target=args.target
        )
    except Exception as e:
        logging.error(f"❌ Ошибка: {e}")
        print(f"❌ Ошибка: {e}")


# === helper: безопасная постановка заявки через общую обёртку ===
async def place_order_safe(c, *, account_id, ticker, class_code, qty_lots, direction, order_type, dry_run=False):
    # TODO: если у тебя другие локальные названия переменных, просто поменяй аргументы при вызове
    return await post_order_safe(
        c=c,
        account_id=account_id,
        ticker=ticker,
        class_code=class_code,
        qty_lots=int(qty_lots),
        direction=direction,
        order_type=order_type,
        dry_run=dry_run,
    )
