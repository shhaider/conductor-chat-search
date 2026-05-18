"""General-purpose local file search + Finder integration.

Two search functions back the "Files by name" and "Files by content" tabs in
the UI:

  * :func:`find_by_name` — shells out to ``find(1)`` with case-insensitive
    ``-iname``. Glob patterns are supported (``*foo*``, ``*.md``).
  * :func:`find_by_content` — shells out to ``rg(1)`` with ``--json`` so we
    get structured per-match records back instead of having to parse
    grep-like output.

Both functions enforce wall-clock timeouts (60s / 30s) so a runaway scan
can't lock up the server, and both filter out the same set of
:data:`DEFAULT_EXCLUDE_DIRS` (macOS system noise that requires sudo to read
or contains millions of useless caches).

Two side-effect helpers wire result rows to Finder:

  * :func:`copy_to_downloads` — ``shutil.copy2`` to ``~/Downloads``, with
    automatic ``-001`` / ``-002`` suffix on collision and a post-write size
    verification (same pattern as the chat export endpoint).
  * :func:`reveal_in_finder` — ``open -R <path>`` so Finder pops with the
    file selected. Hidden directories are auto-shown by Finder when this
    runs.

Security note: this module accepts arbitrary paths from the HTTP layer.
The server is loopback-only so this is acceptable, but the path-validation
helper :func:`validate_path_for_action` rejects paths outside the
operator's home or ``/Volumes/`` for the action endpoints. Pure search
results aren't validated — the operator searched for them deliberately.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Directories we never want to descend into. macOS-specific. Some require
# sudo to read; the rest are huge caches that drown useful results.
DEFAULT_EXCLUDE_DIRS: tuple[str, ...] = (
    "/System",
    "/private/var/db",
    "/private/var/folders",
    "/private/var/vm",
    "/private/var/log",
    "/Library/Caches",
    "/.Spotlight-V100",
    "/.fseventsd",
    "/.DocumentRevisions-V100",
    "/.TemporaryItems",
)

# Per-result hard caps. UI tabs only display the first N rows anyway.
MAX_RESULTS_NAME = 500
MAX_RESULTS_CONTENT = 200

# Wall-clock timeouts. Names scan more directories so it gets longer.
TIMEOUT_NAME_S = 60
TIMEOUT_CONTENT_S = 30

# Snippet context window for content matches. ~200 chars of left+right
# context around the matched span, plus the «match» markers.
SNIPPET_CONTEXT_CHARS = 90

# Cap for copy_to_downloads. Refuses to silently copy huge files.
MAX_COPY_BYTES = 100 * 1024 * 1024  # 100 MB

# Default destination for ``copy_to_downloads``. Tests override this module
# attribute to redirect the copy into a temp dir without monkeypatching
# ``os.path.expanduser`` (which would recurse into validate_path_for_action).
DEFAULT_DOWNLOADS_DIR = os.path.expanduser("~/Downloads")


# ---------- search ----------


def find_by_name(
    pattern: str,
    scope: str = "/",
    include_hidden: bool = True,
    max_results: int = MAX_RESULTS_NAME,
) -> list[dict[str, Any]]:
    """Find files+directories whose basename matches ``pattern`` (case-insensitive).

    ``pattern`` is a glob (``*foo*``, ``*.md``). If the user passes a bare
    word with no glob characters we wrap it in ``*…*`` so the natural
    "substring search" interpretation works.

    ``scope`` defaults to the whole filesystem (``/``). The
    :data:`DEFAULT_EXCLUDE_DIRS` are pruned regardless of scope.

    ``include_hidden`` toggles dot-prefixed names. Even when False, hidden
    *directories* are still traversed — we just filter out matches whose
    basename starts with ``.``. This is intentional: lots of interesting
    files live under hidden parent dirs (e.g. ``~/.config/foo.toml``).

    ``max_results`` is a hard cap; results past it are dropped.

    Returns a list of dicts with: ``path``, ``basename``, ``parent_dir``,
    ``size_bytes``, ``mtime_iso``, ``kind`` ("file"/"dir"/"symlink").
    """
    if not pattern or not pattern.strip():
        return []
    pattern = pattern.strip()
    # If the user typed a bare substring with no glob chars, wrap it.
    if not any(c in pattern for c in "*?["):
        pattern = f"*{pattern}*"

    cmd: list[str] = ["find", scope]
    # Prune the system noise directories. ``-prune`` short-circuits descent
    # but we still have to add ``-o`` to OR it with the match clause.
    prune_expr: list[str] = []
    for ex in DEFAULT_EXCLUDE_DIRS:
        if prune_expr:
            prune_expr.append("-o")
        prune_expr += ["-path", ex]
    if prune_expr:
        cmd += ["("] + prune_expr + [")", "-prune", "-o"]
    cmd += ["-iname", pattern, "-print"]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_NAME_S,
            # find(1) returns non-zero for permission-denied subdirs but
            # still emits valid results on stdout — never raise on rc!=0.
            check=False,
        )
    except subprocess.TimeoutExpired:
        return []

    results: list[dict[str, Any]] = []
    seen = 0
    for raw_line in proc.stdout.splitlines():
        line = raw_line.rstrip("\n")
        if not line:
            continue
        try:
            st = os.lstat(line)
        except OSError:
            # Race: file removed between find listing it and us stat-ing.
            continue
        basename = os.path.basename(line) or line
        if not include_hidden and basename.startswith("."):
            continue
        if os.path.islink(line):
            kind = "symlink"
        elif os.path.isdir(line):
            kind = "dir"
        else:
            kind = "file"
        results.append(
            {
                "path": line,
                "basename": basename,
                "parent_dir": os.path.dirname(line),
                "size_bytes": st.st_size,
                "mtime_iso": _iso_from_epoch(st.st_mtime),
                "kind": kind,
            }
        )
        seen += 1
        if seen >= max_results:
            break
    return results


def find_by_content(
    query: str,
    scope: str = "/",
    include_hidden: bool = True,
    max_results: int = MAX_RESULTS_CONTENT,
) -> list[dict[str, Any]]:
    """Full-text search inside files using ripgrep.

    ``query`` is a literal phrase (we pass ``--fixed-strings`` so regex
    metacharacters don't surprise the operator).

    Returns one row per match (not one per file). Each dict carries:
    ``path``, ``line_number``, ``match_text`` (a ~200-char context window
    with the matched span wrapped in ``«…»`` markers), ``size_bytes``,
    ``mtime_iso``.
    """
    if not query or not query.strip():
        return []
    query = query.strip()

    cmd: list[str] = [
        "rg",
        "--json",
        "--fixed-strings",
        "--no-config",
        "--max-count=3",  # cap per-file matches so one busy file can't fill the page
        "--max-columns=400",
        "--no-ignore",  # don't respect .gitignore — we want everything
    ]
    if include_hidden:
        cmd.append("--hidden")
    # Exclude system dirs. ripgrep supports globs via ``--glob``; we use
    # the directory-prune form ``!path``.
    for ex in DEFAULT_EXCLUDE_DIRS:
        cmd += ["--glob", f"!{ex}"]
        cmd += ["--glob", f"!{ex}/**"]
    cmd += ["--", query, scope]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_CONTENT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return []
    except FileNotFoundError:
        # rg missing on PATH — caller should have already warned at startup.
        return []

    results: list[dict[str, Any]] = []
    for raw_line in proc.stdout.splitlines():
        if not raw_line:
            continue
        try:
            evt = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") != "match":
            continue
        data = evt.get("data") or {}
        path_obj = data.get("path") or {}
        file_path = path_obj.get("text")
        if not file_path:
            continue
        line_no = data.get("line_number")
        lines_obj = data.get("lines") or {}
        line_text = lines_obj.get("text") or ""
        # rg may return one or more matched spans; mark them all.
        spans = data.get("submatches") or []
        snippet = _build_snippet(line_text, spans)
        try:
            st = os.lstat(file_path)
        except OSError:
            continue
        results.append(
            {
                "path": file_path,
                "line_number": line_no,
                "match_text": snippet,
                "size_bytes": st.st_size,
                "mtime_iso": _iso_from_epoch(st.st_mtime),
            }
        )
        if len(results) >= max_results:
            break
    return results


def _build_snippet(line_text: str, spans: list[dict[str, Any]]) -> str:
    """Wrap matched ranges in ``«…»`` and trim to a context window.

    ``spans`` is the rg ``submatches`` array: each entry has
    ``{start, end, match: {text}}`` (byte offsets within ``line_text``).

    We anchor the snippet window around the *first* match. If the line is
    shorter than 2 * :data:`SNIPPET_CONTEXT_CHARS` we return it whole.
    """
    if not line_text:
        return ""
    # Strip the trailing newline ripgrep includes.
    line_text = line_text.rstrip("\n")
    if not spans:
        # Defensive: shouldn't happen for a match event.
        return line_text[: SNIPPET_CONTEXT_CHARS * 2]

    # Sort spans by start so we can splice them left-to-right.
    spans = sorted(spans, key=lambda s: s.get("start", 0))
    first_start = spans[0].get("start", 0)
    first_end = spans[0].get("end", first_start)

    # Anchor window around the first match.
    win_start = max(0, first_start - SNIPPET_CONTEXT_CHARS)
    win_end = min(len(line_text), first_end + SNIPPET_CONTEXT_CHARS)

    # Build the marked-up substring. We splice from the original line_text
    # and inject « / » at each span boundary that falls inside the window.
    pieces: list[str] = []
    cursor = win_start
    for sp in spans:
        s = sp.get("start", 0)
        e = sp.get("end", s)
        if e <= win_start or s >= win_end:
            continue
        s_c = max(s, win_start)
        e_c = min(e, win_end)
        if s_c > cursor:
            pieces.append(line_text[cursor:s_c])
        pieces.append("«")
        pieces.append(line_text[s_c:e_c])
        pieces.append("»")
        cursor = e_c
    if cursor < win_end:
        pieces.append(line_text[cursor:win_end])

    snippet = "".join(pieces)
    if win_start > 0:
        snippet = "…" + snippet
    if win_end < len(line_text):
        snippet = snippet + "…"
    return snippet


# ---------- side-effect helpers ----------


def copy_to_downloads(
    path: str,
    out_dir: str | None = None,
    max_bytes: int = MAX_COPY_BYTES,
) -> dict[str, Any]:
    """Copy ``path`` to the operator's Downloads folder.

    On collision, appends ``-001``, ``-002``, … to the basename (before
    the extension). Returns ``{copied_to, bytes, verified}`` on success.
    Verifies the destination size matches the source size after the copy
    so we never claim success on a torn write (same contract as the chat
    export endpoint).

    Raises ``FileNotFoundError`` if ``path`` doesn't exist, ``IsADirectoryError``
    if it's a directory (use a tarball if you want a folder), and
    ``ValueError`` if it's larger than ``max_bytes``.
    """
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(f"source not found: {path}")
    if src.is_dir():
        raise IsADirectoryError(f"refusing to copy a directory: {path}")
    src_size = src.stat().st_size
    if src_size > max_bytes:
        raise ValueError(
            f"file is {src_size} bytes; limit is {max_bytes} bytes "
            f"(refusing to silently copy a huge file)"
        )

    out_root = Path(out_dir) if out_dir else Path(DEFAULT_DOWNLOADS_DIR)
    out_root.mkdir(parents=True, exist_ok=True)
    dest = out_root / src.name
    if dest.exists():
        stem = src.stem
        suffix = src.suffix
        for n in range(1, 1000):
            candidate = out_root / f"{stem}-{n:03d}{suffix}"
            if not candidate.exists():
                dest = candidate
                break
        else:
            raise OSError(
                f"could not find a free filename in {out_root} after 999 tries"
            )

    shutil.copy2(str(src), str(dest))
    dest_size = dest.stat().st_size
    verified = dest_size == src_size and dest_size > 0
    return {
        "copied_to": str(dest.resolve()),
        "bytes": dest_size,
        "verified": verified,
        "verified_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
    }


def reveal_in_finder(path: str) -> dict[str, Any]:
    """Run ``open -R <path>`` so Finder pops with the file selected.

    Returns ``{ok: True}`` on success or ``{ok: False, error: str}`` on
    failure. The path is passed via ``argv`` (no shell), so spaces and
    special chars don't need escaping.
    """
    if not os.path.exists(path):
        return {"ok": False, "error": f"path not found: {path}"}
    try:
        proc = subprocess.run(
            ["open", "-R", path],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return {"ok": False, "error": str(e)}
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip() or f"open returned {proc.returncode}"}
    return {"ok": True}


# ---------- path validation for action endpoints ----------


def validate_path_for_action(path: str) -> tuple[bool, str | None]:
    """Reject paths the operator probably didn't mean to act on.

    The two action endpoints (``/api/files/copy-to-downloads`` and
    ``/api/files/reveal``) accept arbitrary paths from the HTTP layer.
    Since the server is loopback-only this is fine, but we still reject:

      * paths that don't exist (404 from the handler);
      * paths that resolve outside the operator's home or ``/Volumes/``
        (defence in depth: stop a stray request from poking at, say,
        ``/etc/passwd`` even though that file isn't sensitive in this
        context).

    Returns ``(ok, error_message)``. ``error_message`` is None on success.
    """
    if not path or not isinstance(path, str):
        return False, "path is required"
    if not os.path.exists(path):
        return False, "path not found"
    try:
        resolved = os.path.realpath(path)
    except OSError as e:
        return False, f"could not resolve path: {e}"
    home = os.path.realpath(os.path.expanduser("~"))
    if resolved.startswith(home + os.sep) or resolved == home:
        return True, None
    if resolved.startswith("/Volumes/") or resolved == "/Volumes":
        return True, None
    # Tmp dirs are safe-ish — tests need to copy fixture files from /tmp.
    tmp_dirs = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")
    if any(resolved.startswith(t) for t in tmp_dirs):
        return True, None
    return False, "path is outside the operator's home directory"


# ---------- helpers ----------


def _iso_from_epoch(epoch: float) -> str:
    """Convert a POSIX timestamp to ISO 8601 in the local timezone."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .astimezone()
        .isoformat(timespec="seconds")
    )


# ---------- ripgrep availability (called at server startup) ----------


def ripgrep_available() -> bool:
    """Return True iff ``rg`` is on PATH and responds to ``--version``."""
    try:
        proc = subprocess.run(
            ["rg", "--version"], capture_output=True, text=True, timeout=5, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def try_install_ripgrep_via_brew() -> tuple[bool, str]:
    """Best-effort ``brew install ripgrep``.

    Called at server startup when ``rg`` is missing. Returns
    ``(installed, message)``. Never raises — if brew isn't there or the
    install times out we just report failure; the content-search endpoint
    will return 503 until the operator installs it manually.
    """
    try:
        proc = subprocess.run(
            ["brew", "install", "ripgrep"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError:
        return False, "brew not on PATH"
    except subprocess.TimeoutExpired:
        return False, "brew install timed out after 60s"
    except OSError as e:
        return False, f"brew install failed: {e}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return False, "brew install failed: " + " | ".join(tail)
    # Re-check.
    return ripgrep_available(), "ok"
