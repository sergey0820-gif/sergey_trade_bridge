import os
import csv
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
from aiogram import Bot
from aiogram.types import FSInputFile

BASE = Path(__file__).parent
ENV_PATH = BASE / ".env"
POS_CSV = BASE / "out" / "positions.csv"
OPS_CSV = BASE / "out" / "operations_today.csv"
ORDERS_DIR = BASE / "orders"

load_dotenv(dotenv_path=ENV_PATH)
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

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
                res.append({"ticker": ticker, "qty": qty, "avg": avg, "last": last, "pnl": pnl})
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

def summarize():
    now_local = datetime.now(timezone.utc).astimezone()
    pos = read_positions(POS_CSV)
    ops = read_operations_today(OPS_CSV)

    # Подсчёт PnL по незакрытым позициям (грубая оценка по avg vs market_price)
    total_unreal = sum(p["pnl"] for p in pos)
    # Сводка по позициям (топ-5 по абсолютному PnL)
    pos_sorted = sorted(pos, key=lambda x: abs(x["pnl"]), reverse=True)[:5]
    pos_lines = [
        f"{p['ticker']}: qty={int(p['qty']) if p['qty'].is_integer() else p['qty']}, avg={p['avg']:.2f}, last={p['last']:.2f}, PnL={p['pnl']:.2f}"
        for p in pos_sorted
    ] if pos_sorted else ["—"]

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

    lines = [
        "📥 Sergey-Trade 2025 — вечерний отчёт",
        f"Дата: {now_local.strftime('%Y-%m-%d %H:%M %Z')}",
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

    bot = Bot(token=BOT_TOKEN)
    text = summarize()
    await bot.send_message(chat_id=CHAT_ID, text=text)

    if OPS_CSV.exists() and OPS_CSV.stat().st_size > 0:
        try:
            await bot.send_document(chat_id=CHAT_ID, document=FSInputFile(str(OPS_CSV)),
                                    caption="operations_today.csv")
        except Exception:
            # тихо пропускаем вложение, если что-то не так
            pass

    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
