#!/usr/bin/env bash
set -euo pipefail
BASE="$HOME/sergey_trade_bridge"
DST="$HOME/backups/sergey_trade_bridge_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$DST"
rsync -a --delete "$BASE/out/"  "$DST/out/"
rsync -a --delete "$BASE/logs/" "$DST/logs/"
echo "Backup done to $DST"
