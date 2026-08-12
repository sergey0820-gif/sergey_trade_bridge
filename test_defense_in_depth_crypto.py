"""
Локальный тест defense-in-depth крипто-фильтра в auto_executor.py.
Мокает client.instruments.future_by — не делает реальных запросов к API.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from auto_executor import _is_crypto_futures_candidate  # noqa: E402


def make_future_by_mock(basic_asset, ticker, name):
    resp = SimpleNamespace(instrument=SimpleNamespace(basic_asset=basic_asset, ticker=ticker, name=name))
    return MagicMock(return_value=resp)


def test_share_candidate_never_checked():
    client = MagicMock()
    row = {"class_code": "TQBR", "ticker": "SBER", "uid": "some-uid"}
    result = _is_crypto_futures_candidate(client, row)
    assert result is False, "Акция не должна доходить до проверки вообще"
    client.instruments.future_by.assert_not_called()
    print("OK: акция (TQBR) — не проверяется, всегда False, future_by не вызывается")


def test_crypto_futures_candidate_blocked():
    """Искусственный крипто-тикер — как SOLUSDperpA/BTU6."""
    client = MagicMock()
    client.instruments.future_by = make_future_by_mock(
        basic_asset="Индекс Bitcoin", ticker="BTZ9", name="BTC-12.29 Bitcoin"
    )
    row = {"class_code": "SPBFUT", "ticker": "BTZ9", "uid": "fake-crypto-uid"}
    result = _is_crypto_futures_candidate(client, row)
    assert result is True, "Синтетический крипто-фьючерс ДОЛЖЕН быть отсечён"
    print("OK: синтетический крипто-фьючерс (basic_asset='Индекс Bitcoin') — отсечён (True)")


def test_normal_futures_candidate_passes():
    client = MagicMock()
    client.instruments.future_by = make_future_by_mock(
        basic_asset="GAZP", ticker="GZZ6", name="GAZP-12.26 Газпром"
    )
    row = {"class_code": "SPBFUT", "ticker": "GZZ6", "uid": "fake-gazp-uid"}
    result = _is_crypto_futures_candidate(client, row)
    assert result is False, "Обычный фьючерс на акцию НЕ должен отсекаться"
    print("OK: обычный фьючерс (GAZP) — проходит (False)")


def test_missing_uid_fail_open():
    client = MagicMock()
    row = {"class_code": "SPBFUT", "ticker": "XXX", "uid": ""}
    result = _is_crypto_futures_candidate(client, row)
    assert result is False, "Без uid — fail-open (не блокируем), это доп. слой, не единственный"
    client.instruments.future_by.assert_not_called()
    print("OK: нет uid — fail-open (False), future_by не вызывается")


def test_api_error_fail_open():
    client = MagicMock()
    client.instruments.future_by.side_effect = Exception("сетевая ошибка API")
    row = {"class_code": "SPBFUT", "ticker": "YYY", "uid": "some-uid"}
    result = _is_crypto_futures_candidate(client, row)
    assert result is False, "Ошибка API — fail-open (не блокируем)"
    print("OK: ошибка API при запросе future_by — fail-open (False)")


if __name__ == "__main__":
    test_share_candidate_never_checked()
    test_crypto_futures_candidate_blocked()
    test_normal_futures_candidate_passes()
    test_missing_uid_fail_open()
    test_api_error_fail_open()
    print("\nВСЕ ТЕСТЫ ПРОЙДЕНЫ")
