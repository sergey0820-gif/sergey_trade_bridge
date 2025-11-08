# stop_manager.py

import os
import logging
import pandas as pd
from decimal import Decimal
from tinkoff.invest import (
from trade_utils.orders import post_order_safe_sync
    Client,
    OrderDirection,
    OrderType,
    Quotation,
    InstrumentIdType,
)
from tinkoff.invest.services import InstrumentsService
from dotenv import load_dotenv
from datetime import datetime

# === Настройка логирования ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler()]
)

logging.info("🚦 Запуск stop_manager.py")

# === Загрузка переменных окружения ===
load_dotenv()
TOKEN = os.getenv("TINKOFF_TOKEN")
ACCOUNT_ID = os.getenv("TINKOFF_ACCOUNT_ID")

# === Пути к CSV ===
CSV_PATH = "pending_stops.csv"
LOG_PATH = "logs/stops_placed.csv"

# === Утилиты ===
def quotation_to_float(q: Quotation) -> float:
    return q.units + q.nano / 1e9 if q else 0.0

def decimal_to_quotation(d: float) -> Quotation:
    d = Decimal(str(d))
    units = int(d)
    nano = int((d - units) * Decimal(1e9))
    return Quotation(units=units, nano=nano)

def get_open_positions_by_figi(client: Client) -> dict:
    response = client.operations.get_portfolio(account_id=ACCOUNT_ID)
    positions = {
        p.figi: int(p.quantity.units)
        for p in response.positions
        if int(p.quantity.units) > 0
    }
    logging.info("📥 Получен портфель")
    logging.info(f"📥 Найдено открытых позиций: {len(positions)}")
    return positions

def find_instrument(service: InstrumentsService, ticker: str, class_code: str):
    response = service.find_instrument(query=ticker)
    for instr in response.instruments:
        if instr.class_code == class_code:
            return instr
    raise ValueError(f"Инструмент {ticker} с class_code={class_code} не найден")

# === Основная логика ===
def place_stop_orders():
    df = pd.read_csv(CSV_PATH)
    df.fillna("", inplace=True)
    try:
        df["qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0).astype(int)
    except Exception as e:
        logging.error(f"Ошибка обработки qty: {e}")
        df["qty"] = 0

    with Client(TOKEN) as client:
        positions = get_open_positions_by_figi(client)
        service = client.instruments

        placed_rows = []

        for _, row in df.iterrows():
            ticker = row["ticker"]
            class_code = row["class_code"]
            side = row["side"]
            entry = float(row["entry"])
            stop = float(row["stop"])
            target = float(row["target"])
            qty = int(row["qty"]) if row["qty"] else 0

            logging.info(f"🔍 Обработка {ticker}: entry={entry}, stop={stop}, target={target}, qty={qty}")

            try:
                instrument = find_instrument(service, ticker, class_code)
                figi = instrument.figi

                if figi not in positions:
                    logging.info(f"📉 Нет открытой позиции для {ticker}, пропускаем")
                    continue

                # Определяем сторону заявки
                direction = OrderDirection.ORDER_DIRECTION_SELL if side == "buy" else OrderDirection.ORDER_DIRECTION_BUY

                # === Stop Loss ===
                stop_price = decimal_to_quotation(stop)
                price = decimal_to_quotation(entry)

                sl_order = post_order_safe_sync(client, 
                    order_id=str(__import__("uuid").uuid4())).uuid4())).timestamp()}",
                    figi=figi,
                    quantity=qty,
                    direction=direction,
                    account_id=ACCOUNT_ID,
                    order_type=OrderType.ORDER_TYPE_STOP_LIMIT,
                    price=price,
                    stop_price=stop_price
                )
                sl_order_id=str(__import__("uuid").uuid4())).uuid4())

                # === Take Profit ===
                tp_direction = direction
                tp_price = decimal_to_quotation(target)

                tp_order = post_order_safe_sync(client, 
                    order_id=str(__import__("uuid").uuid4())).uuid4())).timestamp()}",
                    figi=figi,
                    quantity=qty,
                    direction=tp_direction,
                    account_id=ACCOUNT_ID,
                    order_type=OrderType.ORDER_TYPE_LIMIT,
                    price=tp_price
                )
                tp_order_id=str(__import__("uuid").uuid4())).uuid4())

                placed_rows.append({
                    "ticker": ticker,
                    "figi": figi,
                    "side": side,
                    "qty": qty,
                    "stop_price": stop,
                    "target_price": target,
                    "sl_order_id": sl_order_id,
                    "tp_order_id": tp_order_id,
                    "time": datetime.now().isoformat()
                })

                # Обновляем статус
                df.loc[(df["ticker"] == ticker) & (df["class_code"] == class_code), "status"] = "placed"
                df.loc[(df["ticker"] == ticker) & (df["class_code"] == class_code), "sl_order_id"] = sl_order_id
                df.loc[(df["ticker"] == ticker) & (df["class_code"] == class_code), "tp_order_id"] = tp_order_id

                logging.info(f"✅ Заявки по {ticker} размещены (SL: {stop}, TP: {target})")

            except Exception as e:
                logging.error(f"🚨 Ошибка при обработке строки: {row.to_dict()} → {e}")
                df.loc[(df["ticker"] == row["ticker"]) & (df["class_code"] == row["class_code"]), "status"] = "error"
                df.loc[(df["ticker"] == row["ticker"]) & (df["class_code"] == row["class_code"]), "error"] = str(e)

        # Сохраняем обновлённый pending_stops.csv
        df.to_csv(CSV_PATH, index=False)

        # Добавляем в лог
        if placed_rows:
            if os.path.exists(LOG_PATH):
                old_log = pd.read_csv(LOG_PATH)
                new_log = pd.DataFrame(placed_rows)
                pd.concat([old_log, new_log]).to_csv(LOG_PATH, index=False)
            else:
                pd.DataFrame(placed_rows).to_csv(LOG_PATH, index=False)

        logging.info("🏁 stop_manager.py завершён.")

# === Запуск ===
if __name__ == "__main__":
    place_stop_orders()
