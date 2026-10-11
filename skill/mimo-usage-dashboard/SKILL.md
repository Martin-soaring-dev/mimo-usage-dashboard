---
name: mimo-usage-dashboard
description: "MiMo Desktop 本机用量看板的**说明与导航**。这是一个纯文档技能，不执行任何操作、不调用任何工具、不消耗 AI 算力。当用户询问“怎么查看用量 / 用量看板怎么用 / 这个插件是干什么的 / 数据从哪来 / 指标怎么算”，需要解释工具用法与数据口径时使用。真正查看用量请引导用户输入 /usage（那是本地命令，不是本技能）。"
icon: "📖"
---

# MiMo Desktop 用量看板 —— 说明

**本技能只提供说明，不执行任何动作。** 要查看用量，让用户输入 `/usage`。

## 一句话说明

`/usage` 是一个**本地命令行**：MiMo 引擎先在本地运行 Python 脚本，
脚本读 `mimocode.db`、聚合、渲染一份**自包含静态 HTML**，写到当前工作目录。
全程**零 AI 参与、零服务、零端口、零联网**。

## 常用入口

| 用户想做的事 | 引导他输入 |
|---|---|
| 看总览 | `/usage` |
| 看 token 消耗与模型份额 | `/usage token` |
| 看会话明细 | `/usage session` |
| 看上下文占用 | `/usage context` |
| 看最近 7 天 | `/usage token --days 7` |
| 生成并直接打开 | `/usage-open` |

## 数据从哪来

`~/.local/share/mimocode/mimocode.db`（MiMoCode 引擎的 SQLite 库），**只读**打开：

- `message.data` 里 assistant 消息的 `tokens{input,output,reasoning,cache{read,write}}`、`providerID`、`modelID`、`time.created`
- `part.data` 中 `type==="tool"` 的工具调用、`type==="step-finish"` 的上下文快照
- `session` 表的标题、时间、`summary_additions/deletions/files`

## 指标口径

| 指标 | 计算方式 |
|---|---|
| tokens 消耗 | 输入 + 输出（含缓存读写） |
| 输入 tokens | `tokens.input + cache.read` |
| 输出 tokens | `tokens.output + reasoning` |
| 缓存命中率 | `cache.read / (input + cache.read)` |
| 会话 / 请求 / 工具 | `session` 行数 / assistant 消息数 / `part` 中 `type==="tool"` 数 |
| 上下文占用 | 最新 `step-finish` part 的 `tokens.total`；上限取自内置模型表 |

## 为什么是静态网页而不是对话内卡片

MiMo 的插件 API 只有服务端能力，**不能往应用界面注入页面**；
而对话内的 `sci-widget` 卡片必须由 AI 逐字输出 HTML —— 既耗 token 又会出现
「正在生成交互演示…」的流式等待。所以改用**本地脚本直接生成静态 HTML**：
AI 完全不参与，输出稳定、零 token、可反复打开。

## 排错

- **报错找不到数据库**：确认 MiMo Desktop 至少成功用过一次（库会在首次运行时创建）。
- **报错找不到 Python**：确认已安装 Python ≥ 3.10，或重跑 `install.ps1 -Python <路径>`。
- **输入 /usage 没反应**：确认 `mimocode.jsonc` 里有 `command.usage`，然后**彻底重启 MiMo Desktop** 并**新开会话**。
- **数据没更新**：HTML 是静态快照，重新运行命令即可。
