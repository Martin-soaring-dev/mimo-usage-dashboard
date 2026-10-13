# MiMo Usage Dashboard

> See where your MiMo Desktop tokens actually go.

[![release](https://img.shields.io/github/v/release/Martin-soaring-dev/mimo-usage-dashboard?label=release&color=orange)](https://github.com/Martin-soaring-dev/mimo-usage-dashboard/releases)
[![platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-blue)](#install)
[![MiMo Desktop](https://img.shields.io/badge/MiMo%20Desktop-%2Fusage-lightgrey)](https://mimo.xiaomi.com/coder)
[![python](https://img.shields.io/badge/python-%E2%89%A53.10-brightgreen)](#install)
[![license](https://img.shields.io/badge/license-MIT-blue)](./LICENSE)

[简体中文](./README.md) · English

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
| Server | always-on `127.0.0.1:8790` | **none** |
| Inline card | `sci-widget` (AI writes HTML) | static HTML file |
| Runtime | Node ≥ 22 | **Python ≥ 3.10 (stdlib only)** |

## Install

### Option A: one-liner script (Windows)

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
- **A running instance of this server** is identity-checked (valid token + valid port + loopback + `/api/report` returning this project's payload) and then shut down through this project's own protocol, waiting for it to exit. It **never** taskkills, and never kills by port or by an unknown PID; if identity cannot be confirmed, the install aborts.
- **Backup**: files this run would overwrite are copied to `<InstallDir>\.mimo-usage-backups\<timestamp>\`, and the config gets its own timestamped `.bak`. Backups, HTML reports, the database and unknown files are **never deleted automatically**.
- **`-RemoveLegacy`** only cleans legacy files when explicitly requested, and a **non-empty directory** such as `skills\usage` additionally requires `-Force` before it is removed recursively (by default its contents are only listed).
- **`-PreflightOnly`** runs detection and prints the plan without writing anything.
- **Already current means no writes at all**: when both the files and the command registrations already match what would be installed, Backup/Stage/Apply/Verify are skipped and no new backup is created.

Fail-closed by design: an explicit `-Python` that is missing or older than 3.10 aborts instead of falling back, every auto-detected interpreter must be ≥ 3.10, and a non-zero registration exit code aborts the install instead of printing success.

Config edit policy: `register_command.py` edits **in place** — it rewrites only the text spans of `command.usage` / `command.usage-open`, so **JSONC comments, indentation style, CRLF, the BOM, other commands, other keys and any fields you added are preserved byte for byte**. Before writing it re-reads the candidate and asserts both that it parses and that the change stayed inside the target spans, then swaps it in atomically (temp file in the same directory + `os.replace`). Shapes it cannot handle reliably — **duplicate keys, a target that is not an object, an unlocatable region** — **fail closed** (exit code 6) and write nothing. A uniquely named timestamped backup is written first (a numeric suffix is added on same-second collisions, never overwriting) and its path is printed to stderr.

Then **fully restart MiMo Desktop** and **open a new session**.

### Option B: manual

1. Put `usage_cli.py` and `register_command.py` anywhere (e.g. `D:\tools\mimo-usage\`).
2. Run `python register_command.py`, or add a `command` block to
   `~/.config/mimocode/mimocode.jsonc` yourself (see the Chinese README for the exact shape).

> The shell directive inside the template is a **real instruction** — the engine will execute it.
> Never put it in comments or examples.

### Option C: just run it, no command registered

```bash
python usage_cli.py --open          # Python ≥ 3.10, no third-party deps
```

On Windows you can also **double-click `usage.cmd`**.

## Usage

| Input | Effect |
|---|---|
| `/usage` | Overview (fixed safe command; suffix arguments are not parsed) |
| `/usage-open` | Generate and open in the default browser |
Output goes to the **current working directory**: `mimo-usage-<kind>-<date>.html`.

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
```

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
usage_cli.py                core: read-only db → aggregate → self-contained HTML (stdlib only)
register_command.py         writes /usage and /usage-open into mimocode.jsonc (JSONC-aware)
install.ps1                 Windows installer
usage.cmd                   double-click to generate + open
skill/usage/                /usage command docs
skill/mimo-usage-dashboard/ dashboard explanation (docs only, executes nothing)
docs/architecture.md        engine mechanics evidence (command table, shell substitution)
docs/plugin-interface.md    MiMo plugin interface survey
test/test_python.py         Python e2e tests (aggregation / views / injection guard / JSONC)
test/command-e2e.mjs        Node side: simulates the engine's template + shell pipeline
```

## Troubleshooting

- **`/usage` does nothing**: confirm `mimocode.jsonc` has `command.usage`, then **fully restart** MiMo Desktop and **open a new session** (the command table is built at session start).
- **`MiMo database not found`**: make sure MiMo Desktop has run at least once; or pass `--db` / set `MIMO_USAGE_DB`.
- **Python not found**: install Python ≥ 3.10, or run `install.ps1 -Python <path>`.

## Uninstall

```bash
python register_command.py --uninstall
```

Then delete `~/.config/mimocode/usage-dashboard/`.

## License

MIT © Martin-soaring-dev

> Unofficial third-party tool. Not affiliated with Xiaomi / MiMo.
