# check_position_status.py

import os
import logging
from dotenv import load_dotenv
from tinkoff.invest import Client, InstrumentIdType
from tinkoff.invest.utils import decimal_to_quotation

load_dotenv()

TOKEN = os.getenv("TINKOFF_TOKEN")
ACCOUNT_ID = os.getenv("TINKOFF_ACCOUNT_ID")
TICKER = "GAZP"
CLASS_CODE = "TQBR"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)8s | %(message)s"
)

with Client(TOKEN) as client:
    logging.info("📥 Получаем портфель по счёту: %s", ACCOUNT_ID)
    portfolio = client.operations.get_portfolio(account_id=ACCOUNT_ID)

    logging.info("📦 Позиции в портфеле:")
    for p in portfolio.positions:
        figi = p.figi
        qty = p.quantity.units + p.quantity.nano / 1e9
        instr = client.instruments.get_instrument_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=figi
        ).instrument
        logging.info(
            "🔸 %s (%s) | figi=%s | qty=%.2f", instr.name, instr.ticker, figi, qty
        )

    logging.info("🔍 Получаем инструмент: %s (%s)", TICKER, CLASS_CODE)
    instr = client.instruments.get_instrument_by(
        id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
        class_code=CLASS_CODE,
        id=TICKER,
    ).instrument

    logging.info(
        "✔️ Инструмент найден: %s | figi=%s | lot=%s | min_price_increment=%s",
        instr.name,
        instr.figi,
        instr.lot,
        instr.min_price_increment,
    )

    logging.info("📈 Получаем цену инструмента...")
    prices = client.market_data.get_last_prices(figi=[instr.figi])
    if prices.last_prices:
        last_price = prices.last_prices[0].price
        logging.info("💰 Цена последней сделки: %s", last_price)
    else:
        logging.warning("⚠️ Цена не найдена (empty last_prices)")

    logging.info("🔁 Проверка позиции по figi: %s", instr.figi)
    position = next((p for p in portfolio.positions if p.figi == instr.figi), None)
    if position:
        qty = position.quantity.units + position.quantity.nano / 1e9
        logging.info("✅ Позиция найдена: %.2f лотов", qty)
    else:
        logging.warning("❌ Позиция не найдена в портфеле")
