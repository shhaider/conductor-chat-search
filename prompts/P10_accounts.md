# P10 — `src/conductor_chat/accounts.py` (Feature A)

## Goal

Resolve which Claude Code account directory holds each session's resumable JSONL state, so the UI can surface "orphaned" sessions (chat content in conductor.db but Claude Code says "no conversation found with session ID").

## Files

- `src/conductor_chat/accounts.py` (new)
- `src/conductor_chat/db.py` — accept optional `account_index` and decorate each row.
- `src/conductor_chat/search.py` — accept optional `account_index`; decorate result rows.
- `src/conductor_chat/server.py` — build the index once at startup; pass to handlers; expose count in `/api/health`; honour `?account=<name>` filter.
- `tests/test_accounts.py` (new)
- `tests/test_db.py`, `tests/test_search.py`, `tests/test_server.py` — assert the `account` field is present.

## Disk layout we're indexing

Each Claude Code account stores resumable state per-workspace:

```
~/.claude-account<N>/projects/<encoded-workspace-path>/<claude_session_id>.jsonl
```

The default account lives at `~/.claude/projects/...`. The user has 5 candidate roots; some (`~/.claude-account-backups`, `~/.claude-account4`) do NOT have a `projects/` subdir and must be skipped.

## API

```python
# src/conductor_chat/accounts.py

DEFAULT_HOME: str = os.path.expanduser("~")

def discover_account_dirs(home: str = DEFAULT_HOME) -> list[str]:
    """Return absolute paths of every ~/.claude* directory that has a projects/ subdir.

    Heuristic: matches glob ~/.claude* AND contains a projects/ subdirectory.
    Returns sorted absolute paths. Skips files (.claude.json, .claude-peers.db, etc.)
    """

def account_name(account_dir: str) -> str:
    """Convert a dir path into a short display name.

    - ~/.claude        -> "default"
    - ~/.claude-account1 -> "account1"
    - ~/.claude-account-backups -> "account-backups"
    """

def build_index(home: str = DEFAULT_HOME) -> dict[str, str]:
    """Scan every account dir for *.jsonl files; return {claude_session_id: account_name}.

    The session id is the JSONL file's stem. If the same session id appears in
    multiple accounts, the FIRST encountered (sorted by account dir name) wins;
    the rest are silently ignored (logging would be noisy).
    """

ORPHANED_SENTINEL: str = "__orphaned__"

def account_for(index: dict[str, str], session_id: str) -> str | None:
    """O(1) lookup. Returns the account name, or None if not present."""
```

## Indexing efficiency

- Use `os.scandir` (not `Path.glob`) — much faster on large trees.
- Scan once at startup, build a single dict, share it across handlers.
- ~3000 JSONL files total — should take well under 1 second.

## Wiring

### db.list_sessions

Add an optional `account_index: dict[str, str] | None = None` parameter. After fetching rows, decorate each dict with `account = account_index.get(session_id) if account_index else None`.

### search.search

Same: optional `account_index`, decorate each returned row.

### server.py

- At server startup (in `main()`): `account_index = accounts.build_index()`, log `len(account_index)` to stderr.
- Pass index to `make_server` and onward to the handler. Store on `BoundHandler.account_index`.
- `/api/health`: add `accounts_indexed: <count>` (length of the index dict).
- `/api/sessions`: accept `?account=<name>` filter. `account=__orphaned__` means "rows where account is None". Filter is applied AFTER the search result is built, before returning.

## Frontend (in P12)

This prompt focuses on backend wiring. Frontend rendering of the badge and the filter dropdown is covered by P12.

## Tests

`tests/test_accounts.py`:
- Build a fixture dir tree under `tmp_path` mimicking:
  - `~/.claude/projects/<encoded>/aaa.jsonl, bbb.jsonl`
  - `~/.claude-account1/projects/<encoded>/ccc.jsonl`
  - `~/.claude-account-backups/refresh.sh` (no `projects/` → must be skipped)
  - `~/.claude-account4/settings.json` (no `projects/` → must be skipped)
  - `~/.claude.json` (a file, not a dir → must be skipped)
- Assert `discover_account_dirs(tmp_path)` returns exactly the two dirs with `projects/`.
- Assert `build_index(tmp_path)` maps each session id to its account name.
- Assert `account_for(index, "aaa")` == `"default"`; for an unknown id returns None.

`tests/test_db.py`:
- New test: pass a fake account index → assert returned rows have the `account` field populated.

`tests/test_search.py`:
- New test: pass a fake account index → assert returned rows have the `account` field populated.

`tests/test_server.py`:
- `/api/health` includes `accounts_indexed` (an int ≥ 0).
- `/api/sessions` rows include an `account` key (may be None).
- `/api/sessions?account=__orphaned__` filters to None rows.

## Constraints

- No third-party deps.
- Pure stdlib (`os`, `pathlib`, `glob`).
- Must not crash if `~/.claude*` returns zero hits.
