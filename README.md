# conductor-chat-search

Local search + export GUI for [Conductor.app](https://conductor.build) chat history.

When you're rate-limited on one Anthropic account and need to migrate a chat to another, the painful step is finding the right past chat among hundreds of sessions with auto-assigned titles like "Untitled" or "Execute plan". Conductor's built-in resume + summary path is lossy. This tool reads Conductor's local SQLite directly, lets you search across message text, and exports a complete markdown transcript to paste into a fresh chat under a different account.

## Status

🚧 **Planning baseline.** See [SPEC.md](SPEC.md) for goals + acceptance criteria, [PLAN.md](PLAN.md) for the implementation breakdown, [STATE.md](STATE.md) for current phase.

## Quickstart (when implemented)

```bash
git clone https://github.com/shhaider/conductor-chat-search.git
cd conductor-chat-search
./run.sh
```

The browser opens to `http://127.0.0.1:17891/`. Search for any phrase you remember from the chat you want to find. Click Export. The markdown appears in `~/Downloads/`.

## Design constraints

- macOS only (path-bound to Conductor's storage).
- Python 3 standard library only — no pip installs.
- Read-only against `conductor.db` (uses `mode=ro` URI; safe to run while Conductor is open).
- Loopback-only HTTP server. Single user, no auth.

## Companion CLI

`conductor-export.sh` (lives separately in `~/Downloads/`) is the original CLI version that does the same export without a GUI. Both call into the same `render` module once that refactor lands.

## Why this exists

See [SPEC.md § Goals — Broader](SPEC.md#broader). Short version: Conductor's account-switching has bugs and its chat-resume summary is lossy. Until those are fixed upstream, this gives us a fast path around them.
