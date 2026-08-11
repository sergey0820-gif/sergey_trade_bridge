#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
portfolio_backtest.py

Портфельная симуляция поверх уже готовых сделок из backtest_ema921.py.

backtest_ema921.py считает каждый тикер независимо — как будто у каждого
сигнала есть отдельный неограниченный капитал. Реальный счёт так не работает:
капитал общий, сделки на 60 тикерах конкурируют за него, действуют
MAX_OPEN_POSITIONS (лимит одновременных позиций) и MAX_MARGIN_UTILIZATION
(лимит доли капитала под маржой), и прибыль/убыток одной сделки меняет размер
следующей (сложный процент). Этот скрипт берёт CSV со сделками одного или
нескольких прогонов backtest_ema921.py (--tag) и проигрывает их как единый
портфель с этими ограничениями, чтобы получить честную (а не "потолочную")
оценку годовой доходности.

Как считается размер позиции и маржа:
- risk_rub = equity_на_момент_входа * RISK_PER_TRADE (сложный процент —
  считается от текущего, не от стартового капитала)
- notional = risk_rub / risk_pct, где risk_pct = |entry-stop|/entry
- margin_needed = notional / LEVERAGE (допущение x5, как обсуждали ранее —
  реальные ставки риска по инструментам не смоделированы)
- Позиция открывается, только если после неё margin_used <= equity * MAX_MARGIN_UTILIZATION
  и одновременных позиций < MAX_OPEN_POSITIONS; иначе сигнал пропускается
  ("skipped") — это и есть тот эффект, который наивная оценка "сделок/год ×
  экспектация" в backtest_ema921.py игнорирует.
- При нескольких сигналах на один и тот же час — приоритет по большему
  запланированному R:R (target/stop) как грубая замена скорингу
  ai_filter_agent.py, которого в этом бэктесте нет.

Ограничение (честно): просадка считается только по РЕАЛИЗОВАННОМУ капиталу
(в моменты закрытия сделок), без mark-to-market по floating-позициям — в
реальности внутридневная просадка может быть заметно глубже.

Использование:
  python portfolio_backtest.py
  python portfolio_backtest.py --capital 15000 --leverage 5
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
OUT_DIR = BASE_DIR / "out"
OUT_DIR.mkdir(exist_ok=True)

sys.path.insert(0, str(BASE_DIR))
# Переиспользуем напрямую (не копия) — те же кэш-хелперы и константы, что
# использовались при генерации сделок реальной стратегии в backtest_ema921.py.
from backtest_ema921 import (  # noqa: E402
    _cache_path, _load_cache,
    WARMUP_H1_BARS, MIN_D1_BARS, MIN_H1_BARS,
    DYN_ACTIVATE_R, DYN_TRAIL_START_R, DYN_TRAIL_GAP_R,
)
from utils.ta import atr as _atr, _structural_target  # noqa: E402
from dynamic_stop_manager import compute_new_sl_price  # noqa: E402
from config import COMMISSION_BPS_ROUNDTRIP  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "portfolio_backtest.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH if ENV_PATH.exists() else None)

# Сценарии = прогоны backtest_ema921.py, уже сделанные в этой сессии
DEFAULT_SCENARIOS = {
    "без фильтров": OUT_DIR / "backtest_ema921_trades_true_baseline.csv",
    "фильтр объёма": OUT_DIR / "backtest_ema921_trades_baseline.csv",
    "объём+тренд": OUT_DIR / "backtest_ema921_trades_vol_trend.csv",
    "объём+тренд+зазор EMA": OUT_DIR / "backtest_ema921_trades_vol_trend_gap.csv",
}


def load_trades(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["entry_time", "exit_time"])
    df = df.dropna(subset=["exit_time"]).copy()
    df["risk_pct"] = (df.entry - df.stop).abs() / df.entry
    df["rr"] = (df.target - df.entry).abs() / (df.entry - df.stop).abs()
    df = df[df.risk_pct > 0]
    return df.sort_values(["entry_time", "rr"], ascending=[True, False]).reset_index(drop=True)


def simulate_portfolio(
    trades: pd.DataFrame,
    capital: float,
    risk_per_trade: float,
    max_positions: int,
    max_margin_utilization: float,
    leverage: float,
) -> Dict:
    equity = capital
    active: List[Dict] = []  # {"exit_time", "risk_rub", "margin", "r_multiple"}
    equity_curve: List[Tuple[pd.Timestamp, float]] = [(trades.entry_time.min(), equity)]
    taken = 0
    skipped_positions = 0
    skipped_margin = 0

    def close_due(before_time: pd.Timestamp) -> None:
        nonlocal equity
        due = sorted([p for p in active if p["exit_time"] <= before_time], key=lambda p: p["exit_time"])
        for p in due:
            equity += p["r_multiple"] * p["risk_rub"]
            active.remove(p)
            equity_curve.append((p["exit_time"], equity))

    for row in trades.itertuples(index=False):
        close_due(row.entry_time)

        if len(active) >= max_positions:
            skipped_positions += 1
            continue

        margin_used = sum(p["margin"] for p in active)
        risk_rub = equity * risk_per_trade
        notional = risk_rub / row.risk_pct
        margin_needed = notional / leverage

        if margin_used + margin_needed > equity * max_margin_utilization:
            skipped_margin += 1
            continue

        active.append({
            "exit_time": row.exit_time, "risk_rub": risk_rub,
            "margin": margin_needed, "r_multiple": row.r_multiple,
        })
        taken += 1

    if active:
        close_due(max(p["exit_time"] for p in active))

    curve = sorted(equity_curve, key=lambda x: x[0])
    peak = capital
    max_dd_pct = 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        if peak > 0:
            max_dd_pct = max(max_dd_pct, (peak - eq) / peak * 100)

    final_equity = curve[-1][1]
    span_days = (trades.exit_time.max() - trades.entry_time.min()).days
    years = span_days / 365.25 if span_days > 0 else 1.0
    cagr_pct = ((final_equity / capital) ** (1 / years) - 1) * 100 if final_equity > 0 else -100.0

    return {
        "total_candidates": len(trades),
        "taken": taken,
        "skipped_positions": skipped_positions,
        "skipped_margin": skipped_margin,
        "final_equity": final_equity,
        "return_pct": (final_equity / capital - 1) * 100,
        "cagr_pct": cagr_pct,
        "max_dd_pct": max_dd_pct,
        "years": years,
        "curve": curve,
    }


def compute_sharpe(curve: List[Tuple[pd.Timestamp, float]], periods_per_year: int = 252) -> float:
    """
    Sharpe по дневным доходностям equity-кривой (кривая событийная — точки
    только в моменты закрытия сделок/цен buy&hold, — поэтому ресэмплим в
    дневную серию с forward-fill между событиями). Безрисковая ставка не
    вычитается (rf=0) — сравнение внутреннее (стратегия vs random vs
    buy&hold на одном и том же капитале и периоде), а не абсолютная оценка
    доходности с поправкой на RUONIA/ОФЗ.
    """
    if len(curve) < 3:
        return float("nan")
    s = pd.Series({t: eq for t, eq in curve}).sort_index()
    daily = s.resample("1D").last().ffill().dropna()
    rets = daily.pct_change().dropna()
    if len(rets) < 2 or rets.std() == 0:
        return float("nan")
    return float(rets.mean() / rets.std() * (periods_per_year ** 0.5))


def universe_from_trades(trades: pd.DataFrame) -> List[Tuple[str, str]]:
    pairs = trades[["ticker", "class_code"]].drop_duplicates()
    return sorted(pairs.itertuples(index=False, name=None))


def generate_random_entry_trades(
    universe: List[Tuple[str, str]],
    real_trades: pd.DataFrame,
    seed: int,
) -> pd.DataFrame:
    """
    Бенчмарк «случайный вход»: та же механика, что у боевой стратегии —
    стоп 2×ATR(H1,14), цель — структурный swing D1 (или fallback 3R),
    трейлинг compute_new_sl_price, комиссия COMMISSION_BPS_ROUNDTRIP
    (переиспользуются напрямую из utils/ta.py и dynamic_stop_manager.py,
    не копии). Единственная разница с analyze_trade_setup — решение "войти
    сейчас" не завязано на EMA9/21+RSI50, а случайно.

    Калибровка:
    - сторона (long/short) сэмплируется из эмпирического распределения
      real_trades.side — сохраняет реальную пропорцию long/short;
    - интенсивность входов на тикер калибруется так, чтобы ожидаемое число
      сделок на тикере совпадало с реальным (иначе сравнение нечестное —
      больше сделок = больше экспозиции к риску/комиссии). Это оценка
      сверху по общему числу H1-баров тикера (без учёта, что часть баров
      будет заблокирована открытой позицией) — реально достигнутое число
      сделок обычно чуть ниже n_target, это ожидаемо и не скрывается
      (см. лог "random-entry: ... сделок (цель ...)").
    """
    rng = random.Random(seed)
    side_pool = real_trades["side"].tolist()
    per_ticker_target = real_trades.groupby(["ticker", "class_code"]).size().to_dict()

    rows: List[Dict] = []
    for ticker, class_code in universe:
        n_target = per_ticker_target.get((ticker, class_code), 0)
        if n_target <= 0:
            continue

        df_h1 = _load_cache(_cache_path(ticker, class_code, "H1"))
        df_d1 = _load_cache(_cache_path(ticker, class_code, "D1"))
        if df_h1 is None or df_d1 is None or len(df_h1) < WARMUP_H1_BARS + MIN_H1_BARS:
            continue

        n_eligible = len(df_h1) - WARMUP_H1_BARS
        p_entry = min(0.9, n_target / n_eligible) if n_eligible > 0 else 0.0

        open_trade: Optional[Dict] = None
        current_stop = 0.0

        for i in range(WARMUP_H1_BARS, len(df_h1)):
            bar = df_h1.iloc[i]
            bar_time = bar["time"]

            if open_trade is not None:
                side = open_trade["side"]
                min_step = max(open_trade["entry"] * 0.0001, 0.01)
                risk_per_unit = abs(open_trade["entry"] - open_trade["stop"])

                hit_stop = (bar["low"] <= current_stop) if side == "long" else (bar["high"] >= current_stop)
                hit_target = (bar["high"] >= open_trade["target"]) if side == "long" else (bar["low"] <= open_trade["target"])

                if hit_stop:
                    exit_price, reason = current_stop, "stop"
                elif hit_target:
                    exit_price, reason = open_trade["target"], "target"
                else:
                    exit_price, reason = None, ""

                if exit_price is not None:
                    raw_r = ((exit_price - open_trade["entry"]) / risk_per_unit if side == "long"
                             else (open_trade["entry"] - exit_price) / risk_per_unit)
                    cost_r = (open_trade["entry"] * (COMMISSION_BPS_ROUNDTRIP / 10000.0)) / risk_per_unit
                    open_trade["exit_time"] = bar_time
                    open_trade["exit_price"] = exit_price
                    open_trade["exit_reason"] = reason
                    open_trade["r_multiple"] = raw_r - cost_r
                    rows.append(open_trade)
                    open_trade = None
                    continue

                new_sl = compute_new_sl_price(
                    direction=side, entry=open_trade["entry"], current=bar["close"],
                    old_sl=current_stop, min_step=min_step,
                    activate_r=DYN_ACTIVATE_R, trail_start_r=DYN_TRAIL_START_R,
                    trail_gap_r=DYN_TRAIL_GAP_R,
                )
                if new_sl is not None:
                    current_stop = new_sl
                continue

            # нет открытой позиции — случайное решение "войти сейчас"
            if rng.random() >= p_entry:
                continue

            d1_slice = df_d1[df_d1["time"] <= bar_time]
            h1_slice = df_h1.iloc[: i + 1]
            if len(d1_slice) < MIN_D1_BARS or len(h1_slice) < MIN_H1_BARS:
                continue

            atr_val = _atr(h1_slice, 14).iloc[-1]
            if pd.isna(atr_val) or atr_val <= 0:
                continue

            side = rng.choice(side_pool)
            entry = float(bar["close"])
            stop = entry - atr_val * 2 if side == "long" else entry + atr_val * 2
            risk = abs(entry - stop)
            target, target_source = _structural_target(d1_slice, side, entry, risk)

            open_trade = {
                "ticker": ticker, "class_code": class_code, "side": side,
                "entry_time": bar_time, "entry": entry, "stop": stop,
                "target": target, "target_source": target_source,
                "exit_time": None, "exit_price": None, "exit_reason": "", "r_multiple": 0.0,
            }
            current_stop = stop

    cols = ["ticker", "class_code", "side", "entry_time", "entry", "stop", "target",
            "target_source", "exit_time", "exit_price", "exit_reason", "r_multiple"]
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows)[cols]


def compute_buy_and_hold(
    universe: List[Tuple[str, str]], capital: float,
    start: pd.Timestamp, end: pd.Timestamp,
) -> Dict:
    """
    Пассивный бенчмарк: тот же капитал делится поровну между тем же
    universe, что и у реальных сделок (равновзвешенно — без учёта размера
    компании/ликвидности, это сознательное упрощение), покупается по D1
    close на начало периода и держится без ребалансировки/торговли до
    конца периода.
    """
    per_ticker_capital = capital / len(universe)
    price_series: Dict[Tuple[str, str], pd.Series] = {}
    dropped: List[str] = []

    for ticker, class_code in universe:
        df_d1 = _load_cache(_cache_path(ticker, class_code, "D1"))
        if df_d1 is None or df_d1.empty:
            dropped.append(ticker)
            continue
        s = df_d1[(df_d1["time"] >= start) & (df_d1["time"] <= end)].set_index("time")["close"]
        if s.empty:
            dropped.append(ticker)
            continue
        price_series[(ticker, class_code)] = s

    if not price_series:
        raise ValueError("Нет ценовых данных D1 для buy&hold бенчмарка")
    if dropped:
        logger.warning("Buy&hold: без данных за период, исключены из корзины: %s", ", ".join(dropped))

    all_dates = sorted(set().union(*(s.index for s in price_series.values())))
    equity = pd.Series(0.0, index=all_dates)
    for s in price_series.values():
        s = s.reindex(all_dates).ffill().bfill()
        shares = per_ticker_capital / float(s.iloc[0])
        equity = equity.add(shares * s)

    curve = list(zip(equity.index, equity.values))
    peak = capital
    max_dd_pct = 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        if peak > 0:
            max_dd_pct = max(max_dd_pct, (peak - eq) / peak * 100)

    final_equity = float(equity.iloc[-1])
    span_days = (all_dates[-1] - all_dates[0]).days
    years = span_days / 365.25 if span_days > 0 else 1.0
    cagr_pct = ((final_equity / capital) ** (1 / years) - 1) * 100 if final_equity > 0 else -100.0

    return {
        "n_instruments": len(price_series),
        "dropped": dropped,
        "final_equity": final_equity,
        "return_pct": (final_equity / capital - 1) * 100,
        "cagr_pct": cagr_pct,
        "max_dd_pct": max_dd_pct,
        "years": years,
        "curve": curve,
    }


def run_three_way_comparison(
    args: argparse.Namespace,
) -> Optional[pd.DataFrame]:
    """
    Реальная стратегия vs random-entry (та же логика выхода, случайный вход,
    усреднённый по args.random_runs сидам) vs buy&hold — на одном и том же
    капитале, universe и периоде. Отвечает на вопрос "добавляет ли сигнал
    что-то сверх случайности", а не просто показывает, что стратегия
    прибыльна в вакууме.
    """
    real_path = DEFAULT_SCENARIOS.get(args.benchmark_scenario)
    if real_path is None or not real_path.exists():
        logger.warning(
            "Бенчмарки пропущены: сценарий '%s' (%s) не найден — нужен CSV сделок реальной "
            "стратегии, совпадающей с боевым фильтром scan_live_full.py",
            args.benchmark_scenario, real_path,
        )
        return None

    real_trades = load_trades(real_path)
    universe = universe_from_trades(real_trades)
    start, end = real_trades.entry_time.min(), real_trades.exit_time.max()

    missing_cache = [
        (t, c) for t, c in universe
        if not _cache_path(t, c, "H1").exists() or not _cache_path(t, c, "D1").exists()
    ]
    if missing_cache:
        logger.warning(
            "Бенчмарки пропущены: нет кэша свечей (data_cache/candles/) для %d из %d тикеров "
            "(например %s) — нужен полный кэш той же истории, что использовалась для '%s'",
            len(missing_cache), len(universe), missing_cache[0], args.benchmark_scenario,
        )
        return None

    logger.info(
        "Бенчмарки: реальная стратегия ('%s') vs random-entry (%d прогонов) vs buy&hold — "
        "%d тикеров, период %s..%s",
        args.benchmark_scenario, args.random_runs, len(universe), start.date(), end.date(),
    )

    sim_kwargs = dict(
        capital=args.capital, risk_per_trade=args.risk_per_trade,
        max_positions=args.max_positions, max_margin_utilization=args.max_margin_utilization,
        leverage=args.leverage,
    )

    # --- реальная стратегия ---
    real_result = simulate_portfolio(real_trades, **sim_kwargs)
    real_sharpe = compute_sharpe(real_result["curve"])

    # --- random-entry: N независимых прогонов, разные сиды ---
    random_metrics = []
    for k in range(args.random_runs):
        seed = args.random_seed_base + k
        rnd_trades = generate_random_entry_trades(universe, real_trades, seed=seed)
        if rnd_trades.empty:
            continue
        rnd_trades = rnd_trades.dropna(subset=["exit_time"]).copy()
        if rnd_trades.empty:
            continue
        rnd_trades["risk_pct"] = (rnd_trades.entry - rnd_trades.stop).abs() / rnd_trades.entry
        rnd_trades["rr"] = (rnd_trades.target - rnd_trades.entry).abs() / (rnd_trades.entry - rnd_trades.stop).abs()
        rnd_trades = rnd_trades[rnd_trades.risk_pct > 0].sort_values(
            ["entry_time", "rr"], ascending=[True, False]
        ).reset_index(drop=True)
        rnd_result = simulate_portfolio(rnd_trades, **sim_kwargs)
        random_metrics.append({
            "seed": seed, "n_trades": len(rnd_trades),
            "cagr_pct": rnd_result["cagr_pct"], "max_dd_pct": rnd_result["max_dd_pct"],
            "sharpe": compute_sharpe(rnd_result["curve"]),
        })
        logger.info(
            "  random-entry seed=%d: сделок=%d (цель ~%d) CAGR=%+.1f%% Sharpe=%.2f просадка=%.1f%%",
            seed, len(rnd_trades), len(real_trades), rnd_result["cagr_pct"],
            random_metrics[-1]["sharpe"], rnd_result["max_dd_pct"],
        )

    if not random_metrics:
        logger.warning("Бенчмарки пропущены: ни один random-entry прогон не дал сделок")
        return None

    rnd_df = pd.DataFrame(random_metrics)

    def _percentile_rank(value: float, dist: pd.Series) -> float:
        """Доля random-прогонов со значением <= value реальной стратегии."""
        clean = dist.dropna()
        if clean.empty or pd.isna(value):
            return float("nan")
        return float((clean <= value).mean() * 100)

    # --- buy & hold ---
    bh_result = compute_buy_and_hold(universe, args.capital, start, end)
    bh_sharpe = compute_sharpe(bh_result["curve"])

    summary = pd.DataFrame([
        {
            "сценарий": f"реальная стратегия ({args.benchmark_scenario})",
            "сделок": len(real_trades), "CAGR_%": real_result["cagr_pct"],
            "Sharpe": real_sharpe, "MaxDD_%": real_result["max_dd_pct"],
            "итог_₽": real_result["final_equity"],
        },
        {
            "сценарий": f"random-entry (mean, n={len(rnd_df)} прогонов)",
            "сделок": rnd_df["n_trades"].mean(), "CAGR_%": rnd_df["cagr_pct"].mean(),
            "Sharpe": rnd_df["sharpe"].mean(), "MaxDD_%": rnd_df["max_dd_pct"].mean(),
            "итог_₽": float("nan"),
        },
        {
            "сценарий": "random-entry (std)",
            "сделок": rnd_df["n_trades"].std(), "CAGR_%": rnd_df["cagr_pct"].std(),
            "Sharpe": rnd_df["sharpe"].std(), "MaxDD_%": rnd_df["max_dd_pct"].std(),
            "итог_₽": float("nan"),
        },
        {
            "сценарий": "buy & hold",
            "сделок": 0, "CAGR_%": bh_result["cagr_pct"],
            "Sharpe": bh_sharpe, "MaxDD_%": bh_result["max_dd_pct"],
            "итог_₽": bh_result["final_equity"],
        },
    ])

    cagr_pctl = _percentile_rank(real_result["cagr_pct"], rnd_df["cagr_pct"])
    sharpe_pctl = _percentile_rank(real_sharpe, rnd_df["sharpe"])

    print("\n" + "=" * 100)
    print(f"РЕАЛЬНАЯ СТРАТЕГИЯ vs RANDOM-ENTRY vs BUY&HOLD — {len(universe)} тикеров, "
          f"{start.date()}..{end.date()}, старт {args.capital:.0f}₽")
    print("=" * 100)
    for _, r in summary.iterrows():
        n_trades_str = f"{r['сделок']:.0f}" if pd.notna(r["сделок"]) else "—"
        итог_str = f"{r['итог_₽']:>14,.0f}₽" if pd.notna(r["итог_₽"]) else " " * 15
        print(
            f"{r['сценарий']:<38} сделок={n_trades_str:>6}  "
            f"CAGR={r['CAGR_%']:>+8.1f}%  Sharpe={r['Sharpe']:>+6.2f}  "
            f"MaxDD={r['MaxDD_%']:>5.1f}%  итог={итог_str}"
        )
    print("-" * 100)
    print(
        f"Реальная стратегия по CAGR лучше {cagr_pctl:.0f}% random-entry прогонов, "
        f"по Sharpe лучше {sharpe_pctl:.0f}% random-entry прогонов "
        f"(100% = лучше вообще всех {len(rnd_df)} случайных прогонов)."
    )
    if not pd.isna(cagr_pctl) and cagr_pctl < 90:
        print(
            "ВНИМАНИЕ: реальная стратегия НЕ доминирует над случайным входом с той же "
            "механикой выхода (порог условно взят 90-й процентиль) — сигнал, возможно, "
            "не добавляет edge сверх самого правила стоп/тейк/трейлинг."
        )
    print("=" * 100)

    summary_path = OUT_DIR / "portfolio_benchmark_comparison.csv"
    summary.to_csv(summary_path, index=False)
    rnd_runs_path = OUT_DIR / "portfolio_benchmark_random_runs.csv"
    rnd_df.to_csv(rnd_runs_path, index=False)
    print(f"Сводка сохранена: {summary_path}")
    print(f"Все random-entry прогоны (по сидам): {rnd_runs_path}\n")

    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Портфельная симуляция поверх сделок backtest_ema921.py")
    ap.add_argument("--capital", type=float, default=float(os.getenv("CAPITAL", "15000")),
                     help="стартовый капитал, руб (по умолчанию — CAPITAL из .env)")
    ap.add_argument("--risk-per-trade", type=float, default=float(os.getenv("RISK_PER_TRADE", "0.02")),
                     help="риск на сделку, доля капитала (по умолчанию — RISK_PER_TRADE из .env)")
    ap.add_argument("--max-positions", type=int, default=int(os.getenv("MAX_OPEN_POSITIONS", "20")))
    ap.add_argument("--max-margin-utilization", type=float, default=float(os.getenv("MAX_MARGIN_UTILIZATION", "0.8")))
    ap.add_argument("--leverage", type=float, default=5.0,
                     help="допущение по плечу для оценки маржи (реальные ставки риска по инструментам не смоделированы)")
    ap.add_argument("--no-benchmarks", action="store_true",
                     help="не считать random-entry/buy&hold бенчмарки (только исходные сценарии фильтров)")
    ap.add_argument("--benchmark-scenario", default="фильтр объёма",
                     help="какой сценарий из DEFAULT_SCENARIOS считать 'реальной стратегией' для "
                          "сравнения с random-entry/buy&hold (по умолчанию — совпадает с боевым "
                          "min_volume_ratio=1.0 в scan_live_full.py)")
    ap.add_argument("--random-runs", type=int, default=30,
                     help="сколько независимых random-entry прогонов усреднять")
    ap.add_argument("--random-seed-base", type=int, default=0,
                     help="стартовый seed для random-entry прогонов (для воспроизводимости)")
    args = ap.parse_args()

    logger.info(
        "Портфельная симуляция: capital=%.0f risk_per_trade=%.1f%% max_positions=%d max_margin=%.0f%% leverage=x%.0f",
        args.capital, args.risk_per_trade * 100, args.max_positions, args.max_margin_utilization * 100, args.leverage,
    )

    rows = []
    for name, path in DEFAULT_SCENARIOS.items():
        if not path.exists():
            logger.warning("Пропуск сценария '%s' — файл не найден: %s", name, path)
            continue
        trades = load_trades(path)
        result = simulate_portfolio(
            trades, args.capital, args.risk_per_trade,
            args.max_positions, args.max_margin_utilization, args.leverage,
        )
        curve_path = OUT_DIR / f"portfolio_equity_{name.replace(' ', '_').replace('+', 'n')}.csv"
        pd.DataFrame(result["curve"], columns=["time", "equity"]).to_csv(curve_path, index=False)

        logger.info(
            "[%s] кандидатов=%d взято=%d (пропущено: лимит_позиций=%d лимит_маржи=%d) "
            "итог=%.0f₽ (%+.1f%%) CAGR=%+.1f%% макс.просадка(реализованная)=%.1f%%",
            name, result["total_candidates"], result["taken"],
            result["skipped_positions"], result["skipped_margin"],
            result["final_equity"], result["return_pct"], result["cagr_pct"], result["max_dd_pct"],
        )
        rows.append({"сценарий": name, **{k: v for k, v in result.items() if k != "curve"}})

    if not rows:
        logger.error("Ни один сценарий не найден — сначала прогони backtest_ema921.py с нужными --tag")
        return 2

    summary = pd.DataFrame(rows)
    print("\n" + "=" * 100)
    print(f"ПОРТФЕЛЬНАЯ СИМУЛЯЦИЯ — старт {args.capital:.0f}₽, риск {args.risk_per_trade*100:.0f}%/сделку, "
          f"лимит {args.max_positions} позиций, маржа <= {args.max_margin_utilization*100:.0f}%, плечо x{args.leverage:.0f}")
    print("=" * 100)
    for _, r in summary.iterrows():
        print(
            f"{r['сценарий']:<28} взято {r['taken']:>4}/{r['total_candidates']:<4} "
            f"(пропущено поз={r['skipped_positions']:>4} маржа={r['skipped_margin']:>4})  "
            f"итог={r['final_equity']:>12,.0f}₽  доход={r['return_pct']:>+9.1f}%  "
            f"CAGR={r['cagr_pct']:>+8.1f}%  просадка={r['max_dd_pct']:>5.1f}%"
        )
    print("=" * 100)
    summary_path = OUT_DIR / "portfolio_backtest_summary.csv"
    summary.drop(columns=["curve"], errors="ignore").to_csv(summary_path, index=False)
    print(f"Сводка сохранена: {summary_path}\n")

    if not args.no_benchmarks:
        run_three_way_comparison(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
