#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Backtest 3M по стратегии Sergey-Trade 2025
— D1-тренд (EMA), вход по H4-триггеру (engulfing/break/pin)
— H4 считается как «виртуальная свеча» из 8×30m скользящим окном (каждые 30 минут)
— Стоп/тейк: SL за экстремумом виртуальной H4 (+страховка), TP = 2R
— Фильтры: R:R≥2, Cost/R≤0.20, риск ≤2% капитала, shorts только через futures
— Без чтения реальных ордеров: это ИМИТАЦИЯ (бэктест)

ЗАМЕТКИ:
- Токен берётся из .env: TINKOFF_TOKEN (или TINKOFF_INVEST_TOKEN)
- Избегаем pandas; чистый Python
- Ограничиваем запросы таймаутами, чтобы не упереться в rate-limit (600/60s)
"""

import os, time, math, csv
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from dotenv import load_dotenv
from tinkoff.invest import Client, CandleInterval
from tinkoff.invest.utils import now

# ---------- Конфигурация ----------
load_dotenv()
TOKEN = os.getenv("TINKOFF_TOKEN") or os.getenv("TINKOFF_INVEST_TOKEN")
if not TOKEN:
    raise RuntimeError("TINKOFF_TOKEN (или TINKOFF_INVEST_TOKEN) не найден в .env")

UNIVERSE = [
    ("SBER", "stock"),
    ("GAZP", "stock"),
    ("LKOH", "stock"),
    ("RI",   "futures"),  # RTS индекс (ближайший контракт)
    ("Si",   "futures"),  # USD/RUB (ближайший контракт)
]
MONTHS = 3
INITIAL_CAPITAL = 100_000.0
RISK_PCT = 0.02                 # риск ≤ 2% капитала
STOCK_COST_BPS = 25.0           # ~0.25% RT для акций (комиссия+биржа+спред)
FUT_COST_BPS   = 12.0           # ~0.12% RT для фьючерсов (тик-эквивалент)
COSTR_MAX = 0.20
TRADE_HOURS_UTC = (6, 16)       # фильтр торговых окон для MOEX (06:00..16:59 UTC ~ 09:00..19:59 МСК)

# ---------- Утилиты ----------
def ema(values, period):
    if len(values) < period:
        return None
    k = 2/(period+1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v*k + e*(1-k)
    return e

def rsi_from_closes(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i-1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain*(period-1) + gains[i]) / period
        avg_loss = (avg_loss*(period-1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def quotation_to_float(q):
    return q.units + q.nano/1e9

def synth_h4_from_30m(candles_30m):
    """Сшиваем 8×30m в одну «виртуальную H4»."""
    if not candles_30m:
        return None
    opens  = [quotation_to_float(c.open)  for c in candles_30m]
    highs  = [quotation_to_float(c.high)  for c in candles_30m]
    lows   = [quotation_to_float(c.low)   for c in candles_30m]
    closes = [quotation_to_float(c.close) for c in candles_30m]
    vol    = sum([c.volume for c in candles_30m])
    return {
        "open":  opens[0],
        "high":  max(highs),
        "low":   min(lows),
        "close": closes[-1],
        "volume": vol,
        "ts": candles_30m[-1].time,  # конец окна
    }

def detect_h4_trigger(curr, prev):
    """
    Триггеры:
      - engulfing_bull / engulfing_bear
      - break_up / break_down
      - pin_bull / pin_bear
    """
    if not curr or not prev:
        return None
    o1, c1, h1, l1 = prev["open"], prev["close"], prev["high"], prev["low"]
    o2, c2, h2, l2 = curr["open"], curr["close"], curr["high"], curr["low"]
    body1, body2 = abs(c1-o1), abs(c2-o2)
    rng2 = max(h2 - l2, 0.0)

    # 1) Engulfing (упрощённо по телам)
    if body1 > 0 and body2 > 0:
        bull = (c2 > o2) and (o2 <= c1 <= c2) and (o1 >= l2)
        bear = (c2 < o2) and (o2 >= c1 >= c2) and (o1 <= h2)
        if bull: return "engulfing_bull"
        if bear: return "engulfing_bear"

    # 2) Break (импульс: закрытие за high/low предыдущей)
    if c2 > h1:
        return "break_up"
    if c2 < l1:
        return "break_down"

    # 3) Pin-bar (малое тело, длинная тень >60%)
    if rng2 > 0 and body2/rng2 < 0.25:
        upper = h2 - max(c2, o2)
        lower = min(c2, o2) - l2
        if lower/rng2 > 0.6:
            return "pin_bull"
        if upper/rng2 > 0.6:
            return "pin_bear"

    return None

def cost_bps(asset_class):
    return FUT_COST_BPS if asset_class == "futures" else STOCK_COST_BPS

def cost_r_ok(asset_class, planned_move_pct):
    """planned_move_pct — в ПРЦЕНТАХ, напр. 3.0 = 3%."""
    bps = cost_bps(asset_class)
    move_bps = planned_move_pct * 100.0  # 1% = 100 bps
    if move_bps <= 0:
        return False
    return (bps / move_bps) <= COSTR_MAX

# ---------- Работа с API ----------
def find_instrument_figi(api: Client, ticker: str, cls: str):
    if cls == "stock":
        res = api.instruments.shares()
        for s in res.instruments:
            if s.ticker == ticker:
                return s.figi
        return None
    else:  # futures
        futs = api.instruments.futures().instruments
        # Поищем ближайший контракт, чей тикер начинается с нужного кода (RI, Si и т.п.)
        cands = [f for f in futs if f.ticker.startswith(ticker)]
        if not cands:
            return None
        # сортируем по дате экспирации (ближайший вперёд)
        cands.sort(key=lambda x: x.expiration_date or now())
        return cands[0].figi

def fetch_d1(api: Client, figi: str, since: datetime, to: datetime):
    cs = api.market_data.get_candles(figi=figi, from_=since, to=to,
                                     interval=CandleInterval.CANDLE_INTERVAL_DAY).candles
    bars = []
    for c in cs:
        bars.append({
            "ts": c.time,
            "open":  quotation_to_float(c.open),
            "high":  quotation_to_float(c.high),
            "low":   quotation_to_float(c.low),
            "close": quotation_to_float(c.close),
        })
    return bars

def fetch_30m_day(api: Client, figi: str, day_utc):
    """Все 30m свечи за сутки (UTC), с фильтром торговых часов MOEX."""
    day_start = datetime(day_utc.year, day_utc.month, day_utc.day, 0, 0, tzinfo=timezone.utc)
    day_end   = day_start + timedelta(days=1)
    cs = api.market_data.get_candles(figi=figi, from_=day_start, to=day_end,
                                     interval=CandleInterval.CANDLE_INTERVAL_30_MIN).candles
    low_h, high_h = TRADE_HOURS_UTC
    filtered = [c for c in cs if low_h <= c.time.hour <= high_h]
    return filtered

def iter_dynamic_h4_windows(candles_30m):
    """Генератор виртуальных H4 окон (8×30m), скользящий шаг — 30 минут."""
    for i in range(8, len(candles_30m)+1):
        yield candles_30m[i-8:i]

# ---------- Бэктест символа ----------
def backtest_symbol(api: Client, ticker: str, cls: str, capital: float):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=90)  # ~3 мес (для D1)
    figi = find_instrument_figi(api, ticker, cls)
    if not figi:
        return {"ticker": ticker, "error": "FIGI not found"}

    d1 = fetch_d1(api, figi, start, end)
    if len(d1) < 40:
        return {"ticker": ticker, "error": "not enough D1 bars"}

    closes = [b["close"] for b in d1]
    # EMA200 может не считаться на 3 мес → fallback EMA60
    trend_period = 200
    ema_val = ema(closes, trend_period)
    if ema_val is None:
        trend_period = 60
        ema_val = ema(closes, trend_period)
        if ema_val is None:
            return {"ticker": ticker, "error": "not enough bars for EMA"}

    trades = []
    equity = capital

    # Идём по дням D1: для каждого дня — все 30m, затем скользящие окна 8×30m
    for i in range(trend_period+1, len(d1)):
        d = d1[i]
        day = d["ts"].date()
        price_d1_close = d["close"]

        # тренд D1 на этот день — EMA по окну до текущего бара
        sub = closes[i-trend_period:i]
        trend_ema = ema(sub, trend_period)
        if trend_ema is None:
            continue
        up_trend = price_d1_close > trend_ema
        down_trend = price_d1_close < trend_ema

        # Ограничение: акции — только long; фьючи — long/short
        allow_long = True
        allow_short = (cls == "futures")

        # Получаем все 30m свечи за этот день
        c30 = fetch_30m_day(api, figi, d["ts"])
        time.sleep(0.1)
        if len(c30) < 8:
            continue

        prev_synth = None
        # Идём скользящим окном — формируем H4 каждые 30 минут
        for window in iter_dynamic_h4_windows(c30):
            curr = synth_h4_from_30m(window)
            if not curr:
                continue
            # Прев. окно — это H4, закончившаяся 30 минутами ранее
            if prev_synth is None:
                prev_synth = curr
                continue

            # RSI из 30m закрытий в текущем окне
            closes30 = [quotation_to_float(c.close) for c in window]
            rsi_h4 = rsi_from_closes(closes30, 14)
            trig = detect_h4_trigger(curr, prev_synth)

            # Движемся по окнам — каждое следующее становится prev для следующего шага
            prev_synth = curr

            if not trig or rsi_h4 is None:
                continue

            # Направление сигнала
            long_sig  = trig in ("engulfing_bull", "break_up",  "pin_bull")
            short_sig = trig in ("engulfing_bear","break_down","pin_bear")

            # Фильтр тренда D1 + запрет шорта для акций
            if long_sig and not up_trend:
                continue
            if short_sig and not down_trend:
                continue
            if short_sig and not allow_short:
                continue

            entry = curr["close"]
            if long_sig:
                stop = min(curr["low"], prev_synth["low"])
                atr_like = (curr["high"] - curr["low"]) * 0.5
                stop = min(stop, entry - atr_like*0.5)
                target = entry + 2*(entry - stop)
                planned_move_pct = (target/entry - 1.0)*100.0
            else:
                stop = max(curr["high"], prev_synth["high"])
                atr_like = (curr["high"] - curr["low"]) * 0.5
                stop = max(stop, entry + atr_like*0.5)
                target = entry - 2*(stop - entry)
                planned_move_pct = (1.0 - target/entry)*100.0

            # Cost/R фильтр
            if not cost_r_ok(cls, planned_move_pct):
                continue

            # риск ≤ 2% капитала → size
            risk_money = equity * RISK_PCT
            risk_per_unit = abs(entry - stop)
            if risk_per_unit <= 0:
                continue
            size = max(1, int(risk_money / risk_per_unit))
            if size <= 0:
                continue

            # Симуляция исхода: ищем TP/SL в следующих 2 календарных днях на 30m
            start_sim = curr["ts"]
            end_sim   = start_sim + timedelta(days=2)
            cs = api.market_data.get_candles(figi=figi, from_=start_sim, to=end_sim,
                                             interval=CandleInterval.CANDLE_INTERVAL_30_MIN).candles
            time.sleep(0.1)

            hit = "none"
            for c in cs:
                hi = quotation_to_float(c.high)
                lo = quotation_to_float(c.low)
                if long_sig:
                    if lo <= stop:         hit = "SL"; break
                    if hi >= target:       hit = "TP"; break
                else:
                    if hi >= stop:         hit = "SL"; break
                    if lo <= target:       hit = "TP"; break

            if hit == "TP":
                exit_price = target
            elif hit == "SL":
                exit_price = stop
            else:
                exit_price = quotation_to_float(cs[-1].close) if cs else entry

            pnl = (exit_price - entry) * size
            if short_sig:
                pnl = -pnl

            # Списываем круговую стоимость сделки (bps)
            rt_cost = cost_bps(cls) / 10000.0
            pnl -= entry * size * rt_cost

            equity += pnl
            trades.append({
                "date": curr["ts"].isoformat(),
                "ticker": ticker, "class": cls,
                "signal": "long" if long_sig else "short",
                "trig": trig,
                "entry": entry, "stop": stop, "target": target,
                "exit": exit_price, "pnl": pnl, "size": size
            })

        # Переход к следующему дню
    # --- Метрики ---
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] <= 0)
    total = max(1, wins + losses)
    win_rate = (wins / total) * 100.0

    avg_r = None
    if trades:
        rs = []
        for t in trades:
            risk = abs(t["entry"] - t["stop"])
            if risk > 0:
                r = (t["exit"] - t["entry"]) / risk
                if t["signal"] == "short":
                    r = -r
                rs.append(r)
        if rs:
            avg_r = sum(rs) / len(rs)

    return {
        "ticker": ticker, "class": cls, "trades": trades,
        "win_rate": win_rate, "avg_r": avg_r, "final_equity": equity
    }

# ---------- main ----------
def main():
    out_dir = "out"
    os.makedirs(out_dir, exist_ok=True)
    summary_rows = []

    with Client(TOKEN) as api:
        for ticker, cls in UNIVERSE:
            try:
                res = backtest_symbol(api, ticker, cls, INITIAL_CAPITAL)
            except Exception as e:
                print(f"[{ticker}] EXCEPTION: {e}")
                continue

            if "error" in res:
                print(f"[{ticker}] ERROR: {res['error']}")
                continue

            # CSV по сделкам
            path = os.path.join(out_dir, f"bt_{ticker}.csv")
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["date","ticker","class","signal","trig","entry","stop","target","exit","pnl","size"])
                for t in res["trades"]:
                    w.writerow([
                        t["date"], t["ticker"], t["class"], t["signal"], t["trig"],
                        f"{t['entry']:.6f}", f"{t['stop']:.6f}", f"{t['target']:.6f}",
                        f"{t['exit']:.6f}", f"{t['pnl']:.2f}", t["size"]
                    ])

            print(f"[{ticker}] trades={len(res['trades'])}, win_rate={res['win_rate']:.1f}%, avg_R={res['avg_r']}, equity={res['final_equity']:.2f}")
            summary_rows.append([
                ticker, cls, len(res["trades"]),
                f"{res['win_rate']:.1f}%",
                f"{res['avg_r'] if res['avg_r'] is not None else 'n/a'}",
                f"{res['final_equity']:.2f}"
            ])

            # мягкий троттлинг между символами
            time.sleep(0.3)

    # Сводка
    with open(os.path.join(out_dir, "bt_summary.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker","class","trades","win_rate","avg_R","final_equity"])
        w.writerows(summary_rows)

    print("\nDone. See out/bt_summary.csv")

if __name__ == "__main__":
    main()
