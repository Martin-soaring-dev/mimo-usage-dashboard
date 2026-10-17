# 快速上手｜从安装到第一次看懂用量

> 面向首次使用者。当前说明基于 **v0.2.1**，在 **Windows 11 + MiMo Desktop 26.1014.65535.94** 上完成过完整安装、运行与卸载验收。macOS/Linux 有代码兼容路径，但没有相同深度的原生客户端验收。

[返回 README](../README.md) · [技术原理](architecture.md) · [正式发布报告](releases/v0.2.1-release-report.md)

![界面示意图：测试数据，非实机截图](preview.svg)

> **图示说明**：上图是仓库现有的 **合成数据看板预览**，不是你本机的真实用量，也不是验收时的原始截图。验收截图已按隐私与清理约定从本地测试目录删除，不以模拟画面冒充实机证据。

## 先说清楚：你会得到什么

安装以后，MiMo Desktop 的新会话内会注册两个命令：

| 你在 MiMo 输入 | 实际发生的事情 | 你应该看到 |
|---|---|---|
| `/usage` | 本地 Python 只读统计 MiMo 数据库，写出独立 HTML | 聊天中返回“MiMo 用量已生成：…html”，打开文件是**静态快照** |
| `/usage-open` | 同样生成 HTML；额外启动 / 复用本机只读 HTTP 服务并调用默认浏览器 | 浏览器打开 **127.0.0.1** 上的实时看板，可点击**刷新数据** |

**不要把参数附在这两个斜杠命令后面**：它们是固定安全命令，**不转发**后缀参数。例如 `/usage token --days 7` 不等于七天筛选；要精确筛选请在终端运行 CLI（见后文）。这不是聊天机器人统计，也不要求模型解析你的聊天内容。

## 0. 安装前检查（约需三样东西）

- 已安装并运行过一次 **MiMo Desktop**，从而有本地用量数据库；只安装软件但没有任何历史用量时，报告可能为空，数据库甚至可能尚不存在。
- **Python 3.10 或更新**。在 PowerShell 执行 `python --version`，正常示例：`Python 3.12.10`。
- **npm 路线**需要 Node.js/npm（推荐用已发布版本）。如果不想安装 Node，可选“源码 + Windows PowerShell”路线。

不要把本项目当作 MiMo 官方内置功能：它是第三方本地工具。

## 1. 推荐安装：npm（Windows PowerShell）

```powershell
npm install -g mimo-usage-dashboard@0.2.1
mimo-usage-dashboard version
mimo-usage-dashboard install
mimo-usage-dashboard doctor
```

四行各有意义：

1. **npm install**：只是把本项目 CLI 安装到系统 PATH，**尚未修改 MiMo 配置**。
2. **version**：应显示 CLI 版本 `0.2.1`，也会显示已安装版本（首次安装前可能没有）。
3. **install**：执行安装事务，放置 Python 脚本、写入仅属于本项目的 `command.usage` / `command.usage-open`，并建立 manifest 与必要备份。
4. **doctor**：只读体检，可检查 Python、安装目录、命令注册及遗留状态。**退出码**：0 健康、1 警告、2 有问题。请结合文本判断，不能仅看到“完成”就认定正常。

**预期现象**：安装成功的终端日志会显示安装路径/注册结果；如果 `doctor` 提示配置不存在、未知同名命令、解释器不支持，先按信息解决，不要盲目加 `--force`。

### 不用 Node.js：从源码安装（Windows）

```powershell
git clone https://github.com/Martin-soaring-dev/mimo-usage-dashboard.git
cd mimo-usage-dashboard
git checkout v0.2.1
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

如 MiMo Desktop 的真实配置路径或 Python 安装位置不同，可以显式使用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Config "C:\实际路径\mimocode.jsonc" -Python "C:\Python312\python.exe" -InstallDir "D:\your-safe-dir\usage-dashboard"
```

上面 `C:\实际路径` 等只是占位示例，**不要原样执行**；优先用 `mimo-usage-dashboard where` 或核实真实路径。安装器会做 Preflight → Backup → Stage → Validate → Apply → Verify，遇到未知归属文件时拒绝覆盖。

## 2. 一定要完整重启 MiMo Desktop

**关键步骤，最容易漏**：

1. 先结束当前工作，保存需要保存的内容。
2. 退出 MiMo Desktop。Windows 关闭窗口可能只是最小化到**托盘**：确认托盘图标右键“退出”，并确认旧实例结束。
3. 重新打开 MiMo Desktop。
4. **新建一个会话**。旧会话可能保留安装前的命令表。
5. 在输入框先输入 `/usage`：如果命令候选列表出现“命令”标识和说明，说明**命令发现**正常；再真正执行。

若输入框没有候选列表，不代表一定失败——有些界面行为可能不同；以实际执行与 `doctor` 的组合证据判断。

## 3. 第一次运行：静态快照

在 MiMo Desktop **新会话**里输入：

```text
/usage
```

**你会看到的典型反馈**（路径依电脑而变）：

```text
MiMo 用量已生成：C:\Users\<你的用户>\AppData\Local\mimo-usage-dashboard\mimo-usage-all-YYYYMMDD.html
```

输出目录 Windows 默认为 `%LOCALAPPDATA%\mimo-usage-dashboard`；macOS/Linux 为用户数据目录。MiMo 客户端也可能呈现文件链接或自动尝试打开。若链接无法打开，先去输出文件夹确认 HTML 是否真实存在。

**静态页面里**：
- 总览可看到 Tokens、输入/输出、缓存读取、命中率、请求数、工具数、会话数与近 30 天变化。
- 热力图显示近一年每天的活跃情况；它**不随** CLI 的 `--days` 窗口一起缩短。
- “按模型”区域比较模型份额、缓存命中率与请求/工具使用。
- 会话/上下文区域检查近期会话与 Token 构成。

**这时点击页面“重新生成”会怎样？** 静态 HTML 不连数据库，页面会提示需要重新运行本地命令或使用实时模式；它不会自己变成动态数据。若你继续在 MiMo 中对话，此文件不会自动同步。

## 4. 实时模式：浏览器中刷新

在 MiMo Desktop 新会话执行：

```text
/usage-open
```

预期行为：

1. 生成 HTML 快照。
2. 本机服务仅在 `127.0.0.1` 上监听，**端口由系统分配**，不是固定端口。
3. 调用默认浏览器打开 `http://127.0.0.1:<端口>/<随机令牌>/...`。
4. 页面出现“实时模式 · 可点击右上角刷新”。
5. 点击**刷新数据**会重新读取本地数据库并更新统计。失败时保留旧数据并显示提示。

> **隐私提醒**：地址栏路径中包含随机访问令牌。截图分享前遮盖完整地址/查询串；不要将实时 URL 原样贴进公开 Issue。

### 会话用量 / 全局用量切换

实时页面有**会话用量**与**全局用量**两档：

- 会话用量：展示该标签页绑定的会话上下文。
- 全局用量：查看数据库整体聚合。
- `/usage-open` 每次打开的新标签页带不同的随机标识；在该页**第一次加载**时，会定格**当时最近活跃会话**。此后刷新同一标签页不会悄悄换到另一个会话。
- 注意：“最近活跃”不一定是发起 `/usage-open` 的那个聊天会话；MiMo 的斜杠命令接口无法直接提供当前会话 ID。

如果页面显示的不是你刚才输入命令的会话，先检查“最近活跃”的提示；这不是统计自动串库。如果确实需要锁定指定会话，用 CLI `--session`。

## 5. 想筛选 7 天 / 单一会话？用终端 CLI

`/usage`、`/usage-open` **不是自由参数命令**。以下操作在 PowerShell/终端，而**不是 MiMo 的聊天输入框**中完成：

```powershell
mimo-usage-dashboard report
mimo-usage-dashboard open
```

如果从源码运行，可直接：

```powershell
python .\usage_cli.py token --days 7
python .\usage_cli.py session
python .\usage_cli.py context
python .\usage_cli.py context --session <真实会话ID>
python .\usage_cli.py context --limit 262144
python .\usage_cli.py all --out "D:\Reports\usage.html"
python .\usage_cli.py --print-path
```

| 参数/命令 | 什么时候用 | 预期反馈 |
|---|---|---|
| `token --days 7` | 只看近七天 Token 统计 | 输出一个以 token 为视图的 HTML；热力图依然显示近一年 |
| `session` | 想看会话汇总 | 生成会话明细/统计视图 |
| `context --session ID` | 想指定会话 | 上下文卡对应指定会话；不存在或数据不足则有缺省/未知信息 |
| `--limit 262144` | 明确知道本次模型上下文上限 | 覆盖本次显示的配置推断值；**不是**修改模型真实上限 |
| `--out` | 想保存到指定位置 | 在指定路径写入 HTML，不改变源数据库 |
| `--print-path` | 让脚本方便获取产物绝对路径 | stdout 打印路径 |

仓库支持 `MIMO_USAGE_DB`、`MIMO_USAGE_DATA_DIR` 和 `MIMO_USAGE_CONFIG` 环境变量，用于特殊目录与隔离测试；普通用户不需要设置。

## 6. 关闭实时服务、升级和卸载

**关闭实时服务**：从安装目录找到 `usage_cli.py` 后运行 `python usage_cli.py --shutdown --wait-seconds 8`。关闭的是经过本项目身份验证的服务；浏览器关掉不等于立即退出服务，默认有空闲自动退出机制（约 4 小时）。

**升级至已经安装的 npm CLI 所携带版本**：

```powershell
npm install -g mimo-usage-dashboard@0.2.1
mimo-usage-dashboard upgrade
mimo-usage-dashboard doctor
```

**卸载本项目**：

```powershell
mimo-usage-dashboard uninstall
```

预期：从 MiMo 配置中撤掉本项目的两个命令，清理经 manifest 证明属于本项目且未被手工修改的文件；**保留数据库、报告、备份和无关文件**。彻底退出并重新启动 MiMo Desktop、新建会话后，`/usage` 和 `/usage-open` 应不再出现在已注册命令表中。模型仍可能在普通聊天里响应同样字符，那**不代表命令仍已注册**。

要仅注销而保留脚本：`mimo-usage-dashboard uninstall --keep-files`。备份默认保留；除非确实核查过归属，不要用 `--purge-backups` 或 `--force`。

## 7. “我这样做，会有什么响应？”故障速查

| 你遇到的情形 | 原因/预期行为 | 下一步 |
|---|---|---|
| 只执行了 `npm install -g`，斜杠命令没有出现 | npm 安装 ≠ MiMo 注册 | 再运行 `mimo-usage-dashboard install` |
| 安装成功，旧聊天里没有命令 | 命令表在会话启动时构建 | **彻底退出** Desktop，重启，**新建会话** |
| `doctor` 给出 warning/error | 依具体诊断文本判断 | 核对 `where` 显示的配置和 Python 路径 |
| 找不到 `mimocode.db` | 尚未生成 DB，或路径异常 | 先正常使用 MiMo；必要时核对 `MIMO_USAGE_DB` |
| 静态报告点刷新不更新 | 静态文件本来不会自动读库 | 重新执行 `/usage` 或用 `/usage-open` |
| `/usage-open` 不打开浏览器，但打印 HTML 路径 | **已知 #35**：实时服务可能启动失败而退回静态快照，且可能没有明显 CLI 警告 | 先打开快照并确认数据，必要时重试；故障持续时按 Issue #35 收集诊断 |
| 输出路径含 `.config` / `.mimo-sessions`，点击文件链接失败 | **已知 #39**：Markdown 可能将 `\\.` 渲染为 `.` | 从原始终端输出/磁盘找到文件；手工修正缺失的反斜杠 |
| 上下文上限显示“未知” | 配置里缺少 provider/model 的 limit.context | 核对配置，或用 `--limit` 显式指定一次 |
| 实时会话似乎不对 | 页面绑定的是**第一次加载时最近活跃会话** | 切换全局/会话档并核对标签说明；必要时使用 CLI 指定会话 |
| 卸载后命令仍可在聊天中触发 | 旧会话的命令表缓存 | 重启 Desktop + 新会话；确认命令面板已注销 |

## 8. 已知限制与隐私

- v0.2.1 **完整实机验收**限定为 Windows 11 / MiMo Desktop 26.1014.65535.94；其他平台不应从此推断已同等验证。
- #35：实时服务偶发不可用（P2，原因仍需证据）；#39：隐藏目录路径在 Markdown 中反斜杠丢失（P2）；见 [Issue #35](https://github.com/Martin-soaring-dev/mimo-usage-dashboard/issues/35) 与 [Issue #39](https://github.com/Martin-soaring-dev/mimo-usage-dashboard/issues/39)。
- 不上传私有数据库、真实对话内容、API Key 或含实时服务令牌的浏览器截图。报告只在本机从 SQLite **只读**提取统计；实时模式监听 loopback，不能作为公网共享看板。
- 本项目非小米 / MiMo 官方产品。

**读完后进一步了解原理**：[README 中的架构解释](../README.md#设计原理与取舍) · [架构实证](architecture.md)。
