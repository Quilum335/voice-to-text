#!/usr/bin/env sh
set -eu

TG_PID=""
BOT_PID=""

cleanup() {
  if [ -n "$BOT_PID" ]; then
    kill "$BOT_PID" 2>/dev/null || true
  fi
  if [ -n "$TG_PID" ]; then
    kill "$TG_PID" 2>/dev/null || true
  fi
}

trap cleanup INT TERM

LOCAL_TELEGRAM_API="$(printf '%s' "${USE_LOCAL_TELEGRAM_API:-0}" | tr '[:upper:]' '[:lower:]')"

if [ "$LOCAL_TELEGRAM_API" = "1" ] || [ "$LOCAL_TELEGRAM_API" = "true" ] || [ "$LOCAL_TELEGRAM_API" = "yes" ] || [ "$LOCAL_TELEGRAM_API" = "on" ]; then
  if [ -z "${TELEGRAM_API_ID:-}" ] || [ -z "${TELEGRAM_API_HASH:-}" ]; then
    echo "USE_LOCAL_TELEGRAM_API=1 requires TELEGRAM_API_ID and TELEGRAM_API_HASH" >&2
    exit 1
  fi

  TELEGRAM_WORK_DIR="${TELEGRAM_WORK_DIR:-/data/telegram-bot-api}"
  TELEGRAM_TEMP_DIR="${TELEGRAM_TEMP_DIR:-/tmp/telegram-bot-api}"
  mkdir -p "$TELEGRAM_WORK_DIR" "$TELEGRAM_TEMP_DIR"

  if [ "${AUTO_TELEGRAM_LOGOUT:-1}" = "1" ] && [ -n "${BOT_TOKEN:-}" ]; then
    python - "$BOT_TOKEN" <<'PY' || true
import json
import sys
import urllib.request

token = sys.argv[1]
url = f"https://api.telegram.org/bot{token}/logOut"
try:
    with urllib.request.urlopen(url, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    print(f"Official Telegram API logOut: {payload.get('ok')}")
except Exception as exc:
    print(f"Official Telegram API logOut skipped: {exc}")
PY
  fi

  telegram-bot-api \
    --api-id="$TELEGRAM_API_ID" \
    --api-hash="$TELEGRAM_API_HASH" \
    --local \
    --http-ip-address=127.0.0.1 \
    --http-port=8081 \
    --dir="$TELEGRAM_WORK_DIR" \
    --temp-dir="$TELEGRAM_TEMP_DIR" \
    --verbosity="${TELEGRAM_VERBOSITY:-2}" &
  TG_PID="$!"

  export TELEGRAM_API_BASE="http://127.0.0.1:8081"
  sleep 3
fi

python /app/main.py &
BOT_PID="$!"
wait "$BOT_PID"
cleanup
