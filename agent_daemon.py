# agent_daemon.py
import pandas as pd
import os
import time
import logging
from datetime import datetime, timedelta
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = (
    os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT")
)
TELEGRAM_CHAT_ID = int(TELEGRAM_CHAT_ID or 0)
CANDIDATES_FILE = "candidates.csv"
STATE_FILE = "last_sent.txt"
TTL_MINUTES = 60

logging.basicConfig(level=logging.INFO)


def load_last_sent():
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE, "r") as f:
        return set(line.strip() for line in f.readlines())


def save_last_sent(symbols):
    with open(STATE_FILE, "w") as f:
        for sym in symbols:
            f.write(f"{sym}\n")


def should_send(timestamp: str) -> bool:
    ts = datetime.fromisoformat(timestamp)
    return datetime.now() - ts < timedelta(minutes=TTL_MINUTES)


def _getv(row, *keys):
    for k in keys:
        try:
            v = row.get(k)
        except AttributeError:
            try:
                v = row[k]
            except Exception:
                v = None
        if v not in (None, ""):
            return v
    return None


def send_signal(bot, row):
    text = f"📈 <b>Сигнал по {_getv(row, 'symbol', 'ticker')}</b>\n"
    text += f"📊 RSI: <b>{_getv(row, 'rsi', 'rsi_h4', 'rsi_h1') or '-'}</b>\n"
    text += f"📦 Объём x: <b>{row['volume_ratio']}</b>\n"
    text += f"🧠 Паттерн: <b>{row['pattern']}</b>\n"

    buttons = [
        [
            InlineKeyboardButton(
                "✅ Войти", callback_data=f"entry:{_getv(row, 'symbol', 'ticker')}"
            )
        ],
        [
            InlineKeyboardButton(
                "ℹ️ Детали", callback_data=f"info:{_getv(row, 'symbol', 'ticker')}"
            )
        ],
        [
            InlineKeyboardButton(
                "⛔ Пропустить", callback_data=f"skip:{_getv(row, 'symbol', 'ticker')}"
            )
        ],
    ]

    bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML",
    )


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("TELEGRAM_TOKEN или TELEGRAM_CHAT_ID не заданы в .env")
        return

    bot = Bot(token=TELEGRAM_TOKEN)

    if not os.path.exists(CANDIDATES_FILE):
        logging.info("Нет файла кандидатов.")
        return

    df = pd.read_csv(CANDIDATES_FILE)
    if df.empty:
        logging.info("Кандидатов нет.")
        return

    last_sent = load_last_sent()
    new_sent = set()

    for _, row in df.iterrows():
        symbol = row.get("symbol") or row.get("ticker")
        timestamp = row["timestamp"]

        if symbol not in last_sent and should_send(timestamp):
            logging.info(f"📤 Отправка сигнала по {symbol}")
            send_signal(bot, row)
            new_sent.add(symbol)

    all_sent = last_sent.union(new_sent)
    save_last_sent(all_sent)


if __name__ == "__main__":
    main()
