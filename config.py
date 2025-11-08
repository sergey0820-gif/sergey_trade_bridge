from __future__ import annotations
import os

# === Комиссии/издержки ===
# 0.08% round-trip (т.е. 8 bps на круг)
COMMISSION_BPS_ROUNDTRIP: float = float(os.getenv('COMMISSION_BPS_ROUNDTRIP', '8'))  # bps
# Доп. “микроиздержки” на проскальзывание — по желанию
SLIPPAGE_BPS: float = float(os.getenv('SLIPPAGE_BPS', '5'))

# === Риск-менеджмент ===
DEFAULT_RISK: float = float(os.getenv('DEFAULT_RISK', '0.005'))  # 0.5% на сделку по умолчанию

# === Пути ===
OUT_DIR = os.path.abspath(os.getenv('OUT_DIR', './out'))
LOGS_DIR = os.path.abspath(os.getenv('LOGS_DIR', './logs'))

# === Учёт окружения / permissions ===
ALLOW_PLACE = os.getenv('ALLOW_PLACE', 'false').lower() in {'1','true','yes','on'}

# === Вспомогательное ===
def bps_to_pct(bps: float) -> float:
    return bps / 10000.0

