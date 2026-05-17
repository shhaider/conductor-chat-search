#!/bin/bash
# run.sh — launch the conductor-chat-search local server, open the browser, follow logs.
# Stop with Ctrl-C; the server is killed on exit.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

PORT="${CONDUCTOR_CHAT_PORT:-17891}"
DB_PATH="${CONDUCTOR_CHAT_DB:-$HOME/Library/Application Support/com.conductor.app/conductor.db}"

# If the fixed port is busy, fall back to kernel-assigned.
if ! python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1', $PORT)); s.close()" 2>/dev/null; then
  echo "Port $PORT busy — kernel will assign one." >&2
  PORT=0
fi

LOG_FILE=$(mktemp -t cchat-log.XXXXXX)
cleanup() {
  if [ -n "${SERVER_PID:-}" ]; then
    kill "$SERVER_PID" 2>/dev/null || true
  fi
  if [ -n "${TAIL_PID:-}" ]; then
    kill "$TAIL_PID" 2>/dev/null || true
  fi
  rm -f "$LOG_FILE"
}
trap cleanup EXIT INT TERM

PYTHONPATH="$REPO_DIR/src" python3 -m conductor_chat.server \
  --port "$PORT" --db "$DB_PATH" > "$LOG_FILE" 2>&1 &
SERVER_PID=$!

# Wait up to 3s for the server to print its URL line.
URL=""
for _ in 1 2 3 4 5 6; do
  sleep 0.5
  URL=$(grep -oE 'http://127.0.0.1:[0-9]+/' "$LOG_FILE" | head -1 || true)
  [ -n "$URL" ] && break
done

if [ -z "${URL:-}" ]; then
  echo "Server did not start within 3s. Logs:" >&2
  cat "$LOG_FILE" >&2
  exit 1
fi

echo "Conductor Chat Search → $URL" >&2
echo "Ctrl-C to stop." >&2
open "$URL" 2>/dev/null || echo "(open the URL above manually)" >&2

# Stream server logs to operator's stderr.
tail -f "$LOG_FILE" >&2 &
TAIL_PID=$!

wait "$SERVER_PID"
