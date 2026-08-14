
# stop_manager.py — Variant A (StopOrdersService.post_stop_order)
# Rules:
# - SL/TP only via post_stop_order()
# - Direction & qty from actual portfolio
# - Supported: shares (TQBR -> PRICE_TYPE_CURRENCY), futures (else -> PRICE_TYPE_POINT)
# - No duplicates: skip if same type (SL/TP) already active for instrument
# - Robust CSV parsing (skip bad rows)
# - All orders are MARKET via ExchangeOrderType.MARKET
# - Library: tinkoff-investments v0.2.0b59

import os
import csv
import logging
import argparse
from uuid import uuid4
from decimal import Decimal
from typing import Optional, Tuple, Dict, Set

from dotenv import load_dotenv
from tinkoff.invest import (
    Client,
    Quotation,
    StopOrderType,
    StopOrderDirection,
    StopOrderExpirationType,
    ExchangeOrderType,
    InstrumentIdType,
    PriceType,
)
from tinkoff.invest.utils import decimal_to_quotation as dq

from initial_stop_cache import record_initial_sl

# ---------- logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("stop_manager")

# ---------- constants ----------
CSV_PATH   = "pending_stops.csv"              # header: ticker,class_code,stop_price,target_price
LOG_PLACED = "logs/stops_placed.csv"

load_dotenv()
ACCOUNT_ID = os.getenv("TINKOFF_ACCOUNT_ID")
TOKEN      = os.getenv("TINKOFF_TOKEN")

# ---------- utils ----------
def to_dec(v) -> Optional[Decimal]:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except Exception:
        return None

def quotation_to_float(q: Optional[Quotation]) -> Optional[float]:
    if not q:
        return None
    return float(q.units) + float(q.nano) / 1e9

def price_type_for_class(class_code: str) -> PriceType:
    return PriceType.PRICE_TYPE_CURRENCY if (class_code or "").upper() == "TQBR" else PriceType.PRICE_TYPE_POINT

def find_instrument_uid(c: Client, ticker: str, class_code: str) -> Tuple[str, str]:
    cc = (class_code or "").upper()
    if cc == "TQBR":
        r = c.instruments.share_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
            id=ticker,
            class_code=cc,
        )
    else:
        r = c.instruments.future_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
            id=ticker,
            class_code=cc,
        )
    return r.instrument.uid, r.instrument.figi

def get_position_lots_and_side(c: Client, uid: str) -> Tuple[int, str]:
    """
    Returns (lots, side) where side in {'LONG','SHORT','FLAT'} based on actual portfolio.
    Lots are integer lots.
    """
    pos = c.operations.get_portfolio(account_id=ACCOUNT_ID).positions
    for p in pos:
        if p.instrument_uid == uid:
            lots = getattr(p, "quantity_lots", None)
            units = int(getattr(lots, "units", 0) or 0)
            if units > 0:
                return units, "LONG"
            if units < 0:
                return abs(units), "SHORT"
            return 0, "FLAT"
    return 0, "FLAT"

def load_active_stop_types(c: Client) -> Dict[str, Set[StopOrderType]]:
    """
    Map instrument_uid -> set of active stop order types.
    """
    res: Dict[str, Set[StopOrderType]] = {}
    items = c.stop_orders.get_stop_orders(account_id=ACCOUNT_ID).stop_orders
    for o in items:
        uid = o.instrument_uid
        t   = o.stop_order_type
        res.setdefault(uid, set()).add(t)
    return res

def ensure_logs_header():
    os.makedirs("logs", exist_ok=True)
    if not os.path.exists(LOG_PLACED):
        with open(LOG_PLACED, "w", encoding="utf-8", newline="") as f:
            f.write("timestamp,ticker,figi,side,quantity,planned_stop,actual_stop,planned_target,actual_target\n")

def write_result(ts: str, ticker: str, figi: str, side: str, qty: int,
                 planned_sl: Optional[Decimal], actual_sl: str,
                 planned_tp: Optional[Decimal], actual_tp: str):
    ensure_logs_header()
    with open(LOG_PLACED, "a", encoding="utf-8", newline="") as f:
        f.write("{},{},{},{},{},{},{},{},{}\n".format(
            ts, ticker, figi, side, qty,
            "" if planned_sl is None else str(planned_sl),
            actual_sl,
            "" if planned_tp is None else str(planned_tp),
            actual_tp
        ))

def place_stop(
    c: Client,
    uid: str,
    figi: str,
    qty: int,
    side: str,                # 'LONG' or 'SHORT'
    stop_price: Decimal,      # trigger
    price_type: PriceType,
    kind: StopOrderType,      # STOP_ORDER_TYPE_STOP_LOSS / TAKE_PROFIT
    dry: bool = False,
) -> str:
    """
    Places a MARKET stop (SL/TP). Returns order_id or raises.
    """
    direction = StopOrderDirection.STOP_ORDER_DIRECTION_SELL if side == "LONG" else StopOrderDirection.STOP_ORDER_DIRECTION_BUY
    kind_name = "SL" if kind == StopOrderType.STOP_ORDER_TYPE_STOP_LOSS else "TP"
    sp = dq(stop_price)

    log.info("[%s] post_stop_order: uid=%s qty=%s dir=%s stop=%s type=%s (market)",
             kind_name, uid, qty, direction.name, stop_price, kind.name)

    if dry:
        return "DRY"

    # Per official docs: instrumentId (figi or instrument_uid), exchangeOrderType, priceType are supported.
    # GOOD_TILL_CANCEL is the correct enum spelling.
    # TP: price is required by API, SL: price may be omitted, but we set = stop for consistency.
    resp = c.stop_orders.post_stop_order(
        account_id=ACCOUNT_ID,
        instrument_id=uid,  # UID is fine (docs: instrumentId accepts figi or instrument_uid)
        quantity=qty,
        direction=direction,
        stop_order_type=kind,
        expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
        exchange_order_type=ExchangeOrderType.EXCHANGE_ORDER_TYPE_MARKET,
        price_type=price_type,
        stop_price=sp,
        price=sp,  # keep equal to stop for MARKET; TP requires price, SL tolerates it
        order_id=str(uuid4()),
    )
    return resp.stop_order_id if hasattr(resp, "stop_order_id") else "OK"

def process_row(c: Client, row: dict, dry: bool = False):
    # Parse & validate row
    ticker = (row.get("ticker") or "").strip().upper()
    class_code = (row.get("class_code") or "").strip().upper()
    sp = to_dec(row.get("stop_price"))
    tp = to_dec(row.get("target_price"))

    if not ticker or not class_code or (sp is None and tp is None):
        raise ValueError("bad row: need ticker,class_code and at least one of stop_price/target_price")

    uid, figi = find_instrument_uid(c, ticker, class_code)
    qty, side = get_position_lots_and_side(c, uid)
    if qty <= 0 or side == "FLAT":
        log.info("Skip %s:%s — no position (qty=%s).", ticker, class_code, qty)
        return

    pt = price_type_for_class(class_code)
    active_by_uid = load_active_stop_types(c)

    # SL
    sl_id, tp_id = "", ""
    if sp is not None:
        if uid in active_by_uid and StopOrderType.STOP_ORDER_TYPE_STOP_LOSS in active_by_uid[uid]:
            log.info("[SL] %s already active — skip.", ticker)
        else:
            sl_id = place_stop(c, uid, figi, qty, side, sp, pt, StopOrderType.STOP_ORDER_TYPE_STOP_LOSS, dry=dry)
            if sl_id and sl_id != "DRY":
                # Единственное место в пайплайне, где исходный SL достоверно
                # известен — dynamic_stop_manager.py::compute_new_sl_price()
                # нужен именно этот, а не текущий (уже возможно сдвинутый)
                # стоп, см. initial_stop_cache.py.
                record_initial_sl(uid, float(sp), side.lower())
                log.info("📌 initial_stop_cache: записан исходный SL для %s uid=%s -> %.4f", ticker, uid, float(sp))

    # TP
    if tp is not None:
        if uid in active_by_uid and StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT in active_by_uid[uid]:
            log.info("[TP] %s already active — skip.", ticker)
        else:
            tp_id = place_stop(c, uid, figi, qty, side, tp, pt, StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT, dry=dry)

    from datetime import datetime, timezone
    write_result(datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 ticker, figi, side, qty, sp, sl_id or ("DRY" if dry else "SKIP"),
                 tp, tp_id or ("DRY" if dry else "SKIP"))

def main():
    parser = argparse.ArgumentParser(description="Place SL/TP via StopOrdersService.")
    parser.add_argument("--list", dest="list_mode", action="store_true", help="dry-run (list only)")
    parser.add_argument("--list-only", dest="list_mode", action="store_true", help="alias for --list")
    parser.add_argument("--place", action="store_true", help="place orders for rows in CSV")
    parser.add_argument("--csv", default=CSV_PATH, help="path to pending_stops.csv")
    args = parser.parse_args()

    if not TOKEN or not ACCOUNT_ID:
        raise RuntimeError("TINKOFF_TOKEN / TINKOFF_ACCOUNT_ID are not set")

    if not os.path.exists(args.csv):
        log.info("No %s — nothing to do.", args.csv)
        return

    with open(args.csv, "r", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        rows = list(rdr)

    if not rows:
        log.info("%s is empty — nothing to do.", args.csv)
        return

    dry = args.list_mode and not args.place

    with Client(TOKEN) as c:
        for row in rows:
            try:
                process_row(c, row, dry=dry)
            except Exception as e:
                log.error("Row error %r: %s", row, e)

if __name__ == "__main__":
    main()
