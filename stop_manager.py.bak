# stop_manager.py — постановка SL/TP из pending_stops.csv
# Фичи: антидубликаты, опц. отмена прежних, лог в logs/stops_placed.csv, авто-очистка очереди

import os, csv, logging
from decimal import Decimal, InvalidOperation
from datetime import datetime
import pandas as pd
from dotenv import load_dotenv
from tinkoff.invest import Client, StopOrderDirection, StopOrderType, StopOrderExpirationType
from tinkoff.invest.utils import quotation_to_decimal

from trade_utils.csv_helper import load_pending_stops, validate_row
from trade_utils.position_helper import get_instrument_uid, find_position, get_position_details
from trade_utils.price_helper import place_stop_order

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

def ensure_logs_dir():
    os.makedirs("logs", exist_ok=True)

def log_stop(order_id: str, ticker: str, kind: str, qty: int, price: Decimal):
    ensure_logs_dir()
    path = "logs/stops_placed.csv"
    new_file = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["ts", "ticker", "kind", "qty", "price", "stop_order_id"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), ticker, kind.upper(), qty, str(price), order_id])

def has_same_stop(client: Client, account_id: str, instrument_uid: str,
                  direction: StopOrderDirection, kind: str, stop_price_dec: Decimal) -> bool:
    """Есть ли уже активный стоп с теми же параметрами"""
    stop_type = StopOrderType.STOP_ORDER_TYPE_STOP_LOSS if kind.lower() in ("sl","stop_loss","stoploss") \
                else StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT
    resp = client.stop_orders.get_stop_orders(account_id=account_id)
    for so in resp.stop_orders:
        if (so.instrument_uid == instrument_uid
            and so.direction == direction
            and so.order_type == stop_type
            and moneyvalue_to_decimal(so.stop_price) == stop_price_dec):
            return True
    return False

def cancel_all_stops_for_uid(client: Client, account_id: str, instrument_uid: str):
    resp = client.stop_orders.get_stop_orders(account_id=account_id)
    for so in resp.stop_orders:
        if so.instrument_uid == instrument_uid:
            client.stop_orders.cancel_stop_order(account_id=account_id, stop_order_id=so.stop_order_id)

def main():
    logging.info("🚦 Запуск stop_manager.py")
    load_dotenv()

    token = os.getenv("TINKOFF_TOKEN")
    account_id = os.getenv("TINKOFF_ACCOUNT_ID")
    cancel_existing = str(os.getenv("CANCEL_EXISTING_FIRST", "0")).lower() in ("1","true","yes")

    if not token or not account_id:
        logging.error("❌ TOKEN или ACCOUNT_ID не заданы в окружении.")
        return

    df = load_pending_stops("pending_stops.csv")
    if df.empty:
        logging.info("📭 Нет заявок для обработки.")
        return

    # никаких applymap — работаем с типами аккуратно
    to_keep_idx = []
    already_cancelled = set()

    with Client(token) as client:
        for idx, row in df.iterrows():
            if not validate_row(row):
                to_keep_idx.append(idx)
                continue

            ticker = str(row["ticker"]).strip()
            class_code = str(row["class_code"]).strip()
            stop_price = row["stop_price"]
            target_price = row["target_price"]

            try:
                instrument_uid = get_instrument_uid(client, ticker, class_code)
                if not instrument_uid:
                    logging.warning(f"⚠️ Не найден инструмент {ticker}/{class_code}, оставляю в очереди…")
                    to_keep_idx.append(idx); continue
                logging.info(f"🔍 Инструмент найден: {ticker} → uid={instrument_uid}")

                position = find_position(client, account_id, instrument_uid)
                if position is None:
                    logging.info(f"ℹ️ Позиции по {ticker} нет, оставляю в очереди.")
                    to_keep_idx.append(idx); continue

                details = get_position_details(position)
                direction = details["direction"]
                quantity = details["qty"]
                if direction == "none" or quantity == 0:
                    logging.info(f"ℹ️ Нулевая позиция по {ticker}, оставляю в очереди.")
                    to_keep_idx.append(idx); continue

                dir_enum = (StopOrderDirection.STOP_ORDER_DIRECTION_SELL
                            if direction == "long"
                            else StopOrderDirection.STOP_ORDER_DIRECTION_BUY)
                logging.info(f"📌 Направление позиции: {direction.upper()}, объём: {quantity}")

                fulfilled_sl = False
                fulfilled_tp = False

                if cancel_existing and instrument_uid not in already_cancelled:
                    logging.info(f"🧹 Отмена всех активных стопов для {ticker} перед постановкой новых…")
                    cancel_all_stops_for_uid(client, account_id, instrument_uid)
                    already_cancelled.add(instrument_uid)

                if pd.notna(stop_price):
                    try:
                        sp_dec = Decimal(str(stop_price))
                        if has_same_stop(client, account_id, instrument_uid, dir_enum, "sl", sp_dec):
                            logging.info(f"⏭️ SL уже существует для {ticker} @ {sp_dec}, пропускаю.")
                            fulfilled_sl = True
                        else:
                            logging.info(f"📉 Размещение SL для {ticker} @ {sp_dec}")
                            stop_id = place_stop_order(
                                client=client, account_id=account_id, instrument_uid=instrument_uid,
                                quantity=quantity, direction=dir_enum, stop_price=sp_dec, kind="sl",
                                expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
                            )
                            log_stop(stop_id, ticker, "SL", quantity, sp_dec)
                            fulfilled_sl = True
                    except InvalidOperation:
                        logging.warning(f"⚠️ Некорректная цена SL для {ticker}: {stop_price}")
                    except Exception as e:
                        logging.error(f"💥 Ошибка при постановке SL для {ticker}: {e}")

                if pd.notna(target_price):
                    try:
                        tp_dec = Decimal(str(target_price))
                        if has_same_stop(client, account_id, instrument_uid, dir_enum, "tp", tp_dec):
                            logging.info(f"⏭️ TP уже существует для {ticker} @ {tp_dec}, пропускаю.")
                            fulfilled_tp = True
                        else:
                            logging.info(f"🎯 Размещение TP для {ticker} @ {tp_dec}")
                            stop_id = place_stop_order(
                                client=client, account_id=account_id, instrument_uid=instrument_uid,
                                quantity=quantity, direction=dir_enum, stop_price=tp_dec, kind="tp",
                                expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
                            )
                            log_stop(stop_id, ticker, "TP", quantity, tp_dec)
                            fulfilled_tp = True
                    except InvalidOperation:
                        logging.warning(f"⚠️ Некорректная цена TP для {ticker}: {target_price}")
                    except Exception as e:
                        logging.error(f"💥 Ошибка при постановке TP для {ticker}: {e}")

                needs_sl = pd.notna(row["stop_price"])
                needs_tp = pd.notna(row["target_price"])
                done_all = ((not needs_sl) or fulfilled_sl) and ((not needs_tp) or fulfilled_tp)
                if not done_all:
                    to_keep_idx.append(idx)

            except Exception as e:
                logging.error(f"💥 Ошибка при обработке {ticker}: {e}")
                to_keep_idx.append(idx)

    new_df = df.loc[to_keep_idx]
    new_df.to_csv("pending_stops.csv", index=False)
    logging.info("🏁 stop_manager.py завершён.")

from tinkoff.invest import MoneyValue

def moneyvalue_to_decimal(m: MoneyValue):
    from decimal import Decimal
    return Decimal(m.units) + (Decimal(m.nano) / Decimal(1_000_000_000))

if __name__ == "__main__":
    main()

