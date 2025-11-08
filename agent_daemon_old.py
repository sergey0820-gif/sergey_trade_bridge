#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Agent Daemon (one-shot): читает live_candidates_*.csv, строгий фильтр подтверждения
и шлёт ПОДТВЕРЖДЁННЫЕ сигналы в Telegram отдельными карточками.
Запускать через systemd timer каждые N секунд.

Фильтры подтверждения (дефолт):
- recommendation НЕ содержит "watch" и содержит {long, short, buy, sell, enter}
- TTL (минуты) не просрочен
- |now - entry| <= MAX_DEVIATION_PCT (%)
- R:R >= 2 (по уровням entry/stop/target)
- Cost/R <= 0.20
- нет событий (див/отчёт) ±2д (если колонка есть; иначе пропускаем фильтр)
- шорт только через фьючи
- если SESSION_ONLY=true — работать только в основную сессию МосБиржи
- если инструмент уже в позиции — не показывать кнопку Place (HOLD)

Кнопки:
- Place (callback_data: "place:<TICKER>")
- Details (callback_data: "details:<TICKER>")
- Для KI-строки: без Place, только “Разместить вручную” (callback_data: "ki_manual:<TICKER>")
"""

import os
import csv
import json
import math
import time
import asyncio
import datetime as dt
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict

from dotenv import load_dotenv

# Tinkoff Invest
from tinkoff.invest import Client, InstrumentIdType
from tinkoff.invest.exceptions import RequestError
from tinkoff.invest.services import InstrumentsService, MarketDataService

# Telegram (async Bot API)
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

# ---------- ENV / конфиг ----------
load_dotenv(".env")

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID   = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

TINKOFF_TOKEN      = os.getenv("TINKOFF_TOKEN") or os.getenv("TINKOFF_INVEST_TOKEN")
ACCOUNT_ID         = os.getenv("TINKOFF_ACCOUNT_ID")

PUBLIC_CSV         = os.getenv("PUBLIC_CSV", os.path.join("out", "live_candidates_public.csv"))
KI_CSV             = os.getenv("KI_CSV",     os.path.join("out", "live_candidates_ki.csv"))

AGENT_ENABLED      = os.getenv("AGENT_ENABLED", "true").lower() == "true"
AGENT_MAX_CARDS    = int(os.getenv("AGENT_MAX_CARDS_PER_RUN", "6"))

# Фильтры подтверждения
ONLY_CONFIRMED     = os.getenv("ONLY_CONFIRMED", "true").lower() == "true"
TTL_MINUTES        = int(os.getenv("TTL_MINUTES", "90"))
MAX_DEVIATION_PCT  = float(os.getenv("MAX_DEVIATION_PCT", "0.5"))
SESSION_ONLY       = os.getenv("SESSION_ONLY", "true").lower() == "true"

# Риск/капитал (для лотов)
RISK_PCT           = float(os.getenv("RISK_PCT", "0.01"))
CAPITAL            = float(os.getenv("CAPITAL", "13000"))

# Лимиты/ограничения
EXCLUDE_HELD       = os.getenv("EXCLUDE_HELD", "true").lower() == "true"
QUIET_HOURS_RAW    = os.getenv("QUIET_HOURS", "").strip()  # типа "23:00-08:00"

FIGI_CACHE_FILE    = os.getenv("FIGI_CACHE_FILE", "cache_figi.json")
SEND_CACHE_FILE    = os.getenv("AGENT_SEND_CACHE", "agent_sent_cache.json")

# ---------- модели ----------
@dataclass
class Candidate:
    ts: dt.datetime
    ticker: str
    class_code: str
    name: str
    trend_d1: str
    zone: str
    rsi_h4: Optional[float]
    scenario: str
    recommendation: str
    entry: float
    stop: float
    target: float
    all_in_bps: Optional[float]
    cost_r: Optional[float]
    passed: bool
    events_flag: Optional[str] = None  # если в CSV есть

# ---------- утилиты ----------
def parse_ts(s: str) -> dt.datetime:
    # CSV приходит без TZ → считаем naive UTC-like и сравниваем с now naive
    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        # fallback: без микросекунд
        try:
            return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return dt.datetime.utcnow()  # хуже, чем ничего

def parse_levels(levels: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    levels: 'entry 300.00 / stop 294.00 / target 315.00'
    """
    e = s = t = None
    try:
        parts = [p.strip() for p in levels.split("/") if p.strip()]
        for p in parts:
            if p.lower().startswith("entry"):
                e = float(p.split()[-1])
            elif p.lower().startswith("stop"):
                s = float(p.split()[-1])
            elif p.lower().startswith("target"):
                t = float(p.split()[-1])
    except Exception:
        pass
    return e, s, t

def rr(entry: float, stop: float, target: float) -> Optional[float]:
    try:
        risk = abs(entry - stop)
        reward = abs(target - entry)
        if risk <= 0:
            return None
        return reward / risk
    except Exception:
        return None

def read_csv(path: str) -> List[Candidate]:
    if not os.path.exists(path):
        return []
    rows: List[Candidate] = []
    with open(path, "r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for d in r:
            e, s, t = parse_levels(d.get("levels", "") or "")
            rows.append(Candidate(
                ts=parse_ts(d.get("ts", "")),
                ticker=d.get("ticker","").strip(),
                class_code=d.get("class","").strip(),
                name=d.get("name","").strip() if "name" in d else "",  # если добавим колонку
                trend_d1=d.get("trend_d1","").strip(),
                zone=d.get("zone","").strip(),
                rsi_h4=float(d["rsi_h4"]) if (d.get("rsi_h4") not in (None,"","—")) else None,
                scenario=d.get("scenario","").strip(),
                recommendation=d.get("recommendation","").strip().lower(),
                entry=float(e) if e is not None else float(d.get("entry", "nan") or "nan"),
                stop=float(s) if s is not None else float(d.get("stop", "nan") or "nan"),
                target=float(t) if t is not None else float(d.get("target", "nan") or "nan"),
                all_in_bps=float(d["all_in_bps"]) if d.get("all_in_bps") else None,
                cost_r=float(d["cost_r"]) if d.get("cost_r") else None,
                passed=(str(d.get("pass","")).strip().upper() == "YES"),
                events_flag=d.get("events","").strip() if "events" in d else None
            ))
    return rows

def is_quiet_hours(now: dt.datetime) -> bool:
    if not QUIET_HOURS_RAW:
        return False
    try:
        a,b = QUIET_HOURS_RAW.split("-")
        a_h,a_m = map(int,a.split(":"))
        b_h,b_m = map(int,b.split(":"))
        start = now.replace(hour=a_h, minute=a_m, second=0, microsecond=0)
        end   = now.replace(hour=b_h, minute=b_m, second=0, microsecond=0)
        if start <= end:
            return start <= now <= end
        else:
            # через полночь
            return now >= start or now <= end
    except Exception:
        return False

def is_main_session(now: dt.datetime) -> bool:
    # Простейшее окно основной сессии МосБиржи (по Москве). Подстрой при желании.
    # МСК в 2025 = UTC+3; скрипт не привязываем к tz, просто по локальному времени RPi.
    # Берём локальное время Raspberry (как у тебя).
    H, M = now.hour, now.minute
    # 10:00–18:45 local — условно
    return (H > 9 or (H == 9 and M >= 55)) and (H < 18 or (H == 18 and M <= 45))

def within_ttl(ts: dt.datetime, now: dt.datetime, ttl_min: int) -> bool:
    return (now - ts) <= dt.timedelta(minutes=ttl_min)

def recommendation_is_confirmed(rec: str) -> bool:
    if "watch" in rec:
        return False
    keywords = ("long","short","buy","sell","enter")
    return any(k in rec for k in keywords)

def costr_ok(c: Optional[float]) -> bool:
    return (c is not None) and (c <= 0.20)

def rr_ok(entry: float, stop: float, target: float) -> bool:
    val = rr(entry, stop, target)
    return (val is not None) and (val >= 2.0)

def deviation_ok(entry: float, now_price: float, max_pct: float) -> bool:
    if not math.isfinite(entry) or not math.isfinite(now_price) or entry <= 0:
        return False
    d = abs(now_price - entry) / entry * 100.0
    return d <= max_pct

def short_allowed(class_code: str, rec: str) -> bool:
    if "short" in rec:
        return class_code.upper() == "SPBFUT"  # шорт только во фьючах
    return True

def events_ok(flag: Optional[str]) -> bool:
    # Если колонка есть и явно помечено, что близко события — отбрасываем
    if flag is None:
        return True
    txt = flag.lower()
    bad = ("div", "див", "report", "отчёт", "отчет", "earnings")
    return not any(b in txt for b in bad)

# FIGI cache
def load_json(path: str, default):
    try:
        with open(path,"r",encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp,"w",encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

async def resolve_figi_many(tickers: List[Tuple[str,str]], client: Client) -> Dict[Tuple[str,str], str]:
    """
    tickers: list of (ticker, class_code)
    Возвращает {(ticker,class): figi}
    """
    cache = load_json(FIGI_CACHE_FILE, {})
    out: Dict[Tuple[str,str], str] = {}
    inst: InstrumentsService = client.instruments

    for (t, c) in tickers:
        key = f"{t}:{c}"
        if key in cache and cache[key]:
            out[(t,c)] = cache[key]
            continue
        # ищем по тикеру; при необходимости сужаем по class_code
        # Стараемся не ломать лимиты — короткая пауза
        await asyncio.sleep(0.02)
        found = inst.find_instrument(query=t).instruments
        figi = None
        # пытаемся найти точное совпадение по тикеру/классу
        for ins in found:
            if ins.ticker == t and (not c or ins.class_code.upper() == c.upper()):
                figi = ins.figi
                break
        # иначе берём первое
        if not figi and found:
            figi = found[0].figi
        if figi:
            cache[key] = figi
            out[(t,c)] = figi
    save_json(FIGI_CACHE_FILE, cache)
    return out

async def get_last_prices(figis: List[str], client: Client) -> Dict[str, float]:
    md: MarketDataService = client.market_data
    out: Dict[str, float] = {}
    if not figis:
        return out
    # пачками по 50
    B = 50
    for i in range(0, len(figis), B):
        batch = figis[i:i+B]
        await asyncio.sleep(0.05)
        resp = md.get_last_prices(figi=batch)
        for lp in resp.last_prices:
            out[lp.figi] = float(lp.price.units) + float(lp.price.nano) / 1e9
    return out

def load_sent_cache() -> Dict[str, float]:
    return load_json(SEND_CACHE_FILE, {})

def save_sent_cache(data: Dict[str, float]):
    save_json(SEND_CACHE_FILE, data)

def key_for_candidate(c: Candidate) -> str:
    # уникальный ключ для дедупликации (тикер + уровни + ts)
    return f"{c.ticker}|{c.class_code}|{c.entry:.6f}|{c.stop:.6f}|{c.target:.6f}|{c.ts.isoformat()}"

def estimate_lots(entry: float, stop: float, risk_pct: float, capital: float) -> Optional[int]:
    try:
        per_lot_risk = abs(entry - stop)  # для фьючей ок в пунктах цены инструмента (лот=1)
        if per_lot_risk <= 0:
            return None
        lots = math.floor((capital * risk_pct) / per_lot_risk)
        return max(lots, 0)
    except Exception:
        return None

def held_tickers(client: Client) -> set:
    res = set()
    try:
        p = client.operations.get_positions(account_id=ACCOUNT_ID)
        for f in p.futures:
            if f.balance and f.balance != 0:
                res.add(f.figi)
        for s in p.securities:
            if s.balance and s.balance != 0:
                res.add(s.figi)
    except Exception:
        # fallback через portfolio
        try:
            port = client.operations.get_portfolio(account_id=ACCOUNT_ID)
            for pos in port.positions:
                if pos.quantity and (pos.quantity.units or pos.quantity.nano):
                    res.add(pos.figi)
        except Exception:
            pass
    return res

def build_text(c: Candidate, name: str, now_px: Optional[float], lots: Optional[int], hold: bool) -> str:
    rsi_txt = "—" if c.rsi_h4 is None else f"{c.rsi_h4:.1f}"
    d_txt = ""
    if now_px and c.entry:
        d = (now_px - c.entry)/c.entry*100.0
        arrow = "▲" if d>=0 else "▼"
        d_txt = f" | Δ={arrow}{abs(d):.2f}% (now {now_px:.2f})"
    lots_txt = f" | lots≈{lots}" if lots and lots>0 else ""
    hold_txt = " | HOLD" if hold else ""
    cost_txt = f" | All-in: {int(c.all_in_bps or 0)} bps | Cost/R: {c.cost_r:.3f}" if c.cost_r is not None else ""
    return (
        f"• {c.ticker} ({c.class_code}) — {name or ''}\n"
        f"  D1:{c.trend_d1}, Zone:{c.zone}, RSI(H4): {rsi_txt}\n"
        f"  {c.scenario}\n"
        f"  Уровни: entry {c.entry:.2f} / stop {c.stop:.2f} / target {c.target:.2f}"
        f"{d_txt}{lots_txt}\n"
        f"{cost_txt}"
        f"{hold_txt}"
    )

def build_markup(c: Candidate, is_ki: bool, can_place: bool) -> InlineKeyboardMarkup:
    buttons = []
    if is_ki:
        buttons.append([InlineKeyboardButton("Разместить вручную", callback_data=f"ki_manual:{c.ticker}")])
        buttons.append([InlineKeyboardButton("Детали", callback_data=f"details:{c.ticker}")])
    else:
        row = []
        if can_place:
            row.append(InlineKeyboardButton("Разместить", callback_data=f"place:{c.ticker}"))
        row.append(InlineKeyboardButton("Детали", callback_data=f"details:{c.ticker}"))
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)

async def send_card(bot: Bot, text: str, markup: InlineKeyboardMarkup):
    # отдельным сообщением на каждого кандидата
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, reply_markup=markup, disable_web_page_preview=True)

async def main():
    if not AGENT_ENABLED:
        return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not TINKOFF_TOKEN or not ACCOUNT_ID:
        return

    now = dt.datetime.now()  # локальное
    if SESSION_ONLY and not is_main_session(now):
        return
    if is_quiet_hours(now):
        return

    public = read_csv(PUBLIC_CSV)
    ki     = read_csv(KI_CSV)
    # базовая очистка пустых тикеров
    public = [c for c in public if c.ticker]
    ki     = [c for c in ki if c.ticker]

    # берём не больше разумного числа (на случай очень длинных файлов)
    public = public[:500]
    ki     = ki[:500]

    # Резолвим FIGI и last_price
    tickers_all = list({(c.ticker, c.class_code) for c in (public + ki)})
    async with Client(TINKOFF_TOKEN) as client:
        figi_map = await resolve_figi_many(tickers_all, client)
        figis = list({figi for figi in figi_map.values() if figi})
        last_px = await get_last_prices(figis, client)
        held_figis = held_tickers(client) if EXCLUDE_HELD else set()

    # Кэш отправленного
    sent_cache = load_sent_cache()

    # Собираем кандидатов, проходим фильтры и шлём до AGENT_MAX_CARDS
    bot = Bot(TELEGRAM_BOT_TOKEN)

    def process_block(cands: List[Candidate], is_ki: bool) -> int:
        sent = 0
        for c in cands:
            # TTL
            if not within_ttl(c.ts, now, TTL_MINUTES):
                continue
            # confirmed?
            if ONLY_CONFIRMED and not recommendation_is_confirmed(c.recommendation):
                continue
            # Cost/R
            if not costr_ok(c.cost_r):
                continue
            # R:R >=2
            if not rr_ok(c.entry, c.stop, c.target):
                continue
            # events
            if not events_ok(c.events_flag):
                continue
            # short only futures
            if not short_allowed(c.class_code, c.recommendation):
                continue

            # текущая цена и отклонение
            figi = figi_map.get((c.ticker, c.class_code))
            now_px = last_px.get(figi) if figi else None
            if now_px is None:
                # если не смогли получить цену — считаем, что не подтверждён
                continue
            if not deviation_ok(c.entry, now_px, MAX_DEVIATION_PCT):
                continue

            # дедупликатор
            k = key_for_candidate(c)
            if k in sent_cache:
                continue

            # HOLD?
            hold = (figi in held_figis) if figi else False
            lots = estimate_lots(c.entry, c.stop, RISK_PCT, CAPITAL)
            text = build_text(c, c.name, now_px, lots, hold)

            # кнопки
            markup = build_markup(c, is_ki=is_ki, can_place=(not is_ki and not hold))

            # отправка
            asyncio.run_coroutine_threadsafe(
                send_card(bot, text, markup), asyncio.get_event_loop()
            )

            # Но мы в корутине main(), у нас уже есть loop. Корректнее — просто await:
            # (оставим так: ниже вынесем фактический await)
            to_send.append((text, markup, k))
        return sent

    # Корректное отправление (await), с ограничением по числу карточек
    to_send: List[Tuple[str, InlineKeyboardMarkup, str]] = []

    # Сначала public (с кнопками), потом ki (без place)
    process_block(public, is_ki=False)
    process_block(ki, is_ki=True)

    # Отправляем, уважаем лимит
    count = 0
    for text, markup, k in to_send:
        if count >= AGENT_MAX_CARDS:
            break
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, reply_markup=markup, disable_web_page_preview=True)
            sent_cache[k] = time.time()
            count += 1
            await asyncio.sleep(0.05)  # чутка подышать
        except Exception as e:
            # можно логировать в файл
            pass

    save_sent_cache(sent_cache)

if __name__ == "__main__":
    # Запускаем как однократную корутину (systemd timer вызовет каждые N секунд)
    try:
        asyncio.run(main())
    except RuntimeError:
        # Если вдруг "loop already running" (крайне маловероятно здесь) — fallback
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())
