#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

LOGS="logs"; mkdir -p "$LOGS"
source .venv/bin/activate

# Единственные lock-файлы на процессы
TG_LOCK="/tmp/sergey_tg.lock"
SCAN_LOCK="/tmp/sergey_scan.lock"
STOP_LOCK="/tmp/sergey_stops.lock"

# 1) Гасим старые процессы (если остались)
pkill -f 'telegram_bridge.py' 2>/dev/null || true
pkill -f 'scan_live_full.py'  2>/dev/null || true
pkill -f 'stops_guard.py'     2>/dev/null || true
sleep 0.5

# 2) Telegram bot — один экземпляр
#    НИКАКИХ while true; просто один nohup с flock
( flock -n 200 || exit 0
  nohup python -u telegram_bridge.py > "$LOGS/telegram.log" 2>&1 &
  echo $! > "$LOGS/telegram.pid"
) 200>"$TG_LOCK"

# 3) Сканер сигналов — цикл внутри nohup (один процесс), с постобработкой
#    Период берём из SIGNAL_RESCAN_SECS (по умолчанию 1800 сек)
( flock -n 201 || exit 0
  nohup bash -lc '
    while true; do
      cd ~/sergey_trade_bridge
      source .venv/bin/activate
      python -u scan_live_full.py || true
      python -u postprocess_candidates.py || true
      sleep ${SIGNAL_RESCAN_SECS:-1800}
    done
  ' > "$LOGS/scan.log" 2>&1 &
  echo $! > "$LOGS/scan.pid"
) 201>"$SCAN_LOCK"

# 4) Сторож стопов — один экземпляр
( flock -n 202 || exit 0
  nohup python -u stops_guard.py > "$LOGS/stops_guard.log" 2>&1 &
  echo $! > "$LOGS/stops_guard.pid"
) 202>"$STOP_LOCK"

echo "Started. PIDs:"
cat "$LOGS/telegram.pid"
cat "$LOGS/scan.pid"
cat "$LOGS/stops_guard.pid"
