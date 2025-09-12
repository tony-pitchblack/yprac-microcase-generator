#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"

kill_by_pattern() {
  local pattern="$1"
  local pids
  pids=$(pgrep -f -- "$pattern" || true)
  if [ -n "${pids}" ]; then
    kill ${pids} || true
    sleep 1
    pids=$(pgrep -f -- "$pattern" || true)
    if [ -n "${pids}" ]; then
      kill -9 ${pids} || true
    fi
  fi
}

kill_by_pattern "pytasksyn-backend/main.py"
kill_by_pattern "telegram_frontend/telegram_bot.py"

nohup "$PYTHON_BIN" ./pytasksyn-backend/main.py >/dev/null 2>&1 &
nohup "$PYTHON_BIN" ./telegram_frontend/telegram_bot.py >/dev/null 2>&1 &

echo "Backend and frontend restarted."


