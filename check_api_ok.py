import os, csv, sys
from dotenv import load_dotenv
from tinkoff.invest import Client, InstrumentStatus, SecurityTradingStatus

BASE_DIR = os.path.dirname(__file__)
load_dotenv(os.path.join(BASE_DIR, ".env"))

token = os.getenv("TINKOFF_TOKEN")
acc_id = os.getenv("TINKOFF_ACCOUNT_ID")
live_csv = os.path.join(BASE_DIR, "out", "live_candidates.csv")

def side_ok(instr_class: str, direction: str) -> bool:
    # Шорт только фьючерсы
    if direction.lower()=="short" and instr_class=="stock":
        return False
    return True

def print_ok(ticker, clazz, direction, levels):
    print(f"{ticker:8} {clazz:7} {direction:5} | {levels}")

if not token:
    print("ERR: TINKOFF_TOKEN is empty (check .env)")
    sys.exit(1)

with Client(token) as c:
    shares = {s.ticker: s for s in c.instruments.shares(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE).instruments}
    futures= {f.ticker: f for f in c.instruments.futures(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE).instruments}

    ok_list=[]
    with open(live_csv, newline="", encoding="utf-8") as f:
        r=csv.DictReader(f)
        for row in r:
            if row.get("pass","").upper()!="YES":
                continue
            ticker=row["ticker"]
            clazz =row["class"]     # "stock" | "future"
            scen  =row["scenario"] or ""
            levels=row["levels"] or ""
            direction = "long" if "long" in scen.lower() else ("short" if "short" in scen.lower() else "long")

            if not side_ok(clazz, direction):
                continue

            instr = (shares.get(ticker) if clazz=="stock" else futures.get(ticker))
            if not instr:
                continue

            if not getattr(instr, "api_trade_available_flag", False):
                continue
            if clazz=="stock":
                if direction=="long" and not getattr(instr,"buy_available_flag",False):
                    continue
                if direction=="short" and not getattr(instr,"sell_available_flag",False):
                    continue

            if getattr(instr,"trading_status",0) not in (SecurityTradingStatus.SECURITY_TRADING_STATUS_NORMAL_TRADING,):
                continue

            ok_list.append((ticker,clazz,direction,levels))

    if not ok_list:
        print("EMPTY")
        sys.exit(0)

    for t in ok_list:
        print_ok(*t)
