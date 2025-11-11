import os, csv, json, datetime as dt
from dotenv import load_dotenv
from tinkoff.invest import Client, CandleInterval

load_dotenv()
TOKEN = os.getenv("TINKOFF_INVEST_TOKEN")
UNIVERSE = "data/universe.csv"
OUTCSV = "out/morning_candidates.csv"


def load_costs():
    try:
        with open("data/costs.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "default": {"stock_allin_bps": 8, "futures_allin_bps": 4},
            "overrides": {},
        }


COSTCFG = load_costs()


def all_in_bps(ticker, klass):
    over = COSTCFG.get("overrides", {}).get(ticker)
    if isinstance(over, (int, float)):
        return float(over)
    dflt = COSTCFG["default"]
    return float(
        dflt["stock_allin_bps"] if klass == "stock" else dflt["futures_allin_bps"]
    )


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
    gains = []
    losses = []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        (gains if d > 0 else losses).append(abs(d))
    ag = sum(gains[-period:]) / period or 1e-9
    al = sum(losses[-period:]) / period or 1e-9
    rs = ag / al
    return 100 - 100 / (1 + rs)


def cost_over_R(cost_bps, move_pct):
    return (cost_bps / 100.0) / move_pct if move_pct > 0 else 999.0


def planned_move_pct(klass, close, atr_pct):
    if klass == "stock":
        return min(max(3.0, atr_pct), 7.0)
    return max(atr_pct, 1.5)


def q2f(q):
    return q.units + q.nano / 1e9


def candles_to_lists(candles):
    closes = [q2f(c.close) for c in candles]
    highs = [q2f(c.high) for c in candles]
    lows = [q2f(c.low) for c in candles]
    return closes, highs, lows


rows = []
with open(UNIVERSE, "r", encoding="utf-8") as f:
    r = csv.DictReader(f)
    for x in r:
        if x.get("is_traded") in ("True", "true", "1", True):
            rows.append(x)

now = dt.datetime.now(dt.timezone.utc)
d1_from = now - dt.timedelta(days=240)
h1_from = now - dt.timedelta(days=20)

candidates = []
with Client(TOKEN) as c:
    md = c.market_data
    for inst in rows:
        klass = inst["class_"]
        if klass not in ("stock", "futures"):
            continue

        d1 = md.get_candles(
            figi=inst["figi"],
            from_=d1_from,
            to=now,
            interval=CandleInterval.CANDLE_INTERVAL_DAY,
        ).candles
        if len(d1) < 60:
            continue
        closes, highs, lows = candles_to_lists(d1)
        close = closes[-1]
        atr = sum([highs[-i] - lows[-i] for i in range(1, min(15, len(highs)))]) / max(
            1, min(14, len(highs) - 1)
        )
        ema50 = ema(closes[-60:], 50)
        ema200 = (
            ema(closes, 200)
            if len(closes) >= 200
            else ema(closes, min(200, len(closes)))
        )
        trend = (
            "Up"
            if (ema50 and ema200 and ema50 > ema200)
            else ("Down" if (ema50 and ema200 and ema50 < ema200) else "Side")
        )

        h1 = md.get_candles(
            figi=inst["figi"],
            from_=h1_from,
            to=now,
            interval=CandleInterval.CANDLE_INTERVAL_HOUR,
        ).candles
        if len(h1) < 80:
            continue
        h1_closes = [q2f(c.close) for c in h1]
        # H4 через усреднение блоков по 4 часа
        h4 = [sum(h1_closes[i : i + 4]) / 4 for i in range(0, len(h1_closes) - 3, 4)]
        rsi_h4 = rsi(h4, 14)

        atr_pct = (atr / close * 100.0) if close else 0.0
        move = planned_move_pct(klass, close, atr_pct)
        cost_bps = all_in_bps(inst["ticker"], klass)
        c_r = cost_over_R(cost_bps, move)
        if c_r > 0.20:
            continue

        candidates.append(
            {
                "ticker": inst["ticker"],
                "class": klass,
                "trend_d1": trend,
                "rsi_h4": round(rsi_h4, 1) if rsi_h4 else None,
                "close": round(close, 6),
                "atr_pct": round(atr_pct, 2),
                "all_in_bps": cost_bps,
                "planned_move_pct": round(move, 2),
                "cost_r": round(c_r, 3),
            }
        )

candidates.sort(key=lambda x: (x["cost_r"], -x["planned_move_pct"]))

os.makedirs("out", exist_ok=True)
with open(OUTCSV, "w", newline="", encoding="utf-8") as f:
    fields = list(candidates[0].keys()) if candidates else ["ticker", "class"]
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    for row in candidates:
        w.writerow(row)

print(f"Candidates: {len(candidates)} → {OUTCSV}")
