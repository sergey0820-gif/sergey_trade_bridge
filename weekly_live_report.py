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

Читает read-only: logs/signal_journal.csv, orders_log.csv,
client.operations.get_operations_by_cursor (реальный счёт через .env).
Ничего не пишет, ничего не размещает, никаких ордеров.

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

def section_funnel(journal: pd.DataFrame, since: datetime) -> str:
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

def section_commission_economics() -> str:
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
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 4) Распределение размеров позиций
# --------------------------------------------------------------------------

def section_position_sizes(journal: pd.DataFrame, orders: pd.DataFrame, since: datetime) -> str:
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
    lines.append("_Примечание: `risk_rub` напрямую не хранится в orders_log.csv (lot_size/sum_total там "
                 "не заполняются) — приближение через notional=qty×price_used. Для точного risk_rub "
                 "нужен join с signal_journal.csv по entry/stop, не реализовано в этой версии — "
                 "если понадобится точнее, дописать отдельно._")
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Еженедельный отчёт по живой автостратегии")
    parser.add_argument("--days", type=int, default=7, help="За сколько последних дней считать воронку/ошибки/позиции (по умолчанию 7)")
    parser.add_argument("--out", type=str, default=None, help="Сохранить отчёт в файл (дополнительно к выводу в stdout)")
    args = parser.parse_args()

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    journal = load_signal_journal()
    orders = load_orders_log()

    report = [
        f"# Еженедельный отчёт по живой автостратегии",
        f"",
        f"Сгенерировано: {datetime.now(timezone.utc).isoformat()}",
        f"Окно (воронка/ошибки/позиции): последние {args.days} дней (с {since.date()})",
        f"",
        section_funnel(journal, since),
        section_executor_errors(journal, since),
        section_commission_economics(),
        section_position_sizes(journal, orders, since),
    ]
    text = "\n".join(report)
    print(text)

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"\n(сохранено также в {args.out})")


if __name__ == "__main__":
    main()
