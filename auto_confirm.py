#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
import os, csv, datetime as dt
from typing import Optional, List
from dotenv import load_dotenv
from tinkoff.invest import AsyncClient, InstrumentIdType

load_dotenv(".env")

TTL_MINUTES: int = int(os.getenv("TTL_MINUTES", "90"))
MAX_DEVIATION_PCT: float = float(os.getenv("MAX_DEVIATION_PCT", "0.5"))
RSI_LONG_MAX: float = float(os.getenv("RSI_LONG_MAX", "35.0"))
RSI_SHORT_MIN: float = float(os.getenv("RSI_SHORT_MIN", "65.0"))
TINKOFF_TOKEN: Optional[str] = os.getenv("TINKOFF_TOKEN") or os.getenv("TINKOFF_INVEST_TOKEN")

def _parse_ts(s: str) -> Optional[dt.datetime]:
    if not s: return None
    try:
        s2 = s.replace("Z","+00:00")
        return dt.datetime.fromisoformat(s2)
    except Exception: pass
    for fmt in ("%Y-%m-%d %H:%M:%S","%Y-%m-%d %H:%M"):
        try: return dt.datetime.strptime(s, fmt)
        except Exception: continue
    return None

def _side_from_zone(zone: str) -> Optional[str]:
    z = (zone or "").lower()
    if any(k in z for k in ("buy","long","support","поддерж")): return "long"
    if any(k in z for k in ("sell","short","resist","сопротив")): return "short"
    return None

def _ok_ttl(ts: Optional[dt.datetime]) -> bool:
    if ts is None: return False
    now = dt.datetime.now(ts.tzinfo) if ts.tzinfo else dt.datetime.now()
    return (now - ts) <= dt.timedelta(minutes=TTL_MINUTES)

def _ok_rsi(side: str, rsi_h4: Optional[float]) -> bool:
    if rsi_h4 is None: return False
    return (side=="long" and rsi_h4<=RSI_LONG_MAX) or (side=="short" and rsi_h4>=RSI_SHORT_MIN)

def _ok_dev(entry: float, last_px: float) -> bool:
    if not entry or not last_px: return False
    dev_pct = abs(last_px - entry) / entry * 100.0
    return dev_pct <= MAX_DEVIATION_PCT

async def _resolve_figi(cli: AsyncClient, ticker: str, class_code: str) -> Optional[str]:
    try:
        resp = await cli.instruments.get_instrument_by(
            id=ticker, id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
            class_code=(class_code or None),
        )
        if getattr(resp,"instrument",None) and resp.instrument.figi:
            return resp.instrument.figi
    except Exception: pass
    try:
        resp = await cli.instruments.get_instrument_by(
            id=ticker, id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER
        )
        if getattr(resp,"instrument",None) and resp.instrument.figi:
            return resp.instrument.figi
    except Exception: pass
    for cc in ("TQBR","SPBXM"):
        try:
            resp = await cli.instruments.get_instrument_by(
                id=ticker, id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER, class_code=cc
            )
            if getattr(resp,"instrument",None) and resp.instrument.figi:
                return resp.instrument.figi
        except Exception: continue
    return None

async def _get_last_price(cli: AsyncClient, figi: str) -> Optional[float]:
    try:
        lp = await cli.market_data.get_last_prices(figi=[figi])
        if lp.last_prices:
            q = lp.last_prices[0]
            return q.price.units + q.price.nano/1e9
    except Exception:
        return None
    return None

# ===== Главная функция автоподтверждения =====
async def auto_confirm_csv(csv_path: str) -> None:
    if not os.path.exists(csv_path): return
    if not TINKOFF_TOKEN: return
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        if not rows: return
        fieldnames: List[str] = list(rows[0].keys())
    async with AsyncClient(TINKOFF_TOKEN) as cli:
        for r in rows:
            if (r.get("verdict") or "").lower() == "confirm":
                continue
            side = _side_from_zone(r.get("zone") or r.get("Zone") or "")
            if side is None: continue
            ts = _parse_ts(r.get("ts") or r.get("timestamp") or "")
            if not _ok_ttl(ts): continue
            try:
                rsi_h4 = float(r.get("rsi_h4")) if r.get("rsi_h4") not in (None,"","—") else None
            except Exception:
                rsi_h4 = None
            if not _ok_rsi(side, rsi_h4): continue
            try:
                entry = float(r.get("entry"))
            except Exception:
                continue
            ticker     = (r.get("ticker") or "").strip()
            class_code = (r.get("class_code") or "").strip()
            if not ticker: continue
            figi = await _resolve_figi(cli, ticker, class_code)
            if not figi: continue
            last_px = await _get_last_price(cli, figi)
            if last_px is None: continue
            if not _ok_dev(entry, last_px): continue
            r["verdict"] = "confirm"
    if "verdict" not in fieldnames:
        fieldnames.append("verdict")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k,"") for k in fieldnames})

if __name__ == "__main__":
    import asyncio, sys
    if len(sys.argv) < 2:
        print("Usage: python auto_confirm.py out/live_candidates_public.csv [another.csv ...]")
        raise SystemExit(0)
    async def _main(paths: List[str]):
        for p in paths:
            try:
                await auto_confirm_csv(p)
                print(f"[auto-confirm] OK: {p}")
            except Exception as e:
                print(f"[auto-confirm] ERR: {p}: {e}")
    asyncio.run(_main(sys.argv[1:]))
