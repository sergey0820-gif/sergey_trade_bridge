#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Sheets bridge:
- push MORNING/LIVE candidates to sheet
- pull APPROVALS; on APPROVE -> create plan JSON and execute via trade_executor.py
- append result to EXECUTIONS
"""

import os, csv, json, time, subprocess, shlex
from datetime import datetime, timezone
from dotenv import load_dotenv

import gspread
from google.oauth2.service_account import Credentials

BASE_DIR = os.path.dirname(__file__)
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ---- ENV ----
TZ = os.getenv("TIMEZONE", "Europe/Moscow")
GS_ENABLED = os.getenv("GSHEETS_ENABLED", "0") == "1"
GS_CRED = os.getenv("GSHEETS_CRED_FILE", "")
GS_ID = os.getenv("GSHEETS_SPREADSHEET_ID", "")
WS_MORNING = os.getenv("GSHEETS_WORKSHEET_MORNING", "MORNING")
WS_LIVE = os.getenv("GSHEETS_WORKSHEET_LIVE", "LIVE")
WS_APPROVE = os.getenv("GSHEETS_WORKSHEET_APPROVALS", "APPROVALS")
WS_EXEC = os.getenv("GSHEETS_WORKSHEET_EXEC", "EXECUTIONS")

TRADING_ENABLED = os.getenv("TRADING_ENABLED", "0") == "1"
DRY_RUN = os.getenv("DRY_RUN", "1") == "1"
DEFAULT_RISK_PCT = float(os.getenv("RISK_PCT", "2.0"))

OUT_DIR = os.path.join(BASE_DIR, "out")
QUEUE_DIR = os.path.join(BASE_DIR, "orders", "queue")
EXEC_DIR = os.path.join(BASE_DIR, "orders", "executed")
REJ_DIR = os.path.join(BASE_DIR, "orders", "rejected")
os.makedirs(QUEUE_DIR, exist_ok=True)
os.makedirs(EXEC_DIR, exist_ok=True)
os.makedirs(REJ_DIR, exist_ok=True)

MORNING_CSV = os.path.join(OUT_DIR, "morning_candidates.csv")
LIVE_CSV = os.path.join(OUT_DIR, "live_candidates.csv")


def ts_utc():
    return datetime.now(timezone.utc).isoformat()


def open_or_create_worksheet(gc, sh, title, header):
    try:
        ws = sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=2000, cols=30)
        if header:
            ws.append_row(header, value_input_option="RAW")
    return ws


def gspread_client():
    if not (GS_ENABLED and GS_CRED and GS_ID):
        raise RuntimeError("GS not configured")
    creds = Credentials.from_service_account_file(
        GS_CRED,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GS_ID)
    return gc, sh


def csv_rows(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def push_table(ws, header, rows):
    # перезаписываем всю вкладку: заголовок + строки
    ws.clear()
    if header:
        ws.append_row(header, value_input_option="RAW")
    if rows:
        data = [[r.get(h, "") for h in header] for r in rows]
        ws.append_rows(data, value_input_option="RAW")


def ensure_headers(ws, header):
    # если пусто — добавить заголовок
    if ws.row_count == 0 or not ws.get_all_values():
        ws.append_row(header, value_input_option="RAW")


def make_plan_filename(ticker):
    return os.path.join(QUEUE_DIR, f"{ticker}_{int(time.time())}.json")


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def exec_plan(plan_path):
    """Запуск trade_executor.py и возврат (rc, stdout, stderr)"""
    cmd = f"{shlex.quote(os.path.join(BASE_DIR, '.venv', 'bin', 'python'))} {shlex.quote(os.path.join(BASE_DIR, 'trade_executor.py'))} --plan {shlex.quote(plan_path)}"
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def approvals_to_plans(rows):
    """Преобразуем строки APPROVALS со статусом APPROVE в планы"""
    plans = []
    for r in rows:
        status = (r.get("status") or "").strip().upper()
        if status != "APPROVE":
            continue
        try:
            plan = {
                "ts": ts_utc(),
                "ticker": (r.get("ticker") or "").strip(),
                "class": (r.get("class") or "").strip().lower(),  # 'stock'/'future'
                "direction": (r.get("direction") or "")
                .strip()
                .lower(),  # 'long'/'short'
                "entry": float(r.get("entry")),
                "stop": float(r.get("stop")),
                "target": float(r.get("target")),
                "qty": int(float(r.get("qty"))) if r.get("qty") else None,
                "risk_pct": float(r.get("risk_pct") or DEFAULT_RISK_PCT),
            }
            if not plan["ticker"] or not plan["direction"]:
                continue
            plans.append((r, plan))
        except Exception:
            continue
    return plans


def main():
    if not GS_ENABLED:
        print("[GS] disabled")
        return

    gc, sh = gspread_client()

    # 1) MORNING/LIVE → Sheet
    ws_morn = open_or_create_worksheet(
        gc,
        sh,
        WS_MORNING,
        [
            "ts",
            "ticker",
            "class",
            "trend_d1",
            "zone",
            "rsi_h4",
            "scenario",
            "recommendation",
            "levels",
            "all_in_bps",
            "cost_r",
            "pass",
        ],
    )
    ws_live = open_or_create_worksheet(
        gc,
        sh,
        WS_LIVE,
        [
            "ts",
            "ticker",
            "class",
            "trend_d1",
            "zone",
            "rsi_h4",
            "scenario",
            "recommendation",
            "levels",
            "all_in_bps",
            "cost_r",
            "pass",
        ],
    )
    push_table(
        ws_morn,
        [
            "ts",
            "ticker",
            "class",
            "trend_d1",
            "zone",
            "rsi_h4",
            "scenario",
            "recommendation",
            "levels",
            "all_in_bps",
            "cost_r",
            "pass",
        ],
        csv_rows(MORNING_CSV),
    )
    push_table(
        ws_live,
        [
            "ts",
            "ticker",
            "class",
            "trend_d1",
            "zone",
            "rsi_h4",
            "scenario",
            "recommendation",
            "levels",
            "all_in_bps",
            "cost_r",
            "pass",
        ],
        csv_rows(LIVE_CSV),
    )
    print(f"[GS] pushed MORNING/LIVE at {ts_utc()}")

    # 2) APPROVALS → исполнение
    ws_app = open_or_create_worksheet(
        gc,
        sh,
        WS_APPROVE,
        [
            "ts",
            "ticker",
            "class",
            "direction",
            "entry",
            "stop",
            "target",
            "qty",
            "risk_pct",
            "status",
            "note",
        ],
    )
    ensure_headers(
        ws_app,
        [
            "ts",
            "ticker",
            "class",
            "direction",
            "entry",
            "stop",
            "target",
            "qty",
            "risk_pct",
            "status",
            "note",
        ],
    )
    app_rows = ws_app.get_all_records()
    to_run = approvals_to_plans(app_rows)

    ws_exec = open_or_create_worksheet(
        gc,
        sh,
        WS_EXEC,
        [
            "ts",
            "ticker",
            "class",
            "direction",
            "entry",
            "stop",
            "target",
            "qty",
            "result",
            "details",
        ],
    )

    for src_row, plan in to_run:
        # создать JSON-план
        fname = make_plan_filename(plan["ticker"])
        write_json(fname, plan)

        if TRADING_ENABLED:
            rc, out, err = exec_plan(fname)
            if rc == 0:
                result, details = "OK", out
            else:
                result, details = "ERROR", (err or out)
        else:
            result, details = (
                "DRY_RUN",
                "Trading disabled (DRY_RUN or TRADING_ENABLED=0)",
            )

        # лог в EXECUTIONS
        ws_exec.append_row(
            [
                ts_utc(),
                plan["ticker"],
                plan["class"],
                plan["direction"],
                plan["entry"],
                plan["stop"],
                plan["target"],
                plan.get("qty") or "",
                result,
                details,
            ],
            value_input_option="RAW",
        )

        # пометить строку в APPROVALS как DONE
        try:
            # ищем индекс строки (грубый способ)
            all_vals = ws_app.get_all_values()
            # заголовок = 1 строка; реальные строки начинаются с 2
            for i in range(2, len(all_vals) + 1):
                row_vals = all_vals[i - 1]
                if len(row_vals) >= 2 and row_vals[1] == plan["ticker"]:
                    ws_app.update_cell(i, 10, "DONE")  # status -> DONE
                    break
        except Exception:
            pass

        print(f"[EXEC] {plan['ticker']} {plan['direction']} -> {result}")


if __name__ == "__main__":
    main()
