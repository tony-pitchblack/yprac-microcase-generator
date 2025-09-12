#!/usr/bin/env bash
set -euo pipefail

kill_by_pattern() {
  local pattern="$1"
  local pids
  pids=$(pgrep -f -- "$pattern" || true)
  if [ -n "$pids" ]; then
    kill $pids || true
    sleep 1
    pids=$(pgrep -f -- "$pattern" || true)
    if [ -n "$pids" ]; then
      kill -9 $pids || true
    fi
  fi
}

kill_by_pattern "pytasksyn-backend/main.py"
kill_by_pattern "telegram_frontend/telegram_bot.py"


