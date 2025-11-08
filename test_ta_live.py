import pandas as pd
from utils.ta import analyze_ticker_live

# Пример тестовых данных: свечи с ценами и объёмами
data = {
    "time": pd.date_range(end=pd.Timestamp.now(), periods=30, freq="D"),
    "open": [100 + i for i in range(30)],
    "high": [101 + i for i in range(30)],
    "low": [99 + i for i in range(30)],
    "close": [100 + i + (i % 2) for i in range(30)],
    "volume": [1000 + 10 * i for i in range(30)],
}
df = pd.DataFrame(data)

# Тест для актива типа фьючерс
result = analyze_ticker_live(df, "future")
print("Результат анализа (future):")
print(result)

# Тест для актива типа акция
result = analyze_ticker_live(df, "share")
print("\nРезультат анализа (share):")
print(result)
