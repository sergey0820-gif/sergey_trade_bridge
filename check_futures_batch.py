import os
import asyncio
from tinkoff.invest import AsyncClient, InstrumentIdType
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("TINKOFF_TOKEN")

FUTURES_TICKERS = [
    "Si-12.25",
    "Eu-12.25",
    "GOLD-12.25",
    "SBRF-12.25",
    "GAZR-12.25",
    "RTS-12.25",
    "BR-12.25",
    "SPY-12.25",
    "LKOH-12.25",
    "ALRS-12.25",
]

CLASS_CODE = "SPBFUT"


async def check_futures():
    async with AsyncClient(TOKEN) as client:
        available = []
        print("🔍 Проверка фьючерсов:\n")

        for ticker in FUTURES_TICKERS:
            try:
                response = await client.instruments.future_by(
                    id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
                    class_code=CLASS_CODE,
                    id=ticker,
                )
                instrument = response.instrument

                if not instrument.buy_available_flag:
                    print(f"❌ {ticker}: недоступен для покупки через API\n")
                    continue

                figi = instrument.figi
                lot = instrument.lot

                last_price_response = await client.market_data.get_last_prices(
                    figi=[figi]
                )
                price_obj = last_price_response.last_prices[0].price
                price = price_obj.units + price_obj.nano / 1e9

                cost = price * lot

                print(f"✅ {ticker}")
                print(f"• FIGI: {figi}")
                print(f"• Цена: {price:.2f}")
                print(f"• Лот: {lot}")
                print(f"• Стоимость входа: {cost:.2f} ₽\n")

                available.append(
                    {
                        "ticker": ticker,
                        "figi": figi,
                        "lot": lot,
                        "price": price,
                        "cost": cost,
                    }
                )

            except Exception as e:
                print(f"❌ {ticker}: ошибка — {e}\n")

        if available:
            cheapest = sorted(available, key=lambda x: x["cost"])[0]
            print("🎯 Самый дешёвый доступный фьючерс:")
            print(f"→ {cheapest['ticker']} | Цена за лот: {cheapest['cost']:.2f} ₽")
        else:
            print("❌ Не найдено доступных фьючерсов.")


if __name__ == "__main__":
    asyncio.run(check_futures())
