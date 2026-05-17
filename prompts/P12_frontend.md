# P12 — Frontend: account badge, account filter, exact-phrase toggle, match-kind badges

## Goal

Surface the two backend features in the single-page UI.

## Files

- `src/conductor_chat/static/index.html`
- `tests/test_static_html.py` — light text asserts.

## Behaviour

### Account badge

Each result row gets a small muted badge after the workspace name showing the account, e.g. `account2`, `default`, `account-backups`. When `account` is null, show a red `(orphaned)` badge.

CSS:
- `.acct` — small font, muted color, slightly tinted background.
- `.acct.orphaned` — red text.

### Account filter dropdown

In the controls bar (next to workspace), add:

```html
<select id="account">
  <option value="">All accounts</option>
  <!-- populated dynamically from /api/accounts OR from union of seen rows -->
  <option value="__orphaned__">(orphaned)</option>
</select>
```

Populated dynamically on first sessions load: walk the response, collect the unique non-null `account` values, build options. Re-population happens whenever new sessions arrive that introduce new accounts. (Simple: on every load, recompute the set; preserve current selection if still present.)

Sending: when `account` is non-empty, append `&account=<value>` to the request.

### Exact-phrase toggle

Beside the search box, add:

```html
<label class="exact-toggle">
  <input id="exact-toggle" type="checkbox">
  Exact phrase
</label>
```

When OFF (default): current behaviour. Whitespace = AND; `"quoted"` = one term.
When ON: the entire input string is sent as ONE `q_exact=<input>` term, ignoring whitespace splits. Quotes inside still strip out (treat the input as literal phrase).

Updated `parseTerms` returns an array of `{value, exact}` objects:
- Toggle ON → `[{value: input.trim(), exact: true}]`.
- Toggle OFF → split via current regex, but `"quoted"` chunks become `{value: ..., exact: true}` and bare words become `{value: ..., exact: false}`.

When building the URL: bare words → `q=` params; exact entries → `q_exact=` params.

### Match-kind badge per row

Each row gets a left-edge accent color and a badge:
- `match_kind === "exact"` → solid accent border-left + `🎯 exact` badge with bright accent color.
- `match_kind === "and"` → no border accent, muted `🔍 and` badge.

CSS:
- `.row.exact { border-left: 3px solid var(--accent); }`
- `.badge-mk` — small inline badge.

### Snippet rendering

The server already wraps full phrases in `«…»`. The existing `snippetHtml` regex `«([^»]+)»` works as-is.

## Tests

`tests/test_static_html.py` additions:
- `assert b'id="account"' in html` (the dropdown).
- `assert b'id="exact-toggle"' in html`.
- `assert b'Exact phrase' in html`.
- `assert b'badge-mk' in html` or similar — a marker for match-kind rendering.

## Constraints

- Vanilla JS, no frameworks.
- Default toggle to OFF so existing behaviour is preserved.
- No third-party deps.
