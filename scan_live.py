# scan_live.py — двухэтапный скан всех акций РФ и всех фьючерсов Tinkoff
# A: быстрый срез (20d ликвидность, ATR%) по всему универсуму
# B: детальный расчёт (RSI H4 + стакан) только по TOP_DETAILED
# Выход: out/live_candidates.csv в требуемом формате колонок

import os
import csv
import json
import time
import datetime as dt
from dotenv import load_dotenv
from tinkoff.invest import Client, InstrumentStatus, CandleInterval
from tinkoff.invest.exceptions import RequestError

load_dotenv()
TOKEN = os.getenv("TINKOFF_INVEST_TOKEN")

OUT_CSV = "out/live_candidates.csv"
COSTS_JSON = "data/costs.json"
EVENTS_CSV = "data/events.csv"

# --- настройки троттлинга и глубины ---
RATE_LIMIT_DELAY = 0.30  # пауза после КАЖДОГО удачного API вызова (~3-4 req/s)
TOP_DETAILED = 40  # сколько инструментов считать "глубоко" (H1->H4 + стакан)
MAX_RETRIES = 12  # максимум повторов одного вызова при лимите


# -------------- utils --------------
def q2f(q):
    return q.units + q.nano / 1e9


def ema(series, period):
    if not series:
        return None
    k = 2 / (period + 1)
    e = None
    for v in series:
        e = v if e is None else (v - e) * k + e
    return e


def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        (gains if d > 0 else losses).append(abs(d))
    ag = (sum(gains[-period:]) / period) or 1e-9
    al = (sum(losses[-period:]) / period) or 1e-9
    rs = ag / al
    return 100 - 100 / (1 + rs)


def load_costs():
    try:
        with open(COSTS_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "default": {"stock_allin_bps": 8, "futures_allin_bps": 4},
            "overrides": {},
        }


def all_in_commission_bps(ticker, klass, cfg):
    over = cfg.get("overrides", {}).get(ticker)
    if isinstance(over, (int, float)):
        return float(over)
    dflt = cfg["default"]
    return float(
        dflt["stock_allin_bps"] if klass == "stock" else dflt["futures_allin_bps"]
    )


def planned_move_pct(klass, close, atr_pct):
    if klass == "stock":
        # акции: макс(ATR%, 3%) и не больше 7%
        return min(max(atr_pct, 3.0), 7.0)
    # фьючи — используем ATR% (не меньше 1.5%)
    return max(atr_pct, 1.5)


def cost_over_R(cost_bps, move_pct):
    return (cost_bps / 100.0) / move_pct if move_pct > 0 else 999.0


def zone_by_price(close, hi20, lo20, atr):
    if close is None or hi20 is None or lo20 is None or atr is None:
        return "middle"
    if close <= lo20 + atr:
        return "support"
    if close >= hi20 - atr:
        return "resistance"
    return "middle"


def load_events():
    ev = {}
    try:
        with open(EVENTS_CSV, "r", encoding="utf-8") as f:
            r = csv.DictReader(f)
            for row in r:
                t = (row.get("Ticker") or "").strip()
                d = (row.get("Date") or "").strip()
                if t and d:
                    ev.setdefault(t, []).append(d)
    except Exception:
        pass
    return ev


def near_event(ticker, today, ev, win=2):
    if ticker not in ev:
        return False
    for s in ev[ticker]:
        try:
            dd = dt.datetime.strptime(s, "%Y-%m-%d").date()
            if abs((dd - today.date()).days) <= win:
                return True
        except Exception:
            continue
    return False


# безопасный вызов API с ретраями и паузой
def safe_call(fn, *args, **kwargs):
    tries = 0
    while True:
        try:
            res = fn(*args, **kwargs)
            time.sleep(RATE_LIMIT_DELAY)  # мягкий троттлинг
            return res
        except RequestError as e:
            tries += 1
            code = getattr(e, "status_code", None)
            meta = getattr(e, "metadata", None)
            if (
                code
                and str(code).endswith("RESOURCE_EXHAUSTED")
                and tries <= MAX_RETRIES
            ):
                reset = 5
                if meta and getattr(meta, "ratelimit_reset", None):
                    try:
                        reset = int(meta.ratelimit_reset)
                    except Exception:
                        reset = 5
                time.sleep(reset + 1)
                continue
            raise


# -------------- main --------------
def main():
    if not TOKEN:
        raise RuntimeError("Нет токена TINKOFF_INVEST_TOKEN в .env")

    costs_cfg = load_costs()
    events = load_events()

    now = dt.datetime.now(dt.timezone.utc)

    # ЭТАП A: короткие D1 (для ликвидности и ATR%)
    A_from = now - dt.timedelta(days=23)

    # ЭТАП B: расширенные окна для D1/H1
    B_d1_from = now - dt.timedelta(days=240)
    B_h1_from = now - dt.timedelta(days=25)

    out_rows = []

    with Client(TOKEN) as c:
        ins = c.instruments
        md = c.market_data

        # Универсум: все акции РФ (is_traded) + все фьючерсы
        shares = safe_call(
            ins.shares, instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE
        ).instruments
        shares_ru = [
            s
            for s in shares
            if (s.country_of_risk == "RU" or s.country_of_risk_name == "Россия")
            and s.api_trade_available_flag
        ]
        futs = safe_call(ins.futures).instruments

        universe = [
            {"figi": s.figi, "ticker": s.ticker, "class": "stock"} for s in shares_ru
        ] + [{"figi": f.figi, "ticker": f.ticker, "class": "futures"} for f in futs]

        # --- ЭТАП A: 20d ликвидность и ATR% по всему списку ---
        pre = []
        for it in universe:
            figi = it["figi"]
            d1 = safe_call(
                md.get_candles,
                figi=figi,
                from_=A_from,
                to=now,
                interval=CandleInterval.CANDLE_INTERVAL_DAY,
            ).candles
            if len(d1) < 10:
                continue

            closes = [q2f(c.close) for c in d1]
            highs = [q2f(c.high) for c in d1]
            lows = [q2f(c.low) for c in d1]
            vols = [c.volume for c in d1]

            close = closes[-1]
            last = min(20, len(d1))
            liq_val = (
                (sum([vols[-i] * closes[-i] for i in range(1, last + 1)]) / last)
                if last > 0
                else 0.0
            )

            n = min(14, len(d1) - 1)
            atr = (
                (sum([highs[-i] - lows[-i] for i in range(1, n + 1)]) / n)
                if n > 0
                else 0.0
            )
            atr_pct = (atr / close * 100.0) if close else 0.0

            if len(highs) >= 20:
                hi20 = max(highs[-20:])
                lo20 = min(lows[-20:])
            else:
                hi20 = max(highs)
                lo20 = min(lows)

            pre.append((liq_val, it, close, atr_pct, hi20, lo20))

        # ранжирование по ликвидности
        pre.sort(key=lambda x: -x[0])
        pre_all = pre[:150]  # общий топ
        pre_detailed = pre_all[:TOP_DETAILED]  # глубокие расчёты
        pre_light = pre_all[TOP_DETAILED:]  # упрощённые

        # --- ЭТАП B1: детальная обработка TOP_DETAILED (D1 полный + H1->H4 + стакан) ---
        for liq_val, it, close_A, atrpct_A, hi20_A, lo20_A in pre_detailed:
            figi = it["figi"]
            ticker = it["ticker"]
            klass = it["class"]

            # D1 расширенно
            d1 = safe_call(
                md.get_candles,
                figi=figi,
                from_=B_d1_from,
                to=now,
                interval=CandleInterval.CANDLE_INTERVAL_DAY,
            ).candles
            if len(d1) < 60:
                continue

            closes = [q2f(c.close) for c in d1]
            highs = [q2f(c.high) for c in d1]
            lows = [q2f(c.low) for c in d1]
            close = closes[-1]

            n = min(14, len(d1) - 1)
            atr = (
                (sum([highs[-i] - lows[-i] for i in range(1, n + 1)]) / n)
                if n > 0
                else 0.0
            )
            atr_pct = (atr / close * 100.0) if close else atrpct_A

            ema50 = (
                ema(closes[-60:], 50)
                if len(closes) >= 60
                else ema(closes, min(50, len(closes)))
            )
            ema200 = (
                ema(closes, 200)
                if len(closes) >= 200
                else ema(closes, min(200, len(closes)))
            )
            if ema50 and ema200:
                trend = "Up" if ema50 > ema200 else "Down" if ema50 < ema200 else "Side"
            else:
                trend = "Side"

            hi20 = max(highs[-20:]) if len(highs) >= 20 else hi20_A
            lo20 = min(lows[-20:]) if len(lows) >= 20 else lo20_A
            zone = zone_by_price(close, hi20, lo20, atr or 0.0)

            # H1 -> H4 -> RSI
            h1 = safe_call(
                md.get_candles,
                figi=figi,
                from_=B_h1_from,
                to=now,
                interval=CandleInterval.CANDLE_INTERVAL_HOUR,
            ).candles
            if len(h1) >= 80:
                h1cl = [q2f(c.close) for c in h1]
                h4 = [sum(h1cl[i : i + 4]) / 4 for i in range(0, len(h1cl) - 3, 4)]
                rsi_h4 = rsi(h4, 14)
            else:
                rsi_h4 = None

            # стакан depth=1
            try:
                ob = safe_call(md.get_order_book, figi=figi, depth=1)
                bid = q2f(ob.bids[0].price) if ob.bids else None
                ask = q2f(ob.asks[0].price) if ob.asks else None
                if bid and ask and bid > 0 and ask > 0:
                    mid = (bid + ask) / 2
                    spread_pct = (ask - bid) / mid * 100.0
                    spread_bps = (ask - bid) / mid * 10000.0
                else:
                    spread_pct, spread_bps = 0.0, 0.0
            except RequestError:
                spread_pct, spread_bps = 0.0, 0.0

            comm_bps = all_in_commission_bps(ticker, klass, costs_cfg)
            all_in_bps = comm_bps + spread_bps

            move_pct = planned_move_pct(klass, close, atr_pct)
            cost_r = cost_over_R(all_in_bps, move_pct)

            if near_event(ticker, now, events, win=2):
                continue
            if cost_r > 0.20:
                continue

            out_rows.append(
                {
                    "Ticker": ticker,
                    "Class": klass,
                    "Liquidity (20d turnover)": f"{liq_val:.0f}",
                    "Spread (%)": f"{spread_pct:.3f}",
                    "All-in cost (bps)": f"{all_in_bps:.1f}",
                    "ATR% (D1)": f"{atr_pct:.2f}",
                    "Trend (D1)": trend,
                    "Zone": zone,
                    "RSI (H4)": f"{(rsi_h4 if rsi_h4 is not None else 0):.1f}",
                    "Scenario": "trend-break/retest"
                    if trend != "Side"
                    else "range-play",
                    "Cost/R": f"{cost_r:.3f}",
                    "Verdict": "PASS",
                }
            )

        # --- ЭТАП B2: лёгкая обработка остальных (без H1 и без стакана) ---
        for liq_val, it, close_A, atrpct_A, hi20_A, lo20_A in pre_light:
            figi = it["figi"]
            ticker = it["ticker"]
            klass = it["class"]

            d1 = safe_call(
                md.get_candles,
                figi=figi,
                from_=B_d1_from,
                to=now,
                interval=CandleInterval.CANDLE_INTERVAL_DAY,
            ).candles
            if len(d1) < 60:
                continue

            closes = [q2f(c.close) for c in d1]
            highs = [q2f(c.high) for c in d1]
            lows = [q2f(c.low) for c in d1]
            close = closes[-1]

            n = min(14, len(d1) - 1)
            atr = (
                (sum([highs[-i] - lows[-i] for i in range(1, n + 1)]) / n)
                if n > 0
                else 0.0
            )
            atr_pct = (atr / close * 100.0) if close else atrpct_A

            ema50 = (
                ema(closes[-60:], 50)
                if len(closes) >= 60
                else ema(closes, min(50, len(closes)))
            )
            ema200 = (
                ema(closes, 200)
                if len(closes) >= 200
                else ema(closes, min(200, len(closes)))
            )
            if ema50 and ema200:
                trend = "Up" if ema50 > ema200 else "Down" if ema50 < ema200 else "Side"
            else:
                trend = "Side"

            hi20 = max(highs[-20:]) if len(highs) >= 20 else hi20_A
            lo20 = min(lows[-20:]) if len(lows) >= 20 else lo20_A
            zone = zone_by_price(close, hi20, lo20, atr or 0.0)

            # без стакана и без RSI(H4)
            spread_pct = 0.0
            spread_bps = 0.0

            comm_bps = all_in_commission_bps(ticker, klass, costs_cfg)
            all_in_bps = comm_bps + spread_bps

            move_pct = planned_move_pct(klass, close, atr_pct)
            cost_r = cost_over_R(all_in_bps, move_pct)

            if near_event(ticker, now, events, win=2):
                continue
            if cost_r > 0.20:
                continue

            out_rows.append(
                {
                    "Ticker": ticker,
                    "Class": klass,
                    "Liquidity (20d turnover)": f"{liq_val:.0f}",
                    "Spread (%)": f"{spread_pct:.3f}",
                    "All-in cost (bps)": f"{all_in_bps:.1f}",
                    "ATR% (D1)": f"{atr_pct:.2f}",
                    "Trend (D1)": trend,
                    "Zone": zone,
                    "RSI (H4)": f"{0.0:.1f}",
                    "Scenario": "trend-break/retest"
                    if trend != "Side"
                    else "range-play",
                    "Cost/R": f"{cost_r:.3f}",
                    "Verdict": "PASS",
                }
            )

    # сортировка: по ликвидности (desc), затем по Cost/R (asc)
    out_rows.sort(
        key=lambda x: (-float(x["Liquidity (20d turnover)"]), float(x["Cost/R"]))
    )

    os.makedirs("out", exist_ok=True)
    fields = [
        "Ticker",
        "Class",
        "Liquidity (20d turnover)",
        "Spread (%)",
        "All-in cost (bps)",
        "ATR% (D1)",
        "Trend (D1)",
        "Zone",
        "RSI (H4)",
        "Scenario",
        "Cost/R",
        "Verdict",
    ]
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in out_rows:
            w.writerow(row)

    print(f"Live candidates: {len(out_rows)} → {OUT_CSV}")


if __name__ == "__main__":
    main()
