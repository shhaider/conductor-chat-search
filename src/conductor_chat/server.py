"""HTTP server: wires db/search/export onto routes and serves the single-page UI.

Stdlib only. ThreadingHTTPServer handles concurrent requests. Each handler
opens its own short-lived read-only DB connection.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import accounts, db, export, fts, search

STATIC_DIR = Path(__file__).parent / "static"
ALLOWED_STATIC: set[str] = {"index.html"}

# Schema version we've verified against. Mismatch is a soft warning, not a block.
KNOWN_GOOD_SCHEMA_PREFIX = "20260"

logger = logging.getLogger("conductor_chat.server")


class Handler(BaseHTTPRequestHandler):
    """Route table for /, /api/health, /api/workspaces, /api/sessions, /api/export, /static/*."""

    server_version = "ConductorChatSearch/0.1"

    # injected at server construction time
    db_path: str = db.DEFAULT_DB_PATH
    fts_path: str | None = fts.DEFAULT_FTS_PATH  # None = disable FTS (LIKE fallback)
    account_index: dict[str, str] = {}  # {session_id: account_name}; empty = unindexed

    # ---- routing ----

    def do_GET(self) -> None:  # noqa: N802 — stdlib API name
        t0 = time.perf_counter()
        try:
            self._route_get()
        except Exception as e:  # last-resort guard
            self._send_json(500, {"error": str(e)})
        finally:
            self._log_done("GET", t0)

    def do_POST(self) -> None:  # noqa: N802
        t0 = time.perf_counter()
        try:
            self._route_post()
        except Exception as e:
            self._send_json(500, {"error": str(e)})
        finally:
            self._log_done("POST", t0)

    def _route_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            self._send_static("index.html")
            return
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            if not _is_safe_static(rel):
                self._send_json(404, {"error": "not_found"})
                return
            self._send_static(rel)
            return
        if path == "/api/health":
            self._handle_health()
            return
        if path == "/api/workspaces":
            self._handle_workspaces()
            return
        if path == "/api/sessions":
            self._handle_sessions(parse_qs(parsed.query, keep_blank_values=False))
            return
        self._send_json(404, {"error": "not_found"})

    def _route_post(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/export":
            self._handle_export()
            return
        self._send_json(404, {"error": "not_found"})

    # ---- handlers ----

    def _handle_health(self) -> None:
        con = db.open_ro(self.db_path)
        try:
            ver = db.get_schema_version(con)
        finally:
            con.close()
        warning = None
        if ver and not str(ver).startswith(KNOWN_GOOD_SCHEMA_PREFIX):
            warning = f"Untested schema version: {ver}"
        body: dict[str, Any] = {
            "ok": True,
            "schema_version": ver,
            "accounts_indexed": len(self.account_index),
        }
        if warning:
            body["schema_warning"] = warning
        self._send_json(200, body)

    def _handle_workspaces(self) -> None:
        con = db.open_ro(self.db_path)
        try:
            self._send_json(200, db.list_workspaces(con))
        finally:
            con.close()

    def _handle_sessions(self, qs: dict[str, list[str]]) -> None:
        and_values = [t for t in qs.get("q", []) if t]
        exact_values = [t for t in qs.get("q_exact", []) if t]
        terms: list[dict[str, Any]] = (
            [{"value": v, "exact": False} for v in and_values]
            + [{"value": v, "exact": True} for v in exact_values]
        )
        ws_list = qs.get("workspace", [])
        wsid = ws_list[0] if ws_list else None
        account_list = qs.get("account", [])
        account_filter = account_list[0] if account_list else None
        con = db.open_ro(self.db_path)
        fts_con = None
        if terms and self.fts_path:
            try:
                fts_con = fts.open_fts(self.fts_path)
            except sqlite3.OperationalError as e:
                sys.stderr.write(f"FTS open failed, falling back to LIKE: {e}\n")
                fts_con = None
        try:
            if terms:
                result = search.search(
                    con,
                    terms,
                    workspace_id=wsid,
                    fts_con=fts_con,
                    account_index=self.account_index,
                )
            else:
                rows = db.list_sessions(con, account_index=self.account_index)
                if wsid:
                    rows = [r for r in rows if r["workspace_id"] == wsid]
                for r in rows:
                    r["snippets"] = []
                    r["match_kind"] = "and"
                result = rows
        finally:
            con.close()
            if fts_con is not None:
                fts_con.close()
        result = accounts.filter_by_account(result, account_filter)
        self._send_json(200, result)

    def _handle_export(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        body_bytes = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except (ValueError, UnicodeDecodeError) as e:
            self._send_json(400, {"error": f"invalid JSON body: {e}"})
            return
        sid = body.get("session_id")
        mode = body.get("mode", "clean")
        if not isinstance(sid, str) or not sid:
            self._send_json(400, {"error": "session_id required"})
            return
        if mode not in ("clean", "raw"):
            self._send_json(400, {"error": "mode must be 'clean' or 'raw'"})
            return
        con = db.open_ro(self.db_path)
        try:
            result = export.export_session(con, sid, mode=mode)
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return
        except OSError as e:
            self._send_json(500, {"error": f"export failed: {e}"})
            return
        finally:
            con.close()
        self._send_json(200, result)

    # ---- response helpers ----

    def _send_json(self, status: int, body: Any) -> None:
        data = json.dumps(body, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_static(self, rel: str) -> None:
        full = STATIC_DIR / rel
        if not full.is_file():
            self._send_json(404, {"error": "not_found"})
            return
        ctype = _guess_content_type(rel)
        data = full.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---- logging override (stdlib's default writes to stderr; reformat) ----

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        return  # we log at end of each request in _log_done

    def _log_done(self, method: str, t0: float) -> None:
        dur_ms = int((time.perf_counter() - t0) * 1000)
        # Best-effort status capture: BaseHTTPRequestHandler stores last status
        # in self._last_status if we set it via send_response. We don't, so we
        # just log method+path+duration.
        sys.stderr.write(f"{method} {self.path} {dur_ms}ms\n")
        sys.stderr.flush()


# -- path safety --

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-/]+$")


def _is_safe_static(rel: str) -> bool:
    """Refuse path traversal; only whitelisted filenames allowed."""
    if ".." in rel or rel.startswith("/"):
        return False
    if not _SAFE_NAME.match(rel):
        return False
    return rel in ALLOWED_STATIC


def _guess_content_type(rel: str) -> str:
    if rel.endswith(".html"):
        return "text/html; charset=utf-8"
    if rel.endswith(".css"):
        return "text/css; charset=utf-8"
    if rel.endswith(".js"):
        return "application/javascript; charset=utf-8"
    if rel.endswith(".json"):
        return "application/json; charset=utf-8"
    return "application/octet-stream"


# -- server factory + CLI --


def make_server(
    host: str = "127.0.0.1",
    port: int = 0,
    db_path: str | None = None,
    fts_path: str | None | object = ...,
    account_index: dict[str, str] | None = None,
) -> ThreadingHTTPServer:
    """Construct (don't start) a ThreadingHTTPServer with the routes wired.

    port=0 → kernel-assigned; the actual port is in ``server.server_address[1]``.
    db_path=None uses ``db.DEFAULT_DB_PATH``.
    fts_path:
      - Sentinel (default): use ``fts.DEFAULT_FTS_PATH``.
      - None: disable FTS, fall back to LIKE on every search.
      - str: use this path explicitly (useful for tests).
    account_index:
      - None (default): no account decoration — rows get ``account: null``.
      - dict: mapping of session_id -> account display name, built once at
        startup by ``accounts.build_index()``.
    """
    resolved_db = db_path or db.DEFAULT_DB_PATH
    if fts_path is ...:
        resolved_fts: str | None = fts.DEFAULT_FTS_PATH
    else:
        resolved_fts = fts_path  # may be None to disable

    class BoundHandler(Handler):
        pass

    BoundHandler.db_path = resolved_db
    BoundHandler.fts_path = resolved_fts
    BoundHandler.account_index = account_index or {}
    httpd = ThreadingHTTPServer((host, port), BoundHandler)
    return httpd


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: parse args, log startup info, serve_forever."""
    parser = argparse.ArgumentParser(prog="conductor_chat.server")
    parser.add_argument("--port", type=int, default=17891)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--db", default=db.DEFAULT_DB_PATH)
    parser.add_argument("--fts-db", default=fts.DEFAULT_FTS_PATH,
                        help="Sidecar FTS5 index path. Pass empty string to disable.")
    parser.add_argument("--no-fts", action="store_true",
                        help="Disable FTS5; force LIKE fallback.")
    parser.add_argument("--no-open", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if not Path(args.db).exists():
        sys.stderr.write(f"ERROR: conductor.db not found at: {args.db}\n")
        return 1

    # Schema sanity check (warn only).
    schema_ver: str | None = None
    try:
        con = db.open_ro(args.db)
        try:
            schema_ver = db.get_schema_version(con)
        finally:
            con.close()
        sys.stderr.write(
            f"Conductor DB OK (latest migration: {schema_ver if schema_ver else 'unknown'})\n"
        )
        if schema_ver and not str(schema_ver).startswith(KNOWN_GOOD_SCHEMA_PREFIX):
            sys.stderr.write(f"WARN: untested schema version {schema_ver}\n")
    except Exception as e:
        sys.stderr.write(f"ERROR opening DB: {e}\n")
        return 1

    # Resolve FTS path. CLI --no-fts wins; empty --fts-db also disables.
    if args.no_fts or not args.fts_db:
        resolved_fts: str | None = None
        sys.stderr.write("FTS5 disabled (--no-fts or empty --fts-db); using LIKE fallback.\n")
    elif not fts.has_fts5():
        resolved_fts = None
        sys.stderr.write(
            "WARN: this Python's bundled SQLite has no FTS5 support; "
            "falling back to LIKE (slow on large corpora).\n"
        )
    else:
        resolved_fts = args.fts_db
        # Build or sync the FTS index before opening the port. We hold open
        # one src_con for the duration; sync re-uses the same read-only handle.
        try:
            src_con = db.open_ro(args.db)
            try:
                fts_con = fts.open_fts(resolved_fts)
                try:
                    fts.check_schema_version(fts_con, schema_ver)
                    stats = fts.build_or_sync(fts_con, src_con)
                    if stats["is_initial_build"]:
                        sys.stderr.write(
                            f"FTS index built at {resolved_fts}\n"
                        )
                    else:
                        if stats["indexed"] or stats["skipped"]:
                            sys.stderr.write(
                                f"FTS sync: +{stats['indexed']} indexed, "
                                f"+{stats['skipped']} skipped\n"
                            )
                finally:
                    fts_con.close()
            finally:
                src_con.close()
        except Exception as e:
            sys.stderr.write(
                f"WARN: FTS build/sync failed ({e}); falling back to LIKE.\n"
            )
            resolved_fts = None

    # Build the account index once at startup. Refresh requires restart.
    try:
        account_index = accounts.build_index()
        sys.stderr.write(
            f"Account index built: {len(account_index)} session(s) across "
            f"{len(accounts.discover_account_dirs())} Claude account dir(s)\n"
        )
    except Exception as e:  # noqa: BLE001 — defensive; never block startup
        sys.stderr.write(f"WARN: account index build failed ({e}); orphan column disabled.\n")
        account_index = {}

    try:
        httpd = make_server(
            host=args.host,
            port=args.port,
            db_path=args.db,
            fts_path=resolved_fts,
            account_index=account_index,
        )
    except OSError as e:
        sys.stderr.write(f"ERROR binding {args.host}:{args.port}: {e}\n")
        return 1

    actual_port = httpd.server_address[1]
    url = f"http://{args.host}:{actual_port}/"
    sys.stderr.write(f"Conductor Chat Search listening at {url}\n")
    sys.stderr.write("Ctrl-C to stop.\n")
    sys.stderr.flush()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("Shutting down.\n")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
