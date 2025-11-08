# /home/chick/sergey_trade_bridge/market_time.py
from datetime import datetime, time
import os
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))

def is_moex_main_session(dt: datetime | None = None) -> bool:
    """
    Основная сессия акций MOEX: 10:00–18:40 МСК, пн–пт.
    (Без учёта праздников.)
    """
    dt = dt or datetime.now(MOSCOW_TZ)
    if dt.weekday() > 4:   # 0..4 = пн..пт
        return False
    t = dt.time()
    return time(10, 0) <= t <= time(18, 40)

