import os
import csv
import time
import datetime as dt
import traceback
from tinkoff.invest import Client, CandleInterval
from tinkoff.invest.exceptions import InvestError
from dotenv import load_dotenv
from utils.ta import trend_zone_d1, rsi_from_candles

# Загрузка переменных среды
load_dotenv()
TOKEN = os.getenv("TINKOFF_TOKEN")

# Настройки
MAX_UNIVERSE = 300
BROKER_RT_BPS = 20
PUBLIC_CSV = open("live_candidates_public.csv", "w")
KI_CSV = open("live_candidates_ki.csv", "w")


# Упрощённая структура инструмента
class Inst:
    def __init__(self, ticker, class_code, figi, lot, for_ki):
        self.ticker = ticker
        self.class_code = class_code
        self.figi = figi
        self.lot = lot
        self.for_ki = for_ki


# Логирование
log_path = "logs/scan.log"


def log(msg):
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path, "a") as f:
        f.write(f"{ts} {msg}\n")
    print(f"{ts} {msg}")


# Безопасный вызов API
def safe_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except InvestError as e:
        log(f"InvestError: {e}")
        raise


# Получение последней цены
def last_price(cli: Client, figi: str):
    resp = safe_call(cli.market_data.get_last_prices, figi=[figi])
    prices = resp.last_prices
    return float(prices[0].price.units) + prices[0].price.nano / 1e9 if prices else None


# Простой сценарий


def scenario_and_levels(trend, rsi_h4, price):
    if trend == "up" and rsi_h4 and rsi_h4 < 45:
        return (
            "Pullback → long",
            f"entry {price:.2f} / stop {price * 0.98:.2f} / target {price * 1.05:.2f}",
        )
    if trend == "down" and rsi_h4 and rsi_h4 > 55:
        return (
            "Pullback → short",
            f"entry {price:.2f} / stop {price * 1.02:.2f} / target {price * 0.95:.2f}",
        )
    return (
        "Pin off support → long",
        f"entry {price:.2f} / stop {price * 0.98:.2f} / target {price * 1.05:.2f}",
    )


# Отбор инструментов


def pick_universe(cli: Client):
    res = []
    shares = safe_call(cli.instruments.shares).instruments
    for s in shares:
        if s.exchange != "MOEX":
            continue
        res.append(
            Inst(
                s.ticker,
                s.class_code or "TQBR",
                s.figi,
                int(s.lot or 1),
                bool(s.for_qual_investor_flag),
            )
        )

    futs = safe_call(cli.instruments.futures).instruments
    for f in futs:
        if f.basic_asset_size and f.basic_asset:
            res.append(
                Inst(
                    f.ticker,
                    f.class_code or "SPBFUT",
                    f.figi,
                    int(f.lot or 1),
                    bool(f.for_qual_investor_flag),
                )
            )

    # фильтруем по наличию last_price
    scored = []
    for i in res:
        try:
            p = last_price(cli, i.figi)
            if p:
                scored.append((p, i))
        except Exception:
            continue
        if len(scored) >= MAX_UNIVERSE:
            break

    return [i for _, i in scored]


# Основной цикл


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", default=None, help="например 30m")
    args = ap.parse_args()

    if not TOKEN:
        print("ERR: TINKOFF_TOKEN missing in .env")
        return 2

    while True:
        try:
            with Client(TOKEN) as cli:
                universe = pick_universe(cli)
                pub_rows, ki_rows = [], []

                for inst in universe:
                    try:
                        price = last_price(cli, inst.figi)
                        if not price:
                            continue

                        now = dt.datetime.now(dt.timezone.utc)
                        d1 = safe_call(
                            cli.market_data.get_candles,
                            figi=inst.figi,
                            from_=now - dt.timedelta(days=250),
                            to=now,
                            interval=CandleInterval.CANDLE_INTERVAL_DAY,
                        ).candles

                        h4 = safe_call(
                            cli.market_data.get_candles,
                            figi=inst.figi,
                            from_=now - dt.timedelta(days=10),
                            to=now,
                            interval=CandleInterval.CANDLE_INTERVAL_HOUR,
                        ).candles

                        h4 = h4[-24:] if len(h4) > 24 else h4
                        rsi = rsi_from_candles(h4)
                        trend, zone = trend_zone_d1(d1)
                        scenario, levels = scenario_and_levels(trend, rsi, price)

                        spread_pct = 0.0005
                        all_in_bps = BROKER_RT_BPS + int(spread_pct * 10000)
                        planned = 0.05 if "long" in scenario.lower() else 0.05
                        cost_r = all_in_bps / 10000.0 / planned
                        verdict = "YES" if cost_r <= 0.20 else "NO"

                        row = {
                            "ts": dt.datetime.utcnow().isoformat(),
                            "ticker": inst.ticker,
                            "class": inst.class_code,
                            "trend_d1": trend,
                            "zone": zone,
                            "rsi_h4": round(rsi, 1) if rsi is not None else "",
                            "scenario": scenario,
                            "recommendation": "watch long"
                            if "long" in scenario
                            else "watch short",
                            "levels": levels,
                            "all_in_bps": all_in_bps,
                            "cost_r": round(cost_r, 3),
                            "pass": verdict,
                        }

                        if inst.for_ki:
                            ki_rows.append(row)
                        else:
                            pub_rows.append(row)

                    except Exception as e:
                        log(f"[{inst.ticker}] skip due error: {e}")

                # Запись CSV
                def write_csv(filename, rows):
                    with open(filename, "w", newline="", encoding="utf-8") as f:
                        w = csv.DictWriter(
                            f, fieldnames=list(rows[0].keys()) if rows else ["ts"]
                        )
                        w.writeheader()
                        w.writerows(rows)

                write_csv("live_candidates_public.csv", pub_rows)
                write_csv("live_candidates_ki.csv", ki_rows)

                log(
                    f"Sergey-Trade: public={len(pub_rows)} | ki={len(ki_rows)} → live_candidates_public.csv, live_candidates_ki.csv"
                )

        except Exception as e:
            log(f"FATAL: {e}\n{traceback.format_exc()}")

        if not args.loop:
            break

        n = int(args.loop[:-1])
        unit = args.loop[-1].lower()
        sleep_s = n * 60 if unit == "m" else n * 3600
        time.sleep(sleep_s)


if __name__ == "__main__":
    main()
