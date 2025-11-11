from tinkoff.invest.services import Services
from typing import Optional, Dict
import logging


def get_instrument_uid(client: Services, ticker: str, class_code: str) -> Optional[str]:
    """Найти инструмент и вернуть его UID по тикеру и class_code."""
    resp = client.instruments.find_instrument(query=ticker)
    for instr in resp.instruments:
        if instr.class_code == class_code:
            logging.info(f"🔍 Инструмент найден: {ticker} → uid={instr.uid}")
            return instr.uid
    logging.warning(f"⚠️ Не найден инструмент {ticker} в class_code={class_code}")
    return None


def find_position(client: Services, account_id: str, instrument_uid: str):
    """Найти позицию в портфеле по UID инструмента."""
    portfolio = client.operations.get_portfolio(account_id=account_id)
    for pos in portfolio.positions:
        if getattr(pos, "instrument_uid", None) == instrument_uid:
            return pos
    return None


def get_position_details(position) -> Dict[str, str | int]:
    """Определить направление ('long'/'short'/'none') и абсолютное количество."""
    qty = int(position.quantity.units)
    if qty > 0:
        direction = "long"
    elif qty < 0:
        direction = "short"
    else:
        direction = "none"
    return {"direction": direction, "qty": abs(qty)}
