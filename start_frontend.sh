#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"

nohup "$PYTHON_BIN" ./telegram_frontend/telegram_bot.py >/dev/null 2>&1 &


