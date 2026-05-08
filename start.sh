#!/usr/bin/env sh
set -eu

TG_PID=""
BOT_PID=""
MONITOR_PID=""

cleanup() {
  if [ -n "$MONITOR_PID" ]; then
    kill "$MONITOR_PID" 2>/dev/null || true
  fi
  if [ -n "$BOT_PID" ]; then
    kill "$BOT_PID" 2>/dev/null || true
  fi
  if [ -n "$TG_PID" ]; then
    kill "$TG_PID" 2>/dev/null || true
  fi
}

trap cleanup EXIT INT TERM

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
import os
import ssl
import sys
import urllib.request

token = sys.argv[1]
url = f"https://api.telegram.org/bot{token}/logOut"
try:
    context = None
    if os.getenv("TELEGRAM_SSL_VERIFY", "1").strip().lower() in {"0", "false", "no", "off"}:
        context = ssl._create_unverified_context()
    with urllib.request.urlopen(url, timeout=20, context=context) as response:
        payload = json.loads(response.read().decode("utf-8"))
    print(f"Official Telegram API logOut: {payload.get('ok')}")
except Exception as exc:
    print(f"Official Telegram API logOut skipped: {exc}")
PY
  fi

  echo "Starting local Telegram Bot API on 127.0.0.1:8081"
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

  echo "Waiting for local Telegram Bot API to become ready"
  READY="0"
  WAITED="0"
  READY_TIMEOUT="${TELEGRAM_API_READY_TIMEOUT:-120}"
  while [ "$WAITED" -lt "$READY_TIMEOUT" ]; do
    if ! kill -0 "$TG_PID" 2>/dev/null; then
      echo "Local Telegram Bot API exited before it became ready" >&2
      wait "$TG_PID" || true
      exit 1
    fi

    if python - "$BOT_TOKEN" <<'PY'
import json
import sys
import urllib.request

token = sys.argv[1]
url = f"http://127.0.0.1:8081/bot{token}/getMe"
try:
    with urllib.request.urlopen(url, timeout=2) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("ok"):
        print("Local Telegram Bot API is ready")
        raise SystemExit(0)
except Exception:
    pass
raise SystemExit(1)
PY
    then
      READY="1"
      break
    fi

    sleep 2
    WAITED=$((WAITED + 2))
  done

  if [ "$READY" != "1" ]; then
    echo "Local Telegram Bot API was not ready after ${READY_TIMEOUT}s" >&2
    exit 1
  fi
fi

echo "Starting transcriber bot"
python /app/main.py &
BOT_PID="$!"

if [ -n "$TG_PID" ]; then
  (
    while kill -0 "$BOT_PID" 2>/dev/null; do
      if ! kill -0 "$TG_PID" 2>/dev/null; then
        echo "Local Telegram Bot API stopped while bot was running" >&2
        kill "$BOT_PID" 2>/dev/null || true
        exit 1
      fi
      sleep 2
    done
  ) &
  MONITOR_PID="$!"
fi

set +e
wait "$BOT_PID"
BOT_STATUS="$?"
set -e

if [ -n "$MONITOR_PID" ]; then
  kill "$MONITOR_PID" 2>/dev/null || true
fi

exit "$BOT_STATUS"
