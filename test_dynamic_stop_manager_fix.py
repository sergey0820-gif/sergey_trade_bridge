"""
Тесты фикса dynamic_stop_manager.py::compute_new_sl_price() (STRATEGY.md,
"Открытые вопросы" п.8б) — заморозка трейлинга на безубытке.

Не делает реальных запросов к API, не размещает ордеров. Кэш
initial_stop_cache.py тестируется через временный файл (не трогает
.state/initial_stop_prices.json проекта).
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dynamic_stop_manager  # noqa: E402
from dynamic_stop_manager import compute_new_sl_price, _append_stop_event  # noqa: E402
import initial_stop_cache  # noqa: E402


MIN_STEP = 0.01
ACTIVATE_R = 1.0
TRAIL_START_R = 2.0
TRAIL_GAP_R = 0.5


# ---------------------------------------------------------------------------
# Часть 1: воспроизведение бага и подтверждение фикса — LONG
# ---------------------------------------------------------------------------

def test_1_long_breakeven_then_continues_trailing():
    """
    Точное воспроизведение сценария ENPG: entry=100, initial SL=95 (риск=5).

    Шаг 1 — цена доходит до R=1.2, стоп переводится в безубыток (100).
    Шаг 2 — цена продолжает расти до R=3.0 (current=115). СТАРЫЙ баг:
    risk_per_unit считался от old_sl (=100 после шага 1) => entry<=old_sl
    (100<=100) => False positive "некорректные данные" => None, стоп
    замирает навсегда. НОВЫЙ фикс: risk_per_unit считается от initial_sl
    (=95, фиксирован) => трейлинг продолжается корректно.
    """
    entry, initial_sl = 100.0, 95.0

    # Шаг 1: R=1.2 -> перевод в безубыток
    new_sl_1 = compute_new_sl_price(
        direction="long", entry=entry, current=106.0, old_sl=95.0,
        min_step=MIN_STEP, activate_r=ACTIVATE_R, trail_start_r=TRAIL_START_R,
        trail_gap_r=TRAIL_GAP_R, initial_sl=initial_sl,
    )
    assert new_sl_1 == 100.0, f"Шаг 1: ожидали перевод в безубыток (100.0), получили {new_sl_1}"
    print(f"OK 1.1: шаг 1 (R=1.2) — SL переведён в безубыток: {new_sl_1}")

    # Шаг 2: old_sl теперь = результат шага 1 (100.0), цена выросла дальше,
    # R=3.0 >= trail_start_r — трейлинг ДОЛЖЕН продолжиться, не замереть.
    new_sl_2 = compute_new_sl_price(
        direction="long", entry=entry, current=115.0, old_sl=new_sl_1,
        min_step=MIN_STEP, activate_r=ACTIVATE_R, trail_start_r=TRAIL_START_R,
        trail_gap_r=TRAIL_GAP_R, initial_sl=initial_sl,
    )
    assert new_sl_2 is not None, "БАГ ВОСПРОИЗВЕДЁН: трейлинг замер на безубытке (вернул None)"
    assert new_sl_2 == 112.5, f"Ожидали трейлинг до 112.5 (115-2.5 gap), получили {new_sl_2}"
    assert new_sl_2 > new_sl_1, "Новый SL должен быть строго лучше предыдущего"
    print(f"OK 1.2: шаг 2 (R=3.0) — трейлинг ПРОДОЛЖИЛСЯ (не замер): {new_sl_1} -> {new_sl_2}")


def test_2_long_old_buggy_behavior_would_freeze():
    """
    Явно подтверждаем, ЧТО ИМЕННО было сломано: если бы risk_per_unit
    считался от old_sl (старая логика), шаг 2 обязан был бы вернуть None.
    Проверяем через initial_sl=None + old_sl уже на безубытке (эмулирует
    старое поведение без исправления).
    """
    new_sl = compute_new_sl_price(
        direction="long", entry=100.0, current=115.0, old_sl=100.0,
        min_step=MIN_STEP, activate_r=ACTIVATE_R, trail_start_r=TRAIL_START_R,
        trail_gap_r=TRAIL_GAP_R, initial_sl=None,  # имитирует старую логику
    )
    assert new_sl is None, "Ожидали воспроизведение старого бага (None) без initial_sl при old_sl==entry"
    print("OK 2: без initial_sl и с old_sl==entry — старое поведение (заморозка) воспроизведено, как и ожидалось")


# ---------------------------------------------------------------------------
# Часть 2: то же самое для SHORT — не предполагаем симметрию, проверяем явно
# ---------------------------------------------------------------------------

def test_3_short_breakeven_then_continues_trailing():
    """Зеркальный сценарий для short: entry=100, initial SL=105 (риск=5)."""
    entry, initial_sl = 100.0, 105.0

    # Шаг 1: цена падает до current=94, R=(100-94)/5=1.2 -> безубыток
    new_sl_1 = compute_new_sl_price(
        direction="short", entry=entry, current=94.0, old_sl=105.0,
        min_step=MIN_STEP, activate_r=ACTIVATE_R, trail_start_r=TRAIL_START_R,
        trail_gap_r=TRAIL_GAP_R, initial_sl=initial_sl,
    )
    assert new_sl_1 == 100.0, f"Шаг 1 (short): ожидали безубыток (100.0), получили {new_sl_1}"
    print(f"OK 3.1: short, шаг 1 (R=1.2) — SL переведён в безубыток: {new_sl_1}")

    # Шаг 2: цена падает дальше до 85, R=(100-85)/5=3.0 >= trail_start_r
    new_sl_2 = compute_new_sl_price(
        direction="short", entry=entry, current=85.0, old_sl=new_sl_1,
        min_step=MIN_STEP, activate_r=ACTIVATE_R, trail_start_r=TRAIL_START_R,
        trail_gap_r=TRAIL_GAP_R, initial_sl=initial_sl,
    )
    assert new_sl_2 is not None, "БАГ ВОСПРОИЗВЕДЁН (short): трейлинг замер на безубытке"
    assert new_sl_2 == 87.5, f"Ожидали трейлинг до 87.5 (85+2.5 gap), получили {new_sl_2}"
    assert new_sl_2 < new_sl_1, "Для short новый SL должен быть строго ниже (лучше) предыдущего"
    print(f"OK 3.2: short, шаг 2 (R=3.0) — трейлинг ПРОДОЛЖИЛСЯ: {new_sl_1} -> {new_sl_2}")


def test_4_short_old_buggy_behavior_would_freeze():
    new_sl = compute_new_sl_price(
        direction="short", entry=100.0, current=85.0, old_sl=100.0,
        min_step=MIN_STEP, activate_r=ACTIVATE_R, trail_start_r=TRAIL_START_R,
        trail_gap_r=TRAIL_GAP_R, initial_sl=None,
    )
    assert new_sl is None, "Ожидали воспроизведение старого бага (None) для short"
    print("OK 4: short без initial_sl и с old_sl==entry — заморозка воспроизведена, как и ожидалось")


# ---------------------------------------------------------------------------
# Часть 3: регрессия — поведение БЕЗ изменений там, где старая логика была верна
# ---------------------------------------------------------------------------

def test_5_regression_not_yet_moved_matches_old_behavior():
    """
    Если SL ещё ни разу не двигался (old_sl == исходный SL), передача
    initial_sl=None (симулирует легаси-позицию без записи в кэше) должна
    дать РОВНО ТОТ ЖЕ результат, что и передача initial_sl=old_sl явно —
    для этого случая старая логика была корректна, фикс её не должен ломать.
    """
    kwargs = dict(
        direction="long", entry=100.0, current=106.0, old_sl=95.0,
        min_step=MIN_STEP, activate_r=ACTIVATE_R, trail_start_r=TRAIL_START_R,
        trail_gap_r=TRAIL_GAP_R,
    )
    result_without_cache = compute_new_sl_price(**kwargs, initial_sl=None)
    result_with_explicit = compute_new_sl_price(**kwargs, initial_sl=95.0)
    assert result_without_cache == result_with_explicit == 100.0
    print(f"OK 5: регрессия — ещё не сдвинутый SL даёт идентичный результат с/без кэша ({result_without_cache})")


def test_6_no_activation_below_threshold_unchanged():
    """R < activate_r — ничего не делаем, как и раньше (не задето фиксом)."""
    new_sl = compute_new_sl_price(
        direction="long", entry=100.0, current=100.5, old_sl=95.0,
        min_step=MIN_STEP, activate_r=ACTIVATE_R, trail_start_r=TRAIL_START_R,
        trail_gap_r=TRAIL_GAP_R, initial_sl=95.0,
    )
    assert new_sl is None
    print("OK 6: R < activate_r — по-прежнему None, поведение не изменилось")


# ---------------------------------------------------------------------------
# Часть 4: кэш initial_stop_cache.py — round-trip, перезапись, отсутствие записи
# ---------------------------------------------------------------------------

def test_7_cache_round_trip(tmp_path):
    initial_stop_cache.STATE_DIR = tmp_path
    initial_stop_cache.CACHE_PATH = tmp_path / "initial_stop_prices.json"

    assert initial_stop_cache.get_initial_sl("uid-1") is None, "Пустой кэш должен вернуть None"

    initial_stop_cache.record_initial_sl("uid-1", 95.0, "long")
    assert initial_stop_cache.get_initial_sl("uid-1") == 95.0
    print("OK 7.1: запись/чтение из кэша работает")

    # Перезапись — новая успешная постановка стопа для того же uid
    initial_stop_cache.record_initial_sl("uid-1", 98.0, "long")
    assert initial_stop_cache.get_initial_sl("uid-1") == 98.0, "Повторная запись должна перезаписывать значение"
    print("OK 7.2: перезапись для существующего uid работает (новая защита той же позиции)")

    assert initial_stop_cache.get_initial_sl("uid-nonexistent") is None
    print("OK 7.3: отсутствующий uid — None, не исключение")


def test_8_cache_prunes_old_entries(tmp_path):
    from datetime import datetime, timedelta, timezone
    initial_stop_cache.STATE_DIR = tmp_path
    initial_stop_cache.CACHE_PATH = tmp_path / "initial_stop_prices.json"

    old_ts = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    fresh_ts = datetime.now(timezone.utc).isoformat()
    initial_stop_cache.save_cache({
        "uid-old": {"initial_sl": 10.0, "direction": "long", "recorded_at": old_ts},
        "uid-fresh": {"initial_sl": 20.0, "direction": "long", "recorded_at": fresh_ts},
    })
    reloaded = initial_stop_cache.load_cache()
    assert "uid-old" not in reloaded, "Запись старше CACHE_MAX_AGE_DAYS должна быть вычищена"
    assert "uid-fresh" in reloaded, "Свежая запись не должна пострадать"
    print("OK 8: старые записи (>60 дней) вычищаются при save_cache, свежие остаются")


# ---------------------------------------------------------------------------
# Часть 5: структурированный журнал событий (logs/dynamic_stop_events.csv) —
# план наблюдения за фиксом на живых данных
# ---------------------------------------------------------------------------

def test_9_event_log_round_trip(tmp_path):
    """_append_stop_event пишет корректный CSV с заголовком при первой
    записи, дозаписывает без заголовка при последующих."""
    events_path = tmp_path / "dynamic_stop_events.csv"
    dynamic_stop_manager.EVENTS_LOG_PATH = events_path

    _append_stop_event(
        ticker="ENPG", class_code="TQBR", direction="long", uid="uid-1",
        stage="breakeven", old_sl=95.0, new_sl=100.0, entry=100.0,
        initial_sl=95.0, initial_sl_source="cached", post_breakeven=False,
    )
    _append_stop_event(
        ticker="ENPG", class_code="TQBR", direction="long", uid="uid-1",
        stage="trail", old_sl=100.0, new_sl=112.5, entry=100.0,
        initial_sl=95.0, initial_sl_source="cached", post_breakeven=True,
    )

    lines = events_path.read_text(encoding="utf-8").strip().split("\n")
    assert lines[0] == dynamic_stop_manager.EVENTS_LOG_HEADER.strip()
    assert len(lines) == 3, f"Ожидали заголовок + 2 события, получили {len(lines)} строк"
    assert ",breakeven," in lines[1] and lines[1].endswith(",0")
    assert ",trail," in lines[2] and lines[2].endswith(",1")
    print("OK 9: dynamic_stop_events.csv — заголовок пишется один раз, события дозаписываются, post_breakeven=1 на втором (доказательство фикса)")


def test_10_event_log_unrecoverable_skip_has_empty_new_sl():
    """Событие 'не могу восстановить исходный риск' пишется с пустым
    new_sl (движения не было) — отличимо в CSV от реальных движений."""
    import tempfile as _tempfile
    with _tempfile.TemporaryDirectory() as td:
        events_path = Path(td) / "events.csv"
        dynamic_stop_manager.EVENTS_LOG_PATH = events_path
        _append_stop_event(
            ticker="XYZ", class_code="SPBFUT", direction="short", uid="uid-2",
            stage="unrecoverable_skip", old_sl=50.0, new_sl=None, entry=50.0,
            initial_sl=None, initial_sl_source="missing", post_breakeven=True,
        )
        line = events_path.read_text(encoding="utf-8").strip().split("\n")[1]
        fields = line.split(",")
        assert fields[5] == "unrecoverable_skip"
        assert fields[7] == "", "new_sl должен быть пустым — движения стопа не было"
        assert fields[9] == "", "initial_sl должен быть пустым — он неизвестен"
        assert fields[11] == "1", "unrecoverable_skip по определению post_breakeven=1"
    print("OK 10: unrecoverable_skip пишется с пустыми new_sl/initial_sl и post_breakeven=1, отличим от реальных движений")


if __name__ == "__main__":
    test_1_long_breakeven_then_continues_trailing()
    test_2_long_old_buggy_behavior_would_freeze()
    test_3_short_breakeven_then_continues_trailing()
    test_4_short_old_buggy_behavior_would_freeze()
    test_5_regression_not_yet_moved_matches_old_behavior()
    test_6_no_activation_below_threshold_unchanged()

    with tempfile.TemporaryDirectory() as td:
        test_7_cache_round_trip(Path(td))
    with tempfile.TemporaryDirectory() as td:
        test_8_cache_prunes_old_entries(Path(td))
    with tempfile.TemporaryDirectory() as td:
        test_9_event_log_round_trip(Path(td))
    test_10_event_log_unrecoverable_skip_has_empty_new_sl()

    print("\nВСЕ ТЕСТЫ ПРОЙДЕНЫ (10/10)")
