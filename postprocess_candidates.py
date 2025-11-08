#!/usr/bin/env python3
import os, csv, re, sys, math, datetime as dt
from pathlib import Path
from typing import Dict, Tuple, List, Optional

from dotenv import load_dotenv
from tinkoff.invest import Client, InstrumentIdType, StopOrderDirection  # noqa

BASE = Path(__file__).resolve().parent
OUT_DIR = BASE / "out"

# --- настройки ---
ENTRY_DEV_MAX_PCT = float(os.getenv("ENTRY_DEVIATION_MAX_PCT", "0.5"))  # %, по модулю
INPUTS = [
    OUT_DIR / "live_candidates_public.csv",
    OUT_DIR / "live_candidates_ki.csv",
]

# --- утилиты парсинга ---
LEVELS_RE = re.compile(
    r"entry\s+([0-9]+(?:\.[0-9]+)?)\s*/\s*stop\s+([0-9]+(?:\.[0-9]+)?)\s*/\s*target\s+([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)

def parse_levels(levels: str) -> Optional[Tuple[float, float, float]]:
    if not levels:
        return None
    m = LEVELS_RE.search(levels)
    if not m:
        return None
    e, s, t = map(float, m.groups())
    return e, s, t

def q2f(q):
    # безопасный перевод Quotation -> float
    if q is None:
        return None
    return float(getattr(q, "units", 0) + getattr(q, "nano", 0)/1e9)

def safe_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default

# --- загрузка позиций ---
def load_open_position_figis(cli, account_id: str) -> set:
    open_figis = set()
    try:
        pos = cli.operations.get_positions(account_id=account_id)
        # акции/облигации
        for p in getattr(pos, "securities", []) or []:
            bal = safe_float(getattr(p, "balance", 0), 0)
            if bal and bal > 0 and getattr(p, "figi", ""):
                open_figis.add(p.figi)
        # фьючерсы
        for f in getattr(pos, "futures", []) or []:
            bal = safe_float(getattr(f, "balance", 0), 0)
            if bal and bal > 0 and getattr(f, "figi", ""):
                open_figis.add(f.figi)
    except Exception as e:
        print(f"[WARN] load_open_position_figis: {e}", file=sys.stderr)
    return open_figis

# --- резолв тикер+класс -> (figi, name) ---
def resolve_instrument(cli, ticker: str, class_code: str) -> Optional[Tuple[str, str]]:
    try:
        ins = cli.instruments.get_instrument_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
            class_code=class_code,
            id=ticker
        ).instrument
        figi = getattr(ins, "figi", "") or ""
        name = getattr(ins, "name", "") or ticker
        if figi:
            return figi, name
    except Exception as e:
        # не свалимся, просто пропустим name/figi
        print(f"[RESOLVE] {ticker}/{class_code}: {e}", file=sys.stderr)
    return None

# --- вытягиваем последние цены пачкой ---
def fetch_last_prices(cli, figis: List[str]) -> Dict[str, float]:
    out = {}
    # API умеет пачкой по figi
    if not figis:
        return out
    try:
        lp = cli.market_data.get_last_prices(figi=figis).last_prices
        for item in lp:
            p = q2f(item.price)
            if p is not None:
                out[item.figi] = p
    except Exception as e:
        print(f"[WARN] fetch_last_prices: {e}", file=sys.stderr)
    return out

def process_file(cli, account_id: str, path: Path):
    if not path.exists():
        return
    rows = []
    with path.open(newline="", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        src_rows = list(rdr)

    # 1) резолвим FIGI/Name
    figi_map: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for r in src_rows:
        t = (r.get("ticker","").strip(), r.get("class","").strip())
        if not t[0] or not t[1]:
            continue
        if t not in figi_map:
            res = resolve_instrument(cli, t[0], t[1])
            if res:
                figi_map[t] = res

    # 2) загрузим открытые позиции
    open_figis = load_open_position_figis(cli, account_id)

    # 3) последние цены по всем найденным figi
    figis = [v[0] for v in figi_map.values()]
    last_by_figi = fetch_last_prices(cli, figis)

    # 4) соберём обогащённые строки
    for r in src_rows:
        pass_flag = (r.get("pass","").strip().upper() == "YES")
        ticker = r.get("ticker","").strip()
        class_code = r.get("class","").strip()
        levels = r.get("levels","")
        parsed = parse_levels(levels)
        name = ""
        figi = ""
        if (ticker, class_code) in figi_map:
            figi, name = figi_map[(ticker, class_code)]
        else:
            name = ticker  # резервное имя

        entry, stop, target = (None, None, None)
        if parsed:
            entry, stop, target = parsed

        last = last_by_figi.get(figi, None)
        dev_pct = None
        if last and entry:
            dev_pct = abs(last/entry - 1.0) * 100.0  # в %

        # Verdict:
        # - если уже в позиции → HOLD
        # - если pass==YES и dev<=порог → PASS
        # - иначе → SKIP
        verdict = "SKIP"
        if figi and figi in open_figis:
            verdict = "HOLD"
        elif pass_flag and entry and (dev_pct is None or dev_pct <= ENTRY_DEV_MAX_PCT):
            verdict = "PASS"

        # отбрасываем слишком ушедшие от входа строки:
        if pass_flag and dev_pct is not None and dev_pct > ENTRY_DEV_MAX_PCT:
            # слишком далеко от входа — точка ушла
            continue

        rows.append({
            **r,
            "Name": name,
            "FIGI": figi,
            "Last": f"{last:.6f}" if last else "",
            "DeviationPct": f"{dev_pct:.3f}" if dev_pct is not None else "",
            "Verdict": verdict,
        })

    # 5) запишем .enriched.csv
    out_path = path.with_suffix(".enriched.csv")
    if rows:
        cols = list(rows[0].keys())
        with out_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        print(f"[OK] {path.name} → {out_path.name} ({len(rows)} rows)")
    else:
        # создадим пустой файл с заголовком
        header = ["ts","ticker","class","trend_d1","zone","rsi_h4","scenario","recommendation","levels","all_in_bps","cost_r","pass","Name","FIGI","Last","DeviationPct","Verdict"]
        with out_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
        print(f"[OK] {path.name} → {out_path.name} (0 rows)")

def main():
    load_dotenv(BASE/".env")
    token = os.getenv("TINKOFF_TOKEN") or os.getenv("TINKOFF_INVEST_TOKEN")
    account_id = os.getenv("TINKOFF_ACCOUNT_ID","")
    if not token:
        print("ERR: no TINKOFF token in .env", file=sys.stderr)
        sys.exit(1)

    with Client(token) as cli:
        for p in INPUTS:
            process_file(cli, account_id, p)

if __name__ == "__main__":
    main()
