> **Происхождение этого файла** (добавлено 2026-08-13, ниже — оригинал без
> изменений): найден на raspberrypi (`/mnt/ssd/sergey_trade_bridge/CLAUDE.md`)
> как осиротевший, никогда не отслеживаемый git файл — не совпадал с
> версией `CLAUDE.md`, закоммиченной в `origin/main` (короткая политика
> доступа к raspberrypi). Датирован 2026-08-02 (mtime), не встречается ни в
> одном коммите ни на одной ветке. Судя по содержанию — написан отдельной
> Claude Code сессией, работавшей прямо на Pi, документирует архитектуру
> пайплайна и содержит находку про баг `dynamic_stop_manager.py`,
> впоследствии подтверждённую и исправленную (см. STRATEGY.md, "Открытые
> вопросы" п.8). Сохранён здесь целиком, дословно, без изменений — только
> этот преамбула добавлена.
>
> Текст ниже, начиная с `# CLAUDE.md` — оригинал 1:1, включая собственный
> (устаревший) заголовок.

---

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal, cron/nohup-driven algo-trading bot for the **Tinkoff Invest** broker (Russian market: TQBR shares and SPBFUT futures). It scans a universe of tickers for EMA/RSI setups, pushes signals to Telegram, and (optionally) places real entry orders and stop-loss/take-profit orders through the Tinkoff Invest API. **This is live-money trading infrastructure, not a toy project** — treat order-placement code with the same care as production financial code.

There is no build step, no test suite, and no package manager beyond pip. Scripts are run directly with the venv's Python, individually or via the shell wrappers in the repo root.

## Environment & running things

```bash
source .venv/bin/activate          # the project venv (also see venv/, .venv312/ — .venv is the active one)
pip install -r requirements.txt    # minimal direct deps
pip install -r requirements.lock   # full pinned/resolved set actually used in production
```

Configuration is entirely through `.env` (see `.env.example` for the full variable list — Tinkoff tokens, Telegram tokens, risk %, capital, `DRY_RUN`, `ALLOW_PLACE`, rate-limit knobs, etc.). `config.py` centralizes a few cross-cutting settings (commission/slippage bps, default risk, `OUT_DIR`/`LOGS_DIR`, `ALLOW_PLACE`) read from env at import time.

**Two safety gates control real trading and are checked throughout the codebase:**
- `DRY_RUN=true` — compute/log only, never call the order-placement API.
- `ALLOW_PLACE=false` — disables the Telegram "Place" button / order placement path.
Always check how a script reads these before assuming a change is safe to test against the live account.

Orchestration (no process manager/systemd unit files in-repo — these are the entry points):
- `./run_all.sh` — kills any previous instances, then starts `telegram_bridge.py`, a loop of `scan_live_full.py` + `postprocess_candidates.py` (period `SIGNAL_RESCAN_SECS`), and `stops_guard.py`, each under `flock` with a PID file in `logs/`.
- `./run_stop_manager.sh` — one-shot run of `stop_manager.py` (meant to be cron'd).
- `./run_stops_guard.sh` — one-shot `stops_guard.py` run, flock-guarded.
- `./tgctl.sh {start|stop|status}` — start/stop/status for `telegram_bridge.py` specifically.

There is no automated test suite. `test_ta_live.py` and `test_post_stop_order.py` are manual smoke scripts, not pytest — note that **`test_post_stop_order.py` places a real stop order against the live account** when run; don't execute it casually. `scripts/test_post_order_safe.py` is similar. There's no lint config committed (a stale `.ruff_cache/` exists but no `pyproject.toml`/`ruff.toml`) and no CI.

## Pipeline architecture

The system is a chain of independent scripts that communicate via CSV files and small JSON state files on disk (no database, no message queue). Understanding the file handoffs is the key to understanding the codebase:

```
universe_builder.py / build_universe.py
        │  writes universe.csv (ticker, class_code, liquidity/ATR filters)
        ▼
scan_live_full.py  ──uses──▶ utils/ta.py (analyze_trade_setup: EMA 9/21 cross + RSI-50 filter, D1 trend / H1 entry, 2×ATR stop, 1:3 R:R target)
        │  writes candidates.csv
        ▼
postprocess_candidates.py ──▶ out/*.enriched.csv (adds live price, deviation-from-entry %, HOLD/PASS/SKIP verdict)
ai_filter_agent.py        ──▶ candidates_ai.csv (adds AI score/verdict; NEVER overwrites candidates.csv)
positions_guard.py        ──▶ filters candidates_ai.csv, dropping tickers already held (via instrument_uid)
        ▼
telegram_bridge.py  — reads candidates.csv or candidates_ai.csv (AI_STRICT_ONLY), sends Telegram messages
                       with inline "Place / Details" buttons, dedupes, respects TTL/deviation/trading-hours filters
        │  user taps "Place"
        ▼
trade_executor.py  — sizes qty from risk %, normalizes SL<entry<TP (or reverse for short),
                      places the entry order via trade_utils/orders.py (post_order_safe / post_order_safe_sync),
                      appends the row to pending_stops.csv
        ▼
stop_manager.py  /  stops_guard.py  — poll pending_stops.csv (or out/pending_stops.csv for stops_guard),
                      match to actual broker positions, place SL/TP via StopOrdersService
                      (trade_utils/price_helper.py: place_stop_order), avoid duplicate stops,
                      alert to Telegram if a position sits >1h with no stop (.state/stop_alerts.json)
        ▼
dynamic_stop_manager.py  — separate, opt-in (DYNAMIC_STOPS_APPLY=1) trailing-stop mover;
                            never creates a new stop, only tightens an existing SL toward breakeven/trail
```

Supporting/reporting scripts that read but don't feed the main chain: `ai_positions_agent.py` (portfolio summary to Telegram), `ai_weekly_report.py` (stats from `candidates_ai_log.csv`), `export_operations.py`, `show_positions.py`, `inspect_positions.py`.

Key conventions:
- **All order placement goes through `trade_utils/orders.py`** (`post_order_safe`/`post_order_safe_sync`) rather than calling `client.orders.post_order(...)` directly — it normalizes `instrument_id`/`figi`, coerces `quantity` to `int`, and generates an idempotent `order_id`. Follow this pattern for any new order-placing code (see commit `6807b2d`).
- Stop-loss/take-profit orders always go through `StopOrdersService.post_stop_order`, never through the regular orders service.
- Price conversion between the Tinkoff `Quotation` type and `float`/`Decimal` is a recurring pattern (`quotation_to_float`/`float_to_quotation` in `trade_executor.py`, `_to_quotation`/`_quantize_price` in `trade_utils/price_helper.py`, `to_q` in `stops_guard.py`) — several near-duplicate implementations exist; check which one a given script already uses before adding another.
- CSV files are the inter-process contract. When changing a script that reads or writes one of `universe.csv`, `candidates.csv`, `candidates_ai.csv`, or `pending_stops.csv`, check every other script touching that same file — column names/order are not enforced by any shared schema.
- State/lock files live in `.state/`, `state/`, and root-level `.lock.*`/`*.pid` files; these are gitignored runtime artifacts, not source.

## Known issue: dynamic_stop_manager.py freezes the trailing stop at breakeven

Found 2026-08-02 while porting this trailing-stop pattern to a sibling project
(`denmark_trade_bridge`, Saxo Bank). Believed to be a real bug, not intentional
behavior — flagging here rather than silently "fixing" it, since this is
live-money code and the fix needs a deliberate decision, not a drive-by patch.

`compute_new_sl_price()` computes R off the **current** (already-moved) stop,
not the original risk at entry:

```python
if direction == "long":
    if entry <= old_sl:
        return None
    risk_per_unit = entry - old_sl   # <-- old_sl is whatever the stop currently IS
    profit = current - entry
```

Once stage 1 (breakeven) moves the stop so that `old_sl == entry`, the very
next call hits `entry <= old_sl` (now an equality) and returns `None`
immediately — before stage 2 (trailing) is ever evaluated. So a position that
reaches breakeven then keeps running never gets a trailing stop at all; it
just sits with `SL == entry` no matter how far R grows afterwards. This would
only be masked in practice if a position typically resolves (hits TP or a
manual/other exit) before `dynamic_stop_manager.py` gets called again after
breakeven — worth checking against real trade history whether this has
actually been costing missed trailing-stop upside, or whether some other
mechanism prevents the stale-breakeven state from being observed.

The `denmark_trade_bridge` port (`strategy/signal.py::compute_trailing_stop`)
computes R off the **fixed initial stop** instead, specifically to avoid this
freeze. If this project's version gets revisited, that's the reference fix —
but do not port it back here without deciding this is actually a bug and
testing the change against live-relevant scenarios first.

## Repo hygiene notes

This repo accumulates a lot of manual timestamped backups from ad hoc editing sessions: `*.bak`, `*.bak.<timestamp>`, `*_backup_YYYY-MM-DD*.py`, `archive/`, `archives/*.tar.gz`. These are not part of the active codebase and should generally be ignored when reading code for context — always check which file is actually imported/executed (e.g. `stop_manager.py`, not `stop_manager_backup_2025-11-06.py.bak`). Don't assume a `.bak` file next to a script reflects the current design; diff against git history instead if you need to understand recent changes.
