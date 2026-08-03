#!/usr/bin/env python3
# positions_guard.py
# Удаляет из candidates_ai.csv сигналы по инструментам, которые уже есть в портфеле (open positions).
# Ничего не отправляет в Telegram, только фильтрует файл, который читает telegram_bridge.py.

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Set, Tuple

from tinkoff.invest import AsyncClient, InstrumentIdType  # tinkoff-investments v0.2.0b59

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / ".state"
LOG_DIR = BASE_DIR / "logs"
CAND_AI = BASE_DIR / "candidates_ai.csv"

STATE_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

log = logging.getLogger("positions_guard")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def env_str(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v.strip() if v is not None else default


def env_int(name: str, default: int) -> int:
    try:
        return int(env_str(name, str(default)))
    except Exception:
        return default


async def fetch_open_position_uids(token: str, account_id: str) -> Tuple[Set[str], Set[Tuple[str, str]]]:
    """
    Берём портфель и возвращаем (set instrument_uid, set (ticker, class_code))
    по всем позициям с qty != 0.

    candidates_ai.csv на практике всегда приходит с пустой колонкой uid (её
    никто не заполняет выше по пайплайну — ai_filter_agent.py её не считает),
    поэтому матчинг только по uid никогда ничего не находил. ticker+class_code
    матчинг — тот же принцип, что уже работает в trade_executor.has_open_position
    (там uid берётся свежим лукапом инструмента, а не из CSV).
    """
    uids: Set[str] = set()
    keys: Set[Tuple[str, str]] = set()
    async with AsyncClient(token) as client:
        pf = await client.operations.get_portfolio(account_id=account_id)

        # shares/bonds/etf/currencies/futures — берём всё
        for p in list(pf.positions):
            # quantity может быть Quotation; сравним через units/nano
            q = p.quantity
            units = getattr(q, "units", 0) or 0
            nano = getattr(q, "nano", 0) or 0
            if units == 0 and nano == 0:
                continue
            if p.instrument_uid:
                uids.add(p.instrument_uid)
                try:
                    ins = (
                        await client.instruments.get_instrument_by(
                            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
                            id=p.instrument_uid,
                        )
                    ).instrument
                    keys.add((ins.ticker, ins.class_code))
                except Exception:
                    log.warning("Не удалось разрешить instrument_uid=%s в ticker/class_code", p.instrument_uid)

    return uids, keys


def read_candidates_ai(path: Path) -> Tuple[List[Dict[str, str]], List[str]]:
    if not path.exists():
        return [], []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        cols = reader.fieldnames or []
    return rows, cols


def write_candidates_ai(path: Path, rows: List[Dict[str, str]], cols: List[str]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    tmp.replace(path)


def main() -> int:
    setup_logging()

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Не перезаписывать файл, только показать что удалим")
    args = ap.parse_args()

    token = env_str("TINKOFF_TOKEN") or env_str("TINKOFF_INVEST_TOKEN")
    account_id = env_str("TINKOFF_ACCOUNT_ID")
    if not token or not account_id:
        log.error("Нет TINKOFF_TOKEN/TINKOFF_INVEST_TOKEN или TINKOFF_ACCOUNT_ID в env")
        return 2

    rows, cols = read_candidates_ai(CAND_AI)
    if not rows:
        log.info("candidates_ai.csv пуст или отсутствует — нечего фильтровать")
        return 0

    if "uid" not in cols:
        log.error("В candidates_ai.csv нет колонки uid — не могу сопоставить с портфелем")
        return 3

    # Получаем позиции
    import asyncio
    open_uids, open_keys = asyncio.run(fetch_open_position_uids(token, account_id))

    # Сохраним для дебага
    (STATE_DIR / "open_positions_uids.json").write_text(
        json.dumps(
            {"uids": sorted(open_uids), "keys": sorted(f"{t}:{c}" for t, c in open_keys)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    before = len(rows)
    kept: List[Dict[str, str]] = []
    removed: List[Dict[str, str]] = []

    for r in rows:
        uid = (r.get("uid") or "").strip()
        key = ((r.get("ticker") or "").strip(), (r.get("class_code") or "").strip())
        if (uid and uid in open_uids) or (key[0] and key in open_keys):
            removed.append(r)
        else:
            kept.append(r)

    after = len(kept)
    if removed:
        log.info("Удаляем из candidates_ai.csv (в позиции уже есть): %s", ", ".join([x.get("ticker", "?") for x in removed]))
    log.info("Фильтр: было=%d, стало=%d, удалено=%d", before, after, len(removed))

    if args.dry_run:
        log.info("dry-run=1 — файл не перезаписан")
        return 0

    write_candidates_ai(CAND_AI, kept, cols)
    log.info("OK: candidates_ai.csv обновлён")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

