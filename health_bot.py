import logging
from dotenv import dotenv_values
from telegram.ext import Application, CommandHandler

logging.basicConfig(level=logging.INFO)


async def start(update, ctx):
    await update.message.reply_text("bot alive")


async def ping(update, ctx):
    await update.message.reply_text("pong")


cfg = dotenv_values(".env")
token = cfg.get("TELEGRAM_BOT_TOKEN") or cfg.get("TELEGRAM_TOKEN")
app = Application.builder().token(token).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("ping", ping))
app.run_polling(drop_pending_updates=True)
