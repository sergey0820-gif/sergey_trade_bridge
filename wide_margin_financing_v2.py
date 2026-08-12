"""
wide_margin_financing_v2.py — модель платы за перенос, раздельная по типу
инструмента и стороне (задача 1). Заменяет ОШИБОЧНОЕ в v1 допущение
"заём = margin_rub*(leverage-1) для ВСЕГО" на:

- ФЬЮЧЕРСЫ: ГО — не заём, а обеспечение (подтверждено пользователем: тариф
  "перенос непокрытой позиции" на тарифной странице Т-Инвестиций описан в
  разделе про акции/облигации, для фьючерсов работает иначе). uncovered=0,
  ПОКА суммарная занятая маржа (margin_used, та же метрика ёмкости, что и
  раньше — notional/leverage, используется ТОЛЬКО для входного 80%-лимита,
  не меняется) не превышает equity*MAX_MARGIN_UTILIZATION для УЖЕ ОТКРЫТЫХ
  позиций (после входа equity может просесть из-за убытков по другим
  позициям — вход это проверяет только в момент входа, не постфактум).
  Если такое превышение всё же случается — это реальный дефицит ГО на конец
  дня, и по тарифной странице он ВСЁ РАВНО облагается платой за перенос
  (просто с другим триггером, чем у акций) — не игнорируется молча, дефицит
  относится на фьючерсы и облагается по той же тиражной сетке.
- АКЦИИ ЛОНГ: заём = max(0, суммарный notional ВСЕХ одновременно открытых
  лонгов по акциям − equity) — с первого рубля не считается, только
  избыток сверх собственных средств.
- АКЦИИ ШОРТ: заём = 100% notional с первого рубля (шорт — всегда заём
  бумаги у брокера для продажи), независимо от equity.

Тарифная сетка (тариф "Трейдер") и остальная механика (посуточно,
причинно, единый пересчёт equity) — без изменений от v1.
"""
import sys

import pandas as pd

REPO_DIR = "/Users/sergeychikunov/sergey_trade_bridge_dev"
sys.path.insert(0, REPO_DIR)
from portfolio_backtest import compute_sharpe  # noqa: E402

SCRATCH = "/private/tmp/claude-501/-Users-sergeychikunov-sergey-trade-bridge-dev/0e7f3142-941d-403a-8c88-09b5294a1110/scratchpad"
TRADES_PATH = f"{SCRATCH}/wide_backtest_out/wide_backtest_trades.csv"
OUT_DIR = f"{SCRATCH}/wide_backtest_out"

CAPITAL = 15000.0
RISK_PER_TRADE = 0.02
MAX_POSITIONS = 20
MAX_MARGIN_UTILIZATION = 0.8
LEVERAGE = 5.0
SHARES_CLASS_CODE = "TQBR"

MARGIN_FEE_TIERS = [
    (5_000, "flat", 0.0), (50_000, "flat", 40.0), (100_000, "flat", 75.0),
    (250_000, "flat", 180.0), (500_000, "flat", 350.0), (1_000_000, "flat", 700.0),
    (2_500_000, "flat", 1_750.0), (5_000_000, "flat", 3_500.0), (10_000_000, "flat", 6_900.0),
    (25_000_000, "pct", 0.00068), (50_000_000, "pct", 0.00065), (float("inf"), "pct", 0.00057),
]


def daily_margin_fee(uncovered_rub: float) -> float:
    if uncovered_rub <= 0:
        return 0.0
    for upper, kind, value in MARGIN_FEE_TIERS:
        if uncovered_rub <= upper:
            return value if kind == "flat" else uncovered_rub * value
    return uncovered_rub * MARGIN_FEE_TIERS[-1][2]


def load_block_trades(block: str) -> pd.DataFrame:
    df = pd.read_csv(TRADES_PATH, parse_dates=["entry_time", "exit_time"])
    df = df[df["block"] == block].copy()
    df["risk_pct"] = (df.entry - df.stop).abs() / df.entry
    df["rr"] = (df.target - df.entry).abs() / (df.entry - df.stop).abs()
    df = df[df.risk_pct > 0]
    return df.sort_values(["entry_time", "rr"], ascending=[True, False]).reset_index(drop=True)


def simulate_portfolio_baseline(trades: pd.DataFrame) -> dict:
    equity = CAPITAL
    active = []
    equity_curve = [(trades.entry_time.min(), equity)]
    taken = skipped_positions = skipped_margin = 0

    def close_due(before_time):
        nonlocal equity
        due = sorted([p for p in active if p["exit_time"] <= before_time], key=lambda p: p["exit_time"])
        for p in due:
            equity += p["r_multiple"] * p["risk_rub"]
            active.remove(p)
            equity_curve.append((p["exit_time"], equity))

    for row in trades.itertuples(index=False):
        close_due(row.entry_time)
        if len(active) >= MAX_POSITIONS:
            skipped_positions += 1
            continue
        margin_used = sum(p["margin"] for p in active)
        risk_rub = equity * RISK_PER_TRADE
        notional = risk_rub / row.risk_pct
        margin_needed = notional / LEVERAGE
        if margin_used + margin_needed > equity * MAX_MARGIN_UTILIZATION:
            skipped_margin += 1
            continue
        active.append({"exit_time": row.exit_time, "risk_rub": risk_rub, "r_multiple": row.r_multiple, "margin": margin_needed})
        taken += 1

    if active:
        close_due(max(p["exit_time"] for p in active))

    return _finalize(equity_curve, taken, skipped_positions, skipped_margin, len(trades), trades,
                      total_fees=0.0, fee_by_source={})


def simulate_portfolio_with_financing_v2(trades: pd.DataFrame) -> dict:
    equity = CAPITAL
    active = []
    equity_curve = [(trades.entry_time.min(), equity)]
    taken = skipped_positions = skipped_margin = 0
    total_fees_rub = 0.0
    fee_by_source = {"long": 0.0, "short": 0.0, "futures_go_deficit": 0.0}
    edge_case_days = []

    def close_due(before_time):
        nonlocal equity
        due = sorted([p for p in active if p["exit_time"] <= before_time], key=lambda p: p["exit_time"])
        for p in due:
            equity += p["r_multiple"] * p["risk_rub"]
            active.remove(p)
            equity_curve.append((p["exit_time"], equity))

    tz = trades.entry_time.dt.tz
    start_day = trades.entry_time.min().normalize()
    end_day = trades.exit_time.max().normalize()
    all_days = pd.date_range(start_day, end_day, freq="D", tz=tz)
    rows = list(trades.itertuples(index=False))
    idx = 0
    n_rows = len(rows)

    for day in all_days:
        day_end = day + pd.Timedelta(days=1)

        while idx < n_rows and rows[idx].entry_time < day_end:
            row = rows[idx]
            close_due(row.entry_time)
            if len(active) >= MAX_POSITIONS:
                skipped_positions += 1
            else:
                margin_used = sum(p["margin"] for p in active)
                risk_rub = equity * RISK_PER_TRADE
                notional = risk_rub / row.risk_pct
                margin_needed = notional / LEVERAGE
                if margin_used + margin_needed > equity * MAX_MARGIN_UTILIZATION:
                    skipped_margin += 1
                else:
                    active.append({
                        "exit_time": row.exit_time, "risk_rub": risk_rub, "r_multiple": row.r_multiple,
                        "margin": margin_needed, "notional": notional,
                        "class_code": row.class_code, "side": row.side,
                        "entry_date": row.entry_time.normalize(),
                    })
                    taken += 1
            idx += 1

        close_due(day_end)

        overnight = [p for p in active if p["entry_date"] <= day]
        if overnight:
            long_shares = [p for p in overnight if p["class_code"] == SHARES_CLASS_CODE and p["side"] == "long"]
            short_shares = [p for p in overnight if p["class_code"] == SHARES_CLASS_CODE and p["side"] == "short"]
            futures_pos = [p for p in overnight if p["class_code"] != SHARES_CLASS_CODE]

            total_long_notional = sum(p["notional"] for p in long_shares)
            total_short_notional = sum(p["notional"] for p in short_shares)
            long_debt = max(0.0, total_long_notional - equity)
            short_debt = total_short_notional

            margin_used_today = sum(p["margin"] for p in overnight)
            futures_margin_today = sum(p["margin"] for p in futures_pos)
            shortfall_vs_cap = max(0.0, margin_used_today - equity * MAX_MARGIN_UTILIZATION)
            futures_go_deficit = min(shortfall_vs_cap, futures_margin_today)
            if shortfall_vs_cap > 0:
                edge_case_days.append({
                    "day": day, "margin_used": margin_used_today, "cap": equity * MAX_MARGIN_UTILIZATION,
                    "shortfall": shortfall_vs_cap, "futures_go_deficit": futures_go_deficit,
                })

            total_uncovered_today = long_debt + short_debt + futures_go_deficit
            if total_uncovered_today > 0:
                fee = daily_margin_fee(total_uncovered_today)
                equity -= fee
                total_fees_rub += fee
                fee_by_source["long"] += fee * (long_debt / total_uncovered_today)
                fee_by_source["short"] += fee * (short_debt / total_uncovered_today)
                fee_by_source["futures_go_deficit"] += fee * (futures_go_deficit / total_uncovered_today)

    if active:
        close_due(max(p["exit_time"] for p in active))

    result = _finalize(equity_curve, taken, skipped_positions, skipped_margin, len(trades), trades,
                        total_fees=total_fees_rub, fee_by_source=fee_by_source)
    result["edge_case_days"] = edge_case_days
    return result


def _finalize(equity_curve, taken, skipped_positions, skipped_margin, total_candidates, trades, total_fees, fee_by_source):
    curve = sorted(equity_curve, key=lambda x: x[0])
    peak = CAPITAL
    max_dd_pct = 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        if peak > 0:
            max_dd_pct = max(max_dd_pct, (peak - eq) / peak * 100)
    final_equity = curve[-1][1]
    span_days = (trades.exit_time.max() - trades.entry_time.min()).days
    years = span_days / 365.25 if span_days > 0 else 1.0
    cagr_pct = ((final_equity / CAPITAL) ** (1 / years) - 1) * 100 if final_equity > 0 else -100.0
    return {
        "total_candidates": total_candidates, "taken": taken,
        "skipped_positions": skipped_positions, "skipped_margin": skipped_margin,
        "final_equity": final_equity, "return_pct": (final_equity / CAPITAL - 1) * 100,
        "cagr_pct": cagr_pct, "max_dd_pct": max_dd_pct, "years": years,
        "curve": curve, "total_fees_rub": total_fees, "fee_by_source": fee_by_source,
    }


def main():
    print("=" * 120)
    print("ЗАДАЧА 1: РАЗДЕЛЬНАЯ МОДЕЛЬ ПО ТИПУ ИНСТРУМЕНТА (v2) — ДО/ПОСЛЕ по блокам")
    print(f"Капитал {CAPITAL:.0f}₽, риск {RISK_PER_TRADE*100:.0f}%/сделку, лимит {MAX_POSITIONS} позиций, "
          f"маржа<={MAX_MARGIN_UTILIZATION*100:.0f}%, плечо x{LEVERAGE:.0f} (метрика ёмкости для входа — как раньше)")
    print("=" * 120)

    rows = []
    for block in ["in_sample", "validation", "holdout"]:
        trades = load_block_trades(block)
        before = simulate_portfolio_baseline(trades)
        after = simulate_portfolio_with_financing_v2(trades)
        sharpe_before = compute_sharpe(before["curve"])
        sharpe_after = compute_sharpe(after["curve"])

        gross_profit_before = before["final_equity"] - CAPITAL
        fee_share = after["total_fees_rub"] / gross_profit_before * 100 if gross_profit_before > 0 else float("nan")

        print(f"\n[{block}]  взято ДО={before['taken']} ПОСЛЕ(v2)={after['taken']}")
        print(f"  ДО         : итог={before['final_equity']:>12,.0f}₽  доход={before['return_pct']:>+8.1f}%  "
              f"CAGR={before['cagr_pct']:>+8.1f}%  Sharpe={sharpe_before:>+.2f}  просадка={before['max_dd_pct']:>5.1f}%")
        print(f"  ПОСЛЕ (v2) : итог={after['final_equity']:>12,.0f}₽  доход={after['return_pct']:>+8.1f}%  "
              f"CAGR={after['cagr_pct']:>+8.1f}%  Sharpe={sharpe_after:>+.2f}  просадка={after['max_dd_pct']:>5.1f}%")
        print(f"  Комиссия за перенос (v2) суммарно: {after['total_fees_rub']:>12,.0f}₽ ({fee_share:.1f}% от вал. прибыли ДО)")
        print(f"    из них: long={after['fee_by_source']['long']:>10,.0f}₽  short={after['fee_by_source']['short']:>10,.0f}₽  "
              f"futures_ГО_дефицит={after['fee_by_source']['futures_go_deficit']:>10,.0f}₽")
        n_edge = len(after["edge_case_days"])
        print(f"  Дней с превышением 80%-лимита для УЖЕ открытых позиций (edge case): {n_edge} из {len(pd.date_range(trades.entry_time.min().normalize(), trades.exit_time.max().normalize(), freq='D'))} дней блока")
        if n_edge:
            print("    Примеры edge case дней (до 5):")
            for e in after["edge_case_days"][:5]:
                print(f"      {e['day'].date()}: margin_used={e['margin_used']:,.0f}₽ cap={e['cap']:,.0f}₽ "
                      f"shortfall={e['shortfall']:,.0f}₽ (из них фьючерсы={e['futures_go_deficit']:,.0f}₽)")

        rows.append({
            "block": block,
            "final_equity_before": before["final_equity"], "return_pct_before": before["return_pct"],
            "cagr_pct_before": before["cagr_pct"], "sharpe_before": sharpe_before,
            "final_equity_after_v2": after["final_equity"], "return_pct_after_v2": after["return_pct"],
            "cagr_pct_after_v2": after["cagr_pct"], "sharpe_after_v2": sharpe_after,
            "total_fees_rub_v2": after["total_fees_rub"],
            "fee_long_rub": after["fee_by_source"]["long"], "fee_short_rub": after["fee_by_source"]["short"],
            "fee_futures_go_deficit_rub": after["fee_by_source"]["futures_go_deficit"],
            "fee_share_of_gross_profit_pct": fee_share,
            "taken_before": before["taken"], "taken_after_v2": after["taken"],
            "n_edge_case_days": n_edge,
        })

    print("=" * 120)
    pd.DataFrame(rows).to_csv(f"{OUT_DIR}/wide_margin_financing_v2_before_after.csv", index=False)
    print(f"Сохранено (v2, старый файл НЕ перезаписан): {OUT_DIR}/wide_margin_financing_v2_before_after.csv")


if __name__ == "__main__":
    main()
