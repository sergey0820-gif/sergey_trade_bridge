import os, csv
from dotenv import load_dotenv
from tinkoff.invest import Client, InstrumentStatus

load_dotenv()
TOKEN = os.getenv("TINKOFF_INVEST_TOKEN")
OUTDIR = "data"; os.makedirs(OUTDIR, exist_ok=True)

def write_csv(path, rows):
    if not rows: return
    keys = rows[0].keys()
    with open(path,"w",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys); w.writeheader()
        for r in rows: w.writerow(r)

with Client(TOKEN) as c:
    sh = c.instruments.shares(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE).instruments
    shares_ru = [s for s in sh if (s.country_of_risk=="RU" or s.country_of_risk_name=="Россия") and s.api_trade_available_flag]
    futs = c.instruments.futures().instruments

rows=[]
for s in shares_ru:
    mpi = (s.min_price_increment.units + s.min_price_increment.nano/1e9) if s.min_price_increment else None
    rows.append(dict(figi=s.figi, ticker=s.ticker, class_="stock", name=s.name, exch=s.exchange,
                     lot=s.lot, mpi=mpi, currency=s.currency, is_traded=s.api_trade_available_flag))
for f in futs:
    mpi = (f.min_price_increment.units + f.min_price_increment.nano/1e9) if f.min_price_increment else None
    rows.append(dict(figi=f.figi, ticker=f.ticker, class_="futures", name=f.name, exch=f.exchange,
                     lot=(f.basic_asset_size or f.lot), mpi=mpi, currency=f.currency, is_traded=True))

write_csv(os.path.join(OUTDIR,"universe.csv"), rows)
print(f"Universe saved: {len(rows)} → data/universe.csv")
