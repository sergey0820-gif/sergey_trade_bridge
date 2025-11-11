import os
import csv
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
from aiogram import Bot
from aiogram.types import FSInputFile

# Настройки/пути
BASE = Path(__file__).parent
ENV_PATH = BASE / ".env"
CSV_PATH = BASE / "out" / "live_candidates.csv"

load_dotenv(dotenv_path=ENV_PATH)
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))


def build_summary(csv_path: Path, limit: int = 8) -> str:
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return "⚠️ Файл live_candidates.csv не найден или пуст."

    with csv_path.open("r", encoding="utf-8") as f:
        r = list(csv.DictReader(f))
    total = len(r)
    # первые строки уже отсортированы по ликвидности у сканера
    top = r[:limit]
    tickers = ", ".join(row["Ticker"] for row in top)
    # усреднённый Cost/R по топу
    try:
        avg_cost_r = sum(float(row["Cost/R"]) for row in top) / max(1, len(top))
    except Exception:
        avg_cost_r = None
    avg_s = f"{avg_cost_r:.3f}" if avg_cost_r is not None else "n/a"

    lines = [
        f"📊 Sergey-Trade 2025 — утренний отчёт",
        f"Дата: {datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M %Z')}",
        f"Кандидатов (PASS): {total}",
        f"Top {limit} по ликвидности: {tickers}",
        f"Средний Cost/R (по топу): {avg_s}",
        "",
        "ℹ️ Полная таблица во вложении (CSV).",
        "Правило: шорты — только через фьючерсы.",
    ]
    return "\n".join(lines)


async def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Не заданы TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID в .env")
    bot = Bot(token=BOT_TOKEN)
    text = build_summary(CSV_PATH)
    await bot.send_message(chat_id=CHAT_ID, text=text)
    if CSV_PATH.exists() and CSV_PATH.stat().st_size > 0:
        await bot.send_document(
            chat_id=CHAT_ID,
            document=FSInputFile(str(CSV_PATH)),
            caption="live_candidates.csv",
        )
    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
