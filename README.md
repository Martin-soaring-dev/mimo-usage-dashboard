# MiMo Usage Dashboard

> 看清明 MiMo Desktop 的 token 都花在哪了。

[![release](https://img.shields.io/github/v/release/Martin-soaring-dev/mimo-usage-dashboard?label=release&color=orange)](https://github.com/Martin-soaring-dev/mimo-usage-dashboard/releases)
[![npm](https://img.shields.io/npm/v/mimo-usage-dashboard?label=npm&color=cb3837)](https://www.npmjs.com/package/mimo-usage-dashboard)
[![platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-blue)](#%E5%AE%89%E8%A3%85)
[![MiMo Desktop](https://img.shields.io/badge/MiMo%20Desktop-plugin-lightgrey)](https://mimo.xiaomi.com/coder)
[![node](https://img.shields.io/badge/node-%E2%89%A522-brightgreen)](#%E7%9B%B4%E6%8E%A5%E8%B7%91%E4%BB%AA%E8%A1%A8%E7%9B%98%E4%B8%8D%E8%A3%85%E6%8F%92%E4%BB%B6%E4%B9%9F%E8%A1%8C)
[![license](https://img.shields.io/badge/license-MIT-blue)](./LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/Martin-soaring-dev/mimo-usage-dashboard/ci.yml?label=CI&logo=github)](https://github.com/Martin-soaring-dev/mimo-usage-dashboard/actions)

简体中文 · [English](./README.en.md)

给 **MiMo Desktop**（内置 MiMoCode 引擎，OpenCode 分支）用的本地用量统计：
一个**本地网页仪表盘** + 一个 **MiMoCode 插件** + 一个**技能**。所有数据在本机读取，不出网。

- 总 tokens、会话 / 请求 / 工具 / 改动
- 输入 / 输出拆分、**缓存命中率**
- **12 个月 token 热力图**（自适应铺满，格子悬停显示日期与 token）
- 模型份额（深 / 浅 = 未命中 / 命中缓存）
- 每日 tokens 曲线、每日活跃会话；**「今日」按小时**
- 每 5 分钟自动刷新

> **为什么是网页而不是应用内页面？** MiMo 的插件 API（`@mimo-ai/plugin`）只能注册 agent 工具，
> **不能往应用界面注入自定义页面**。所以图表界面用本地网页实现，由插件/技能负责打开。

![预览](https://raw.githubusercontent.com/Martin-soaring-dev/mimo-usage-dashboard/main/docs/preview.svg)

## 安装

安装后 **重启 MiMo Desktop**，再新开一个会话。

### 方式 A：npm（推荐）

在 `~/.config/mimocode/mimocode.jsonc`（或项目 `.mimocode/mimocode.json`）里加一行：

```jsonc
{
  "plugin": ["mimo-usage-dashboard"]
}
```

引擎会自动 `npm install` 该包。

### 方式 B：GitHub 源

```jsonc
{ "plugin": ["github:Martin-soaring-dev/mimo-usage-dashboard"] }

// 无 git 环境时更稳，纯 https tarball：
{ "plugin": ["https://github.com/Martin-soaring-dev/mimo-usage-dashboard/archive/refs/heads/main.tar.gz"] }
```

### 方式 C：Windows 一键脚本

```powershell
irm https://raw.githubusercontent.com/Martin-soaring-dev/mimo-usage-dashboard/main/install.ps1 | iex
```

脚本会：把看板代码拷到 `~/.config/mimocode/usage-dashboard/`，把 `file://…/plugin/usage-dashboard.mjs`
写进配置的 `plugin` 数组，并安装技能（出现在「插件」页）。

### 方式 D：手动 clone + `file://`

```bash
git clone https://github.com/Martin-soaring-dev/mimo-usage-dashboard
```

再把 `file:///…/mimo-usage-dashboard/plugin/usage-dashboard.mjs` 加进 `plugin` 数组。

### 直接跑仪表盘（不装插件也行）

```bash
node server.mjs --open   # 需要 Node.js >= 22（用了内置 node:sqlite）
```

> **为什么是 `.mjs` 还要写进 `plugin` 数组？** MiMo 的插件目录扫描是 `{plugin,plugins}/*.{ts,js}`，**不含 `.mjs`**；
> `.mjs` 需像自带插件那样通过 `plugin` 数组以 `file://` 注册。

## 使用

在 MiMo Desktop 里：

- 说「**看我的用量 / token 消耗 / 用量统计**」→ 调用 `usage_stats`，对话里返回 Markdown 摘要。
- 说「**打开用量仪表盘**」→ 调用 `usage_dashboard`，启动本地服务并在默认浏览器打开（地址也可粘进 MiMo 内置浏览器）。
- 技能「用量统计仪表盘」会出现在 **插件页**。

直接打开仪表盘：`http://127.0.0.1:8790`。

### 实验性：在桌面头像菜单增加「用量统计」

MiMo Desktop 没有公开的 React/侧边栏 UI 插件接口。仓库提供一个**可选的 app.asar 补丁**，在头像菜单的「剩余用量」下面插入「用量统计」，点击后以弹窗 iframe 打开完整仪表盘。补丁会先备份原始 app.asar 和 app.asar.unpacked，支持还原；它**不是官方支持的插件扩展点**。

1. 克隆此仓库，在仓库目录安装开发依赖并检查：

   ```powershell
   npm install
   # 只读检查安装位置
   npm run desktop:inspect
   # 在临时目录抽取、注入并重打包验证；不改已安装 App
   npm run desktop:dry-run
   ```

2. 运行 `install.ps1` 安装仓库中的最新插件文件（插件初始化时会启动本机服务），然后完全退出 MiMo Desktop，包括托盘进程：

   ```powershell
   .\install.ps1
   ```

3. 在**管理员 PowerShell**中进入仓库目录并运行：

   ```powershell
   npm run desktop:patch
   ```

4. 重开 MiMo Desktop，打开左下角头像菜单，点「用量统计」。

还原原版：完全退出 MiMo Desktop，在管理员 PowerShell 中运行 `npm run desktop:restore`。

> **风险与限制**：应用升级可能覆盖补丁；升级后需重新运行补丁。此补丁会重打包官方 app.asar，只建议个人本机使用。若启动异常，退出应用后运行还原命令。不要把改过的 app.asar 分发给他人。

## 工作原理

```
MiMo Desktop ──► mimocode.db ──► usage-core.mjs ──► server.mjs ──► dashboard.html
                     ▲                                              (http://127.0.0.1:8790)
                     └── plugin/usage-dashboard.mjs ── 工具 usage_stats / usage_dashboard
```

- **数据层** `usage-core.mjs`：只读 `mimocode.db` 并聚合（若库被占用则做临时快照）。
- **服务** `server.mjs`：仅监听 `127.0.0.1`，提供 `/`、`/api/usage`、`/api/summary`、`/api/ping`。
- **插件** `plugin/usage-dashboard.mjs`：注册两个工具；自带定位看板文件（npm 安装）或读 `usage-dashboard.config.json`（脚本安装）。
- **技能** `skill/`：让 agent 知道何时调用工具。

## 指标口径

| 指标 | 计算方式 |
|---|---|
| **tokens 消耗** | 输入 + 输出 |
| **输入 tokens** | `tokens.input` + `tokens.cache.read` |
| **输出 tokens** | `tokens.output` + `tokens.reasoning` |
| **缓存命中率** | `cache.read / (tokens.input + cache.read)` |
| **会话 / 请求 / 工具** | `session` 行数 / assistant 消息数 / `part` 中 `type==="tool"` 数 |
| **改动** | `session` 的 `summary_additions + summary_deletions`（含文件数） |

数据来自 `~/.local/share/mimocode/mimocode.db` 的 `message` / `part` / `session` 表。

## 仓库结构

```
index.mjs                   包入口（导出插件函数）
package.json                npm 包清单
plugin/usage-dashboard.mjs  MiMoCode 插件（usage_stats / usage_dashboard）
usage-core.mjs              数据层：读库并聚合
server.mjs                  本地 HTTP 服务（仅 127.0.0.1）
dashboard.html              仪表盘页面（自带 SVG 图表，无外部依赖）
skill/                      技能（Plugins 页显示）
install.ps1 / register-plugin.mjs / start-dashboard.cmd
test/smoke.mjs              冒烟测试
.github/workflows/          CI 与发布
```

## 开发

```bash
npm install
npm test          # 语法检查 + 冒烟测试
npm run server    # 本地起仪表盘
npm pack --dry-run
npm run desktop:inspect # 查看本机 MiMo asar 信息；只读
```

## 发布（维护者）

推一个 `v*` tag 即触发 `.github/workflows/release.yml`：

```bash
git tag v0.1.1
git push origin v0.1.1
```

工作流：跑测试 → 用 tag 号覆盖 `package.json` 版本 → `npm publish --provenance --access public` → 建 GitHub Release。
认证使用仓库 Secret `NPM_TOKEN`（npm 的 **Granular token**，勾选 *Bypass two-factor authentication*，
Permissions = `Read and write (publish and stage)`，Select packages = `All Packages`）。

> 也可改用 npm **Trusted Publishing**（OIDC，免 token）：在 npm 包页配置 Trusted Publisher 指向本仓库 + `release.yml`，
> 然后删除工作流里的 `NODE_AUTH_TOKEN`。

## 安全与隐私

- 服务只监听 `127.0.0.1`，**不上传任何数据**。
- 数据库**仅只读**打开；被占用时做一份临时快照再读，**绝不写库**。

## 排错

- **对话里没有 `usage_stats`**：确认 `plugin` 数组含该条目，然后**彻底重启** MiMo Desktop。
- **工具报「服务启动超时」**：需要系统 `node` ≥ 22，或设环境变量 `MIMO_USAGE_NODE` 指向 `node.exe` 全路径。
- **端口被占**：`--port` 换端口，并同步 `usage-dashboard.config.json` 的 `port`。
- **数据不更新**：页面每 5 分钟自动刷新，也可点「刷新」。

## 卸载

```powershell
Remove-Item "$env:USERPROFILE\.config\mimocode\usage-dashboard" -Recurse -Force
Remove-Item "$env:USERPROFILE\.config\mimocode\plugin\usage-dashboard.mjs" -Force
Remove-Item "$env:USERPROFILE\.config\mimocode\skills\mimo-usage-dashboard" -Recurse -Force
Remove-Item "$env:USERPROFILE\.config\mimocode\usage-dashboard.config.json" -Force
```

再从 `plugin` 数组里删掉那条 `usage-dashboard.mjs`（脚本安装时的备份在 `mimocode.jsonc.bak-usage-dashboard`）。

## License

MIT © Martin-soaring-dev

> 本项目为**非官方**第三方插件，与小米 / MiMo 无关。
