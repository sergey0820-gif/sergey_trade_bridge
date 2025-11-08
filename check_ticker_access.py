import os
import logging
from tinkoff.invest import Client, InstrumentIdType, InstrumentStatus
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("TINKOFF_TOKEN")

logging.basicConfig(level=logging.INFO)

def check_ticker_access(ticker: str):
    with Client(TOKEN) as client:
        instruments = client.instruments.find_instrument(query=ticker).instruments
        if not instruments:
            print(f"❌ Тикер {ticker} не найден.")
            return

        instrument_short = instruments[0]
        figi = instrument_short.figi
        instrument_type = instrument_short.instrument_type

        # Определяем, какой тип запроса использовать
        if instrument_type == "share":
            instrument = client.instruments.share_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=figi).instrument
        elif instrument_type == "bond":
            instrument = client.instruments.bond_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=figi).instrument
        elif instrument_type == "etf":
            instrument = client.instruments.etf_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=figi).instrument
        elif instrument_type == "future":
            instrument = client.instruments.future_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=figi).instrument
        else:
            print(f"⚠️ Неизвестный тип инструмента: {instrument_type}")
            return

        print(f"\n🔎 Проверка тикера: {ticker}")
        print(f"• FIGI: {instrument.figi}")
        print(f"• Название: {instrument.name}")
        print(f"• Тип: {instrument_type}")
        print(f"• Доступен для покупки через API: {instrument.buy_available_flag}")
        print(f"• Доступен для продажи через API: {instrument.sell_available_flag}")
        print(f"• Доступен через API: {instrument.api_trade_available_flag}")
        print(f"• Статус торговли: {instrument.trading_status}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("⚠️ Укажи тикер: python check_ticker_access.py GAZP")
    else:
        check_ticker_access(sys.argv[1])

