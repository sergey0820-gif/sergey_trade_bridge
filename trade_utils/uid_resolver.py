from tinkoff.invest import InstrumentIdType

async def _resolve_uid(c, ticker: str, class_code: str | None):
    # 1) точный поиск по тикеру+классу
    if class_code:
        gr = await c.instruments.get_instrument_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
            class_code=class_code,
            id=ticker,
        )
        inst = getattr(gr, "instrument", None)
        if inst and getattr(inst, "uid", None):
            return inst.uid
    # 2) фолбэк: общий поиск и фильтр
    fr = await c.instruments.find_instrument(query=ticker)
    for it in getattr(fr, "instruments", []):
        if (class_code and it.class_code == class_code) or it.ticker == ticker:
            return it.uid
    return None
