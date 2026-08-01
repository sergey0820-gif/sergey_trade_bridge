#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ai_filter_agent.py — AI-фильтр кандидатов (без затирания candidates.csv)

Задача:
- прочитать candidates.csv (сырой выход сканера)
- посчитать "AI-скор" и verdict (approve/borderline/reject)
- записать:
  - out/candidates_ai.csv (полная версия)
  - candidates_ai.csv (копия в корне проекта для telegram_bridge)
- НЕ переписывать candidates.csv (это критично)

Формат candidates.csv (ожидаем):
ticker,class_code,side,entry,stop,target,rsi_d1,rsi_h4,volume_ratio,pattern,timestamp

Формат candidates_ai.csv (как у тебя уже закреплено):
ticker,class_code,side,entry,stop,target,rsi_d1,rsi_h4,volume_ratio,pattern,timestamp,
asset_class,name,sector,uid,score,decision,verdict,reasons,flags
"""

from __future__ import annotations

import os
import sys
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

import pandas as pd
from dotenv import load_dotenv

from signal_journal import append_journal_row


# ----------------------------
# Утилиты логирования
# ----------------------------

def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} | {msg}", flush=True)


def fnum(x, default=None):
    try:
        if x is None:
            return default
        if isinstance(x, str) and x.strip() == "":
            return default
        return float(x)
    except Exception:
        return default


def strbool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    v = v.strip().lower()
    return v in ("1", "true", "yes", "y", "on")


# ----------------------------
# Настройки из .env
# ----------------------------

def get_env_config() -> dict:
    # если нужно “жестко” — поднимем порог PASS, но логика одна
    strict_only = strbool(os.getenv("AI_STRICT_ONLY"), default=False)

    # Порог по score
    # В strict режиме чуть выше (чтобы шло меньше мусора)
    min_pass = fnum(os.getenv("AI_MIN_SCORE_PASS"), 2.6 if strict_only else 2.2)
    min_borderline = fnum(os.getenv("AI_MIN_SCORE_BORDERLINE"), 2.1 if strict_only else 1.8)

    # Минимальные объемы (volume_ratio) — ключевой фильтр
    # Можно подстраивать отдельно для лонга/шорта
    vol_min_long = fnum(os.getenv("AI_VOL_MIN_LONG"), 1.10 if strict_only else 0.90)
    vol_min_short = fnum(os.getenv("AI_VOL_MIN_SHORT"), 1.20 if strict_only else 1.00)

    # Шорты по акциям (обычно нельзя/сложно) — по умолчанию запрещаем
    allow_short_shares = strbool(os.getenv("AI_ALLOW_SHORT_SHARES"), default=False)

    # Доп. “мягкая” проверка R:R (если есть entry/stop/target)
    min_rr = fnum(os.getenv("AI_MIN_RR"), 1.4)

    # Пути
    in_path = os.getenv("AI_IN_CSV", "candidates.csv")
    out_dir = os.getenv("AI_OUT_DIR", "out")
    out_ai = os.getenv("AI_OUT_AI_CSV", "candidates_ai.csv")

    return {
        "strict_only": strict_only,
        "min_pass": float(min_pass),
        "min_borderline": float(min_borderline),
        "vol_min_long": float(vol_min_long),
        "vol_min_short": float(vol_min_short),
        "allow_short_shares": allow_short_shares,
        "min_rr": float(min_rr),
        "in_path": in_path,
        "out_dir": out_dir,
        "out_ai": out_ai,
    }


# ----------------------------
# Скорая оценка качества сетапа
# ----------------------------

def rr(entry: float | None, stop: float | None, target: float | None, side: str | None) -> float | None:
    if entry is None or stop is None or target is None or side is None:
        return None
    if side == "long":
        risk = entry - stop
        reward = target - entry
    else:
        risk = stop - entry
        reward = entry - target
    if risk <= 0:
        return None
    return reward / risk


def score_candidate(row: pd.Series, cfg: dict) -> Tuple[float, str, str, str]:
    """
    Возвращает: (score, decision, verdict, reasons, flags)
    decision: PASS / BORDERLINE / REJECT
    verdict: approve / borderline / reject
    """
    reasons: List[str] = []
    flags: List[str] = []

    ticker = str(row.get("ticker", "")).strip()
    class_code = str(row.get("class_code", "")).strip()
    side = str(row.get("side", "")).strip().lower()
    pattern = str(row.get("pattern", "")).strip().lower()

    # asset_class по class_code (если нет явно)
    asset_class = row.get("asset_class")
    asset_class = (str(asset_class).strip().lower() if asset_class is not None else "")
    if not asset_class:
        if class_code == "SPBFUT":
            asset_class = "future"
        elif class_code == "TQBR":
            asset_class = "share"
        else:
            asset_class = ""

    entry = fnum(row.get("entry"))
    stop = fnum(row.get("stop"))
    target = fnum(row.get("target"))
    rsi_d1 = fnum(row.get("rsi_d1"))
    rsi_h4 = fnum(row.get("rsi_h4"))
    vol_ratio = fnum(row.get("volume_ratio"), 1.0)

    s = 0.0

    # 0) Базовые sanity checks
    if side not in ("long", "short"):
        reasons.append("bad_side")
        return (0.0, "REJECT", "reject", ";".join(reasons) + ";", ";".join(flags))

    # 1) Шорт по акциям (если запрещено)
    if asset_class == "share" and side == "short" and not cfg["allow_short_shares"]:
        reasons.append("short_shares_disabled")
        flags.append("policy_short_shares")
        # не сразу 0, но фактически будет REJECT
        s -= 3.0

    # 2) Паттерн
    # Поддерживаем “pullback”/“breakout” (ты просил откат/пробой) + совместимость с тем что уже есть
    if pattern in ("pullback", "otkat", "rollback"):
        s += 1.2
        reasons.append("pattern_pullback")
    elif pattern in ("breakout", "proboy"):
        s += 1.2
        reasons.append("pattern_breakout")
    elif pattern in ("hammer", "engulfing", "bullish_engulfing", "bearish_engulfing"):
        s += 0.9
        reasons.append("pattern_classic")
    elif pattern in ("", "none", "nan"):
        s -= 0.8
        reasons.append("pattern_missing")
    else:
        # неизвестный паттерн — не валим, но скромно
        s += 0.3
        reasons.append("pattern_other")

    # 3) RSI логика (простая)
    # long лучше при rsi_d1 40-65, short лучше при 35-60 (сверху вниз)
    if rsi_d1 is not None:
        if side == "long":
            if 40 <= rsi_d1 <= 65:
                s += 0.8
                reasons.append("rsi_d1_ok")
            elif rsi_d1 < 35:
                s += 0.4
                reasons.append("rsi_d1_oversold")
            elif rsi_d1 > 70:
                s -= 0.8
                reasons.append("rsi_d1_overbought")
        else:
            if 35 <= rsi_d1 <= 60:
                s += 0.6
                reasons.append("rsi_d1_ok")
            elif rsi_d1 > 70:
                s += 0.4
                reasons.append("rsi_d1_overbought")
            elif rsi_d1 < 30:
                s -= 0.6
                reasons.append("rsi_d1_too_low_for_short")

    if rsi_h4 is not None:
        # небольшой вес для младшего ТФ
        if side == "long" and rsi_h4 >= 50:
            s += 0.4
            reasons.append("rsi_tf_ok")
        elif side == "short" and rsi_h4 <= 50:
            s += 0.4
            reasons.append("rsi_tf_ok")
        else:
            s -= 0.2
            reasons.append("rsi_tf_weak")

    # 4) Объём (главный фактор)
    if side == "long":
        if vol_ratio >= cfg["vol_min_long"]:
            s += 1.1
            reasons.append("vol_ok")
        else:
            # низкий объём — сильный минус
            s -= 1.3
            reasons.append(f"vol_ratio<{cfg['vol_min_long']}")
    else:
        if vol_ratio >= cfg["vol_min_short"]:
            s += 1.1
            reasons.append("vol_ok")
        else:
            s -= 1.4
            reasons.append(f"vol_ratio<{cfg['vol_min_short']}")

    # 5) R:R (если есть)
    rr_val = rr(entry, stop, target, side)
    if rr_val is not None:
        if rr_val >= cfg["min_rr"]:
            s += 0.8
            reasons.append(f"rr_ok({rr_val:.2f})")
        else:
            s -= 1.0
            reasons.append(f"rr_low({rr_val:.2f})")
    else:
        reasons.append("rr_na")

    # 6) Финальное решение
    if s >= cfg["min_pass"]:
        decision = "PASS"
        verdict = "approve"
    elif s >= cfg["min_borderline"]:
        decision = "BORDERLINE"
        verdict = "borderline"
    else:
        decision = "REJECT"
        verdict = "reject"

    # Сбор строк
    reasons_s = ";".join(reasons) + ";"
    flags_s = ";".join(flags) + (";" if flags else "")

    return (float(s), decision, verdict, reasons_s, flags_s)


# ----------------------------
# Основной пайплайн
# ----------------------------

def main() -> int:
    load_dotenv()
    cfg = get_env_config()

    project = Path.cwd()
    in_path = (project / cfg["in_path"]).resolve()
    out_dir = (project / cfg["out_dir"]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    out_ai_root = (project / cfg["out_ai"]).resolve()
    out_ai_outdir = (out_dir / cfg["out_ai"]).resolve()

    log("=== ai_filter_agent.py START ===")
    log(f"in={in_path} out={out_ai_outdir} overwrite=False (IMPORTANT: candidates.csv will NOT be modified)")

    if not in_path.exists():
        log(f"[WARN] input not found: {in_path}")
        # все равно “тачнем” файлы чтобы telegram_bridge не паниковал
        out_ai_outdir.write_text("", encoding="utf-8")
        out_ai_root.write_text("", encoding="utf-8")
        log("=== ai_filter_agent.py END (no input) ===")
        return 0

    try:
        df = pd.read_csv(in_path)
    except Exception as e:
        log(f"[ERROR] failed to read {in_path}: {e}")
        return 2

    if df.empty:
        log("[OK] candidates.csv empty -> write empty candidates_ai.csv")
        df_out = pd.DataFrame(columns=[
            "ticker","class_code","side","entry","stop","target","rsi_d1","rsi_h4","volume_ratio","pattern","timestamp",
            "asset_class","name","sector","uid","score","decision","verdict","reasons","flags"
        ])
        df_out.to_csv(out_ai_outdir, index=False)
        shutil.copyfile(out_ai_outdir, out_ai_root)
        log("=== ai_filter_agent.py END ===")
        return 0

    # гарантируем нужные колонки
    for col in ["asset_class","name","sector","uid"]:
        if col not in df.columns:
            df[col] = ""

    # считаем скор
    scores = []
    decisions = []
    verdicts = []
    reasons_list = []
    flags_list = []

    pass_count = 0
    for _, row in df.iterrows():
        s, decision, verdict, reasons, flags = score_candidate(row, cfg)
        scores.append(s)
        decisions.append(decision)
        verdicts.append(verdict)
        reasons_list.append(reasons)
        flags_list.append(flags)
        if decision == "PASS":
            pass_count += 1

    df_out = df.copy()
    df_out["score"] = scores
    df_out["decision"] = decisions
    df_out["verdict"] = verdicts
    df_out["reasons"] = reasons_list
    df_out["flags"] = flags_list

    # порядок колонок как у тебя в candidates_ai.csv
    cols = [
        "ticker","class_code","side","entry","stop","target","rsi_d1","rsi_h4","volume_ratio","pattern","timestamp",
        "asset_class","name","sector","uid","score","decision","verdict","reasons","flags"
    ]
    for c in cols:
        if c not in df_out.columns:
            df_out[c] = ""
    df_out = df_out[cols]

    # журналируем всё, что дальше правил не прошло (approve идёт дальше, в LLM,
    # и журналируется уже там/в auto_executor.py — сигнал пишем ровно один раз)
    for _, r in df_out.iterrows():
        if r["verdict"] != "approve":
            append_journal_row({
                "ts": r.get("timestamp", ""),
                "ticker": r.get("ticker", ""), "class_code": r.get("class_code", ""),
                "side": r.get("side", ""), "entry": r.get("entry", ""), "stop": r.get("stop", ""),
                "target": r.get("target", ""), "rsi_d1": r.get("rsi_d1", ""), "rsi_h4": r.get("rsi_h4", ""),
                "volume_ratio": r.get("volume_ratio", ""), "pattern": r.get("pattern", ""),
                "rules_score": r.get("score", ""), "rules_decision": r.get("decision", ""),
                "rules_reasons": r.get("reasons", ""),
                "final_status": "borderline_by_rules" if r["decision"] == "BORDERLINE" else "rejected_by_rules",
                "final_reason": r.get("reasons", ""),
            })

    # пишем файлы
    df_out.to_csv(out_ai_outdir, index=False)
    shutil.copyfile(out_ai_outdir, out_ai_root)

    # touch (обновим mtime)
    now = datetime.now().timestamp()
    os.utime(out_ai_root, (now, now))
    os.utime(out_ai_outdir, (now, now))

    log(f"[OK] written: {out_ai_outdir} + {out_ai_root} | PASS={pass_count}/{len(df_out)}")
    log("=== ai_filter_agent.py END ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

