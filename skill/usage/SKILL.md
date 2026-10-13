---
name: usage
description: "MiMo Desktop 本机用量查看（/usage）。这是一个**本地命令行**，不是需要 AI 参与的技能：输入 /usage 后，MiMo 引擎会先在本地运行 Python 脚本，生成一份静态 HTML 看板（Token 用量 / Session 用量 / 上下文占用），并把结果路径插进消息。当用户输入 /usage、/usage token、/usage session、/usage context，或询问“我的用量 / token 消耗 / session 用量 / 上下文占用 / 用量看板”时使用。"
icon: "📊"
---

# /usage — MiMo Desktop 本机用量（本地命令，零 AI 参与）

## 这是什么

`/usage` 由一个 **Python 脚本**在本地直接完成全部工作，**不需要 AI 读库、计算或渲染**：

```
/usage  ─►  引擎执行 !`python usage_cli.py --arg-string '…'`
        ─►  脚本读 mimocode.db、聚合、渲染自包含 HTML、写到当前目录
        ─►  stdout（一行文件路径）替换回消息
        ─►  AI 只需把这一行**原样**转述给用户
```

脚本默认**不联网、不写数据库**。`/usage-open` / `--open` 会额外启动仅监听 `127.0.0.1` 的只读刷新服务（随机 path token，空闲自动退出）。

## 参数

| 输入 | 效果 |
|---|---|
| `/usage` | 生成总览页快照（Token + Session + 上下文三合一） |
| `/usage token` | 只生成 Token 用量页 |
| `/usage session` | 只生成 Session 用量页 |
| `/usage context` | 只生成上下文占用页 |
| `/usage token --days 7` | 最近 7 天（仅本地 CLI） |
| `/usage-open` / `python usage_cli.py --open` | 打开**可页内刷新**的实时看板（loopback） |

## 你的职责（就这么简单）

1. **不要**自己读数据库、**不要**自己算 token、**不要**自己写 HTML。
2. 命令执行后你会看到脚本输出，形如：

   ```text
   MiMo 用量已生成：E:\...\mimo-usage-all-20261011.html
   ```

   或实时模式：

   ```text
   MiMo 用量已生成：http://127.0.0.1:PORT/TOKEN/（快照 …）
   ```

3. 把这行**原样**告诉用户。静态快照：打开 HTML 即可；实时看板：打开输出里的 `http://127.0.0.1:...` 地址，页面内可点「刷新数据」。
4. 不要臆造或改写任何数字。

## 三种视图看什么

- **Token 用量**：Hero 大卡（总量 + 30 天迷你趋势）→ 8 个统计小卡（输入/输出/缓存读取/缓存命中率/请求/工具/会话/平均每次请求）→ **12 个月日历热力图**（4 档分位配色，全年视角，不受 `--days` 影响）→ **按模型的调用情况**（每模型含 token 总量、请求/工具数、占比条、缓存命中率条；浅蓝=命中、橙=未命中）。
- **Session 用量**：会话总数、累计 tokens、请求数、改动行/文件数，以及按活跃度排序的最近会话明细。
- **上下文占用**：当前会话上下文占用（tokens）、构成（缓存读取 / 新输入 / 推理 / 输出）、模型、工作目录、消息/步/工具计数与最近活动时间。

## 说明

- 数据源：`~/.local/share/mimocode/mimocode.db`，**只读**打开（被占用时做临时快照）。
- 默认 HTML 输出到用户数据目录（不再依赖进程 CWD）。
- 静态快照：`/usage`；实时刷新：`/usage-open` 或 `--open`（loopback 只读服务）。
- 指标口径见仓库 README 的「指标口径」一节。
- 若脚本报错（找不到数据库 / 找不到 Python），把错误原样告诉用户，并提示确认 Python ≥ 3.10、MiMo Desktop 至少用过一次。

## 想刷新数据？

- **静态快照**：重新运行 `/usage` 或本地 CLI。
- **实时看板**（`--open`）：直接点页面右上角「刷新数据」——会重新读库并更新图表，无需回聊天窗。
