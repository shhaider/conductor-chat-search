# P05 — `src/conductor_chat/server.py` + `run.sh`

## Goal

HTTP server that wires together the API + serves the static page. Plus a `run.sh` launcher that picks a port, starts the server, and opens the browser.

## Files

- `src/conductor_chat/server.py`
- `run.sh`
- `tests/test_server.py`

## Server API

```python
def make_server(host: str = "127.0.0.1", port: int = 0, db_path: str | None = None) -> ThreadingHTTPServer:
    """Construct (don't start) a ThreadingHTTPServer with the routes wired.

    port=0 → kernel-assigned; check server.server_address[1] after creation.
    db_path None → use db.DEFAULT_DB_PATH.
    """

def main(argv: list[str] | None = None) -> int:
    """CLI entry point: parse args, log startup info to stderr, serve_forever.
    Args: --port <int> (default 17891), --db <path>, --no-open.
    Returns exit code.
    """
```

## Routes

- `GET /` → return `index.html` (200 text/html)
- `GET /static/<path>` → serve files from `src/conductor_chat/static/` (only if path is whitelisted to known static files; refuse `..`)
- `GET /api/health` → `{ok: true, schema_version: <str>}`
- `GET /api/workspaces` → JSON list from `db.list_workspaces`
- `GET /api/sessions` → calls `search.search()` if any `q` params present; else `db.list_sessions()`. Accepts repeated `q=` params; `workspace=<id>` optional.
- `POST /api/export` → body `{session_id: str, mode?: "clean"|"raw"}`. Calls `export.export_session`. Returns `{path, bytes, messages, mode}`.
- All other paths → 404 with `{error: "not_found"}`.

## Behavior details

- On startup: open the DB once to check `db.get_schema_version()`; print to stderr `Conductor DB OK (migrations=N, latest=<version>)`. If DB missing, print error and exit 1.
- Each HTTP handler opens its own DB connection (fast, threadsafe).
- Errors return JSON `{error: <message>}` with appropriate status (400/404/500).
- Log each request to stderr in a one-line format: `<METHOD> <path> <status> <duration_ms>`.

## `run.sh`

```bash
#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

PORT="${CONDUCTOR_CHAT_PORT:-17891}"
DB_PATH="${CONDUCTOR_CHAT_DB:-$HOME/Library/Application Support/com.conductor.app/conductor.db}"

# Try the fixed port; if it's taken, fall back to kernel-assigned (port=0).
python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1', $PORT)); s.close()" 2>/dev/null \
  || { echo "Port $PORT busy — kernel will assign one." >&2; PORT=0; }

URL_FILE=$(mktemp -t cchat-url)
trap "rm -f $URL_FILE" EXIT

# Start server in background, capture the actual URL it printed.
python3 -m conductor_chat.server --port "$PORT" --db "$DB_PATH" > "$URL_FILE" 2>&1 &
SERVER_PID=$!
trap "kill $SERVER_PID 2>/dev/null; rm -f $URL_FILE" EXIT INT TERM

# Wait up to 3s for the server to print its URL line.
for i in 1 2 3 4 5 6; do
  sleep 0.5
  URL=$(grep -oE 'http://127.0.0.1:[0-9]+/' "$URL_FILE" | head -1)
  [ -n "$URL" ] && break
done

if [ -z "${URL:-}" ]; then
  echo "Server did not start within 3s. Logs:" >&2
  cat "$URL_FILE" >&2
  exit 1
fi

echo "Conductor Chat Search → $URL" >&2
echo "Ctrl-C to stop." >&2
open "$URL" 2>/dev/null || echo "(open the URL above manually)" >&2

# Stream server logs to operator's stderr.
tail -f "$URL_FILE" &
TAIL_PID=$!
trap "kill $SERVER_PID $TAIL_PID 2>/dev/null; rm -f $URL_FILE" EXIT INT TERM

wait $SERVER_PID
```

## Tests

`tests/test_server.py`:

1. `make_server` on fixture DB → server constructs, picks a port, can be served+stopped.
2. `GET /api/health` → 200, JSON has `ok: true`, `schema_version: <str>`.
3. `GET /api/workspaces` → returns the fixture's workspaces.
4. `GET /api/sessions` (no q) → returns the fixture's sessions.
5. `GET /api/sessions?q=cat` → returns only sessions matching "cat" in text blocks.
6. `GET /api/sessions?q=cat&q=happy` → AND-narrows.
7. `POST /api/export` with valid session_id → 200, file path returned, file exists.
8. `POST /api/export` with unknown session_id → 400, error message in JSON.
9. `GET /` → 200, `text/html`, body contains the page title.
10. `GET /static/../etc/passwd` → 404 or 400 (path traversal refused).

Use `urllib.request` from stdlib for the tests; no `requests` lib.

## Acceptance

- 10 tests pass.
- `bash run.sh` starts the server, prints URL, opens browser, responds to Ctrl-C cleanly.
- No third-party deps.
- server.py ≤ 250 lines. run.sh ≤ 60 lines.
