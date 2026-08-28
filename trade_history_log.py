#!/usr/bin/env python3
"""
trade_history_log.py — сплошной лог ВСЕХ сигналов (прошедших ИИ-проверку
и отклонённых — все в одном файле) с реальными данными сделки (вход/выход
в рублях, комиссия, P&L) там, где сигнал реально привёл к исполнению;
для остальных сигналов эти колонки просто пустые. Плюс дневные свечи/
объёмы по каждому реально торгованному инструменту для анализа.

Источники:
  - logs/signal_journal.csv — ВСЕ сигналы (прошли/не прошли правила,
    прошли/не прошли LLM, исполнены/протухли/ошибка исполнителя) —
    перечитывается целиком каждый запуск, это локальный файл, не API.
  - client.operations.get_operations_by_cursor — РЕАЛЬНЫЕ сделки по счёту.
    Тут используется ИНКРЕМЕНТАЛЬНЫЙ режим (по умолчанию): каждый запуск
    запрашивает только новые операции с момента последнего запуска
    (водяной знак в logs/trade_history_state.json) — при первом запуске
    последние 14 дней. Найденные новые реальные сделки дописываются в
    внутренний logs/trade_history_real_trades.csv (накопительно, не
    трогая старые). Позиции, открытые на конец прошлого запуска,
    переносятся в состояние — длинные (>14 дней) сделки не теряются на
    границе окна. --rebuild игнорирует водяной знак и перечитывает все
    реальные операции с AUTO_STRATEGY_START заново (редко, например для
    восстановления доверия к данным).

Итоговый файл logs/trade_history_log.csv — ОДИН сигнал = ОДНА строка,
полностью пересобирается каждый запуск (join сигналов с накопленными
реальными сделками — это дёшево, без обращений к API) и целиком
перезаписывается в Google Sheets (--push-sheets). Так сигнал, который был
"executed" и сначала открыт, а закрылся позже, корректно дополняется
данными о выходе в следующем запуске, а не остаётся навсегда неполным.

Сопоставление сигнала с реальной сделкой: по (ticker, class_code,
side==direction) и ближайшей по времени реальной сделке, открытой не
раньше сигнала (окно — 24 часа). Каждая реальная сделка используется
только один раз (защита от повторного приписывания одной сделки
нескольким сигналам).

Только чтение: client.operations.get_operations_by_cursor, client.
instruments.get_instrument_by, client.market_data.get_candles (D1). Ничего
не пишет на счёт, не размещает ордеров.

Цены входа/выхода — в рублях, взяты из фактической выплаты по каждой
операции (payment/qty), а НЕ из price * курс пункта на сегодня — см.
STRATEGY.md, разбор RIU6 (2026-08-25/26).

ВАЖНАЯ ОГОВОРКА про P&L: для акций и однодневных фьючерсных сделок
gross/net P&L точен. Для МНОГОДНЕВНЫХ фьючерсных сделок с несколькими
параллельно открытыми позициями реальный результат идёт через
вариационную маржу (списывается на весь счёт разом, не по инструментам) —
P&L здесь лучшее приближение, не гарантированно совпадающее день-в-день с
отчётом брокера; сверка с официальным отчётом брокера — отдельный, ещё не
закрытый пункт.

Использование:
  python3 trade_history_log.py --push-sheets            # инкрементально + полная пересборка объединённого лога
  python3 trade_history_log.py --rebuild --push-sheets  # реальные сделки — с нуля с AUTO_STRATEGY_START
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH if ENV_PATH.exists() else None)

from tinkoff.invest import Client, CandleInterval, InstrumentIdType
from tinkoff.invest.schemas import GetOperationsByCursorRequest, OperationType

LOGS_DIR = BASE_DIR / "logs"
CANDLES_DIR = LOGS_DIR / "trade_history_candles"
SIGNAL_JOURNAL_PATH = LOGS_DIR / "signal_journal.csv"
REAL_TRADES_PATH = LOGS_DIR / "trade_history_real_trades.csv"  # внутренний, накопительный, только реальные сделки
LOG_PATH = LOGS_DIR / "trade_history_log.csv"  # итоговый, все сигналы + реальные данные где есть
OPEN_POSITIONS_PATH = LOGS_DIR / "trade_history_open_positions.csv"
STATE_PATH = LOGS_DIR / "trade_history_state.json"

AUTO_STRATEGY_START = datetime(2026, 8, 1, tzinfo=timezone.utc)
DEFAULT_LOOKBACK_DAYS = 14  # если state ещё нет — с чего начать первый инкрементальный запуск
SIGNAL_MATCH_WINDOW_HOURS = 24  # макс. задержка между сигналом и реальным входом в позицию

WS_TITLE = "TRADE_HISTORY"
WS_OPEN_TITLE = "TRADE_HISTORY_OPEN"


def load_state() -> dict:
    """{"processed_through": iso, "open_legs": {uid: [leg,...]}} — leg с
    датой как iso-строкой (сериализуется/десериализуется отдельно)."""
    if not STATE_PATH.exists():
        return {"processed_through": None, "open_legs": {}}
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"processed_through": None, "open_legs": {}}
    open_legs = {}
    for uid, legs in raw.get("open_legs", {}).items():
        restored = []
        for leg in legs:
            leg = dict(leg)
            leg["date"] = datetime.fromisoformat(leg["date"])
            restored.append(leg)
        open_legs[uid] = restored
    return {"processed_through": raw.get("processed_through"), "open_legs": open_legs}


def save_state(processed_through: datetime, open_legs: dict) -> None:
    serializable = {}
    for uid, legs in open_legs.items():
        serializable[uid] = [
            {**{k: v for k, v in leg.items() if k != "date"}, "date": leg["date"].isoformat()}
            for leg in legs
        ]
    STATE_PATH.write_text(
        json.dumps({"processed_through": processed_through.isoformat(), "open_legs": serializable},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_signal_journal() -> list[dict]:
    """ВАЖНО: ts в signal_journal.csv записан БЕЗ явного часового пояса,
    но фактически это московское время (крон проекта работает с
    TZ=Europe/Moscow) — не UTC. Проверено на реальных данных: разница
    между ts (как МСК) и временем настоящего исполнения ордера у брокера
    (UTC) — стабильно ~2 минуты (задержка пайплайна), если ts
    интерпретировать как МСК и перевести в UTC; если считать ts уже UTC —
    получается сдвиг на 3 часа и сопоставление с реальными сделками
    ломается. _ts_dt здесь — уже переведённое в UTC время, для join с
    реальными сделками."""
    if not SIGNAL_JOURNAL_PATH.exists():
        return []
    with SIGNAL_JOURNAL_PATH.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    try:
        from zoneinfo import ZoneInfo
        msk = ZoneInfo("Europe/Moscow")
    except Exception:
        msk = timezone(timedelta(hours=3))  # fallback, Россия без перехода на летнее время с 2014
    for r in rows:
        ts = (r.get("ts") or "").strip()
        if not ts:
            r["_ts_dt"] = None
            continue
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=msk)
        r["_ts_dt"] = dt.astimezone(timezone.utc)
    return rows


REAL_TRADES_HEADER = ["uid", "ticker", "class_code", "direction", "qty",
                       "open_date", "open_price_rub", "open_price_quote", "open_commission_rub",
                       "close_date", "close_price_rub", "close_price_quote", "close_commission_rub",
                       "gross_pnl_rub", "commission_rub", "net_pnl_rub"]


def load_real_trades_csv() -> list[dict]:
    """Ранее накопленные реальные закрытые сделки (свои, персистентный
    файл), с распарсенными датами — для join с сигналами."""
    if not REAL_TRADES_PATH.exists():
        return []
    with REAL_TRADES_PATH.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        r["open_date"] = datetime.fromisoformat(r["open_date"])
        r["close_date"] = datetime.fromisoformat(r["close_date"])
        for k in ("qty", "open_price_rub", "open_price_quote", "open_commission_rub",
                  "close_price_rub", "close_price_quote", "close_commission_rub",
                  "gross_pnl_rub", "commission_rub", "net_pnl_rub"):
            r[k] = float(r[k])
        out.append(r)
    return out


def append_real_trades_csv(new_trades_rows: list[list]) -> None:
    is_new = not REAL_TRADES_PATH.exists()
    with REAL_TRADES_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(REAL_TRADES_HEADER)
        w.writerows(new_trades_rows)


def join_signals_with_real_trades(signals: list[dict], closed_trades: list[dict], open_legs_flat: list[dict]) -> list[dict]:
    """ОДИН сигнал = ОДНА строка итогового лога. Для final_status=='executed'
    ищем ближайшую по времени неиспользованную реальную сделку (закрытую
    или ещё открытую) с тем же ticker/class_code/направлением, открытую не
    раньше сигнала и не позже SIGNAL_MATCH_WINDOW_HOURS после него. Остальным
    сигналам (rejected_by_rules/rejected_by_llm/skipped_*) реальные колонки
    просто не заполняются."""
    window = timedelta(hours=SIGNAL_MATCH_WINDOW_HOURS)
    closed_pool = [dict(t, _consumed=False, _kind="closed") for t in closed_trades]
    open_pool = [dict(t, _consumed=False, _kind="open") for t in open_legs_flat]
    pool = closed_pool + open_pool

    out_rows = []
    for sig in sorted(signals, key=lambda r: r.get("_ts_dt") or datetime.min.replace(tzinfo=timezone.utc)):
        row = {
            "ts": sig.get("ts", ""), "ticker": sig.get("ticker", ""), "class_code": sig.get("class_code", ""),
            "side": sig.get("side", ""), "rules_score": sig.get("rules_score", ""),
            "rules_decision": sig.get("rules_decision", ""), "llm_decision": sig.get("llm_decision", ""),
            "final_status": sig.get("final_status", ""),
            "planned_entry": sig.get("entry", ""), "planned_stop": sig.get("stop", ""), "planned_target": sig.get("target", ""),
            "trade_status": "", "real_open_date": "", "real_open_price_rub": "", "real_open_price_quote": "",
            "open_commission_rub": "", "real_close_date": "", "real_close_price_rub": "", "real_close_price_quote": "",
            "close_commission_rub": "", "gross_pnl_rub": "", "commission_rub": "", "net_pnl_rub": "",
        }

        ts_dt = sig.get("_ts_dt")
        if sig.get("final_status") == "executed" and ts_dt is not None:
            candidates = [
                t for t in pool
                if not t["_consumed"] and t["ticker"] == sig.get("ticker") and t["class_code"] == sig.get("class_code")
                and t.get("direction", t.get("side")) == sig.get("side")
                and t["open_date"] >= ts_dt and (t["open_date"] - ts_dt) <= window
            ]
            if candidates:
                best = min(candidates, key=lambda t: t["open_date"])
                best["_consumed"] = True
                row["real_open_date"] = best["open_date"].isoformat()
                row["real_open_price_rub"] = round(best["open_price_rub"], 2)
                row["real_open_price_quote"] = round(best.get("open_price_quote", best.get("open_price", 0)), 4)
                row["open_commission_rub"] = round(best.get("open_commission_rub", best.get("open_commission", 0)), 2)
                if best["_kind"] == "closed":
                    row["trade_status"] = "closed"
                    row["real_close_date"] = best["close_date"].isoformat()
                    row["real_close_price_rub"] = round(best["close_price_rub"], 2)
                    row["real_close_price_quote"] = round(best.get("close_price_quote", best.get("close_price", 0)), 4)
                    row["close_commission_rub"] = round(best.get("close_commission_rub", best.get("close_commission", 0)), 2)
                    row["gross_pnl_rub"] = round(best["gross_pnl_rub"], 2)
                    row["commission_rub"] = round(best["commission_rub"], 2)
                    row["net_pnl_rub"] = round(best["net_pnl_rub"], 2)
                else:
                    row["trade_status"] = "open"
        out_rows.append(row)
    return out_rows


LOG_HEADER = ["ts", "ticker", "class_code", "side", "rules_score", "rules_decision", "llm_decision", "final_status",
              "planned_entry", "planned_stop", "planned_target",
              "trade_status", "real_open_date", "real_open_price_rub", "real_open_price_quote", "open_commission_rub",
              "real_close_date", "real_close_price_rub", "real_close_price_quote", "close_commission_rub",
              "gross_pnl_rub", "commission_rub", "net_pnl_rub"]


def q_to_float(q) -> float:
    if q is None:
        return 0.0
    return float(q.units) + float(q.nano) / 1e9


def fetch_operations(client, account_id, since, now):
    items = []
    cursor = ""
    while True:
        req = GetOperationsByCursorRequest(
            account_id=account_id, from_=since, to=now, cursor=cursor, limit=1000,
        )
        resp = client.operations.get_operations_by_cursor(request=req)
        items.extend(resp.items)
        if not resp.has_next or not resp.next_cursor:
            break
        cursor = resp.next_cursor
    return items


def get_instrument_info(client, uid, cache):
    """Тикер/class_code/имя по uid. Курс пункта в рублях не нужен —
    рублёвая цена берётся напрямую из фактической выплаты по каждой
    операции (price_rub = payment/qty в match_round_trips)."""
    if uid in cache:
        return cache[uid]
    try:
        resp = client.instruments.get_instrument_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id=uid
        )
        instr = resp.instrument
        info = {
            "ticker": instr.ticker,
            "class_code": instr.class_code,
            "figi": instr.figi,
            "name": instr.name,
        }
    except Exception as e:
        info = {"ticker": uid, "class_code": "?", "figi": "?", "name": f"(ошибка: {e})"}
        print(f"  [get_instrument_info] {uid}: {type(e).__name__}: {e}")
    cache[uid] = info
    return info


def match_round_trips(ops_by_uid, carried_open_legs=None):
    """
    FIFO-сопоставление BUY/SELL в закрытые сделки на инструмент.
    carried_open_legs — позиции, открытые на конец ПРОШЛОГО запуска
    (из state), подставляются в начало очереди ДО новых операций этого
    запуска — иначе сделка, открытая раньше водяного знака и закрытая
    сейчас, ошибочно осталась бы "неоткрытой" в этом окне.
    Возвращает (closed_trades, open_legs) — open_legs — то, что не
    нашло пары (открытая позиция на конец периода, включая перенесённые).
    """
    closed = []
    open_legs = {}
    carried_open_legs = carried_open_legs or {}

    all_uids = set(ops_by_uid.keys()) | set(carried_open_legs.keys())
    for uid in all_uids:
        ops = ops_by_uid.get(uid, [])
        ops_sorted = sorted(ops, key=lambda o: o.date)
        queue = deque(dict(leg) for leg in carried_open_legs.get(uid, []))
        for o in ops_sorted:
            side = "buy" if o.type == OperationType.OPERATION_TYPE_BUY else "sell"
            qty = abs(o.quantity)
            price = q_to_float(o.price)
            payment = q_to_float(o.payment)
            commission = q_to_float(o.commission)
            price_rub = abs(payment) / qty if qty else 0.0
            remaining = qty
            leg = {"side": side, "qty": remaining, "price": price, "price_rub": price_rub,
                   "payment": payment, "date": o.date, "commission": commission, "orig_qty": qty}

            while remaining > 0 and queue and queue[0]["side"] != side:
                opp = queue[0]
                matched_qty = min(remaining, opp["qty"])
                frac_opp = matched_qty / opp["orig_qty"]
                frac_this = matched_qty / qty

                buy_leg = opp if opp["side"] == "buy" else leg
                sell_leg = opp if opp["side"] == "sell" else leg
                buy_frac = frac_opp if opp["side"] == "buy" else frac_this
                sell_frac = frac_opp if opp["side"] == "sell" else frac_this

                buy_commission = buy_leg["commission"] * buy_frac
                sell_commission = sell_leg["commission"] * sell_frac

                # Открывающая нога — та, что случилась РАНЬШЕ по времени,
                # а не "buy=открытие" (для short открытие — это SELL).
                if buy_leg["date"] <= sell_leg["date"]:
                    direction = "long"
                    open_date, open_price, open_price_rub, open_commission = (
                        buy_leg["date"], buy_leg["price"], buy_leg["price_rub"], buy_commission)
                    close_date, close_price, close_price_rub, close_commission = (
                        sell_leg["date"], sell_leg["price"], sell_leg["price_rub"], sell_commission)
                else:
                    direction = "short"
                    open_date, open_price, open_price_rub, open_commission = (
                        sell_leg["date"], sell_leg["price"], sell_leg["price_rub"], sell_commission)
                    close_date, close_price, close_price_rub, close_commission = (
                        buy_leg["date"], buy_leg["price"], buy_leg["price_rub"], buy_commission)

                if direction == "long":
                    gross = (close_price_rub - open_price_rub) * matched_qty
                else:
                    gross = (open_price_rub - close_price_rub) * matched_qty
                commission_total = open_commission + close_commission
                net = gross + commission_total

                closed.append({
                    "uid": uid,
                    "open_date": open_date, "close_date": close_date,
                    "direction": direction,
                    "qty": matched_qty,
                    "open_price": open_price, "close_price": close_price,
                    "open_price_rub": open_price_rub, "close_price_rub": close_price_rub,
                    "open_commission": open_commission, "close_commission": close_commission,
                    "gross_pnl": gross, "commission": commission_total, "net_pnl": net,
                })

                opp["qty"] -= matched_qty
                remaining -= matched_qty
                if opp["qty"] <= 0:
                    queue.popleft()
            if remaining > 0:
                leg["qty"] = remaining
                queue.append(leg)

        if queue:
            open_legs[uid] = list(queue)

    return closed, open_legs


def _gsheets_client():
    if os.getenv("GSHEETS_ENABLED", "0") != "1":
        print("[Sheets] GSHEETS_ENABLED != 1 — пропуск")
        return None, None
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        print("[Sheets] gspread/google-auth не установлены — пропуск")
        return None, None

    cred_file = os.getenv("GSHEETS_CRED_FILE", "")
    sheet_id = os.getenv("GSHEETS_SPREADSHEET_ID", "")
    if not cred_file or not sheet_id:
        print("[Sheets] GSHEETS_CRED_FILE/GSHEETS_SPREADSHEET_ID не заданы — пропуск")
        return None, None

    creds = Credentials.from_service_account_file(
        cred_file, scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(sheet_id), sheet_id


def push_combined_to_sheets(log_rows, open_rows):
    """Полная перезапись обеих вкладок — TRADE_HISTORY (все сигналы + join)
    и TRADE_HISTORY_OPEN (снимок открытых позиций). Дёшево: пересборка
    самого лога не требует обращений к API, только Sheets-запись."""
    sh, sheet_id = _gsheets_client()
    if sh is None:
        return
    import gspread

    def push(title, rows):
        try:
            ws = sh.worksheet(title)
            ws.clear()
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=title, rows=max(200, len(rows) + 10), cols=max(20, len(rows[0]) if rows else 20))
        ws.update(rows, value_input_option="USER_ENTERED")
        print(f"[Sheets] {title}: перезаписано {len(rows) - 1} строк -> https://docs.google.com/spreadsheets/d/{sheet_id}")

    push(WS_TITLE, log_rows)
    push(WS_OPEN_TITLE, open_rows if len(open_rows) > 1 else [open_rows[0]])


OPEN_HEADER = ["ticker", "class_code", "name", "side", "qty", "open_date", "open_price_rub", "open_price_quote"]


def real_trade_to_row(t):
    """Строка для REAL_TRADES_PATH (внутренний накопительный файл)."""
    return [
        t["uid"], t["ticker"], t["class_code"], t["direction"], t["qty"],
        t["open_date"].isoformat(), round(t["open_price_rub"], 2), round(t["open_price"], 4),
        round(t["open_commission"], 2),
        t["close_date"].isoformat(), round(t["close_price_rub"], 2), round(t["close_price"], 4),
        round(t["close_commission"], 2),
        round(t["gross_pnl"], 2), round(t["commission"], 2), round(t["net_pnl"], 2),
    ]


def fetch_candles_for(client, uid, info, from_date, to_date, now):
    candle_from = from_date - timedelta(days=10)
    candle_to = min(to_date + timedelta(days=3), now)
    try:
        resp = client.market_data.get_candles(
            instrument_id=uid, interval=CandleInterval.CANDLE_INTERVAL_DAY,
            from_=candle_from, to=candle_to,
        )
    except Exception as e:
        print(f"  {info['ticker']}: ошибка свечей: {e}")
        return
    safe_ticker = info["ticker"].replace("/", "_")
    candle_path = CANDLES_DIR / f"{safe_ticker}_{info['class_code']}_D1.csv"
    with candle_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for c in resp.candles:
            w.writerow([
                c.time.isoformat(),
                round(q_to_float(c.open), 4), round(q_to_float(c.high), 4),
                round(q_to_float(c.low), 4), round(q_to_float(c.close), 4),
                c.volume,
            ])
    print(f"  {info['ticker']}: {len(resp.candles)} свечей -> {candle_path}")


def num(x, cast=float):
    """Строку из CSV — в число, если получится, иначе '' (пусто, не 0 —
    чтобы не выдумывать данные там, где их реально нет)."""
    s = (x or "").strip()
    if s == "":
        return ""
    try:
        return cast(s)
    except (TypeError, ValueError):
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true",
                     help="реальные сделки — с нуля с AUTO_STRATEGY_START, игнорируя водяной знак (редко)")
    ap.add_argument("--since", default=None,
                     help="YYYY-MM-DD — явное начало для --rebuild (по умолчанию AUTO_STRATEGY_START)")
    ap.add_argument("--push-sheets", action="store_true")
    ap.add_argument("--no-candles", action="store_true", help="пропустить скачивание свечей (быстрее)")
    args = ap.parse_args()

    token = os.getenv("TINKOFF_TOKEN")
    account_id = os.getenv("TINKOFF_ACCOUNT_ID")
    if not token or not account_id:
        raise RuntimeError("Не заданы TINKOFF_TOKEN/TINKOFF_ACCOUNT_ID")

    now = datetime.now(timezone.utc)
    LOGS_DIR.mkdir(exist_ok=True)
    if not args.no_candles:
        CANDLES_DIR.mkdir(exist_ok=True)

    if args.rebuild:
        since = (
            datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if args.since else AUTO_STRATEGY_START
        )
        carried_open_legs = {}
        if REAL_TRADES_PATH.exists():
            REAL_TRADES_PATH.unlink()  # --rebuild пересобирает реальные сделки с нуля
    else:
        state = load_state()
        since = (
            datetime.fromisoformat(state["processed_through"])
            if state["processed_through"] else now - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        )
        carried_open_legs = state["open_legs"]
        print(f"Инкрементальный режим: водяной знак = {since.isoformat()} "
              f"({'из state' if state['processed_through'] else f'по умолчанию, последние {DEFAULT_LOOKBACK_DAYS} дней'}), "
              f"перенесено открытых позиций: {sum(len(v) for v in carried_open_legs.values())}")

    with Client(token) as client:
        ops = fetch_operations(client, account_id, since, now)
        buys_sells = [o for o in ops if o.type in (OperationType.OPERATION_TYPE_BUY, OperationType.OPERATION_TYPE_SELL)]
        print(f"Новых BUY/SELL операций с {since.date()}: {len(buys_sells)}")

        ops_by_uid = defaultdict(list)
        for o in buys_sells:
            ops_by_uid[o.instrument_uid].append(o)

        new_closed, open_legs = match_round_trips(ops_by_uid, carried_open_legs)
        new_closed = sorted(new_closed, key=lambda x: x["close_date"])
        print(f"Новых закрытых реальных сделок: {len(new_closed)}, инструментов с открытой позицией: {len(open_legs)}")

        instr_cache = {}
        for t in new_closed:
            t.update(get_instrument_info(client, t["uid"], instr_cache))
        for uid in open_legs:
            get_instrument_info(client, uid, instr_cache)

        if new_closed:
            append_real_trades_csv([real_trade_to_row(t) for t in new_closed])
            print(f"Дописано в {REAL_TRADES_PATH}: {len(new_closed)} новых реальных сделок")

        # Полный накопленный набор реальных сделок (старые + новые) — для
        # join с сигналами нужны ВСЕ, не только найденные в этом запуске.
        all_real_closed = load_real_trades_csv()

        open_legs_flat = []
        for uid, legs in open_legs.items():
            info = instr_cache[uid]
            for leg in legs:
                # leg["side"] — сырой тип операции (buy/sell), а не позиции
                # (long/short): открыли BUY -> long, открыли SELL -> short.
                open_legs_flat.append({
                    "ticker": info["ticker"], "class_code": info["class_code"],
                    "direction": "long" if leg["side"] == "buy" else "short",
                    "open_date": leg["date"], "open_price_rub": leg["price_rub"], "open_price_quote": leg["price"],
                    "open_commission_rub": leg["commission"],
                })

        open_rows = [OPEN_HEADER]
        for uid, legs in open_legs.items():
            info = instr_cache[uid]
            for leg in legs:
                direction = "long" if leg["side"] == "buy" else "short"
                open_rows.append([info["ticker"], info["class_code"], info["name"], direction,
                                   leg["qty"], leg["date"].isoformat(),
                                   round(leg["price_rub"], 2), round(leg["price"], 4)])
        with OPEN_POSITIONS_PATH.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(open_rows)
        print(f"Сохранён снимок открытых позиций: {OPEN_POSITIONS_PATH} ({len(open_rows) - 1})")

        # --- пересборка ИТОГОВОГО лога: все сигналы + join с реальными сделками ---
        signals = load_signal_journal()
        joined = join_signals_with_real_trades(signals, all_real_closed, open_legs_flat)
        log_rows = [LOG_HEADER]
        for r in joined:
            log_rows.append([
                r["ts"], r["ticker"], r["class_code"], r["side"],
                num(r["rules_score"]), r["rules_decision"], r["llm_decision"], r["final_status"],
                num(r["planned_entry"]), num(r["planned_stop"]), num(r["planned_target"]),
                r["trade_status"], r["real_open_date"], r["real_open_price_rub"], r["real_open_price_quote"],
                r["open_commission_rub"], r["real_close_date"], r["real_close_price_rub"], r["real_close_price_quote"],
                r["close_commission_rub"], r["gross_pnl_rub"], r["commission_rub"], r["net_pnl_rub"],
            ])
        with LOG_PATH.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(log_rows)
        n_matched = sum(1 for r in joined if r["trade_status"])
        print(f"Пересобран {LOG_PATH}: {len(joined)} сигналов всего, из них с реальной сделкой: {n_matched}")

        if not args.no_candles:
            all_uids = set(t["uid"] for t in new_closed) | set(open_legs.keys())
            for uid in all_uids:
                info = instr_cache[uid]
                trades_for_uid = [t for t in new_closed if t["uid"] == uid]
                if trades_for_uid:
                    earliest = min(t["open_date"] for t in trades_for_uid)
                    latest = max(t["close_date"] for t in trades_for_uid)
                else:
                    earliest = min(leg["date"] for leg in open_legs[uid])
                    latest = now
                fetch_candles_for(client, uid, info, earliest, latest, now)

    if args.push_sheets:
        push_combined_to_sheets(log_rows, open_rows)

    # Состояние (для реальных сделок) сохраняем всегда, включая --rebuild —
    # after любого прогона open_legs это корректный текущий снимок
    # незакрытых позиций, а now безопасный водяной знак.
    save_state(now, open_legs)
    print(f"Состояние сохранено: водяной знак = {now.isoformat()}, "
          f"открытых позиций перенесено на следующий запуск: {sum(len(v) for v in open_legs.values())}")

    total_commission = sum(t["open_commission"] + t["close_commission"] for t in new_closed)
    total_net = sum(t["net_pnl"] for t in new_closed)
    n_win = sum(1 for t in new_closed if t["net_pnl"] > 0)
    print(f"\n=== ЗА ЭТОТ ЗАПУСК (реальные сделки) ===")
    print(f"Новых закрытых сделок: {len(new_closed)}" + (f", прибыльных: {n_win} ({n_win/len(new_closed)*100:.0f}%)" if new_closed else ""))
    print(f"Суммарная комиссия: {total_commission:,.2f}₽")
    print(f"Net P&L (приближённо для многодневных фьючерсов — см. docstring): {total_net:,.2f}₽")
    print(f"Сигналов всего в объединённом логе: {len(joined)}")


if __name__ == "__main__":
    main()
