#!/bin/bash
set -e

cd /home/chick/sergey_trade_bridge

# Подхватываем переменные из .env
if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

source .venv/bin/activate

python -u auto_executor.py >> logs/auto_executor.log 2>&1
