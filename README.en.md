# MiMo Usage Dashboard

> See where your MiMo Desktop tokens actually go.

[![release](https://img.shields.io/github/v/release/Martin-soaring-dev/mimo-usage-dashboard?label=release&color=orange)](https://github.com/Martin-soaring-dev/mimo-usage-dashboard/releases)
[![npm](https://img.shields.io/npm/v/mimo-usage-dashboard?label=npm&color=cb3837)](https://www.npmjs.com/package/mimo-usage-dashboard)
[![platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-blue)](#install)
[![MiMo Desktop](https://img.shields.io/badge/MiMo%20Desktop-plugin-lightgrey)](https://mimo.xiaomi.com/coder)
[![node](https://img.shields.io/badge/node-%E2%89%A522-brightgreen)](#run-the-dashboard-standalone)
[![license](https://img.shields.io/badge/license-MIT-blue)](./LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/Martin-soaring-dev/mimo-usage-dashboard/ci.yml?label=CI&logo=github)](https://github.com/Martin-soaring-dev/mimo-usage-dashboard/actions)

English · [简体中文](./README.md)

Local usage statistics for **MiMo Desktop** (the embedded MiMoCode engine, an OpenCode fork):
a **local web dashboard** + a **MiMoCode plugin** + a **skill**. Everything is read locally — nothing leaves your machine.

- Total tokens, sessions / requests / tools / edits
- Input / output split, **cache hit rate**
- **12-month token heatmap** (fills the card, cells show date + tokens on hover)
- Model share (dark / light = cache-miss / cache-hit)
- Daily tokens curve and daily active sessions; **"Today" is hourly**
- Auto-refreshes every 5 minutes

> **Why a web page instead of an in-app page?** The MiMo plugin API (`@mimo-ai/plugin`) can only register
> agent tools — it **cannot inject custom UI into the app**. So the charts live in a local web page that the
> plugin/skill opens for you.

![preview](https://raw.githubusercontent.com/Martin-soaring-dev/mimo-usage-dashboard/main/docs/preview.svg)

## Install

After installing, **restart MiMo Desktop** and open a new session.

### Option A: npm (recommended)

Add one line to `~/.config/mimocode/mimocode.jsonc` (or project `.mimocode/mimocode.json`):

```jsonc
{
  "plugin": ["mimo-usage-dashboard"]
}
```

The engine installs the package from npm automatically.

### Option B: from GitHub

```jsonc
{ "plugin": ["github:Martin-soaring-dev/mimo-usage-dashboard"] }

// more robust without git (plain https tarball):
{ "plugin": ["https://github.com/Martin-soaring-dev/mimo-usage-dashboard/archive/refs/heads/main.tar.gz"] }
```

### Option C: one-line script (Windows)

```powershell
irm https://raw.githubusercontent.com/Martin-soaring-dev/mimo-usage-dashboard/main/install.ps1 | iex
```

It copies the dashboard into `~/.config/mimocode/usage-dashboard/`, registers
`file://…/plugin/usage-dashboard.mjs` in the `plugin` array, and installs the skill (shown on the Plugins page).

### Option D: manual clone + `file://`

```bash
git clone https://github.com/Martin-soaring-dev/mimo-usage-dashboard
```

Then add `file:///…/mimo-usage-dashboard/plugin/usage-dashboard.mjs` to the `plugin` array.

### Run the dashboard standalone

```bash
node server.mjs --open   # requires Node.js >= 22 (uses built-in node:sqlite)
```

> **Why register a `.mjs` in the `plugin` array?** MiMo scans plugin dirs with `{plugin,plugins}/*.{ts,js}`,
> which **excludes `.mjs`**; a `.mjs` must be registered via the `plugin` array with a `file://` URL, like the
> app's own plugins.

## Usage

Inside MiMo Desktop:

- Say **"show my usage / token consumption / usage stats"** → calls `usage_stats` and returns a Markdown summary in chat.
- Say **"open the usage dashboard"** → calls `usage_dashboard`, starts the local server and opens it in your browser
  (the URL also works in MiMo's built-in browser).
- The "Usage Dashboard" skill appears on the **Plugins page**.

Open the dashboard directly at `http://127.0.0.1:8790`.

### Experimental: add “Usage” to the Desktop account menu

MiMo Desktop has no public React/sidebar UI plugin API. This repository includes an **optional app.asar patch** that adds a “用量统计” item below “剩余用量” in the account menu and opens the full dashboard in a modal iframe. It backs up the original `app.asar` and `app.asar.unpacked` and includes a restore command. This is **not an officially supported extension point**.

1. Clone this repository and run:

   ```powershell
   npm install
   # Read-only installation inspection
   npm run desktop:inspect
   # Extract, inject, and repack in a temp directory only; does not alter the installed app
   npm run desktop:dry-run
   ```

2. Run `install.ps1` to install the latest plugin file from this checkout (the plugin starts the local service on initialization), then fully quit MiMo Desktop, including its tray process:

   ```powershell
   .\install.ps1
   ```

3. In an **Administrator PowerShell** window, switch to the repository and run:

   ```powershell
   npm run desktop:patch
   ```

4. Restart MiMo Desktop, open the account menu at the lower left, and choose “用量统计”.

To restore the original app, fully quit MiMo Desktop and run `npm run desktop:restore` in an Administrator PowerShell window.

> **Risks and limitations**: App updates can overwrite this patch; rerun the patch after updating. It repacks the official app.asar and is intended for personal use only. If startup fails, quit the app and restore the backup. Do not distribute a modified app.asar.

## Extensibility (read this before adding an "entry" / button)

The MiMo **engine** is open source ([XiaomiMiMo/MiMo-Code](https://github.com/XiaomiMiMo/MiMo-Code), MIT), but the **desktop app is closed source**. The right-panel "+" menu and sidebar tabs are constants compiled into the bundle — only an app.asar patch can change them. What the plugin ecosystem *can* add: entries in the composer `/` menu (Command / Skill / MCP), inline `sci-widget` blocks in the chat, and artifacts / the built-in browser.

Full interface inventory, code evidence, and three possible routes: [`docs/extensibility.md`](./docs/extensibility.md) (Chinese).

## How it works

```
MiMo Desktop ──► mimocode.db ──► usage-core.mjs ──► server.mjs ──► dashboard.html
                     ▲                                             (http://127.0.0.1:8790)
                     └── plugin/usage-dashboard.mjs ── tools: usage_stats / usage_dashboard
```

- **Data layer** `usage-core.mjs`: opens `mimocode.db` read-only and aggregates (falls back to a temp snapshot if locked).
- **Server** `server.mjs`: binds to `127.0.0.1` only; serves `/`, `/api/usage`, `/api/summary`, `/api/ping`.
- **Plugin** `plugin/usage-dashboard.mjs`: registers two tools; locates its own dashboard files (npm install) or reads `usage-dashboard.config.json` (script install).
- **Skill** `skill/`: tells the agent when to call the tools.

## Metric definitions

| Metric | Definition |
|---|---|
| **tokens** | input + output |
| **input tokens** | `tokens.input` + `tokens.cache.read` |
| **output tokens** | `tokens.output` + `tokens.reasoning` |
| **cache hit rate** | `cache.read / (tokens.input + cache.read)` |
| **sessions / requests / tools** | `session` rows / assistant messages / `part` rows with `type==="tool"` |
| **edits** | `session.summary_additions + summary_deletions` (plus file count) |

Source: the `message` / `part` / `session` tables in `~/.local/share/mimocode/mimocode.db`.

## Repository layout

```
index.mjs                   package entry (exports the plugin)
package.json                npm manifest
plugin/usage-dashboard.mjs  MiMoCode plugin (usage_stats / usage_dashboard)
usage-core.mjs              data layer: read + aggregate the DB
server.mjs                  local HTTP server (127.0.0.1 only)
dashboard.html              dashboard page (hand-rolled SVG charts, no external deps)
skill/                      skill (shown on the Plugins page)
install.ps1 / register-plugin.mjs / start-dashboard.cmd
test/smoke.mjs              smoke test
.github/workflows/          CI and release
docs/preview.svg            preview image
docs/extensibility.md       extension-surface survey (plugin / MCP / command / sci-widget / artifacts)
desktop-patch/              experimental app.asar patch (menu injection + restore)
```

## Development

```bash
npm install
npm test          # syntax check + smoke test
npm run server    # run the dashboard locally
npm pack --dry-run
npm run desktop:inspect # read-only inspection of the local MiMo asar
npm run desktop:dry-run # patch/repack validation in a temporary directory
```

## Publishing (maintainers)

Pushing a `v*` tag triggers `.github/workflows/release.yml`:

```bash
git tag v0.1.1
git push origin v0.1.1
```

The workflow: runs tests → sets the version from the tag → `npm publish --provenance --access public` → creates a GitHub Release.
Auth uses the repo secret `NPM_TOKEN` (an npm **granular token** with *Bypass two-factor authentication* checked,
Permissions = `Read and write (publish and stage)`, Select packages = `All Packages`).

> You can switch to npm **Trusted Publishing** (OIDC, no token): configure a Trusted Publisher on the npm package page
> pointing at this repo + `release.yml`, then drop `NODE_AUTH_TOKEN` from the workflow.

## Security & privacy

- The server binds to `127.0.0.1` only and **never uploads any data**.
- The database is opened **read-only**; if it is locked, a temp snapshot is used — the DB is **never written**.

## Troubleshooting

- **No `usage_stats` tool**: make sure the `plugin` array contains the entry, then **fully restart** MiMo Desktop.
- **"service start timed out"**: you need system `node` >= 22, or set `MIMO_USAGE_NODE` to the full path of `node.exe`.
- **Port in use**: use `--port` and update `port` in `usage-dashboard.config.json`.
- **Data not changing**: the page auto-refreshes every 5 minutes; you can also click "刷新".

## Uninstall

```powershell
Remove-Item "$env:USERPROFILE\.config\mimocode\usage-dashboard" -Recurse -Force
Remove-Item "$env:USERPROFILE\.config\mimocode\plugin\usage-dashboard.mjs" -Force
Remove-Item "$env:USERPROFILE\.config\mimocode\skills\mimo-usage-dashboard" -Recurse -Force
Remove-Item "$env:USERPROFILE\.config\mimocode\usage-dashboard.config.json" -Force
```

Then remove the `usage-dashboard.mjs` entry from the `plugin` array (the script install backs it up as `mimocode.jsonc.bak-usage-dashboard`).

## License

MIT © Martin-soaring-dev

> This is an **unofficial** third-party plugin, not affiliated with Xiaomi / MiMo.
