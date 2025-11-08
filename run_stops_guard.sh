#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate
mkdir -p logs
# не стартуем, если уже держим lock
exec flock -n /tmp/stops_guard.lock \
  python -u stops_guard.py >> logs/stops_guard.log 2>&1

