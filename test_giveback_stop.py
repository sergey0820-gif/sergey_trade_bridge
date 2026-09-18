"""
Тесты give-back-защиты (dynamic_stop_manager.py, STRATEGY.md "Открытые
вопросы" п.9) — закрытие позиции, если она дошла до пика прибыли и
откатилась от него больше чем на порог ("зашёл уверенно, потом потух,
развернулся").

Не делает реальных запросов к API, не размещает ордеров. peak_r_cache
тестируется через временный файл (не трогает .state/peak_r_prices.json
проекта).
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import peak_r_cache  # noqa: E402
from dynamic_stop_manager import compute_giveback_exit_price  # noqa: E402

MIN_STEP = 0.01


# ---------------------------------------------------------------------------
# compute_giveback_exit_price — чистая функция, без API
# ---------------------------------------------------------------------------

def test_long_moves_sl_to_just_below_current():
    """Long: entry=100, old_sl уже в безубытке (100), цена сейчас 108 —
    give-back должен подтянуть SL к 107.99 (на 1 шаг цены безопаснее рынка)."""
    price = compute_giveback_exit_price("long", current=108.0, old_sl=100.0, min_step=MIN_STEP)
    assert price is not None
    assert abs(price - 107.99) < 1e-9
    assert price > 100.0  # строже старого SL


def test_short_moves_sl_to_just_above_current():
    """Short: entry=100, old_sl в безубытке (100), цена сейчас 92 —
    give-back должен подтянуть SL к 92.01."""
    price = compute_giveback_exit_price("short", current=92.0, old_sl=100.0, min_step=MIN_STEP)
    assert price is not None
    assert abs(price - 92.01) < 1e-9
    assert price < 100.0


def test_never_widens_risk_long():
    """Если получившаяся цена не строже старого SL (например old_sl уже
    подтянут трейлингом ближе к рынку, чем даёт give-back) — None, риск не
    расширяем."""
    price = compute_giveback_exit_price("long", current=108.0, old_sl=107.995, min_step=MIN_STEP)
    assert price is None


def test_never_widens_risk_short():
    price = compute_giveback_exit_price("short", current=92.0, old_sl=92.005, min_step=MIN_STEP)
    assert price is None


def test_invalid_direction():
    assert compute_giveback_exit_price("sideways", current=100.0, old_sl=95.0, min_step=MIN_STEP) is None


def test_invalid_min_step():
    assert compute_giveback_exit_price("long", current=100.0, old_sl=95.0, min_step=0.0) is None


# ---------------------------------------------------------------------------
# peak_r_cache — персистентность пика, через временный файл
# ---------------------------------------------------------------------------

def _with_temp_cache(fn):
    orig_path = peak_r_cache.CACHE_PATH
    orig_dir = peak_r_cache.STATE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        peak_r_cache.STATE_DIR = Path(tmp)
        peak_r_cache.CACHE_PATH = Path(tmp) / "peak_r_prices.json"
        try:
            fn()
        finally:
            peak_r_cache.STATE_DIR = orig_dir
            peak_r_cache.CACHE_PATH = orig_path


def test_peak_r_tracks_maximum():
    def run():
        uid = "test-uid-1"
        assert peak_r_cache.get_peak_r(uid) == 0.0
        assert peak_r_cache.update_peak_r(uid, 0.8) == 0.8
        assert peak_r_cache.update_peak_r(uid, 1.5) == 1.5
        # просадка НЕ должна снижать сохранённый пик
        assert peak_r_cache.update_peak_r(uid, 0.3) == 1.5
        assert peak_r_cache.get_peak_r(uid) == 1.5
    _with_temp_cache(run)


def test_peak_r_clear_resets_for_next_position():
    def run():
        uid = "test-uid-2"
        peak_r_cache.update_peak_r(uid, 2.0)
        assert peak_r_cache.get_peak_r(uid) == 2.0
        peak_r_cache.clear_peak_r(uid)
        assert peak_r_cache.get_peak_r(uid) == 0.0
    _with_temp_cache(run)


# ---------------------------------------------------------------------------
# Сценарий целиком: воспроизведение "зашёл уверенно - потух - развернулся"
# ---------------------------------------------------------------------------

def test_scenario_give_back_triggers_at_50pct_retrace_from_peak():
    """
    Long, entry=100, initial_sl=95 (risk=5). Цена доходит до 101.5 (R=0.3,
    ниже порога GIVEBACK_MIN_PEAK_R=0.5 — ещё не отслеживаем), затем до 103
    (R=0.6, пик зафиксирован), затем откатывается до 101.5 (R=0.3) — это
    50%-й откат от пика 0.6R, должно сработать.
    """
    GIVEBACK_MIN_PEAK_R = 0.5
    GIVEBACK_FRAC = 0.5
    entry, initial_sl, risk = 100.0, 95.0, 5.0
    uid = "test-uid-scenario"

    def run():
        for current, expect_trigger in [(101.5, False), (103.0, False), (101.5, True)]:
            current_r = (current - entry) / risk
            peak_r = peak_r_cache.update_peak_r(uid, current_r)
            triggered = peak_r >= GIVEBACK_MIN_PEAK_R and current_r <= peak_r * (1 - GIVEBACK_FRAC)
            assert triggered == expect_trigger, (
                f"current={current} current_r={current_r:.2f} peak_r={peak_r:.2f} "
                f"triggered={triggered} expected={expect_trigger}"
            )
    _with_temp_cache(run)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} тестов прошли")
    sys.exit(1 if failed else 0)
