import os
from dotenv import load_dotenv
from tinkoff.invest import Client

load_dotenv()
TOKEN = os.getenv("TINKOFF_TOKEN")
FIGI = "BBG004730RP0"


def to_float(q):
    return q.units + q.nano / 1e9


with Client(TOKEN) as client:
    # Получаем минимальный шаг цены
    instr = client.instruments.get_instrument_by(id_type=1, id=FIGI)
    step = to_float(instr.instrument.min_price_increment)
    print("🔹 Минимальный шаг цены:", step)

    # Получаем текущую цену
    last_price = client.market_data.get_last_prices(figi=[FIGI]).last_prices[0].price
    current = to_float(last_price)
    print("💰 Текущая цена:", current)

    # Лимит безопасного TP (условный — на +7%)
    limit_up = round(current * 1.07, 2)
    print("📈 Безопасный лимит вверх (+7%):", limit_up)
