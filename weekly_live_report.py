#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
weekly_live_report.py

Лёгкий переиспользуемый отчёт по живой автостратегии — не дашборд, просто
markdown-текст. Запускать вручную раз в 1-2 недели (НЕ по крону).

Использование:
    python3 weekly_live_report.py                 # за последние 7 дней
    python3 weekly_live_report.py --days 14        # за последние 14 дней
    python3 weekly_live_report.py --out report.md  # сохранить в файл (и всё равно вывести в stdout)
    python3 weekly_live_report.py --push-sheets    # + записать сводку и полный текст в Google Sheets
    python3 weekly_live_report.py --push-sheets --notify-telegram  # + короткое уведомление в Telegram со ссылкой

Читает read-only: logs/signal_journal.csv, orders_log.csv,
client.operations.get_operations_by_cursor (реальный счёт через .env).
Ничего не пишет, ничего не размещает, никаких ордеров (кроме опциональной
записи отчёта в Google Sheets/уведомления в Telegram при явных флагах
--push-sheets/--notify-telegram — по умолчанию оба выключены).

Google Sheets (--push-sheets): переиспользует ту же инфраструктуру, что
sheet_bridge.py (GSHEETS_ENABLED/GSHEETS_CRED_FILE/GSHEETS_SPREADSHEET_ID
из .env, тот же service account). Две вкладки, обе кумулятивные (строка на
запуск, для сравнения по неделям), автосоздаются при первом запуске:
  WEEKLY_SUMMARY — структурные метрики (даты, воронка, ошибки, комиссии,
                   позиции) одной строкой на запуск.
  WEEKLY_FULL    — дата + полный markdown-текст отчёта одной ячейкой.

Telegram (--notify-telegram): короткое сообщение "отчёт готов" + ссылка на
таблицу — переиспользует TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID из .env (та
же пара, что telegram_bridge.py). Работает только вместе с --push-sheets
(нужна ссылка на реальную таблицу, не отправляем уведомление без неё).

Секция комиссионной экономики (п.3) считается ВСЕГДА с начала периода
текущей автостратегии (2026-08-01) — не зависит от --days, чтобы не
смешивать с историей ручной торговли до этой даты (см. STRATEGY.md
"Открытые вопросы" п.1-2).
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
SIGNAL_JOURNAL = LOGS_DIR / "signal_journal.csv"
ORDERS_LOG = BASE_DIR / "orders_log.csv"
DYNAMIC_STOP_EVENTS = LOGS_DIR / "dynamic_stop_events.csv"
ENV_PATH = BASE_DIR / ".env"

AUTO_STRATEGY_START = datetime(2026, 8, 1, tzinfo=timezone.utc)

load_dotenv(ENV_PATH if ENV_PATH.exists() else None)


def q_to_float(v) -> float:
    if v is None:
        return 0.0
    return float(v.units) + float(v.nano) / 1e9


def load_signal_journal() -> pd.DataFrame:
    if not SIGNAL_JOURNAL.exists():
        return pd.DataFrame()
    df = pd.read_csv(SIGNAL_JOURNAL, parse_dates=["ts"])
    if df["ts"].dt.tz is None:
        df["ts"] = df["ts"].dt.tz_localize("UTC")
    return df


def load_orders_log() -> pd.DataFrame:
    if not ORDERS_LOG.exists():
        return pd.DataFrame()
    df = pd.read_csv(ORDERS_LOG, parse_dates=["ts"])
    if df["ts"].dt.tz is None:
        df["ts"] = df["ts"].dt.tz_localize("UTC")
    return df


# --------------------------------------------------------------------------
# 1) Воронка LLM-фильтра
# --------------------------------------------------------------------------

def section_funnel(journal: pd.DataFrame, since: datetime, metrics: dict) -> str:
    lines = ["## 1. Воронка LLM-фильтра", ""]
    if journal.empty:
        return "\n".join(lines + ["_signal_journal.csv не найден или пуст._", ""])

    window = journal[journal.ts >= since]
    if window.empty:
        return "\n".join(lines + [f"_Нет записей с {since.date()}._", ""])

    total = len(window)
    rules_pass = (window.rules_decision == "PASS").sum()
    rules_reject = (window.rules_decision == "REJECT").sum()
    llm_approve = (window.llm_decision == "approve").sum()
    llm_reject = (window.llm_decision == "reject").sum()
    executed = (window.final_status == "executed").sum()
    stale = window.final_status.isin(["skipped_stale", "skipped_stale_at_executor"]).sum()
    executor_error = (window.final_status == "skipped_executor_error").sum()
    crypto_filtered = (window.final_status == "skipped_crypto_filter").sum()

    lines += [
        f"Период: с {since.date()} по сегодня ({total} сигналов дошло до `rules_score`)",
        "",
        f"- Прошли `rules_score` (PASS): **{rules_pass}** / отклонены правилами: {rules_reject}",
        f"- Из них LLM approve: **{llm_approve}** / LLM reject: {llm_reject}",
        f"- Реально исполнено: **{executed}**",
        f"- Протухло по времени: {stale}",
        f"- Отсечено defense-in-depth крипто-фильтром: {crypto_filtered}",
        f"- Ошибка исполнителя (`skipped_executor_error`): {executor_error}",
    ]
    if rules_pass > 0:
        lines.append(f"- Процент одобрения LLM (среди дошедших): {llm_approve/(llm_approve+llm_reject)*100:.1f}%" if (llm_approve+llm_reject) else "")

    metrics.update({
        "signals_generated": total, "rules_pass": int(rules_pass), "rules_reject": int(rules_reject),
        "llm_approve": int(llm_approve), "llm_reject": int(llm_reject), "executed": int(executed),
        "stale": int(stale), "crypto_filtered": int(crypto_filtered), "executor_error": int(executor_error),
    })

    lines.append("")
    lines.append("По дням:")
    daily = window.groupby(window.ts.dt.date).agg(
        n=("ts", "size"),
        rules_pass=("rules_decision", lambda s: (s == "PASS").sum()),
        llm_approve=("llm_decision", lambda s: (s == "approve").sum()),
        executed=("final_status", lambda s: (s == "executed").sum()),
    )
    lines.append("")
    lines.append("| Дата | Сигналов | rules PASS | LLM approve | Исполнено |")
    lines.append("|---|---|---|---|---|")
    for date, row in daily.iterrows():
        lines.append(f"| {date} | {row.n} | {row.rules_pass} | {row.llm_approve} | {row.executed} |")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 2) executor_error — частота и причины
# --------------------------------------------------------------------------

def section_executor_errors(journal: pd.DataFrame, since: datetime) -> str:
    lines = ["## 2. Ошибки исполнения (executor_error)", ""]
    if journal.empty:
        return "\n".join(lines + ["_signal_journal.csv не найден или пуст._", ""])

    window = journal[(journal.ts >= since) & (journal.final_status == "skipped_executor_error")]
    if window.empty:
        return "\n".join(lines + ["Ошибок исполнения за период не было.", ""])

    lines.append(f"Всего ошибок за период: **{len(window)}**")
    lines.append("")
    lines.append("| Дата | Тикер | Причина |")
    lines.append("|---|---|---|")
    for _, r in window.sort_values("ts", ascending=False).iterrows():
        reason = str(r.get("final_reason", "") or "")[:120]
        lines.append(f"| {r.ts} | {r.ticker} | {reason} |")
    lines.append("")

    # Явный сигнал-триггер: похоже ли что-то на BTU6 (крипто/qualified investors)
    # или BRU6 (нехватка ГО) паттерны
    qual_hits = window[window.final_reason.astype(str).str.contains("qualified investors", case=False, na=False)]
    margin_hits = window[window.final_reason.astype(str).str.contains("Not enough assets|margin trade", case=False, na=False)]
    if len(qual_hits):
        lines.append(f"⚠️ {len(qual_hits)} случаев с текстом 'qualified investors' (паттерн BTU6, 2026-08-03) — "
                      f"проверить, не проходит ли крипто-инструмент через оба слоя фильтра.")
    if len(margin_hits):
        lines.append(f"⚠️ {len(margin_hits)} случаев с текстом про нехватку ГО/маржи (паттерн BRU6, 2026-08-04) — "
                      f"см. DESIGN_margin_aware_sizing.md, ещё не реализовано.")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 3) Комиссионная экономика — только период автостратегии (с 2026-08-01)
# --------------------------------------------------------------------------

def section_commission_economics(metrics: dict) -> str:
    lines = ["## 3. Комиссионная экономика (весь период автостратегии, с 2026-08-01)", ""]

    token = __import__("os").getenv("TINKOFF_TOKEN")
    account_id = __import__("os").getenv("TINKOFF_ACCOUNT_ID")
    if not token or not account_id:
        return "\n".join(lines + ["_TINKOFF_TOKEN/TINKOFF_ACCOUNT_ID не заданы — раздел пропущен._", ""])

    try:
        from tinkoff.invest import Client
        from tinkoff.invest.schemas import GetOperationsByCursorRequest, OperationType
    except ImportError:
        return "\n".join(lines + ["_tinkoff-investments не установлен — раздел пропущен._", ""])

    with Client(token) as client:
        items = []
        cursor = ""
        now = datetime.now(timezone.utc)
        while True:
            req = GetOperationsByCursorRequest(
                account_id=account_id, from_=AUTO_STRATEGY_START, to=now, cursor=cursor, limit=1000,
            )
            resp = client.operations.get_operations_by_cursor(request=req)
            items.extend(resp.items)
            if not resp.has_next or not resp.next_cursor:
                break
            cursor = resp.next_cursor

    broker_fee = [o for o in items if o.type == OperationType.OPERATION_TYPE_BROKER_FEE]
    margin_fee = [o for o in items if o.type == OperationType.OPERATION_TYPE_MARGIN_FEE]
    buys = [o for o in items if o.type == OperationType.OPERATION_TYPE_BUY]
    sells = [o for o in items if o.type == OperationType.OPERATION_TYPE_SELL]

    total_broker_fee = sum(abs(q_to_float(o.payment)) for o in broker_fee)
    total_margin_fee = sum(abs(q_to_float(o.payment)) for o in margin_fee)
    total_turnover = sum(abs(q_to_float(o.payment)) for o in buys + sells)
    n_trades = len(buys) + len(sells)

    lines += [
        f"Период: {AUTO_STRATEGY_START.date()} → {now.date()} ({(now - AUTO_STRATEGY_START).days} дней)",
        "",
        f"- Сделок (BUY+SELL): **{n_trades}**",
        f"- Оборот: {total_turnover:,.0f}₽",
        f"- BROKER_FEE суммарно: {total_broker_fee:,.2f}₽" + (f" (ср. {total_broker_fee/n_trades:.2f}₽/сделку)" if n_trades else ""),
        f"- MARGIN_FEE суммарно: {total_margin_fee:,.2f}₽",
    ]
    if n_trades < 30:
        lines.append("")
        lines.append(f"⚠️ n={n_trades} сделок — по STRATEGY.md п.1 выборка ещё слишком мала "
                      f"для устойчивых выводов об экономике (ориентир — 30-50 сделок).")
    lines.append("")

    metrics.update({
        "commission_period_trades": n_trades, "turnover": round(total_turnover, 2),
        "broker_fee_total": round(total_broker_fee, 2), "margin_fee_total": round(total_margin_fee, 2),
    })
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 4) Распределение размеров позиций
# --------------------------------------------------------------------------

def section_position_sizes(journal: pd.DataFrame, orders: pd.DataFrame, since: datetime, metrics: dict) -> str:
    lines = ["## 4. Распределение размеров позиций", ""]
    if orders.empty:
        return "\n".join(lines + ["_orders_log.csv не найден или пуст._", ""])

    window = orders[orders.ts >= since].copy()
    if window.empty:
        return "\n".join(lines + [f"_Нет исполненных ордеров с {since.date()}._", ""])

    window["notional"] = pd.to_numeric(window["qty"], errors="coerce") * pd.to_numeric(window["price_used"], errors="coerce")

    lines.append(f"Исполненных ордеров за период: {len(window)}")
    lines.append("")
    lines.append(f"- Notional (qty × price): p10={window.notional.quantile(0.1):,.0f}₽  "
                  f"медиана={window.notional.median():,.0f}₽  p90={window.notional.quantile(0.9):,.0f}₽")
    lines.append(f"- qty: min={window.qty.min()}  медиана={window.qty.median():.0f}  max={window.qty.max()}")
    n_qty1 = (pd.to_numeric(window.qty, errors="coerce") == 1).sum()
    lines.append(f"- Позиций с qty=1 лот (граница минимального лота): {n_qty1} из {len(window)} "
                 f"({n_qty1/len(window)*100:.1f}%)")
    lines.append("")

    metrics.update({
        "orders_in_window": len(window), "notional_median": round(float(window.notional.median()), 2),
        "pct_qty1": round(n_qty1 / len(window) * 100, 1),
    })
    lines.append("_Примечание: `risk_rub` напрямую не хранится в orders_log.csv (lot_size/sum_total там "
                 "не заполняются) — приближение через notional=qty×price_used. Для точного risk_rub "
                 "нужен join с signal_journal.csv по entry/stop, не реализовано в этой версии — "
                 "если понадобится точнее, дописать отдельно._")
    lines.append("")
    return "\n".join(lines)


def section_dynamic_stop_health(since: datetime, metrics: dict) -> str:
    """
    План наблюдения за фиксом заморозки трейлинга на безубытке
    (STRATEGY.md "Открытые вопросы" п.8б, 2026-08-13). Читает
    logs/dynamic_stop_events.csv, который dynamic_stop_manager.py пишет
    при КАЖДОМ подтверждённом движении SL — метрика "post_breakeven=1"
    прямо доказывает, что стоп сдвинулся ПОСЛЕ того, как уже был на
    безубытке (до фикса это было физически невозможно, стоп замирал).
    """
    lines = ["## 5. Здоровье динамического трейлинга SL (фикс п.8б)", ""]
    if not DYNAMIC_STOP_EVENTS.exists():
        return "\n".join(lines + [
            "_dynamic_stop_events.csv не найден — либо фикс ещё не деплоился, "
            "либо ни одна позиция ещё не доходила до условий движения SL "
            "(R >= DYN_ACTIVATE_R). Не тревога, просто пока нет данных._", "",
        ])

    df = pd.read_csv(DYNAMIC_STOP_EVENTS, parse_dates=["ts"])
    if df["ts"].dt.tz is None:
        df["ts"] = df["ts"].dt.tz_localize("UTC")
    window = df[df.ts >= since].copy()
    if window.empty:
        return "\n".join(lines + [f"_Событий с {since.date()} не было._", ""])

    n_breakeven = (window.stage == "breakeven").sum()
    n_trail = (window.stage == "trail").sum()
    n_skip = (window.stage == "unrecoverable_skip").sum()
    n_post_breakeven = (window.post_breakeven == 1).sum()

    lines.append(f"Событий за период: {len(window)} "
                 f"(переводов в безубыток: {n_breakeven}, продолжений трейлинга: {n_trail}, "
                 f"пропусков из-за отсутствия исходного SL: {n_skip})")
    lines.append("")
    lines.append(
        f"**Прямое подтверждение фикса**: движений SL ПОСЛЕ безубытка "
        f"(`post_breakeven=1` — до фикса было физически невозможно) — **{n_post_breakeven}**."
    )
    if n_post_breakeven > 0:
        confirmed = window[window.post_breakeven == 1][["ts", "ticker", "stage", "old_sl", "new_sl"]]
        lines.append("")
        for _, r in confirmed.iterrows():
            lines.append(f"  - {r.ts} {r.ticker}: {r.stage}, {r.old_sl:.4f} -> "
                         f"{r.new_sl if pd.notna(r.new_sl) else '(без движения)'}")
    if n_skip > 0:
        lines.append("")
        lines.append(f"⚠️ {n_skip} позиция(й) без записи исходного SL уже на безубытке — "
                     "требует ручной проверки (см. dynamic_stop_manager_alerts.log).")
    lines.append("")

    metrics.update({
        "dyn_stop_events_total": int(len(window)), "dyn_stop_breakeven": int(n_breakeven),
        "dyn_stop_trail": int(n_trail), "dyn_stop_unrecoverable_skip": int(n_skip),
        "dyn_stop_post_breakeven_confirmed": int(n_post_breakeven),
    })
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Google Sheets (--push-sheets) — переиспользует инфраструктуру sheet_bridge.py
# --------------------------------------------------------------------------

WS_SUMMARY = "WEEKLY_SUMMARY"
WS_FULL = "WEEKLY_FULL"
SUMMARY_HEADER = [
    "run_ts", "days_window", "since", "signals_generated", "rules_pass", "rules_reject",
    "llm_approve", "llm_reject", "executed", "stale", "crypto_filtered", "executor_error",
    "commission_period_trades", "turnover", "broker_fee_total", "margin_fee_total",
    "orders_in_window", "notional_median", "pct_qty1",
    "dyn_stop_events_total", "dyn_stop_breakeven", "dyn_stop_trail",
    "dyn_stop_unrecoverable_skip", "dyn_stop_post_breakeven_confirmed",
]
FULL_HEADER = ["run_ts", "days_window", "since", "full_report_markdown"]


def push_to_google_sheets(summary: dict, full_text: str, run_ts: str, since: datetime, days: int) -> Optional[str]:
    """Возвращает URL таблицы при успехе, None при отключённой/нерабочей конфигурации."""
    import os as _os

    if _os.getenv("GSHEETS_ENABLED", "0") != "1":
        print("[Sheets] GSHEETS_ENABLED != 1 — пропуск (используйте --push-sheets только при готовой конфигурации)")
        return None

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        print("[Sheets] gspread/google-auth не установлены — пропуск")
        return None

    cred_file = _os.getenv("GSHEETS_CRED_FILE", "")
    sheet_id = _os.getenv("GSHEETS_SPREADSHEET_ID", "")
    if not cred_file or not sheet_id:
        print("[Sheets] GSHEETS_CRED_FILE/GSHEETS_SPREADSHEET_ID не заданы — пропуск")
        return None

    creds = Credentials.from_service_account_file(
        cred_file,
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)

    def open_or_create(title, header):
        try:
            ws = sh.worksheet(title)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=title, rows=2000, cols=max(30, len(header)))
            ws.append_row(header, value_input_option="RAW")
            return ws
        if not ws.get_all_values():
            ws.append_row(header, value_input_option="RAW")
        return ws

    ws_summary = open_or_create(WS_SUMMARY, SUMMARY_HEADER)
    row = {"run_ts": run_ts, "days_window": days, "since": str(since.date()), **summary}
    ws_summary.append_row([row.get(h, "") for h in SUMMARY_HEADER], value_input_option="RAW")

    ws_full = open_or_create(WS_FULL, FULL_HEADER)
    ws_full.append_row([run_ts, days, str(since.date()), full_text], value_input_option="RAW")

    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
    print(f"[Sheets] записано в {WS_SUMMARY} и {WS_FULL}: {url}")
    return url


# --------------------------------------------------------------------------
# Telegram (--notify-telegram) — переиспользует TELEGRAM_BOT_TOKEN/CHAT_ID
# --------------------------------------------------------------------------

def send_telegram_notification(sheet_url: str, since: datetime, days: int) -> bool:
    import os as _os

    token = _os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = _os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[Telegram] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы — пропуск")
        return False

    try:
        from telegram import Bot
    except ImportError:
        print("[Telegram] python-telegram-bot не установлен — пропуск")
        return False

    import asyncio

    text = (
        f"📊 Еженедельный отчёт по автостратегии готов "
        f"(окно: последние {days} дней, с {since.date()}).\n"
        f"Подробности: {sheet_url}"
    )

    async def _send():
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=text)

    asyncio.run(_send())
    print("[Telegram] уведомление отправлено")
    return True


def main():
    parser = argparse.ArgumentParser(description="Еженедельный отчёт по живой автостратегии")
    parser.add_argument("--days", type=int, default=7, help="За сколько последних дней считать воронку/ошибки/позиции (по умолчанию 7)")
    parser.add_argument("--out", type=str, default=None, help="Сохранить отчёт в файл (дополнительно к выводу в stdout)")
    parser.add_argument("--push-sheets", action="store_true", help="Записать сводку и полный текст в Google Sheets (WEEKLY_SUMMARY/WEEKLY_FULL)")
    parser.add_argument("--notify-telegram", action="store_true", help="Отправить короткое уведомление в Telegram со ссылкой (требует --push-sheets)")
    args = parser.parse_args()

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    run_ts = datetime.now(timezone.utc).isoformat()
    journal = load_signal_journal()
    orders = load_orders_log()
    metrics: dict = {}

    report = [
        f"# Еженедельный отчёт по живой автостратегии",
        f"",
        f"Сгенерировано: {run_ts}",
        f"Окно (воронка/ошибки/позиции): последние {args.days} дней (с {since.date()})",
        f"",
        section_funnel(journal, since, metrics),
        section_executor_errors(journal, since),
        section_commission_economics(metrics),
        section_position_sizes(journal, orders, since, metrics),
        section_dynamic_stop_health(since, metrics),
    ]
    text = "\n".join(report)
    print(text)

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"\n(сохранено также в {args.out})")

    sheet_url = None
    if args.push_sheets:
        sheet_url = push_to_google_sheets(metrics, text, run_ts, since, args.days)

    if args.notify_telegram:
        if not sheet_url:
            print("[Telegram] --notify-telegram без успешной записи в Sheets — уведомление не отправлено (нет ссылки)")
        else:
            send_telegram_notification(sheet_url, since, args.days)


if __name__ == "__main__":
    main()
