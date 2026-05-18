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
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import accounts, db, export, files, fts, search

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
    ripgrep_ok: bool = True  # set False at startup when `rg` couldn't be found/installed

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
        if path == "/api/sessions/lookup":
            self._handle_sessions_lookup(
                parse_qs(parsed.query, keep_blank_values=False)
            )
            return
        if path == "/api/sessions":
            self._handle_sessions(parse_qs(parsed.query, keep_blank_values=False))
            return
        if path == "/api/files/by-name":
            self._handle_files_by_name(parse_qs(parsed.query, keep_blank_values=False))
            return
        if path == "/api/files/by-content":
            self._handle_files_by_content(parse_qs(parsed.query, keep_blank_values=False))
            return
        self._send_json(404, {"error": "not_found"})

    def _route_post(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/export":
            self._handle_export()
            return
        if parsed.path == "/api/files/copy-to-downloads":
            self._handle_files_copy()
            return
        if parsed.path == "/api/files/reveal":
            self._handle_files_reveal()
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

    def _handle_sessions_lookup(self, qs: dict[str, list[str]]) -> None:
        """Resolve a chat by id or id prefix.

        ``?id=<value>``:
          - Full UUID (36 chars) -> equality lookup.
          - 8+ char prefix -> ``id LIKE '<value>%'``.
          - <8 chars or missing -> 400 with explanatory error.
        Returns a JSON list of session rows (same shape as ``/api/sessions``).
        Empty list means "no chat with that id".
        """
        id_list = qs.get("id", [])
        id_value = (id_list[0] if id_list else "").strip()
        if not id_value:
            self._send_json(400, {"error": "id query param required"})
            return
        # Minimum prefix length; UUIDs that are 36 chars are fine.
        if len(id_value) < 8:
            self._send_json(
                400,
                {"error": "id must be at least 8 characters (UUID prefix)"},
            )
            return
        con = db.open_ro(self.db_path)
        try:
            result = db.lookup_sessions_by_id(
                con, id_value, account_index=self.account_index
            )
        finally:
            con.close()
        # Decorate with empty snippets / match_kind so the frontend's render()
        # can reuse the same row template as the search results path.
        for r in result:
            r["snippets"] = []
            r["match_kind"] = "and"
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

        # Verify step: confirm the file is actually visible at the returned
        # path before claiming success. The UI used to flip to "Exported"
        # ~500ms before Finder/Spotlight could see the file. We stat,
        # short-pause, re-stat, and check the first 100 bytes for the
        # markdown header. If anything is off, return 500 so the frontend
        # doesn't lie.
        verified = _verify_export(result["path"], result["bytes"])
        if not verified["ok"]:
            self._send_json(500, verified["error"])
            return
        result["verified"] = True
        result["verified_size_bytes"] = verified["size"]
        result["verified_at"] = verified["at"]
        self._send_json(200, result)

    # ---- files-tab handlers (name search, content search, copy, reveal) ----

    def _handle_files_by_name(self, qs: dict[str, list[str]]) -> None:
        q = (qs.get("q", [""])[0] or "").strip()
        if not q:
            self._send_json(400, {"error": "q query param required"})
            return
        scope = (qs.get("scope", ["/"])[0] or "/").strip() or "/"
        include_hidden = _qs_bool(qs, "include_hidden", default=True)
        try:
            rows = files.find_by_name(q, scope=scope, include_hidden=include_hidden)
        except OSError as e:
            self._send_json(500, {"error": f"find failed: {e}"})
            return
        self._send_json(200, rows)

    def _handle_files_by_content(self, qs: dict[str, list[str]]) -> None:
        if not self.ripgrep_ok:
            self._send_json(
                503,
                {"error": "ripgrep not installed. Run: brew install ripgrep"},
            )
            return
        q = (qs.get("q", [""])[0] or "").strip()
        if not q:
            self._send_json(400, {"error": "q query param required"})
            return
        scope = (qs.get("scope", ["/"])[0] or "/").strip() or "/"
        include_hidden = _qs_bool(qs, "include_hidden", default=True)
        try:
            rows = files.find_by_content(q, scope=scope, include_hidden=include_hidden)
        except OSError as e:
            self._send_json(500, {"error": f"rg failed: {e}"})
            return
        self._send_json(200, rows)

    def _handle_files_copy(self) -> None:
        body = self._read_json_body()
        if body is None:
            return  # _read_json_body already sent the error response
        path = body.get("path")
        if not isinstance(path, str) or not path:
            self._send_json(400, {"error": "path required"})
            return
        ok, err = files.validate_path_for_action(path)
        if not ok:
            # 404 for missing, 400 for outside-home — pick by message.
            status = 404 if err and "not found" in err else 400
            self._send_json(status, {"error": err})
            return
        try:
            result = files.copy_to_downloads(path)
        except FileNotFoundError as e:
            self._send_json(404, {"error": str(e)})
            return
        except (IsADirectoryError, ValueError) as e:
            self._send_json(400, {"error": str(e)})
            return
        except OSError as e:
            self._send_json(500, {"error": f"copy failed: {e}"})
            return
        if not result.get("verified"):
            self._send_json(500, {"error": "copy verify failed", **result})
            return
        self._send_json(200, result)

    def _handle_files_reveal(self) -> None:
        body = self._read_json_body()
        if body is None:
            return
        path = body.get("path")
        if not isinstance(path, str) or not path:
            self._send_json(400, {"error": "path required"})
            return
        ok, err = files.validate_path_for_action(path)
        if not ok:
            status = 404 if err and "not found" in err else 400
            self._send_json(status, {"ok": False, "error": err})
            return
        result = files.reveal_in_finder(path)
        # reveal_in_finder always returns a dict; surface its ok flag.
        self._send_json(200 if result.get("ok") else 500, result)

    def _read_json_body(self) -> dict[str, Any] | None:
        """Read+parse the request JSON body. Sends 400 and returns None on error."""
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError) as e:
            self._send_json(400, {"error": f"invalid JSON body: {e}"})
            return None
        if not isinstance(body, dict):
            self._send_json(400, {"error": "JSON body must be an object"})
            return None
        return body

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


# -- query-string helpers --


def _qs_bool(qs: dict[str, list[str]], key: str, default: bool = False) -> bool:
    """Parse a boolean query-string param.

    Accepts ``1``/``0``, ``true``/``false``, ``yes``/``no`` (case-insensitive).
    Missing or empty value falls back to ``default``.
    """
    raw_list = qs.get(key, [])
    if not raw_list:
        return default
    raw = (raw_list[0] or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


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


# How long to pause between the two stat calls. Short enough to be invisible
# to the user, long enough that a half-written file would either finish or
# fail visibly. 50ms matches the spec.
_VERIFY_PAUSE_S = 0.05


def _verify_export(path: str, expected_size: int) -> dict[str, Any]:
    """Confirm that ``path`` is a real file at least ``expected_size`` bytes
    long, with the markdown frontmatter at the head.

    Returns ``{"ok": True, "size": int, "at": iso}`` on success or
    ``{"ok": False, "error": {...}}`` on failure. The error dict matches the
    spec's failure shape: ``{error, path, expected_size, actual_size}``.

    The flow is:
      1. ``os.stat`` — fails fast if the file vanished before we got here.
      2. brief sleep (50ms) — gives any pending fs/cache work a moment.
      3. second ``os.stat`` — confirms the file is still there with the
         expected size.
      4. read first 100 bytes — confirms the markdown header ('# ') is
         present so we know it isn't a half-written truncated file.
    """
    try:
        st1 = os.stat(path)
    except OSError as e:
        return {"ok": False, "error": {
            "error": f"verify failed: file not found ({e})",
            "path": path,
            "expected_size": expected_size,
            "actual_size": 0,
        }}
    if st1.st_size <= 0:
        return {"ok": False, "error": {
            "error": "verify failed: file is empty",
            "path": path,
            "expected_size": expected_size,
            "actual_size": st1.st_size,
        }}
    time.sleep(_VERIFY_PAUSE_S)
    try:
        st2 = os.stat(path)
    except OSError as e:
        return {"ok": False, "error": {
            "error": f"verify failed: file disappeared between stats ({e})",
            "path": path,
            "expected_size": expected_size,
            "actual_size": 0,
        }}
    if st2.st_size != expected_size:
        return {"ok": False, "error": {
            "error": "verify failed: size mismatch",
            "path": path,
            "expected_size": expected_size,
            "actual_size": st2.st_size,
        }}
    # Optional content sanity check — head of the file should start with the
    # markdown frontmatter / header. render.render_session_header always emits
    # a '# <title>' line as the first non-frontmatter line.
    try:
        with open(path, "rb") as f:
            head = f.read(100)
    except OSError as e:
        return {"ok": False, "error": {
            "error": f"verify failed: could not read head ({e})",
            "path": path,
            "expected_size": expected_size,
            "actual_size": st2.st_size,
        }}
    # The rendered export always begins with a markdown heading or frontmatter
    # '---' line; either form starts with '#' or '-'. If neither is present in
    # the first 100 bytes, the file is wrong.
    if not head.lstrip().startswith((b"#", b"-")):
        return {"ok": False, "error": {
            "error": "verify failed: unexpected file head (no markdown header)",
            "path": path,
            "expected_size": expected_size,
            "actual_size": st2.st_size,
        }}
    iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
    return {"ok": True, "size": st2.st_size, "at": iso}


# -- server factory + CLI --


def make_server(
    host: str = "127.0.0.1",
    port: int = 0,
    db_path: str | None = None,
    fts_path: str | None | object = ...,
    account_index: dict[str, str] | None = None,
    ripgrep_ok: bool | None = None,
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
    # If caller didn't explicitly state ripgrep status, probe at construction
    # time. ``False`` here disables the /api/files/by-content endpoint with
    # a 503 explaining how to install rg.
    if ripgrep_ok is None:
        BoundHandler.ripgrep_ok = files.ripgrep_available()
    else:
        BoundHandler.ripgrep_ok = ripgrep_ok
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

    # Probe ripgrep. If it's missing, try one best-effort `brew install` so the
    # operator doesn't have to. Failures are non-fatal — the file-content tab
    # just returns 503 with the install command in the error body.
    ripgrep_ok = files.ripgrep_available()
    if not ripgrep_ok:
        sys.stderr.write(
            "WARN: ripgrep (`rg`) not found. Attempting `brew install ripgrep`…\n"
        )
        installed, msg = files.try_install_ripgrep_via_brew()
        if installed:
            sys.stderr.write("ripgrep installed via brew.\n")
            ripgrep_ok = True
        else:
            sys.stderr.write(
                f"WARN: could not install ripgrep ({msg}). "
                f"Content search will be unavailable. "
                f"Install manually: brew install ripgrep\n"
            )

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
            ripgrep_ok=ripgrep_ok,
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
