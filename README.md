# MiMo Usage Dashboard

> 看清明 MiMo Desktop 的 token 都花在哪了。

[![release](https://img.shields.io/github/v/release/Martin-soaring-dev/mimo-usage-dashboard?label=release&color=orange)](https://github.com/Martin-soaring-dev/mimo-usage-dashboard/releases)
[![platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-blue)](#%E5%AE%89%E8%A3%85)
[![MiMo Desktop](https://img.shields.io/badge/MiMo%20Desktop-%2Fusage-lightgrey)](https://mimo.xiaomi.com/coder)
[![python](https://img.shields.io/badge/python-%E2%89%A53.10-brightgreen)](#%E5%AE%89%E8%A3%85)
[![license](https://img.shields.io/badge/license-MIT-blue)](./LICENSE)

简体中文 · [English](./README.en.md)

给 **MiMo Desktop**（内置 MiMoCode 引擎，OpenCode 分支）做**本机**用量统计：
一个**本地命令行**，把用量渲染成**自包含的静态 HTML 看板**。

**零 AI 参与、零常驻服务、零端口、零联网。**

```
/usage  ─►  引擎本地执行 python usage_cli.py
        ─►  脚本只读 mimocode.db → 聚合 → 渲染静态 HTML → 写入当前目录
        ─►  AI 只把这行结果原样转述
```

### Token 页的区块顺序

1. **Hero 大卡** — 范围内 tokens 总量 + 最近 30 天迷你趋势
2. **8 个统计小卡**（2×4）— 输入 / 输出 / 缓存读取 / 缓存命中率 / 请求数 / 工具调用 / 会话数 / 平均每次请求
3. **Token 热力图** — 最近 12 个月 × 每日 tokens，GitHub 风格 4 档分位配色（**不受 `--days` 限制**，始终看全年）
4. **按模型的调用情况** — 每个模型一行：token 总量、请求 / 工具数、占比条、**缓存命中率条**（浅蓝=命中、橙=未命中）

配色语义互不混用：

| 区块 | 配色 |
|---|---|
| 模型行命中率条 | 浅蓝=命中，**橙=未命中（警示）** |
| 上下文构成条 | 浅蓝=缓存读取，蓝=新输入，紫=推理，绿=输出 |

- **Session 用量**：会话总数、累计 tokens、改动行 / 文件数、最近会话明细
- **上下文占用**：当前会话上下文 tokens、构成（缓存读取 / 新输入 / 推理 / 输出）、模型、工作目录

## 为什么是「本地命令 + 静态网页」

两条来自 MiMo 应用本体的硬约束（详见 [`docs/architecture.md`](./docs/architecture.md)）：

1. **插件 API 只有服务端能力**。`@mimo-ai/plugin` 只能注册 agent 工具，
   **不能往应用界面注入自定义页面**（右侧面板菜单是编译进 bundle 的常量）。
2. **对话内的 `sci-widget` 卡片必须由 AI 逐字输出 HTML**。只要卡片 HTML 由 AI 流式生成，
   正文里就会经历一段「正在生成交互演示…」的等待，还消耗大量 token。

于是改走**第三条路**：让 MiMo 引擎的 `command` 机制**先在本地执行脚本**。

引擎内置 shell 替换语法（`src/config/markdown.ts` 的 `SHELL_REGEX`）：
写在 `command.template` 里的反引号命令会在**本地真实执行**，stdout 插回文本 ——
这就是 qwen code `/update` 那种**确定性本地函数**。

| | 旧方案（v0.1.x） | 新方案（v0.2+） |
|---|---|---|
| `/usage` 机制 | 技能 → AI 理解 → AI 调工具 → AI 贴结果 | `command` + 本地 shell 替换 → **本地执行** |
| AI 参与 | 必须（两跳） | **仅复述一行输出** |
| 服务 | 常驻 `127.0.0.1:8790` | **无** |
| 对话内卡片 | `sci-widget`（AI 逐字输出 HTML） | 静态 HTML 文件 |
| 运行时 | Node ≥ 22 | **Python ≥ 3.10（仅标准库）** |

## 安装

### 方式 A：一键脚本（Windows）

```powershell
git clone https://github.com/Martin-soaring-dev/mimo-usage-dashboard
cd mimo-usage-dashboard
powershell -ExecutionPolicy Bypass -File install.ps1
```

脚本会：把 CLI 拷到 `~/.config/mimocode/usage-dashboard/`，并把 `command.usage` /
`command.usage-open` 写进 `mimocode.jsonc`，同时清理旧版遗留的技能与配置。

安装后**彻底重启 MiMo Desktop**，再**新开一个会话**。

### 方式 B：手动

1. 把 `usage_cli.py` 与 `register_command.py` 放到任意目录（例如 `D:\tools\mimo-usage\`）。
2. 在 `~/.config/mimocode/mimocode.jsonc` 里加一个 `command` 段：

```jsonc
{
  "command": {
    "usage": {
      "description": "MiMo Desktop 本机用量看板",
      "template": "用户请求查看用量。下面一行是本地脚本的真实输出：\n\n<反引号 shell 命令>\n\n请把这一行原样告诉用户，不要臆造数字。"
    }
  }
}
```

其中 shell 命令写作：`!` + 反引号包裹的 `python <路径>\usage_cli.py --arg-string '$ARGUMENTS'`。

> **`$ARGUMENTS` 必须用单引号包裹**，别改成双引号或不加引号 ——
> 引擎是先替换 `$ARGUMENTS`、再扫 shell 的，包裹方式直接决定安全性。
> 三种写法的实测差异见 [docs/architecture.md §5.1](docs/architecture.md)。
>
> 注意：模板里的 shell 指令是**真指令**，引擎会执行它 —— 不要写进注释或示例里。
> 也可以直接跑 `python register_command.py` 让它自动写入。

### 方式 C：只想直接看，不装命令

```bash
python usage_cli.py --open          # 需要 Python ≥ 3.10，无第三方依赖
```

Windows 也可以直接**双击 `usage.cmd`**。

## 使用

### `/usage`（推荐）

| 输入 | 效果 |
|---|---|
| `/usage` | 生成总览页（Token + Session + 上下文三合一） |
| `/usage token` | 只生成 Token 用量页 |
| `/usage session` | 只生成 Session 用量页 |
| `/usage context` | 只生成上下文占用页 |
| `/usage token --days 7` | 最近 7 天 |
| `/usage-open` | 生成并用默认浏览器打开 |

生成的文件默认落在**当前工作目录**：`mimo-usage-<kind>-<日期>.html`。

### 直接跑 CLI

```bash
python usage_cli.py                     # 总览
python usage_cli.py token --days 7      # 近 7 天 token
python usage_cli.py session             # 会话明细
python usage_cli.py context             # 上下文占用
python usage_cli.py --open              # 生成并打开
python usage_cli.py --out D:/x.html     # 指定输出
python usage_cli.py --db <path>         # 指定数据库
python usage_cli.py --print-path        # 只打印路径（便于脚本串联）
```

### 刷新数据

HTML 是**静态快照**（页面上的「重新生成」按钮只是提示你重跑命令，不请求任何服务）。
重新运行同一个命令即可。

## 指标口径

| 指标 | 计算方式 |
|---|---|
| **tokens 消耗** | 输入 + 输出 |
| **输入 tokens** | `tokens.input + tokens.cache.read` |
| **输出 tokens** | `tokens.output + tokens.reasoning` |
| **缓存命中率** | `cache.read / (tokens.input + cache.read)` |
| **会话 / 请求 / 工具** | `session` 行数 / assistant 消息数 / `part` 中 `type==="tool"` 数 |
| **改动** | `session` 的 `summary_additions + summary_deletions`（含文件数） |
| **上下文占用** | 该会话最新 `step-finish` part 的 `tokens.total`（无则退回最新 assistant 消息） |

数据来自 `~/.local/share/mimocode/mimocode.db` 的 `message` / `part` / `session` 表。

## 安全与隐私

- **不联网**、不上传任何数据、不监听任何端口。
- 数据库**仅只读**打开（`mode=ro`）；被占用时复制一份临时快照再读，**绝不写库**。
- 命令参数走**白名单校验**：`$ARGUMENTS` 会被拆解并剔除任何含 shell 元字符的 token，
  防止用户输入造成命令注入。详见 `usage_cli.py` 的 `_sanitize_args`。

## 仓库结构

```
usage_cli.py                核心：只读 db → 聚合 → 渲染自包含 HTML（纯标准库）
register_command.py         把 /usage 与 /usage-open 写进 mimocode.jsonc（JSONC 感知）
install.ps1                 Windows 一键安装
usage.cmd                   双击即用（生成 + 打开）
skill/usage/                /usage 命令说明
skill/mimo-usage-dashboard/ 用量看板说明（纯文档，不执行任何操作）
docs/architecture.md        架构与引擎机制实证（命令注册、shell 替换、为何弃用服务）
docs/plugin-interface.md    MiMo 插件接口调研（Hooks / ToolContext / 加载机制）
test/test_python.py         Python 端到端测试（聚合口径 / 视图 / 注入防护 / JSONC）
test/command-e2e.mjs        Node 侧：模拟引擎的模板替换与 shell 提取
```

## 开发

```bash
npm test                    # = python test/test_python.py（51 项断言）
node test/command-e2e.mjs   # 模板形状与引擎行为模拟
python usage_cli.py --open  # 手动查看
```

## 排错

- **输入 `/usage` 没反应**：确认 `mimocode.jsonc` 里有 `command.usage`，然后**彻底重启** MiMo Desktop，再**新开一个会话**（引擎在会话开始时构建命令表）。
- **报错 `找不到 MiMo 数据库`**：确认 MiMo Desktop 至少成功用过一次；或用 `--db` 指定路径 / 设 `MIMO_USAGE_DB`。
- **报错找不到 Python**：装 Python ≥ 3.10，或 `install.ps1 -Python <python.exe 路径>`。
- **页面打不开**：确认文件已生成（CLI 会打印绝对路径）；或改用 `--open`。
- **上下文显示「上限未知」**：该模型不在内置上限表里，只展示占用绝对值；可用 `--limit` 显式指定。

## 卸载

```bash
python register_command.py --uninstall          # 从 mimocode.jsonc 移除命令
```

再删除 `~/.config/mimocode/usage-dashboard/` 即可。

## License

MIT © Martin-soaring-dev

> 本项目为**非官方**第三方工具，与小米 / MiMo 无关。
