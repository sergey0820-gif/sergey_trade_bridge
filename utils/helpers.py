import os
import pandas as pd
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from tinkoff.invest import Client, CandleInterval, InstrumentIdType

load_dotenv()
TOKEN = os.getenv("TINKOFF_TOKEN")

interval_map = {
    "day": CandleInterval.CANDLE_INTERVAL_DAY,
    "hour": CandleInterval.CANDLE_INTERVAL_HOUR,
    "5min": CandleInterval.CANDLE_INTERVAL_5_MIN,
}


def get_candles(
    ticker: str, class_code: str, interval: str = "day", days: int = 30
) -> pd.DataFrame | None:
    try:
        with Client(TOKEN) as client:
            # Поиск инструмента по тикеру и классу
            response = client.instruments.get_instrument_by(
                id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
                id=ticker,
                class_code=class_code,
            )
            instrument = response.instrument

            if not instrument or not instrument.figi:
                print(f"⚠️ Не найден инструмент: {ticker} ({class_code})")
                return None

            figi = instrument.figi

            # Установка временного диапазона
            interval_enum = interval_map.get(
                interval, CandleInterval.CANDLE_INTERVAL_DAY
            )
            to_ = datetime.now(timezone.utc)
            from_ = to_ - timedelta(days=days)

            # Получение свечей
            candles = client.market_data.get_candles(
                figi=figi,
                from_=from_,
                to=to_,
                interval=interval_enum,
            ).candles

            if not candles:
                print(f"⚠️ Нет свечей для: {ticker}")
                return None

            df = pd.DataFrame(
                [
                    {
                        "time": c.time,
                        "open": c.open.units + c.open.nano / 1e9,
                        "high": c.high.units + c.high.nano / 1e9,
                        "low": c.low.units + c.low.nano / 1e9,
                        "close": c.close.units + c.close.nano / 1e9,
                        "volume": c.volume,
                    }
                    for c in candles
                ]
            )

            return df

    except Exception as e:
        print(f"⚠️ Ошибка в get_candles() для {ticker}: {e}")
        return None
