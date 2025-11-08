#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, time, csv
from decimal import Decimal
from datetime import datetime, timezone
from dotenv import load_dotenv
from tinkoff.invest import Client, StopOrderDirection, StopOrderType, StopOrderExpirationType, Quotation

LOG_PATH = "logs/orders_watch.log"
PENDING_PATH = "out/pending_stops.csv"
DONE_PATH = "out/pending_stops.done.csv"
os.makedirs("logs", exist_ok=True)
os.makedirs("out", exist_ok=True)

def log(msg: str):
    ts = datetime.now(timezone.utc).isoformat()
    line = f"{ts} {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def to_q(x: Decimal) -> Quotation:
    s = f"{x:.9f}"
    units, _, frac = s.partition(".")
    units = int(units)
    nano = int(frac[:9].ljust(9, "0"))
    return Quotation(units=units, nano=nano)

def load_pending():
    if not os.path.exists(PENDING_PATH):
        return {}
    out = {}
    with open(PENDING_PATH, "r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            out[row["order_id"]] = row
    return out

def write_done(row):
    new = not os.path.exists(DONE_PATH)
    with open(DONE_PATH, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts","order_id","ticker","figi","lots","stop","target"])
        w.writerow([datetime.now(timezone.utc).isoformat(),
                    row["order_id"], row["ticker"], row["figi"],
                    row["lots"], row["stop"], row["target"]])

def rewrite_pending(pending: dict, done_ids: set):
    if not os.path.exists(PENDING_PATH):
        return
    with open(PENDING_PATH, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    left = [r for r in rows if r["order_id"] not in done_ids]
    with open(PENDING_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["order_id","ticker","figi","lots","stop","target","tick"])
        w.writeheader()
        w.writerows(left)

def place_stops(cli: Client, account_id: str, row: dict):
    figi = row["figi"]
    lots = int(row["lots"])
    stop = Decimal(row["stop"])
    target = Decimal(row["target"])
    # SELL для выхода из лонга
    cli.stop_orders.post_stop_order(
        account_id=account_id,
        figi=figi,
        quantity=lots,
        price=to_q(stop),
        stop_price=to_q(stop),
        direction=StopOrderDirection.STOP_ORDER_DIRECTION_SELL,
        stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LOSS,
        expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
    )
    cli.stop_orders.post_stop_order(
        account_id=account_id,
        figi=figi,
        quantity=lots,
        price=to_q(target),
        stop_price=to_q(target),
        direction=StopOrderDirection.STOP_ORDER_DIRECTION_SELL,
        stop_order_type=StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT,
        expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
    )

def main():
    load_dotenv(".env")
    token = os.getenv("TINKOFF_TOKEN") or os.getenv("TINKOFF_INVEST_TOKEN")
    account_id = os.getenv("TINKOFF_ACCOUNT_ID")

    log("orders_watch: started")
    while True:
        try:
            pending = load_pending()
            if not pending:
                time.sleep(5)
                continue

            with Client(token) as cli:
                res = cli.orders.get_orders(account_id=account_id).orders
                done_ids = set()
                for o in res:
                    oid = o.order_id
                    if oid in pending and o.execution_report_status.name in ("EXECUTION_REPORT_STATUS_FILL", "EXECUTION_REPORT_STATUS_PARTIALLYFILL"):
                        row = pending[oid]
                        try:
                            place_stops(cli, account_id, row)
                            log(f"[STOPS] {row['ticker']}: SL/TP placed for order {oid}")
                            write_done(row)
                            done_ids.add(oid)
                        except Exception as e:
                            log(f"[ERR] stops for {row['ticker']} ({oid}): {e}")

                if done_ids:
                    rewrite_pending(pending, done_ids)

            time.sleep(5)

        except KeyboardInterrupt:
            log("orders_watch: stopped by user")
            break
        except Exception as e:
            log(f"[ERR] loop: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
