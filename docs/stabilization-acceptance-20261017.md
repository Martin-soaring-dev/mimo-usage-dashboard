# Stabilization candidate — 2026-10-17

> Status: **NOT RELEASE-APPROVED**. This document is an evidence register and a bounded Windows acceptance contract, not a claim that unexecuted tests passed.

## Baseline and change scope

- Base `main`: `5ea1fc89ce03c8c30162af5c7fbdf7efd59a7370`.
- Candidate: `release/stabilization-20261017`.
- Runtime, database, install/uninstall behavior, endpoint security, reporting calculations: **unchanged**.
- CI change: split the five commands previously invoked by `npm test` into individually named GitHub Actions steps, preserving the original order and command content. This narrows failure attribution without changing tested behavior.

## Branch disposition (reviewed against main)

| Previous branch | Ahead / behind main at review | Decision |
| --- | --- | --- |
| `diag/issue-19-ensure-live-server` | 1 / 4 | Already integrated via PR #34; retire after confirming squash-equivalent code |
| `feat/issue-19-config-path-override` | 1 / 3 | Integrated via PR #32; retire |
| `refactor/issue-19-builtin-limits-reachable` | 1 / 2 | Integrated via PR #33; retire |
| `test/issue-19-scope-switch-regression` | 1 / 5 | Integrated via PR #30; retire |
| `fix/issue-4-session-context-binding` | 5 / 21 | Existing #4 resolution must be checked against main, **do not merge stale branch** |
| `bugfix/issue-6-desktop-artifact-preview` | 1 / 21 | Contains `docs/issue-6-preview-spike.md` not on main; #6 closed not planned. Preserve documentation through Git history, do not enable unverified native preview |

**Deletion status:** pending repository-ref deletion via an authorized Git client/API. The current connected GitHub action set provides branch creation/ref updates but no delete-ref operation. Do **not** force-update stale branches to simulate deletion.

## Release gates

1. **Source integrity:** candidate changes must stay within named CI diagnostics and this evidence file; no changes to real user data paths or live-server retry/timeout budgets.
2. **CI:** the entire `.github/workflows/ci.yml` Windows workflow must be green **for the exact candidate SHA**. A missing or inconclusive Actions check is NOT PASS.
3. **Issue #35:** a documented resolution with reliable reproduction evidence, or an explicitly approved risk disposition. The existing 70 successful local start attempts do **not** prove the intermittent failure fixed. Failure diagnostics include `reason`, `spawnedPid`, `statePid`, `stateFile`, and `stateTokenMatches`.
4. **Installed MiMo Desktop:** run `python test/e2e_live.py` only in the user's dedicated Windows acceptance environment with an intentionally installed test configuration. It reads the installed `~/.config/mimocode/mimocode.jsonc` and invokes PowerShell; it is **not** a hermetic CI test. Do not add it to generic CI without first building an isolated installation fixture.
5. **Safety:** installed commands and DB remain read-only for reporting; only loopback service, validated origin/host, and token-protected routes; installer downgrade/rollback semantics intact.
6. **Release:** PR review, evidence receipt, then explicit release approval. No automatic main merge or tag.

## Expected local verification report

Return a table per command with exit status, final PASS/FAIL, relevant failure stack, exact commit SHA, Windows / Python / Node versions, CI run URL, and whether test inputs were synthetic or user data. For #35 report diagnostic fields only on genuine failure; **do not run unlimited stress loops**. Do not paste credentials, installed config contents, private DBs, or bearer tokens.

Commands on a clean candidate checkout:

```powershell
git status --short
git rev-parse HEAD
python test/test_python.py
python test/test_issue5.py
python test/test_installer.py
node test/runtime-check.mjs
node test/command-e2e.mjs
node test/ui-dom.mjs
node test/ui-geometry.mjs --require-chrome
node test/refresh-e2e.mjs --require-chrome
node test/scope-switch.mjs --require-chrome
node test/cli.test.mjs
powershell -NoProfile -ExecutionPolicy Bypass -File test/install-smoke.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File test/install-upgrade.ps1
npm pack --dry-run
```

If any prerequisite is unavailable, record **BLOCKED (environment)**, not PASS. No need to run `test/e2e_live.py` against personal real data by default.
