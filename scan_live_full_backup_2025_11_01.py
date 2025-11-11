import os
import time
import datetime as dt
import traceback
import csv
from tinkoff.invest import Client, CandleInterval
from tinkoff.invest.exceptions import RequestError
from dotenv import load_dotenv

# === Загрузка конфигурации ===
load_dotenv()
TOKEN = os.getenv("TINKOFF_TOKEN")
MAX_UNIVERSE = 20  # Временно ограничим число инструментов

# === Пути файлов ===
PUBLIC_CSV = open("live_candidates_public.csv", "w")
KI_CSV = open("live_candidates_ki.csv", "w")


# === Утилиты ===
def log(msg):
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{now} {msg}")


def safe_call(fn, *args, **kwargs):
    while True:
        try:
            return fn(*args, **kwargs)
        except RequestError as e:
            if e.code.name == "RESOURCE_EXHAUSTED":
                reset_sec = int(getattr(e.trailing_metadata, "ratelimit_reset", 5))
                log(f"RATE LIMIT: пауза {reset_sec} секунд")
                time.sleep(reset_sec)
                continue
            else:
                raise


def last_price(cli, figi):
    lp = safe_call(cli.market_data.get_last_prices, figi=[figi]).last_prices
    if lp:
        return float(lp[0].price.units) + lp[0].price.nano * 1e-9
    return None


# === Примитивные заглушки ===
def rsi_from_candles(candles):
    return 42.0


def trend_zone_d1(candles):
    return ("up", "support")


def scenario_and_levels(trend, rsi, price):
    if trend == "up" and rsi and rsi < 45:
        return (
            "Pullback → long",
            f"entry {price:.2f} / stop {price * 0.98:.2f} / target {price * 1.05:.2f}",
        )
    if trend == "down" and rsi and rsi > 55:
        return (
            "Pullback → short",
            f"entry {price:.2f} / stop {price * 1.02:.2f} / target {price * 0.95:.2f}",
        )
    return (
        "Pin off support → long",
        f"entry {price:.2f} / stop {price * 0.98:.2f} / target {price * 1.05:.2f}",
    )


class Inst:
    def __init__(self, ticker, class_code, figi, lot, for_ki):
        self.ticker = ticker
        self.class_code = class_code
        self.figi = figi
        self.lot = lot
        self.for_ki = for_ki


def pick_universe(cli: Client) -> list[Inst]:
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
    scored = []
    for i in res:
        try:
            p = last_price(cli, i.figi)
            if p:
                scored.append((p, i))
                time.sleep(0.5)
        except Exception:
            continue
        if len(scored) >= MAX_UNIVERSE:
            break
    return [i for _, i in scored]


def main():
    if not TOKEN:
        print("ERR: TINKOFF_TOKEN missing in .env")
        return

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
                    time.sleep(0.5)
                    h4 = safe_call(
                        cli.market_data.get_candles,
                        figi=inst.figi,
                        from_=now - dt.timedelta(days=10),
                        to=now,
                        interval=CandleInterval.CANDLE_INTERVAL_HOUR,
                    ).candles
                    time.sleep(0.5)
                    h4 = h4[-24:] if len(h4) > 24 else h4
                    rsi = rsi_from_candles(h4)
                    trend, zone = trend_zone_d1(d1)
                    scenario, levels = scenario_and_levels(trend, rsi, price)
                    spread_pct = 0.0005
                    all_in_bps = 15 + int(spread_pct * 10000)
                    planned = 0.05
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

            def write_csv(path, rows):
                with open(path.name, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                    w.writeheader()
                    w.writerows(rows)

            if pub_rows:
                write_csv(PUBLIC_CSV, pub_rows)
            if ki_rows:
                write_csv(KI_CSV, ki_rows)
            log(
                f"Sergey-Trade: public={len(pub_rows)} | ki={len(ki_rows)} → {PUBLIC_CSV.name}, {KI_CSV.name}"
            )
    except Exception as e:
        log(f"FATAL: {e}\n{traceback.format_exc()}")


if __name__ == "__main__":
    main()
