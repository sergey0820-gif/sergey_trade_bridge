"""
Локальные тесты margin-aware sizing (DESIGN_margin_aware_sizing.md, 2026-08-13).
Мокает client.users.get_margin_attributes и instrument-объекты — не делает
реальных запросов к API, не размещает ордеров.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trade_executor import calc_quantity_from_risk, get_margin_headroom  # noqa: E402


# ---------------------------------------------------------------------------
# Часть 1: calc_quantity_from_risk() — юнит-тесты по плану п.в design-документа
# ---------------------------------------------------------------------------

def test_1_regression_shares_unchanged():
    """Акции: go_per_lot=None => побитово идентично старой формуле."""
    cases = [
        dict(entry=250.0, stop_price=245.0, lot_size=10, capital=15000, risk_per_trade=0.02),
        dict(entry=100.0, stop_price=98.0, lot_size=1, capital=50000, risk_per_trade=0.003),
        dict(entry=3000.0, stop_price=2950.0, lot_size=1, capital=200000, risk_per_trade=0.005),
    ]
    for c in cases:
        old_style = calc_quantity_from_risk(**c)  # go_per_lot/headroom не переданы (None по умолчанию)
        new_style = calc_quantity_from_risk(**c, go_per_lot=None, available_margin_headroom=None)
        assert old_style == new_style, f"Регрессия для акций: {c} -> old={old_style} new={new_style}"
    print("OK 1: акции (go_per_lot=None) — результат идентичен старой формуле")


def test_2_futures_go_sufficient_no_change():
    """Фьючерс, ГО с большим запасом => risk-based qty не меняется."""
    qty = calc_quantity_from_risk(
        entry=82.85, stop_price=84.6614, lot_size=1, capital=18570.97, risk_per_trade=0.0025,
        go_per_lot=100.0, available_margin_headroom=1_000_000.0,  # запас с огромным избытком
    )
    qty_baseline = calc_quantity_from_risk(
        entry=82.85, stop_price=84.6614, lot_size=1, capital=18570.97, risk_per_trade=0.0025,
    )
    assert qty == qty_baseline == 25, f"Ожидали 25 (как в реальном инциденте BRU6), получили qty={qty} baseline={qty_baseline}"
    print(f"OK 2: ГО с избытком — qty не урезан ({qty} лотов, совпадает с risk-based)")


def test_3_bru6_reproduction_insufficient_go():
    """
    Реконструкция реального инцидента BRU6 (2026-08-04, INVALID_ARGUMENT 30042
    'Not enough assets for a margin trade') — те же entry/stop/capital/risk,
    что были в реальном логе. Точное значение ГО BRU6 на тот момент не
    зафиксировано в логах (только сам факт отказа брокера) — здесь
    иллюстративный go_per_lot/headroom, воспроизводящий МЕХАНИЗМ (недостаточно
    маржи на посчитанный от риска объём), не точные исторические рубли ГО.
    Главное: новая логика должна отклонить ДО попытки размещения ордера.
    """
    qty = calc_quantity_from_risk(
        entry=82.85, stop_price=84.6614, lot_size=1, capital=18570.97, risk_per_trade=0.0025,
        go_per_lot=1000.0, available_margin_headroom=800.0,  # не хватает даже на 1 лот
    )
    assert qty == 0, f"BRU6-сценарий должен дать qty=0 (отказ ДО ордера), получили {qty}"
    print("OK 3: реконструкция BRU6 — недостаточно ГО даже на 1 лот => qty=0 (отказ до попытки ордера)")


def test_4_futures_go_partial_cap():
    """Фьючерс: risk-based qty=25 (как реальный BRU6), но ГО позволяет только 10."""
    risk_based_qty = calc_quantity_from_risk(
        entry=82.85, stop_price=84.6614, lot_size=1, capital=18570.97, risk_per_trade=0.0025,
    )
    assert risk_based_qty == 25
    go_per_lot = 1000.0
    headroom = 10_500.0  # ровно на 10 лотов с небольшим запасом
    qty = calc_quantity_from_risk(
        entry=82.85, stop_price=84.6614, lot_size=1, capital=18570.97, risk_per_trade=0.0025,
        go_per_lot=go_per_lot, available_margin_headroom=headroom,
    )
    assert qty == 10, f"Ожидали min(25, 10)=10, получили {qty}"
    print(f"OK 4: частичное урезание — risk-based=25, ГО позволяет 10 => qty={qty} (min сработал)")


def test_5_go_per_lot_invalid_failsafe():
    """go_per_lot<=0 (некорректные/нулевые данные) => fail-safe qty=0, не старая формула молча."""
    qty = calc_quantity_from_risk(
        entry=100.0, stop_price=98.0, lot_size=1, capital=50000, risk_per_trade=0.01,
        go_per_lot=0.0, available_margin_headroom=100_000.0,
    )
    assert qty == 0, f"go_per_lot=0 должен дать fail-safe qty=0, получили {qty}"
    qty2 = calc_quantity_from_risk(
        entry=100.0, stop_price=98.0, lot_size=1, capital=50000, risk_per_trade=0.01,
        go_per_lot=-5.0, available_margin_headroom=100_000.0,
    )
    assert qty2 == 0, f"Отрицательный go_per_lot должен дать fail-safe qty=0, получили {qty2}"
    print("OK 5: go_per_lot<=0 (нет валидных данных ГО) — fail-safe qty=0, не тихий откат на старую формулу")


def test_6_headroom_non_positive():
    """available_margin_headroom<=0 (счёт уже на пределе маржи) => qty=0, не отрицательное число."""
    qty = calc_quantity_from_risk(
        entry=100.0, stop_price=98.0, lot_size=1, capital=50000, risk_per_trade=0.01,
        go_per_lot=500.0, available_margin_headroom=0.0,
    )
    assert qty == 0, f"Нулевой запас маржи должен дать qty=0, получили {qty}"
    qty2 = calc_quantity_from_risk(
        entry=100.0, stop_price=98.0, lot_size=1, capital=50000, risk_per_trade=0.01,
        go_per_lot=500.0, available_margin_headroom=-200.0,
    )
    assert qty2 == 0, f"Отрицательный запас маржи должен дать qty=0 (не отрицательное число), получили {qty2}"
    print("OK 6: available_margin_headroom<=0 — qty=0, не отрицательное число и не деление на ноль")


def test_7_rounding_down():
    """available_margin_headroom // go_per_lot — дробный результат округляется ВНИЗ, не вверх."""
    # risk-based qty будет большим (капитал большой), ГО ограничит точно на грани
    qty = calc_quantity_from_risk(
        entry=100.0, stop_price=99.0, lot_size=1, capital=10_000_000, risk_per_trade=0.02,
        go_per_lot=1000.0, available_margin_headroom=9_980.0,  # 9.98 лота -> должно округлиться до 9, не 10
    )
    assert qty == 9, f"9980/1000=9.98 должно округлиться ВНИЗ до 9, получили {qty} (переоценка объёма — риск для реальных денег)"
    print(f"OK 7: округление вниз — 9980₽/1000₽ дало {qty} лотов (не 10) — недооценка безопасна, переоценка нет")


# ---------------------------------------------------------------------------
# Часть 2: get_margin_headroom() — формула с мок-клиентом
# ---------------------------------------------------------------------------

def test_8_get_margin_headroom_formula():
    client = MagicMock()
    # liquid_portfolio=100000, starting_margin=30000, max_margin_utilization=0.8
    # ожидаем: 100000*0.8 - 30000 = 50000
    client.users.get_margin_attributes.return_value = SimpleNamespace(
        liquid_portfolio=SimpleNamespace(units=100000, nano=0),
        starting_margin=SimpleNamespace(units=30000, nano=0),
    )
    headroom = get_margin_headroom(client, account_id="test-acc", max_margin_utilization=0.8)
    assert abs(headroom - 50000.0) < 0.01, f"Ожидали 50000.0, получили {headroom}"
    print(f"OK 8: get_margin_headroom() формула верна — {headroom}₽ (liquid=100000, starting=30000, cap=80%)")


def test_9_get_margin_headroom_propagates_exception():
    """Ошибка API НЕ должна тихо проглатываться внутри get_margin_headroom —
    вызывающий код (main()) отвечает за fail-safe решение."""
    client = MagicMock()
    client.users.get_margin_attributes.side_effect = Exception("сетевая ошибка")
    try:
        get_margin_headroom(client, account_id="test-acc", max_margin_utilization=0.8)
        raise AssertionError("Ожидали исключение, но его не было — fail-safe в main() не сработает")
    except Exception as e:
        assert "сетевая ошибка" in str(e)
        print("OK 9: исключение API прокидывается наружу (не проглатывается) — main() сможет применить fail-safe")


if __name__ == "__main__":
    test_1_regression_shares_unchanged()
    test_2_futures_go_sufficient_no_change()
    test_3_bru6_reproduction_insufficient_go()
    test_4_futures_go_partial_cap()
    test_5_go_per_lot_invalid_failsafe()
    test_6_headroom_non_positive()
    test_7_rounding_down()
    test_8_get_margin_headroom_formula()
    test_9_get_margin_headroom_propagates_exception()
    print("\nВСЕ ТЕСТЫ ПРОЙДЕНЫ (9/9)")
