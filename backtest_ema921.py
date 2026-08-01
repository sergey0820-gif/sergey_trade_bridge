#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
backtest_ema921.py

Событийный бэктест ТЕКУЩЕЙ живой стратегии (utils.ta.analyze_trade_setup:
EMA9/21 + RSI50, стоп 2×ATR, структурная swing-цель) — в отличие от
backtest_3m.py, который тестирует старую, другую версию стратегии
(D1-тренд + H4-триггер, TP=2R) и не отражает то, что реально торгуется.

Идея: для каждого тикера идём по H1-барам вперёд; на каждом баре, где нет
открытой виртуальной позиции, вызываем analyze_trade_setup на СРЕЗЕ данных
"по этот момент включительно" — точно так же, как это делает
scan_live_full.py в реальном времени (без заглядывания в будущее). Если
сетап найден — открываем позицию и дальше по барам симулируем выход:
- SL/TP хиты проверяются по high/low бара (не по close — иначе занизим
  вероятность срабатывания внутри бара);
- трейлинг-стопа — та же формула compute_new_sl_price из
  dynamic_stop_manager.py (переиспользуется напрямую, не копия);
- комиссия (config.COMMISSION_BPS_ROUNDTRIP) вычитается из каждого R.

Ограничения (честно, не скрываем):
- Один тикер — не более одной позиции одновременно (без пирамидинга).
- Капитал считается ОБЩИЙ по всем тикерам без учёта MAX_OPEN_POSITIONS/
  лимита маржи — то есть это верхняя оценка (в реальности часть сигналов
  могла бы не пройти из-за лимита одновременных позиций).
- LLM-барьер (llm_signal_reviewer.py) не симулируется — бэктест меряет
  только техническую часть (правила EMA/RSI/ATR/swing), не решение Claude.
- Проскальзывание не моделируется отдельно от комиссии.

Использование:
  python backtest_ema921.py --months 12 --max-tickers 60
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from dotenv import load_dotenv
from tinkoff.invest import CandleInterval, Client
from tinkoff.invest.exceptions import RequestError

BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
OUT_DIR = BASE_DIR / "out"
OUT_DIR.mkdir(exist_ok=True)
CANDLE_CACHE_DIR = BASE_DIR / "data_cache" / "candles"
CANDLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "backtest_ema921.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

sys.path.insert(0, str(BASE_DIR))
from utils.ta import analyze_trade_setup  # noqa: E402
from dynamic_stop_manager import compute_new_sl_price  # noqa: E402
from config import COMMISSION_BPS_ROUNDTRIP  # noqa: E402

ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH if ENV_PATH.exists() else None)

# Параметры трейлинга — те же дефолты, что в dynamic_stop_manager.py/.env
DYN_ACTIVATE_R = 1.0
DYN_TRAIL_START_R = 2.0
DYN_TRAIL_GAP_R = 0.5

H1_CHUNK_DAYS = 55  # API отдаёт H1 максимум ~60 дней за запрос
MIN_D1_BARS = 30
MIN_H1_BARS = 22
WARMUP_H1_BARS = 30  # даём EMA21/RSI14 на H1 устояться, прежде чем искать сигналы

# Кэш скачанных свечей на диске (data_cache/candles/) — чтобы прогонять разные
# стратегии на одной и той же истории без повторного скачивания с Tinkoff API
# (60 тикеров × 12 мес занимает ~30 мин только на GetCandles).
CACHE_MAX_AGE_HOURS = 12  # если последняя свеча кэша старше этого — считаем кэш неактуальным


@dataclass
class Trade:
    ticker: str
    class_code: str
    side: str
    entry_time: datetime
    entry: float
    initial_stop: float
    target: float
    target_source: str
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: str = ""
    r_multiple: float = 0.0


def candles_to_df(candles) -> pd.DataFrame:
    rows = []
    for c in candles:
        rows.append({
            "time": c.time.replace(tzinfo=timezone.utc),
            "open": c.open.units + c.open.nano / 1e9,
            "high": c.high.units + c.high.nano / 1e9,
            "low": c.low.units + c.low.nano / 1e9,
            "close": c.close.units + c.close.nano / 1e9,
            "volume": c.volume,
        })
    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df.sort_values("time", inplace=True)
    df.drop_duplicates(subset="time", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def _cache_path(ticker: str, class_code: str, interval: str) -> Path:
    return CANDLE_CACHE_DIR / f"{ticker}_{class_code}_{interval}.csv"


def _load_cache(path: Path) -> Optional[pd.DataFrame]:
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path)
        df["time"] = pd.to_datetime(df["time"], utc=True)
        return df
    except Exception:
        return None


def _save_cache(path: Path, df: pd.DataFrame) -> None:
    tmp = path.with_suffix(".tmp")
    df.to_csv(tmp, index=False)
    tmp.replace(path)


def _try_cache(cache_path: Optional[Path], needed_start: datetime, now: datetime) -> Optional[pd.DataFrame]:
    if cache_path is None:
        return None
    cached = _load_cache(cache_path)
    if cached is None or cached.empty:
        return None
    fresh_enough = (now - cached["time"].max()).total_seconds() < CACHE_MAX_AGE_HOURS * 3600
    covers_range = cached["time"].min() <= needed_start + timedelta(days=2)
    if fresh_enough and covers_range:
        return cached[cached["time"] >= needed_start].reset_index(drop=True)
    return None


def fetch_d1(client: Client, figi: str, days: int, ticker: Optional[str] = None,
             class_code: Optional[str] = None, use_cache: bool = True) -> pd.DataFrame:
    now = datetime.now(timezone.utc)
    needed_start = now - timedelta(days=days)
    cache_path = _cache_path(ticker, class_code, "D1") if (use_cache and ticker and class_code) else None
    cached = _try_cache(cache_path, needed_start, now)
    if cached is not None:
        logger.info("D1 %s:%s — из кэша (%d баров)", ticker, class_code, len(cached))
        return cached

    resp = client.market_data.get_candles(
        figi=figi, interval=CandleInterval.CANDLE_INTERVAL_DAY,
        from_=needed_start, to=now,
    )
    df = candles_to_df(resp.candles)
    if cache_path is not None and not df.empty:
        _save_cache(cache_path, df)
    return df


def fetch_h1(client: Client, figi: str, days: int, ticker: Optional[str] = None,
             class_code: Optional[str] = None, use_cache: bool = True) -> pd.DataFrame:
    """H1 максимум ~60 дней за запрос — тянем чанками и склеиваем."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    cache_path = _cache_path(ticker, class_code, "H1") if (use_cache and ticker and class_code) else None
    cached = _try_cache(cache_path, start, now)
    if cached is not None:
        logger.info("H1 %s:%s — из кэша (%d баров)", ticker, class_code, len(cached))
        return cached

    frames = []
    cursor = start
    while cursor < now:
        chunk_end = min(cursor + timedelta(days=H1_CHUNK_DAYS), now)
        try:
            resp = client.market_data.get_candles(
                figi=figi, interval=CandleInterval.CANDLE_INTERVAL_HOUR,
                from_=cursor, to=chunk_end,
            )
            frames.append(candles_to_df(resp.candles))
        except RequestError as e:
            logger.warning("H1 чанк %s..%s: ошибка %s", cursor.date(), chunk_end.date(), e)
        cursor = chunk_end
        time.sleep(0.15)  # анти-rate-limit пауза между чанками
    if not frames:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    df = pd.concat(frames, ignore_index=True)
    df.sort_values("time", inplace=True)
    df.drop_duplicates(subset="time", inplace=True)
    df.reset_index(drop=True, inplace=True)
    if cache_path is not None and not df.empty:
        _save_cache(cache_path, df)
    return df


def simulate_ticker(ticker: str, class_code: str, df_d1: pd.DataFrame, df_h1: pd.DataFrame, *,
                     require_d1_ema_trend: bool = False, min_volume_ratio: float = 0.0,
                     min_ema_gap_pct: float = 0.0) -> List[Trade]:
    trades: List[Trade] = []
    if len(df_h1) < WARMUP_H1_BARS + MIN_H1_BARS:
        return trades

    open_trade: Optional[Trade] = None
    current_stop = 0.0

    for i in range(WARMUP_H1_BARS, len(df_h1)):
        bar = df_h1.iloc[i]
        bar_time = bar["time"]

        if open_trade is not None:
            side = open_trade.side
            min_step = max(open_trade.entry * 0.0001, 0.01)
            risk_per_unit = abs(open_trade.entry - open_trade.initial_stop)

            # 1) проверяем хиты SL/TP по high/low бара (консервативно: если оба
            # в диапазоне бара — считаем, что стоп сработал первым)
            hit_stop = (bar["low"] <= current_stop) if side == "long" else (bar["high"] >= current_stop)
            hit_target = (bar["high"] >= open_trade.target) if side == "long" else (bar["low"] <= open_trade.target)

            if hit_stop:
                exit_price = current_stop
                reason = "stop"
            elif hit_target:
                exit_price = open_trade.target
                reason = "target"
            else:
                exit_price = None
                reason = ""

            if exit_price is not None:
                raw_r = (exit_price - open_trade.entry) / risk_per_unit if side == "long" else (open_trade.entry - exit_price) / risk_per_unit
                cost_r = (open_trade.entry * (COMMISSION_BPS_ROUNDTRIP / 10000.0)) / risk_per_unit
                open_trade.exit_time = bar_time
                open_trade.exit_price = exit_price
                open_trade.exit_reason = reason
                open_trade.r_multiple = raw_r - cost_r
                trades.append(open_trade)
                open_trade = None
                continue

            # 2) трейлинг — та же формула, что в dynamic_stop_manager.py
            new_sl = compute_new_sl_price(
                direction=side, entry=open_trade.entry, current=bar["close"],
                old_sl=current_stop, min_step=min_step,
                activate_r=DYN_ACTIVATE_R, trail_start_r=DYN_TRAIL_START_R,
                trail_gap_r=DYN_TRAIL_GAP_R,
            )
            if new_sl is not None:
                current_stop = new_sl
            continue

        # нет открытой позиции — ищем сигнал на срезе "по этот момент включительно"
        d1_slice = df_d1[df_d1["time"] <= bar_time]
        h1_slice = df_h1.iloc[: i + 1]
        if len(d1_slice) < MIN_D1_BARS or len(h1_slice) < MIN_H1_BARS:
            continue

        setup = analyze_trade_setup(
            d1_slice, h1_slice,
            require_d1_ema_trend=require_d1_ema_trend,
            min_volume_ratio=min_volume_ratio,
            min_ema_gap_pct=min_ema_gap_pct,
        )
        if not setup.side or not setup.entry or not setup.stop or not setup.target:
            continue

        open_trade = Trade(
            ticker=ticker, class_code=class_code, side=setup.side,
            entry_time=bar_time, entry=setup.entry, initial_stop=setup.stop,
            target=setup.target, target_source=setup.reason.split("target=")[-1],
        )
        current_stop = setup.stop

    return trades


def load_universe(path: Path, max_tickers: Optional[int]) -> List[tuple]:
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append((row["ticker"], row["class_code"]))
    if not max_tickers or max_tickers >= len(rows):
        return rows
    # Простой rows[:max_tickers] систематически недобирал SPBFUT (в
    # universe.csv тикеры вперемешку по алфавиту, и первые N строк почти
    # целиком состоят из TQBR) — round-robin по class_code, чтобы срез
    # представлял обе группы пропорционально их доле в полном universe.csv.
    by_class: Dict[str, List[tuple]] = {}
    for r in rows:
        by_class.setdefault(r[1], []).append(r)
    picked: List[tuple] = []
    idx = {c: 0 for c in by_class}
    while len(picked) < max_tickers:
        progressed = False
        for c, items in by_class.items():
            if len(picked) >= max_tickers:
                break
            if idx[c] < len(items):
                picked.append(items[idx[c]])
                idx[c] += 1
                progressed = True
        if not progressed:
            break
    return picked


def build_instrument_cache(client: Client) -> dict:
    cache = {}
    for s in client.instruments.shares().instruments:
        cache[(s.ticker, s.class_code)] = s.figi
    for f in client.instruments.futures().instruments:
        cache[(f.ticker, f.class_code)] = f.figi
    return cache


def main() -> int:
    ap = argparse.ArgumentParser(description="Бэктест текущей стратегии EMA9/21+RSI50")
    ap.add_argument("--months", type=int, default=12, help="глубина бэктеста в месяцах")
    ap.add_argument("--max-tickers", type=int, default=60, help="сколько тикеров из universe.csv брать")
    ap.add_argument("--universe-csv", default=str(BASE_DIR / "universe.csv"))
    ap.add_argument("--no-cache", action="store_true",
                     help="игнорировать кэш свечей в data_cache/candles/ и скачать заново")
    ap.add_argument("--require-d1-trend", action="store_true",
                     help="доп. фильтр: D1 close выше/ниже D1 EMA50 (не только RSI>50/<50)")
    ap.add_argument("--min-volume-ratio", type=float, default=0.0,
                     help="мин. volume_ratio_d1 для входа (0.0 = без фильтра; 1.0 — уже реальный фильтр)")
    ap.add_argument("--min-ema-gap-pct", type=float, default=0.0,
                     help="мин. разрыв EMA9/EMA21 на баре сигнала, доля цены (0 = без фильтра)")
    ap.add_argument("--tag", default="", help="суффикс для имени выходного CSV (сравнение A/B прогонов)")
    args = ap.parse_args()

    token = __import__("os").getenv("TINKOFF_TOKEN")
    if not token:
        logger.error("Не задан TINKOFF_TOKEN в .env")
        return 2

    warmup_days = 120  # запас для D1 RSI/EMA + 90-дневного окна swing-поиска
    total_days = args.months * 30 + warmup_days

    universe = load_universe(Path(args.universe_csv), args.max_tickers)
    logger.info("Бэктест: %d тикеров, %d месяцев (+%d дней прогрева)", len(universe), args.months, warmup_days)

    all_trades: List[Trade] = []

    with Client(token) as client:
        cache = build_instrument_cache(client)
        for idx, (ticker, class_code) in enumerate(universe, 1):
            figi = cache.get((ticker, class_code))
            if not figi:
                logger.warning("[%d/%d] %s:%s — не найден FIGI, пропуск", idx, len(universe), ticker, class_code)
                continue
            try:
                df_d1 = fetch_d1(client, figi, total_days, ticker=ticker, class_code=class_code,
                                  use_cache=not args.no_cache)
                df_h1 = fetch_h1(client, figi, total_days, ticker=ticker, class_code=class_code,
                                  use_cache=not args.no_cache)
            except Exception as e:
                logger.warning("[%d/%d] %s: ошибка загрузки свечей: %s", idx, len(universe), ticker, e)
                continue

            trades = simulate_ticker(
                ticker, class_code, df_d1, df_h1,
                require_d1_ema_trend=args.require_d1_trend,
                min_volume_ratio=args.min_volume_ratio,
                min_ema_gap_pct=args.min_ema_gap_pct,
            )
            all_trades.extend(trades)
            logger.info("[%d/%d] %s:%s — сделок найдено: %d", idx, len(universe), ticker, class_code, len(trades))
            time.sleep(0.1)

    # ---- сохраняем сделки ----
    suffix = f"_{args.tag}" if args.tag else ""
    trades_path = OUT_DIR / f"backtest_ema921_trades{suffix}.csv"
    with trades_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "class_code", "side", "entry_time", "entry", "stop", "target",
                    "target_source", "exit_time", "exit_price", "exit_reason", "r_multiple"])
        for t in all_trades:
            w.writerow([t.ticker, t.class_code, t.side, t.entry_time, t.entry, t.initial_stop,
                        t.target, t.target_source, t.exit_time, t.exit_price, t.exit_reason,
                        round(t.r_multiple, 4)])

    # ---- статистика ----
    closed = [t for t in all_trades if t.exit_time is not None]
    if not closed:
        logger.warning("Ни одной закрытой сделки за период — статистику посчитать не на чем")
        return 0

    wins = [t for t in closed if t.r_multiple > 0]
    losses = [t for t in closed if t.r_multiple <= 0]
    win_rate = len(wins) / len(closed)
    avg_r_win = sum(t.r_multiple for t in wins) / len(wins) if wins else 0.0
    avg_r_loss = sum(t.r_multiple for t in losses) / len(losses) if losses else 0.0
    expectancy_r = sum(t.r_multiple for t in closed) / len(closed)

    # макс. просадка в R (по кумулятивной сумме R в хронологическом порядке)
    closed_sorted = sorted(closed, key=lambda t: t.entry_time)
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for t in closed_sorted:
        cum += t.r_multiple
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    years = args.months / 12.0
    trades_per_year = len(closed) / years if years > 0 else 0
    risk_per_trade_pct = 2.0  # RISK_PER_TRADE=0.02 по умолчанию
    naive_annual_return_pct = trades_per_year * expectancy_r * risk_per_trade_pct

    swing_n = sum(1 for t in closed if t.target_source == "swing")
    fallback_n = sum(1 for t in closed if t.target_source == "fallback_3R")

    class_lines = []
    for cc in sorted({t.class_code for t in closed}):
        sub = [t for t in closed if t.class_code == cc]
        sub_wr = sum(1 for t in sub if t.r_multiple > 0) / len(sub) * 100
        sub_exp = sum(t.r_multiple for t in sub) / len(sub)
        class_lines.append(f"  {cc}: n={len(sub)} win_rate={sub_wr:.1f}% expectancy={sub_exp:+.3f}R")

    filters_desc = (
        f"require_d1_trend={args.require_d1_trend} "
        f"min_volume_ratio={args.min_volume_ratio} "
        f"min_ema_gap_pct={args.min_ema_gap_pct}"
    )

    summary = (
        f"\n{'=' * 60}\n"
        f"БЭКТЕСТ EMA9/21+RSI50 — {len(universe)} тикеров, {args.months} мес.\n"
        f"Фильтры: {filters_desc}\n"
        f"{'=' * 60}\n"
        f"Всего сделок закрыто: {len(closed)} (~{trades_per_year:.0f}/год)\n"
        f"Win rate: {win_rate * 100:.1f}%  ({len(wins)} побед / {len(losses)} убытков)\n"
        f"Средний R на победе: {avg_r_win:+.2f}   Средний R на убытке: {avg_r_loss:+.2f}\n"
        f"Экспектация на сделку: {expectancy_r:+.3f}R\n"
        f"Макс. просадка (в R): {max_dd:.2f}R\n"
        f"По классам:\n" + "\n".join(class_lines) + "\n"
        f"Цель: swing={swing_n} ({swing_n/len(closed)*100:.0f}%)  fallback_3R={fallback_n} ({fallback_n/len(closed)*100:.0f}%)\n"
        f"{'—' * 60}\n"
        f"Грубая оценка годовой доходности (без сложного процента, без\n"
        f"учёта MAX_OPEN_POSITIONS/лимита маржи, без LLM-барьера,\n"
        f"при риске {risk_per_trade_pct:.0f}% на сделку):\n"
        f"  {trades_per_year:.0f} сделок/год × {expectancy_r:+.3f}R × {risk_per_trade_pct:.0f}% "
        f"≈ {naive_annual_return_pct:+.1f}% годовых\n"
        f"{'=' * 60}\n"
        f"Сделки сохранены: {trades_path}\n"
    )
    logger.info(summary)
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
