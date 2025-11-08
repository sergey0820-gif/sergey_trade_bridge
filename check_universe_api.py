import os, csv, json, sys
from pathlib import Path
from dotenv import load_dotenv
from tinkoff.invest import Client, InstrumentStatus, SecurityTradingStatus

BASE = Path(__file__).resolve().parent
OUT  = BASE / "out"
OUT.mkdir(exist_ok=True, parents=True)

load_dotenv(BASE / ".env", override=True)
TOKEN = os.getenv("TINKOFF_TOKEN")
if not TOKEN:
    print("❌ Нет TINKOFF_TOKEN в окружении"); sys.exit(1)

def normal(tr_status):
    return tr_status == SecurityTradingStatus.SECURITY_TRADING_STATUS_NORMAL_TRADING

def main():
    summary = {
        "shares": {"total":0, "api_ok":0, "buy_ok":0, "sell_ok":0, "normal":0},
        "futures":{"total":0, "api_ok":0, "normal":0}
    }
    rows = []  # для CSV

    with Client(TOKEN) as c:
        shares  = c.instruments.shares(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE).instruments
        futures = c.instruments.futures(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE).instruments

        # --- акции ---
        summary["shares"]["total"] = len(shares)
        for s in shares:
            api_ok  = bool(getattr(s, "api_trade_available_flag", False))
            buy_ok  = bool(getattr(s, "buy_available_flag", False))
            sell_ok = bool(getattr(s, "sell_available_flag", False))
            norm    = normal(getattr(s, "trading_status", 0))

            if api_ok: summary["shares"]["api_ok"]+=1
            if buy_ok: summary["shares"]["buy_ok"]+=1
            if sell_ok: summary["shares"]["sell_ok"]+=1
            if norm: summary["shares"]["normal"]+=1

            rows.append({
                "type":"share",
                "ticker":s.ticker, "figi":s.figi, "class_code":s.class_code,
                "api_ok":api_ok, "buy_ok":buy_ok, "sell_ok":sell_ok, "normal":norm,
                "lot":getattr(s, "lot", 1)
            })

        # --- фьючерсы ---
        summary["futures"]["total"] = len(futures)
        for f in futures:
            api_ok = bool(getattr(f, "api_trade_available_flag", False))
            norm   = normal(getattr(f, "trading_status", 0))

            if api_ok: summary["futures"]["api_ok"]+=1
            if norm: summary["futures"]["normal"]+=1

            rows.append({
                "type":"future",
                "ticker":f.ticker, "figi":f.figi, "class_code":f.class_code,
                "api_ok":api_ok, "buy_ok":"-", "sell_ok":"-", "normal":norm,
                "lot":getattr(f, "lot", 1)
            })

    # --- вывод и файлы ---
    print("=== SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    csv_path = OUT / "api_universe.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["type","ticker","figi","class_code","api_ok","buy_ok","sell_ok","normal","lot"])
        w.writeheader()
        w.writerows(rows)
    print(f"CSV → {csv_path}")

    sum_path = OUT / "api_universe_summary.json"
    with open(sum_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"JSON → {sum_path}")

if __name__ == "__main__":
    main()
