# P07 — Verification + CI setup

## Goal

CI runs every test on push/PR. A verification report ties the green build to acceptance criteria A1-A10 from SPEC.md.

## Files

- `.github/workflows/ci.yml`
- `verification.md` (filled in at the end of Phase 6)

## CI workflow

`.github/workflows/ci.yml`:

```yaml
name: ci
on:
  push:
    branches: [main]
  pull_request:
permissions:
  contents: read
jobs:
  test:
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python -m pip install --upgrade pip pytest
      - run: python -m pytest tests/ -v
      - name: bash syntax check on run.sh
        run: bash -n run.sh
```

(Pytest is the one allowed third-party dep — for tests only. The runtime code stays stdlib-only.)

## Verification report

After all unit tests pass, write `verification.md`:

```
# Verification — v0.1.0

Commit: <hash>
CI run: <url>
Date: <date>

## Unit tests
- test_render.py: <N> passed
- test_db.py: <N> passed
- test_search.py: <N> passed
- test_export.py: <N> passed
- test_server.py: <N> passed

Total: <N> tests, <N> passed, <N> failed.

## Acceptance criteria check (SPEC.md A1-A10)
- [ ] A1: launching run.sh opens browser within 3s — (deferred to live validation)
- [x] A2: list endpoint returns sessions ordered by updated_at — covered by test_db test 3, test_server test 4
- [x] A3: search filters via text-block match — covered by test_search tests 2,4,9
- [x] A4: AND semantics across terms — covered by test_search test 3, test_server test 6
- [x] A5: export writes to ~/Downloads, format matches CLI — covered by test_export tests 1,2,5
- [ ] A6: Ctrl-C stops server cleanly — (deferred to live validation)
- [ ] A7: <2s search on real 329k-row DB — (deferred to live validation; informational, not a gate)
- [x] A8: read-only DB access — covered by test_db test 9
- [x] A9: no pip deps in runtime code — verified by import scan
- [x] A10: no native compile — verified by absence of build step

## Live-validation-blocked items
A1, A6, A7 require running against the operator's real environment. Reported as
"implementation complete, awaiting live validation" per definition of done.
```

## Acceptance

- CI workflow runs to green on push to feature/v0.1.0.
- verification.md exists with the table filled in.
- The "implementation-complete, live-validation-blocked" framing is followed — do NOT claim done.
