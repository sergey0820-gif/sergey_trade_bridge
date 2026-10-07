import os
import csv
import asyncio
from pathlib import Path
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import FSInputFile

BASE = Path(__file__).parent
ENV_PATH = BASE / ".env"
POS_CSV = BASE / "out" / "positions.csv"
OPS_CSV = BASE / "out" / "operations_today.csv"
ORDERS_DIR = BASE / "orders"
REAL_TRADES_CSV = BASE / "logs" / "trade_history_real_trades.csv"

load_dotenv(dotenv_path=ENV_PATH)
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
MSG_PREFIX = os.getenv("TELEGRAM_MSG_PREFIX", "")
PROXY_URL = os.getenv("TELEGRAM_PROXY_URL", "")


def safe_float(x, default=0.0):
    try:
        return float(str(x).replace(",", "."))
    except Exception:
        return default


def read_positions(pth: Path):
    res = []
    if not pth.exists() or pth.stat().st_size == 0:
        return res
    with pth.open("r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                ticker = row.get("ticker") or row.get("Ticker") or "?"
                qty = safe_float(row.get("qty", 0))
                avg = safe_float(row.get("avg_price", 0))
                last = safe_float(row.get("market_price", 0))
                pnl = (last - avg) * qty
                res.append(
                    {"ticker": ticker, "qty": qty, "avg": avg, "last": last, "pnl": pnl}
                )
            except Exception:
                continue
    return res


def read_operations_today(pth: Path):
    ops = []
    if not pth.exists() or pth.stat().st_size == 0:
        return ops
    with pth.open("r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            ops.append(row)
    return ops


def q_to_float(q) -> float:
    if q is None:
        return 0.0
    return float(q.units) + float(q.nano) / 1e9


def closed_trades_today(today_date) -> tuple[int, float]:
    """(кол-во закрытых сделок, их суммарный net P&L в рублях) за today_date
    (локальная дата, date-объект) — источник logs/trade_history_real_trades.csv
    (пишет trade_history_log.py, round-trip вход/выход уже сопоставлен там).
    Файл должен обновляться ДО вызова этого скрипта в тот же день (см.
    crontab — trade_history_log.py --no-candles идёт перед notify_close.py)."""
    if not REAL_TRADES_CSV.exists():
        return 0, 0.0
    n, pnl = 0, 0.0
    with REAL_TRADES_CSV.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            close_date = row.get("close_date", "")
            if not close_date:
                continue
            try:
                dt = datetime.fromisoformat(close_date).astimezone()
            except Exception:
                continue
            if dt.date() == today_date:
                n += 1
                pnl += safe_float(row.get("net_pnl_rub", 0))
    return n, pnl


def fetch_variation_margin_today(day_start_utc, day_end_utc):
    """Реальная списанная/начисленная вариационная маржа за сегодня
    (WRITING_OFF_VARMARGIN + ACCRUING_VARMARGIN) — та же логика, что
    compute_reconciliation() в trade_history_log.py, но за один день.

    ВАЖНО: используем get_operations_by_cursor (не get_operations) — у
    "старого" get_operations() поле .type приходит ЧЕЛОВЕКОЧИТАЕМОЙ
    СТРОКОЙ на русском (например, "Списание вариационной маржи"), а не
    enum OperationType, из-за чего сравнение `o.type ==
    OperationType.OPERATION_TYPE_...` молча никогда не совпадает (баг,
    найденный 2026-10-07 — функция всегда возвращала 0 вместо реальной
    суммы). get_operations_by_cursor отдаёт настоящий enum, как и
    trade_history_log.py.

    Возвращает None, если TINKOFF_TOKEN/TINKOFF_ACCOUNT_ID недоступны или
    запрос не удался (не блокируем отчёт — показываем прочерк)."""
    token = os.getenv("TINKOFF_TOKEN")
    account_id = os.getenv("TINKOFF_ACCOUNT_ID")
    if not token or not account_id:
        return None
    try:
        from tinkoff.invest import Client, OperationType
        from tinkoff.invest.schemas import GetOperationsByCursorRequest
        with Client(token) as client:
            req = GetOperationsByCursorRequest(
                account_id=account_id, from_=day_start_utc, to=day_end_utc, cursor="", limit=1000,
            )
            resp = client.operations.get_operations_by_cursor(request=req)
        varmargin_types = (
            OperationType.OPERATION_TYPE_WRITING_OFF_VARMARGIN,
            OperationType.OPERATION_TYPE_ACCRUING_VARMARGIN,
        )
        return sum(q_to_float(o.payment) for o in resp.items if o.type in varmargin_types)
    except Exception:
        return None


def summarize():
    now_local = datetime.now(timezone.utc).astimezone()
    pos = read_positions(POS_CSV)
    ops = read_operations_today(OPS_CSV)

    # Подсчёт PnL по незакрытым позициям (грубая оценка по avg vs market_price)
    total_unreal = sum(p["pnl"] for p in pos)
    # Сводка по позициям (топ-5 по абсолютному PnL)
    pos_sorted = sorted(pos, key=lambda x: abs(x["pnl"]), reverse=True)[:5]
    pos_lines = (
        [
            f"{p['ticker']}: qty={int(p['qty']) if p['qty'].is_integer() else p['qty']}, avg={p['avg']:.2f}, last={p['last']:.2f}, PnL={p['pnl']:.2f}"
            for p in pos_sorted
        ]
        if pos_sorted
        else ["—"]
    )

    # Операции за сегодня
    ops_cnt = len(ops)

    # Заказы/планы в каталоге orders за сегодня
    today_str = now_local.strftime("%Y-%m-%d")
    orders_cnt = 0
    if ORDERS_DIR.exists():
        for p in ORDERS_DIR.iterdir():
            try:
                if p.is_file():
                    ts = datetime.fromtimestamp(p.stat().st_mtime, tz=now_local.tzinfo)
                    if ts.strftime("%Y-%m-%d") == today_str:
                        orders_cnt += 1
            except Exception:
                continue

    n_closed, closed_pnl = closed_trades_today(now_local.date())
    day_start_utc = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    varmargin = fetch_variation_margin_today(day_start_utc, now_local.astimezone(timezone.utc))

    lines = [
        "📥 Sergey-Trade 2025 — вечерний отчёт",
        f"Дата: {now_local.strftime('%Y-%m-%d %H:%M %Z')}",
        "",
        f"Закрыто сделок сегодня: {n_closed}",
        f"Результат по закрытым сделкам: {closed_pnl:+.2f}₽",
        (
            "Вариационная маржа сегодня: нет данных" if varmargin is None
            else "Вариационная маржа сегодня: движения не было" if abs(varmargin) < 0.01
            else f"Вариационная маржа сегодня: {varmargin:+.2f}₽"
        ),
        "",
        f"Операций за сегодня: {ops_cnt}",
        f"Файлов заявок/сигналов за сегодня (orders/): {orders_cnt}",
        f"Нереализованный PnL (оценка): {total_unreal:.2f}",
        "",
        "Топ-5 позиций по |PnL|:",
        *pos_lines,
        "",
        "ℹ️ Если есть файл operations_today.csv — приложу его ниже.",
    ]
    return "\n".join(lines)


async def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы в .env")

    session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
    bot = Bot(token=BOT_TOKEN, session=session)
    text = MSG_PREFIX + summarize()
    await bot.send_message(chat_id=CHAT_ID, text=text)

    if OPS_CSV.exists() and OPS_CSV.stat().st_size > 0:
        try:
            await bot.send_document(
                chat_id=CHAT_ID,
                document=FSInputFile(str(OPS_CSV)),
                caption="operations_today.csv",
            )
        except Exception:
            # тихо пропускаем вложение, если что-то не так
            pass

    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
