#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
llm_signal_reviewer.py

STRATEGY.md: "LLM-проверка сигналов" — второй, независимый барьер после
ai_filter_agent.py (формальные правила на Python, без ИИ).

Берёт кандидатов, уже одобренных правилами (decision=PASS/verdict=approve
в candidates_ai.csv), и для каждого вызывает Claude API в роли количественного
риск-ревьюера. Модели передаются ТОЛЬКО уже посчитанные числа сетапа
(side, entry, stop, target, rsi_d1, rsi_h4, volume_ratio, pattern,
risk:reward, оценка/причины формальных правил) — ни тикер, ни имя компании,
ни сектор не передаются, чтобы модель не могла подмешать какие-либо
"знания" о конкретной бумаге вместо анализа предоставленных цифр. Никакого
поиска новостей не выполняется.

Ответ модели — строго JSON {"decision": "approve"/"reject", "reasoning": "..."}.
При сбое вызова (таймаут/ошибка API) или невалидном JSON в ответе —
decision=reject (безопасное поведение по умолчанию), с отдельной пометкой
в логе, что это техническая ошибка, а не содержательное решение модели.

Выход:
  - candidates_llm_approved.csv — только approved-строки (полная
    перезапись каждый запуск, как candidates.csv/candidates_ai.csv).
  - logs/llm_review.log — запись КАЖДОГО решения (approve и reject).

auto_executor.py читает candidates_llm_approved.csv, а не candidates_ai.csv
напрямую — вход в реальный ордер требует одобрения и правил, и LLM.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

import anthropic
from dotenv import load_dotenv

from signal_journal import append_journal_row

BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

CANDIDATES_AI = BASE_DIR / "candidates_ai.csv"
CANDIDATES_LLM_APPROVED = BASE_DIR / "candidates_llm_approved.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "llm_review.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

ENV_PATH = BASE_DIR / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
LLM_REVIEW_MODEL = os.getenv("LLM_REVIEW_MODEL", "claude-haiku-4-5-20251001")
LLM_REVIEW_TIMEOUT_SEC = float(os.getenv("LLM_REVIEW_TIMEOUT_SEC", "15"))

OUTPUT_COLUMNS = [
    "ticker", "class_code", "side", "entry", "stop", "target", "rsi_d1", "rsi_h4",
    "volume_ratio", "pattern", "timestamp", "asset_class", "name", "sector", "uid",
    "score", "decision", "verdict", "reasons", "flags",
    "llm_decision", "llm_reasoning",
]

SYSTEM_PROMPT = """Ты — количественный риск-ревьюер торговых сигналов для алгоритмической
торговой системы на Московской бирже.

Тебе присылают JSON с уже посчитанными характеристиками ОДНОГО торгового
сигнала, который уже прошёл формальный скоринг по правилам (rules_score,
rules_reasons).

ОГРАНИЧЕНИЯ (обязательны к соблюдению):
- Анализируй ТОЛЬКО поля, присланные в JSON: side, entry, stop, target,
  rsi_d1, rsi_h4, volume_ratio, pattern, risk_reward, rules_score, rules_reasons.
- Тебе намеренно НЕ сообщают тикер, название компании, сектор или любую
  другую идентифицирующую информацию. Не пытайся угадать, о какой бумаге
  идёт речь, и не используй никакие "общие знания" о рынке, компаниях,
  новостях или макроэкономике — их здесь просто не может быть релевантно,
  так как у тебя нет доступа к актуальным новостям.
- Не выдумывай данные, которых нет в JSON.

ЗАДАЧА: оцени внутреннюю согласованность и качество сетапа как второй,
независимый барьер после формальных правил — например, разумны ли
risk_reward и rules_score вместе, не противоречат ли rsi_d1/rsi_h4 друг
другу и стороне сделки (side), адекватен ли volume_ratio для заявленного
pattern. Если сомневаешься — отклоняй (в этой системе цена ошибки — реальные
деньги, безопасность важнее пропуска хорошего сигнала).

Ответь СТРОГО в виде JSON, без markdown-обёртки (без ```), без каких-либо
слов вне JSON, ровно в этом формате:
{"decision": "approve", "reasoning": "<1-2 предложения на русском>"}
или
{"decision": "reject", "reasoning": "<1-2 предложения на русском>"}
"""

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def fnum(x, default=None):
    try:
        if x is None:
            return default
        if isinstance(x, str) and x.strip() == "":
            return default
        return float(x)
    except Exception:
        return default


def compute_rr(entry, stop, target, side) -> Optional[float]:
    """Та же формула, что ai_filter_agent.py::rr() — R:R по сторонам сделки."""
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


def build_payload(row: dict) -> dict:
    """
    Только уже посчитанные числа сетапа. Намеренно НЕ включает ticker,
    class_code, name, sector, uid — модель не должна иметь возможность
    опереться на "знание" о конкретной бумаге вместо анализа цифр.
    """
    entry = fnum(row.get("entry"))
    stop = fnum(row.get("stop"))
    target = fnum(row.get("target"))
    side = (row.get("side") or "").strip().lower()
    return {
        "side": side,
        "entry": entry,
        "stop": stop,
        "target": target,
        "rsi_d1": fnum(row.get("rsi_d1")),
        "rsi_h4": fnum(row.get("rsi_h4")),
        "volume_ratio": fnum(row.get("volume_ratio")),
        "pattern": row.get("pattern", ""),
        "risk_reward": compute_rr(entry, stop, target, side),
        "rules_score": fnum(row.get("score")),
        "rules_reasons": row.get("reasons", ""),
    }


def extract_json(text: str) -> dict:
    cleaned = _JSON_FENCE_RE.sub("", text).strip()
    return json.loads(cleaned)


def review_candidate(client: anthropic.Anthropic, payload: dict) -> tuple[str, str]:
    """
    Возвращает (decision, reasoning). Одна попытка, без ретраев.
    При любом сбое — ("reject", "<пометка сбоя>"), не исключение наружу.
    """
    try:
        resp = client.messages.create(
            model=LLM_REVIEW_MODEL,
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            timeout=LLM_REVIEW_TIMEOUT_SEC,
        )
    except Exception as e:
        logger.warning("LLM call failed: %s", e)
        return "reject", "LLM call failed - defaulting to reject"

    raw_text = "".join(
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    )

    try:
        parsed = extract_json(raw_text)
        decision = str(parsed.get("decision", "")).strip().lower()
        reasoning = str(parsed.get("reasoning", "")).strip()
        if decision not in ("approve", "reject"):
            raise ValueError(f"unexpected decision value: {decision!r}")
        return decision, reasoning
    except Exception as e:
        logger.warning("LLM response was not valid JSON: %s | raw=%r", e, raw_text[:300])
        return "reject", "LLM response was not valid JSON - defaulting to reject"


def read_approved_from_rules() -> list[dict]:
    if not CANDIDATES_AI.exists():
        logger.info("candidates_ai.csv не найден — нечего проверять")
        return []
    with CANDIDATES_AI.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    approved = [
        r for r in rows
        if (r.get("decision") or "").strip().upper() == "PASS"
        and (r.get("verdict") or "").strip().lower() == "approve"
    ]
    logger.info("candidates_ai.csv: всего=%d, approve(правила)=%d", len(rows), len(approved))
    return approved


def write_output(rows: list[dict]) -> None:
    with CANDIDATES_LLM_APPROVED.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c, "") for c in OUTPUT_COLUMNS})


def main() -> int:
    logger.info("=== llm_signal_reviewer.py START ===")

    if not ANTHROPIC_API_KEY:
        logger.error(
            "Не задан ANTHROPIC_API_KEY в .env — это ошибка конфигурации, "
            "а не сбой вызова. Останов без обработки кандидатов."
        )
        return 2

    candidates = read_approved_from_rules()
    if not candidates:
        write_output([])
        logger.info("Нет кандидатов, прошедших правила — candidates_llm_approved.csv пуст")
        logger.info("=== llm_signal_reviewer.py END ===")
        return 0

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    approved_rows: list[dict] = []
    approve_count = 0
    reject_count = 0
    fail_count = 0

    for row in candidates:
        ticker = row.get("ticker", "?")
        class_code = row.get("class_code", "?")
        payload = build_payload(row)

        decision, reasoning = review_candidate(client, payload)
        is_failure = reasoning.startswith("LLM call failed") or reasoning.startswith(
            "LLM response was not valid JSON"
        )
        if is_failure:
            fail_count += 1

        logger.info(
            "%s %s side=%s | decision=%s%s | reasoning=%s",
            ticker, class_code, payload["side"],
            decision, " [СБОЙ ВЫЗОВА, не решение модели]" if is_failure else "",
            reasoning,
        )

        row_out = dict(row)
        row_out["llm_decision"] = decision
        row_out["llm_reasoning"] = reasoning

        if decision == "approve":
            approve_count += 1
            approved_rows.append(row_out)
        else:
            reject_count += 1
            # approve идёт дальше в auto_executor.py и журналируется там —
            # reject здесь финальный, журналируем сразу
            append_journal_row({
                "ts": row.get("timestamp", ""),
                "ticker": ticker, "class_code": class_code, "side": payload["side"],
                "entry": payload["entry"], "stop": payload["stop"], "target": payload["target"],
                "rsi_d1": payload["rsi_d1"], "rsi_h4": payload["rsi_h4"],
                "volume_ratio": payload["volume_ratio"], "pattern": payload["pattern"],
                "rules_score": payload["rules_score"], "rules_decision": "PASS",
                "rules_reasons": payload["rules_reasons"],
                "llm_decision": decision, "llm_reasoning": reasoning,
                "final_status": "rejected_by_llm", "final_reason": reasoning,
            })

    write_output(approved_rows)

    logger.info(
        "[OK] approved=%d/%d (rejected=%d, technical_failures=%d) -> %s",
        approve_count, len(candidates), reject_count, fail_count, CANDIDATES_LLM_APPROVED,
    )
    logger.info("=== llm_signal_reviewer.py END ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
