# P04 — `src/conductor_chat/static/index.html`

## Goal

Single self-contained HTML file. Vanilla JS, no build, no framework. ~150 lines including inline CSS.

## File

- `src/conductor_chat/static/index.html`

## Page structure

```
<header>
  Conductor Chat Search   |   button: Refresh
</header>

<controls>
  Search input: placeholder "type words; space = AND; quote = exact"
  Clear button
  Workspace dropdown (loaded from /api/workspaces)
</controls>

<results>
  Repeated row:
    <row>
      first line: title  ·  workspace_name  ·  model  ·  age (e.g. "2h ago")  ·  msg count
      if any snippets: indented block of "term: …«term»…"  lines
      buttons: Export
    </row>
</results>

<status bar>
  "Last export: <path>" (or empty)
</status>
```

## Behavior

- On load: `GET /api/sessions` (no terms) — populate result list. Concurrently `GET /api/workspaces` — populate dropdown.
- On search-input change: debounce 200ms idle, then `GET /api/sessions?q=<term1>&q=<term2>&workspace=<wsid>` with terms split by whitespace (respect double-quoted phrases as one term).
- On workspace dropdown change: re-fetch with the new filter.
- On Refresh: re-fetch with current params.
- On Clear: blank the search input, re-fetch.
- On Export click: `POST /api/export` with `{session_id}` JSON. On success: update status bar with returned path + copy path to clipboard (`navigator.clipboard.writeText(path)`). On failure: replace status bar text with red error message.

## Visual conventions

- Monospace for ids, code-style background for snippets.
- Highlight `«term»` markers from the snippet response — replace with `<mark>term</mark>`.
- Each row clickable to toggle a "details" section showing all snippets (optional v1, OK to omit if it makes the file complex).
- Age formatting: relative (e.g. "2h ago", "3d ago"). 1 line of JS.

## Acceptance

- File is a single self-contained HTML (no external CSS / JS imports — inline only).
- Loads in any modern browser without errors in console.
- Search debounce works (don't fire on every keystroke).
- Highlighted matches render correctly (test: search for a known term, see `<mark>` element in DOM).
- Workspace dropdown works.
- Export button writes a file and shows the path in status bar.

## Tests (manual + automatable)

For CI/automation:
- Server-side route tests already cover the API surface.
- Frontend: include a small Playwright smoke test if Playwright is available; otherwise skip and rely on the manual live-validation phase.

For the live validation phase (operator runs after merge):
- Open page → list of sessions renders.
- Type "metabuilder" → results filter to chats containing that text.
- Add a second term "merge" → results narrow.
- Click Export on a row → file appears in ~/Downloads.

## Constraints

- No external JS/CSS imports. No CDNs. (Loopback-only server; works offline.)
- Total HTML file ≤ 250 lines.
- Vanilla JS only — no React, no jQuery, no anything.
