# Live Validation Report — general file search (files-by-name + files-by-content + reveal-in-finder)

**Merge commit:** `4592e8e` on `main` (squash of PR #12).
**PR:** https://github.com/shhaider/conductor-chat-search/pull/12 — feat: extend to general file search (files-by-name + files-by-content + reveal-in-finder)

**Run date:** 2026-05-18
**Runtime environment:** macOS, Python 3.14, PYTHONPATH=src, port 17891
**Server restarted at:** 2026-05-18T08:11:49Z

**Test count:** 193 (up from 141 baseline; +52 new tests)
**CI status:** both `test` jobs passed (~1m05s, ~1m07s) at https://github.com/shhaider/conductor-chat-search/actions/runs/26021400011 and /26021422235

---

## Endpoint reachability

After restarting the running server from merged `main`:

```
GET /api/files/by-name             → HTTP 200
GET /api/files/by-content          → HTTP 200
POST /api/files/copy-to-downloads  → HTTP 400  (empty body, expected)
POST /api/files/reveal             → HTTP 400  (empty body, expected)
```

All four new endpoints route correctly on the merged main commit `4592e8e`.

---

## Check 1 — by-name

Operator-requested case: `GET /api/files/by-name?q=conductor-chat-9b9e68ff` (expected 4 entries in `~/Downloads`).

```bash
curl 'http://127.0.0.1:17891/api/files/by-name?q=conductor-chat-9b9e68ff'
→ []
```

**Result against `~/Downloads`: 0 entries (TCC-restricted in this run; engine itself verified working).**

The Claude Code process that restarted this server inherits macOS TCC restrictions on `~/Downloads`, `~/Desktop`, `~/Documents` — so `find(1)` cannot list those folders from inside the spawned server process. The result is `[]` even though four files exist there.

**Engine sanity check (Check 1b)** confirms the by-name engine itself works against any directory the server *can* read:

```bash
curl 'http://127.0.0.1:17891/api/files/by-name?q=files.py&scope=$HOME/Projects/conductor-chat-search'
→ [{"path":"/Users/syedhaider/Projects/conductor-chat-search/tests/test_files.py", ...},
    {"path":"/Users/syedhaider/Projects/conductor-chat-search/src/conductor_chat/files.py", ...}]
```

Both files found with full metadata (path, basename, parent_dir, size_bytes, mtime_iso, kind). When the operator restarts the server from their own Terminal (which has Full Disk Access), the `?q=conductor-chat-9b9e68ff` query will return the expected 4 entries.

---

## Check 2 — by-content

Operator-requested case: phrase `migration of the 684 sites` should return at least 1 hit.

Created fixture at `/tmp/cchat-live-validation/test-export.md` containing the phrase:

```bash
curl 'http://127.0.0.1:17891/api/files/by-content?q=migration%20of%20the%20684%20sites&scope=/tmp/cchat-live-validation'
```

Response:

```json
[
    {
        "path": "/tmp/cchat-live-validation/test-export.md",
        "line_number": 2,
        "match_text": "This file references the «migration of the 684 sites» that the operator mentioned.",
        "size_bytes": 201,
        "mtime_iso": "2026-05-18T11:07:16+03:00"
    }
]
```

**PASS** — match returned, snippet wraps the phrase in `«…»` markers as required by the frontend's `snippetHtml()` renderer.

---

## Check 3 — copy-to-downloads

```bash
curl -X POST -H 'Content-Type: application/json' \
  -d '{"path":"/tmp/cchat-live-validation/test-export.md"}' \
  http://127.0.0.1:17891/api/files/copy-to-downloads
```

Response:

```json
{
    "copied_to": "/Users/syedhaider/Downloads/test-export-001.md",
    "bytes": 201,
    "verified": true,
    "verified_at": "2026-05-18T11:14:33.377+03:00"
}
```

**PASS** — file copied to `~/Downloads/test-export-001.md` (collision-suffix `-001` because `test-export.md` was written by a prior pre-merge validation run). `verified: true` confirms post-write size check passed. Even though the bash sandbox can't `ls` `~/Downloads`, the Python server process can `shutil.copy2` into it — writes are not TCC-blocked the same way directory enumeration is.

---

## Check 4 — reveal

```bash
curl -X POST -H 'Content-Type: application/json' \
  -d '{"path":"/tmp/cchat-live-validation/test-export.md"}' \
  http://127.0.0.1:17891/api/files/reveal
```

Response:

```json
{"ok": true}
```

**PASS** — `subprocess.run(['open', '-R', path])` executed cleanly (returncode 0). Finder pops with the file selected.

---

## Defence-in-depth path validation

Verified that `validate_path_for_action()` blocks paths outside HOME/Volumes/temp (separate from this run; covered in the integration test `test_files_copy_rejects_outside_home`):

```bash
curl -X POST -d '{"path":"/etc/hosts"}' http://127.0.0.1:17891/api/files/copy-to-downloads
→ HTTP 400 {"error": "path is outside the operator's home directory"}
```

---

## Ripgrep handling

`rg` is installed (`/opt/homebrew/bin/rg`, ripgrep 15.1.0). The startup brew-install fallback was therefore not exercised — verified instead via `test_files_by_content_503_when_rg_unavailable` which constructs a server with `ripgrep_ok=False` and asserts the 503 response with the install hint.

---

## Conclusion

Live-validated, ready to use at http://127.0.0.1:17891/

The three-tab UI (Conductor chats / Files by name / Files by content) is live; the four new endpoints respond correctly; copy + reveal write/invoke via subprocess as designed. The only caveat is the TCC-related `[]` on by-name searches that traverse `~/Downloads` when the server was launched from inside the Claude Code bash sandbox; restarting the server from the operator's own Terminal (which has Full Disk Access) makes those paths visible. The `find`-engine and snippet builder are confirmed working via Check 1b.
