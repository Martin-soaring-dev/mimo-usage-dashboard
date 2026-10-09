---
name: mimo-usage-dashboard
description: "MiMo Desktop 本机用量统计。当用户询问“我的用量 / token 消耗 / 用了多少 / 用量统计 / 调用次数 / 模型份额 / 打开用量仪表盘”时使用。会调用 usage_stats 工具返回 Markdown 摘要，或用 usage_dashboard 打开可视化仪表盘（热力图、模型份额、每日曲线）。"
icon: "📊"
---

# MiMo Desktop 用量统计

本技能把 **MiMo Desktop（mimocode 引擎）本机**的用量数据变成可读的报告。数据来自
`~/.local/share/mimocode/mimocode.db`（assistant 消息里的 `tokens`、`part` 里的工具调用、`session` 表的改动统计）。

## 什么时候用

- 用户问“我用了多少 token / 消耗 / 用量 / 调用次数 / 模型份额 / 每天用多少” → 调用 `usage_stats`。
- 用户想“看图表 / 热力图 / 打开用量仪表盘 / 可视化” → 调用 `usage_dashboard`，它会在默认浏览器打开本地页面，并把它作为内置浏览器可访问的地址返回。

## 怎么调用

1. **文本摘要**：调用 `usage_stats`，参数 `days`（最近多少天，默认 30，`0` 表示全部时间）。
   把工具返回的 Markdown 原样呈现给用户，不要改写数字。
2. **可视化仪表盘**：调用 `usage_dashboard`（无参数）。工具会启动本地服务并返回地址，例如
   `http://127.0.0.1:8790`。把它告诉用户；如果用户在 MiMo 里，也可建议把该地址粘贴到内置浏览器打开。

## 说明与约定

- 仅统计 MiMo Desktop 自己（不包含 OpenCode、Qwen 等其他客户端）。
- 所有数据都在本机读取，服务只监听 `127.0.0.1`，不上传任何内容。
- 仪表盘服务可能需要几秒启动；若工具返回“服务启动超时”，让用户确认已安装 Node ≥ 22，或设置环境变量 `MIMO_USAGE_NODE` 指向 node.exe。
- 服务代码位于 `~/.config/mimocode/usage-dashboard/`（由安装脚本复制）。修改源码后重新运行安装脚本即可。
