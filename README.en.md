# MiMo Usage Dashboard

> See where your MiMo Desktop tokens actually go.

[![release](https://img.shields.io/github/v/release/Martin-soaring-dev/mimo-usage-dashboard?label=release&color=orange)](https://github.com/Martin-soaring-dev/mimo-usage-dashboard/releases)
[![platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-blue)](#install)
[![MiMo Desktop](https://img.shields.io/badge/MiMo%20Desktop-%2Fusage-lightgrey)](https://mimo.xiaomi.com/coder)
[![python](https://img.shields.io/badge/python-%E2%89%A53.10-brightgreen)](#install)
[![license](https://img.shields.io/badge/license-MIT-blue)](./LICENSE)

[简体中文](./README.md) · English · **[Quick start (中文)](./docs/quick-start.md)**

Local usage stats for **MiMo Desktop** (built on the MiMoCode engine, an OpenCode fork):
a single **local command** that renders usage into an **HTML dashboard**.

**Zero AI involvement. No network upload.** `/usage` writes a static snapshot;
`/usage-open` may start an optional loopback-only (`127.0.0.1`) read-only refresh server.

```
/usage      ─►  the engine runs `python usage_cli.py` locally
            ─►  reads mimocode.db → aggregates → writes a static HTML snapshot
/usage-open ─►  same, plus a loopback refresh server + browser open
            ─►  the AI just relays that one line
```

- **Token usage**: total tokens, input/output split, **cache hit rate**, requests/tool calls, daily sparkline
- **Model share**: per-model token share (with each model's cache hit rate)
- **Session usage**: session count, cumulative tokens, lines/files changed, recent session table
- **Context occupancy**: current session's context tokens, composition (cache read / fresh input / reasoning / output), model, working directory

## Why "local command + static page"

Two hard constraints in the MiMo app itself (see [`docs/architecture.md`](./docs/architecture.md)):

1. **The plugin API is server-side only.** `@mimo-ai/plugin` can register agent tools but
   **cannot inject custom pages into the UI** (the right-panel menu is a compile-time constant).
2. **Inline `sci-widget` cards must be written out by the AI, token by token.** While the HTML streams,
   the chat shows a "generating interactive demo…" placeholder and burns a lot of tokens.

So we take a **third path**: let the engine's `command` mechanism **run a local script first**.

The engine has built-in shell substitution (`src/config/markdown.ts`, `SHELL_REGEX`): a backtick-wrapped
command inside `command.template` is **actually executed locally**, and its stdout is spliced back into
the text — exactly the kind of deterministic local function that qwen code's `/update` provides.

| | Old (v0.1.x) | New (v0.2+) |
|---|---|---|
| `/usage` mechanism | skill → AI understands → AI calls tool → AI pastes result | `command` + local shell substitution |
| AI involvement | required (two hops) | **relays one line only** |
| Server | always-on `127.0.0.1:8790` | **none by default**; `/usage-open` starts an on-demand loopback-only read-only server |
| Inline card | `sci-widget` (AI writes HTML) | static HTML file |
| Runtime | Node ≥ 22 | **Python ≥ 3.10 (stdlib only)** |

## Install

### Option A: npm (recommended)

```powershell
npm install -g mimo-usage-dashboard
mimo-usage-dashboard install
```

**Those are two different steps — running only the first one installs nothing.** `npm install -g` merely puts the CLI on your PATH (it touches none of your configuration); `mimo-usage-dashboard install` is what actually installs the dashboard into MiMo. Then **fully restart MiMo Desktop**, **open a new session** and type `/usage`.

| Command | What it does |
|---|---|
| `mimo-usage-dashboard install` | Install (repeat installs take the same safe path; when already current no file is reinstalled, no backup is created and Backup/Stage/Apply/Verify are skipped — the only possible write is the manifest metadata when its recorded version lags the package) |
| `mimo-usage-dashboard upgrade` | Upgrade to the version bundled with this CLI; **refuses to silently downgrade** (unless `--allow-downgrade`) |
| `mimo-usage-dashboard uninstall` | Unregister and remove only files provably owned by this project and untouched by you; reports, database and backups are kept |
| `mimo-usage-dashboard doctor` | Read-only health report: Python, config, registration, install state, running instance, residue. Exit 0 healthy / 1 warnings / 2 problems |
| `mimo-usage-dashboard where` | Print every relevant path (useful when troubleshooting) |
| `mimo-usage-dashboard version` | CLI version and installed version |
| `mimo-usage-dashboard report` | Write a static snapshot and print its path (same as `/usage`) |
| `mimo-usage-dashboard open` | Open the live board (same as `/usage-open`) |

Common flags: `--config <mimocode.jsonc>`, `--install-dir <dir>`, `--python <python.exe>`. Beyond Python there are no other requirements, and the CLI itself has zero npm dependencies.

To try it without a global install: `npx mimo-usage-dashboard doctor`.

Uninstall:

```powershell
mimo-usage-dashboard uninstall                   # unregister + remove project files (keeps reports/DB/backups)
mimo-usage-dashboard uninstall --keep-files      # unregister only, keep every file
mimo-usage-dashboard uninstall --purge-backups   # also clear backups (listed before removal)
```

### Option B: one-liner script (Windows, no Node required)

If you cloned the repository and have no Node on the machine:

```powershell
git clone https://github.com/Martin-soaring-dev/mimo-usage-dashboard
cd mimo-usage-dashboard
powershell -ExecutionPolicy Bypass -File install.ps1
```

By default it copies the CLI to `~/.config/mimocode/usage-dashboard/` and registers `command.usage` / `command.usage-open`. If the local AI identifies a different active config path, pass it explicitly:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -Config "C:\actual\path\mimocode.jsonc" -Python "C:\actual\path\python.exe"
```

You can also pass `-InstallDir`. For local path discovery and user-confirmed live acceptance, see [Windows live test guide](./docs/windows-live-test-prompts.md).

#### The installer is a reliable upgrader, not a one-shot copy

Installs run as **Preflight → Backup → Stage → Validate → Apply → Verify**; any failing stage rolls back what this run changed.

- **Pre-install detection**: existing project files, an existing `install-manifest.json`, and a running instance of this project's server are all detected first. An existing install enters `upgrade` (manifest present) or `adopt` (no manifest but the files carry this project's ownership marker) mode — it is **never overwritten blindly**.
- **`install-manifest.json`** records product name, version, install time and the sha256 of each managed script, so the installer can tell **our files** from **yours**. Without a manifest, ownership is decided only by the marker inside the file — never by filename.
- **Unowned or modified files are refused**, with the reasons listed. If you have confirmed those files came from an older release of this project, re-run with `-Force` (they are still backed up first).
- **A running instance of this server**: identity = valid token + valid port + loopback + `GET /{token}/api/ping` returning this project's identity marker. Only when all three checks pass does the installer send `shutdown` over this project's own protocol and wait for exit (a failed shutdown aborts the install). If the listener is **reachable but not this project's service**, the install aborts and leaves it alone. If the state is **unreachable/invalid**, the installer treats it as stale and **continues installing** — a service started by an earlier version that predates the `/api/ping` route falls into this class (the probe gets HTTP 404), so a graceful shutdown of that process is **not** guaranteed; close it yourself or let it exit on its own idle timeout. In **every** branch it never taskkills and never kills by port or by an unknown PID. Identity probing uses the constant-time `/api/ping` (it reads no database), because `/api/report` aggregates the whole database on every request and would exceed the probe timeout on a large database.
- **Backup**: files this run would overwrite are copied to `<InstallDir>\.mimo-usage-backups\<timestamp>\`, and the config gets its own timestamped `.bak`. Backups, HTML reports, the database and unknown files are **never deleted automatically**.
- **`-RemoveLegacy`** only cleans legacy files when explicitly requested, and a **non-empty directory** such as `skills\usage` additionally requires `-Force` before it is removed recursively (by default its contents are only listed).
- **`-PreflightOnly`** runs detection and prints the plan **without installing any file or touching your config**; it does create the target install directory and may gracefully shut down a running instance of this project's own service (own protocol only — a listener that is not this project's service aborts the run; an unreachable or invalid one is treated as stale; it never force-kills by port or PID).
- **Already current means no reinstall**: when both the program files and the command registrations already match what would be installed, nothing is reinstalled or overwritten, no new backup is created, and Backup/Stage/Apply/Verify are skipped. The only possible write is **metadata**: if the version recorded in `install-manifest.json` lags the current package version, the installer updates that one file; when the version already matches, the manifest is not rewritten and `installedAt` and its other fields stay as they were.

Fail-closed by design: an explicit `-Python` that is missing or older than 3.10 aborts instead of falling back, every auto-detected interpreter must be ≥ 3.10, and a non-zero registration exit code aborts the install instead of printing success.

Config edit policy: `register_command.py` edits **in place** — it rewrites only the text spans of `command.usage` / `command.usage-open`, so **JSONC comments, indentation style, CRLF, the BOM, other commands, other keys and any fields you added are preserved byte for byte**. Before writing it re-reads the candidate and asserts both that it parses and that the change stayed inside the target spans, then swaps it in atomically (temp file in the same directory + `os.replace`). Shapes it cannot handle reliably — **duplicate keys, a target that is not an object, an unlocatable region** — **fail closed** (exit code 6) and write nothing. A uniquely named timestamped backup is written first (a numeric suffix is added on same-second collisions, never overwriting) and its path is printed to stderr.

Then **fully restart MiMo Desktop** and **open a new session**.

### Option C: manual

1. Put `usage_cli.py` and `register_command.py` anywhere (e.g. `D:\tools\mimo-usage\`).
2. Run `python register_command.py`, or add a `command` block to
   `~/.config/mimocode/mimocode.jsonc` yourself (see the Chinese README for the exact shape).

> The shell directive inside the template is a **real instruction** — the engine will execute it.
> Never put it in comments or examples.

### Option D: just run it, no command registered

```bash
python usage_cli.py --open          # Python ≥ 3.10, no third-party deps
```

On Windows you can also **double-click `usage.cmd`**.

## Usage

| Input | Effect |
|---|---|
| `/usage` | Overview (fixed safe command; suffix arguments are not parsed) |
| `/usage-open` | Generate and open in the default browser |
Output goes to the **user data directory** (`%LOCALAPPDATA%\mimo-usage-dashboard` on Windows, `~/.local/share/mimo-usage-dashboard` elsewhere; override with `MIMO_USAGE_DATA_DIR` or `--out`): `mimo-usage-<kind>-<date>.html`.

### CLI

```bash
python usage_cli.py                     # overview
python usage_cli.py token --days 7
python usage_cli.py session
python usage_cli.py context
python usage_cli.py --open
python usage_cli.py --out D:/x.html
python usage_cli.py --db <path>
python usage_cli.py --print-path
python usage_cli.py reset-bindings      # clear the per-tab session freeze (see below)
```

**About the session freeze**: every `/usage-open` opens a **distinct URL**. The first time that URL is loaded, the dashboard pins whatever session was most recently active at that moment to that URL. The tab then keeps showing that session on refresh instead of silently switching to another one, and the address bar carries the pinned session id so the URL can be copied or bookmarked.

The page itself offers **only refresh and the usage-scope switch**; to release the freeze — for instance when it pinned a session that had merely been active just before — use the CLI:

```bash
python usage_cli.py reset-bindings
```

The **session / global** switch below the tab bar picks what is shown: the frozen session, or the whole-database aggregate (which is session-independent). Note that the freeze captures the **most recently active session at open time**, which is **not** the same as the session that ran the command — slash commands cannot obtain session identity, as measured.

### Refresh

| Mode | Refresh behavior |
|---|---|
| `python usage_cli.py` / `/usage` | **Static snapshot** (default path is the user data dir; override with `--out`). No live service; the refresh button explains how to open the live board. |
| `python usage_cli.py --open` / `/usage-open` | **Live board**. Starts/reuses a read-only server bound to `127.0.0.1` and opens `http://127.0.0.1:<port>/<token>/`. "Refresh data" re-reads the DB and updates charts; failures keep the last good data and show an error. |

Live server limits: loopback only, random path token, Host/Origin checks, JSON API without absolute DB paths, idle auto-exit (~4h), `--shutdown` to stop.

## Metrics

| Metric | Formula |
|---|---|
| **tokens consumed** | input + output |
| **input tokens** | `tokens.input + tokens.cache.read` |
| **output tokens** | `tokens.output + tokens.reasoning` |
| **cache hit rate** | `cache.read / (tokens.input + cache.read)` |
| **sessions / requests / tools** | `session` rows / assistant messages / `part` rows with `type==="tool"` |
| **changes** | `session.summary_additions + summary_deletions` (plus file count) |
| **context occupancy** | latest `step-finish` part's `tokens.total` (falls back to the latest assistant message) |

Source: the `message` / `part` / `session` tables in `~/.local/share/mimocode/mimocode.db`.

## Security & privacy

- **No external network** / no data upload. `/usage` does not open a port.
- `/usage-open` may start a loopback-only (`127.0.0.1`) read-only HTTP helper with token/Origin checks and idle exit; it never binds `0.0.0.0` and never writes the DB.
- Database opened **read-only** (`mode=ro`); if locked, a temporary snapshot is copied and read — **never written**.
- The registered command does not interpolate user input into shell text. Pass filters directly to the local CLI; database and output paths are validated.
## Layout

```
bin/mimo-usage.js           npm entry point (no third-party deps)
usage_cli.py                core: read-only db → aggregate → self-contained HTML (stdlib only)
register_command.py         writes /usage and /usage-open into mimocode.jsonc (JSONC-aware)
installer.py                transactional install / upgrade / uninstall engine (single source of truth)
install.ps1                 Windows shell: finds Python, forwards to installer.py
usage.cmd                   double-click to generate + open
docs/architecture.md        engine mechanics evidence (command table, shell substitution)
docs/plugin-interface.md    MiMo plugin interface survey
test/test_python.py         Python e2e tests (aggregation / views / injection guard / JSONC)
test/test_installer.py      installer engine tests (doctor / uninstall / residue, isolated)
test/command-e2e.mjs        Node side: simulates the engine's template + shell pipeline
test/cli.test.mjs           npm pack → install → full CLI lifecycle, isolated prefix
```

## Troubleshooting

- **`/usage` does nothing**: confirm `mimocode.jsonc` has `command.usage`, then **fully restart** MiMo Desktop and **open a new session** (the command table is built at session start).
- **`MiMo database not found`**: make sure MiMo Desktop has run at least once; or pass `--db` / set `MIMO_USAGE_DB`.
- **Python not found**: install Python ≥ 3.10, or point at an interpreter explicitly — `mimo-usage-dashboard install --python <path>` (or `MIMO_USAGE_PY`), or `install.ps1 -Python <path>` on the PowerShell path.
- **The context card shows "limit unknown", or a limit that disagrees with MiMo Desktop**: the limit is read per `(providerID, modelID)` from the `limit.context` your config declares — first `~/.config/mimocode/mimocode.jsonc`, then `mimocode.jsonc` / `.mimocode/mimocode.jsonc` in the session working directory. If your config does not declare the model, or cannot be read, the card says "limit unknown" rather than guessing (unknown beats wrong). Point `MIMO_USAGE_CONFIG` at your config if it lives elsewhere, or pass `--limit` to override it for one run.

## Uninstall

```bash
python register_command.py --uninstall
```

Or let the CLI do it (recommended — it prints exactly what it kept):

```powershell
mimo-usage-dashboard uninstall
```

Manually: unregister the commands, then delete `~/.config/mimocode/usage-dashboard/` and the `command.usage` / `command.usage-open` entries.

## Acknowledgements

Thanks to the teams behind **Xiaomi MiMo models** and **MiMo Desktop / MiMoCode**. The everyday experience of using MiMo Desktop inspired this project, and its local custom-command mechanism made it possible to integrate `/usage` and `/usage-open` with a local analytics tool.

This independent community project aims to make token usage, caching, and context consumption easier for MiMo Desktop users to understand. Thanks also to the developer community for sharing experiences and feedback.

> **Disclaimer:** This project is independently developed and maintained by the community. It is not an official Xiaomi or MiMo product, and these acknowledgements do not imply sponsorship, endorsement, or affiliation.

## License

MIT © Martin-soaring-dev
