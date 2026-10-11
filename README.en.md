# MiMo Usage Dashboard

> See where your MiMo Desktop tokens actually go.

[![release](https://img.shields.io/github/v/release/Martin-soaring-dev/mimo-usage-dashboard?label=release&color=orange)](https://github.com/Martin-soaring-dev/mimo-usage-dashboard/releases)
[![platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-blue)](#install)
[![MiMo Desktop](https://img.shields.io/badge/MiMo%20Desktop-%2Fusage-lightgrey)](https://mimo.xiaomi.com/coder)
[![python](https://img.shields.io/badge/python-%E2%89%A53.10-brightgreen)](#install)
[![license](https://img.shields.io/badge/license-MIT-blue)](./LICENSE)

[简体中文](./README.md) · English

Local usage stats for **MiMo Desktop** (built on the MiMoCode engine, an OpenCode fork):
a single **local command** that renders usage into a **self-contained static HTML dashboard**.

**Zero AI involvement, no long-running server, no ports, no network.**

```
/usage  ─►  the engine runs `python usage_cli.py` locally
        ─►  the script reads mimocode.db → aggregates → renders static HTML → writes to cwd
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

It copies the CLI to `~/.config/mimocode/usage-dashboard/`, writes `command.usage` /
`command.usage-open` into `mimocode.jsonc`, and cleans up leftovers from the old design.

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

The HTML is a **static snapshot** (the "regenerate" button just tells you to re-run the command;
it makes no network requests). Re-run the same command to refresh.

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

- **No network**, no data upload, no listening port.
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
