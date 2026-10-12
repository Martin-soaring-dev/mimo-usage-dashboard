# 用量看板 UI 改进规格（v1）

> 交付对象：外部实现方（ChatGPT / 其他编码代理）
> 目标文件：`usage_cli.py`（单文件 Python CLI + 内联 HTML/CSS/JS 运行时）
> 基线：分支 `fix/usage-command-argument-safety`，提交 `c318718`（含 P1 会话数修复）
> 本文档所有数字均为**实测值**，测量方法见 §7 与附录 B

---

## 0. 怎么用这份文档

1. §1 是不许违反的硬约束；先读完再动手。
2. §2 是代码地图：改哪个函数、数据从哪来、payload 长什么样。
3. §3 是四个待修问题，每条都给了「现象 / 证据 / 根因（精确到行）/ 期望 / 实现要点 / 验收标准」。
4. §4 是参考 UI 对照（来自 MiMo Desktop 内置页面），说明本次要采纳哪些、暂缓哪些。
5. §8 是必须由用户拍板的决策点，**动手前先确认**。

---

## 1. 硬约束（违反即返工）

| # | 约束 |
|---|---|
| C1 | **零服务、零端口、零网络**。产物是一个静态 HTML 快照，双击直接可用。不得引入 HTTP 服务、CDN、外部字体、外部 JS 库。 |
| C2 | **自包含**。HTML 内不得出现 `fetch(`、`XMLHttpRequest`、外部 `<script src>`/`<link href>`。已有测试断言 HTML 内无 `fetch(`、无外部 `http(s)` 资源。 |
| C3 | **只用标准库**。Python 侧只用 `sqlite3` 等标准库；JS 侧不得引入任何 npm 包。 |
| C4 | **数据只在生成时读取一次**。浏览器端不能访问数据库；所有维度切换只能基于已内联进 HTML 的 `var DATA`。 |
| C5 | **数据库只读**。`_open_readonly()` 用 `file:…?mode=ro`，不得改成读写、不得写入任何表或临时表。 |
| C6 | **一次性零 AI 参与**。页面渲染是纯 JavaScript，不得依赖模型调用。 |
| C7 | **保持中文界面**，术语与现有页面一致（见 §4 术语表）。 |
| C8 | 改动必须让现有测试继续通过或**有意识地同步更新断言**（断言清单见 §7.3）。 |

---

## 2. 代码地图与数据流

### 2.1 文件结构

`usage_cli.py` 单文件，分两半：

- **Python 侧**：读数据库 → 聚合 → 生成 HTML。
  - `read_usage(db_path)` — 全量聚合，产出 `totals` / `daily` / `monthly` / `models` / `sessions` 等（`:132` 起）。
  - `card_data(db_path, kind, days, …)` — 把聚合裁剪成给前端的 payload（`:442` 起）。
  - `render_html(data, cmd, out_path)` — 拼 HTML；CSS 在 `:525` 起的 CSS 字符串里，JS 运行时在 `_RUNTIME = """…"""` 里。
  - `main()` — 解析 `--days/--db/--out/--session/--limit`（`:1095` 起）。
- **前端侧**：`_RUNTIME` 里的渲染函数，**封装在 IIFE 内，不是全局变量**。
  - `tokenCard(t)` `:769`、`sessionCard(s)`、`contextCard(c)`、`heatmapCard(t)` `:657`、`modelCard(t)` `:735`、`sparkline(daily)` `:797`、`buildBody(data)` `:885`、`renderTab(selected, DATA)`。

> 施工提示：前端不是独立文件，改 UI 就是在 Python 三引号字符串里改 JS/CSS。改完务必跑 `python test/test_python.py`，它会在 HTML 里查字符串。

### 2.2 当前 payload 形状（实测，`fixture_main-all.html`）

```
DATA.token.totals      = tokens, input, output, reasoning, cacheRead, cacheWrite,
                         requests, tools, cacheShare, sessions          ← 窗口口径(--days)
DATA.token.totalsAll   = 全量 totals（含 sessions）
DATA.token.days        = 30
DATA.token.daily[]     = { date, tokens }                                 ← 仅两个字段！
DATA.token.recent[]    = { date, tokens }                                 ← 窗口内，顶部迷你趋势用
DATA.token.models[]    = { name, providerID, tokens, read, input, requests, tools, share }
DATA.session           = { total, totals, changes, files, top[] }
DATA.context           = { ok, sessionId, context:{used,limit,ratio,source,detail}, … }
```

⚠️ **关键缺口**：`daily[]` 只带 `date` 和 `tokens`。而 Python 侧 `read_usage` 的 daily 其实**已经有** `input/output/reasoning/read/write/requests/tools`（`:149` 附近初始化）——是 `card_data` 在 `:491` 把它裁成了两个字段。要做热力图悬浮提示里的「缓存命中率」，必须先把这些字段放回 payload（见 §5）。

---

## 3. 待修问题

### UI-1 热力图格子无悬浮信息，且出现横向滚动条

**现象（用户描述）**
鼠标悬停方块时，看不到日期、星期、token 用量、缓存命中率；同时热力图下方出现一条横向滚动条。

**证据**
- 实测：`document.querySelectorAll('.hm .cell[data-tip]').length = 367`，而 `.hm .cell[title]` **= 0**。
- 代码 `:697` 只写了 `data-tip="<date> · <n> tokens|无调用"`；全文件检索 `data-tip` **只有这一处写入，没有任何读取**（唯一的 `getAttribute` 是 `:956` 读 `data-tab`）。CSS 里也没有 `::after{content:attr(data-tip)}`。→ 提示数据是**死数据**。
- 现有的悬停反馈只有 `:602` 的 `transform:scale(1.35)` + outline，没有任何文字。
- 当前 `data-tip` 内容也缺字段：实测样例 `"2025-10-12 · 无调用"`，没有星期、没有缓存命中率。

横向滚动条实测（同一页面，只改视口宽度）：

| 视口宽 | `.hm .cols` clientWidth | scrollWidth | 溢出 |
|---:|---:|---:|---:|
| 1200px | 862 | 862 | 0 |
| 1000px | 862 | 862 | 0 |
| 900px | 786 | 848 | **62px** |
| 820px | 706 | 848 | **142px** |
| 760px | 646 | 848 | **202px** |

**根因**
1. 无提示：`data-tip` 从未被消费。
2. 滚动条：`:594` 给网格设了固定下限 `min-width:var(--minw,560px)`，而 `--minw` 在 `:726` 由 `nc*(cell+gap)` 算出（53 列 ×(13+3) = **848px** 硬下限）；`:592` 又让 `.hm .cols` 始终 `overflow-x:auto`。所以视口一旦窄于约 1000px（卡片可用宽度 <848px）就必然出现滚动条——**与是否有数据无关**。

**期望行为**
1. 悬停任一格子显示提示，内容至少包含：**日期（含星期）+ token 用量 + 缓存命中率**；无调用日显示「无调用」。
2. 热力图在合理宽度内**自适应铺满、不出现横向滚动条**；只有当窗口极窄（例如 <720px）时才允许横向滚动，且滚动条样式与卡片协调。

**实现要点**
- 提示：二选一。
  - 最省事：给格子补 `title` 属性（原生 tooltip，零 CSS/JS）。
  - 更好：自绘 tooltip——一个 `position:fixed` 的提示层，监听格子 `mouseenter/mousemove`，读 `data-tip`（或结构化 `data-date`）渲染，`mouseleave` 隐藏；需处理贴近视口右/下边缘时的翻转。
  两者都要把 tip 内容扩成「日期 · 星期 · N tokens · 缓存命中率 X%」。
- 滚动条：把「固定 min-width + 外层滚动」换成「网格自适应」：
  - 去掉 `:594` 的 `min-width`（或改成 `min-width:0`），让 `grid-template-columns:repeat(nc,minmax(0,1fr))` 自己均分；
  - 格子保持正方形：`aspect-ratio:1/1` + `max-height` 已有，但注意**当列宽 > max-height 时格子会被压扁/裁切**，需要给格子一个合理的最小尺寸下限，或者干脆去掉 `max-height` 只留 `aspect-ratio`；
  - 若确实要保底可读性，用媒体查询在窄屏时**减少月份跨度**（12 → 6 个月）而不是加滚动条；
  - 去掉 `.hm .cols` 上为了容纳滚动条的 `padding-bottom:2px`。
- 注意 `:703` 的 `cell` 尺寸是**按列数猜的**（`nc>56?12:nc>48?13:15`），与真实渲染宽度不同步；改成由 CSS 决定尺寸更稳。

**验收标准**
- 悬停任意非空格子与空格子都能看到包含日期/星期/tokens/缓存命中率的提示。
- 视口宽度 1200 / 1000 / 900 / 820px 下，`.hm .cols` 的 `scrollWidth - clientWidth == 0`（当前 900px 起为 62px）。
- 12 个月格子仍全部可见，未被裁切。

---

### UI-2 星期标签位置不准确

**现象**
热力图左侧「一 / 三 / 五」三行标签与网格行没对齐，越往下偏得越多。

**证据（实测同一列几何）**

| 项 | 实测 |
|---|---|
| 7 个格子的行中心 y | 705.5, 721.9, 738.2, 754.5, 770.8, 787.1, 803.4 → **行距 16.31px** |
| 「一/三/五」标签中心 y | 695.4, 714.4, 733.4 → **标签行距 19.0px** |
| 首标签相对第 0 行 | **高 10.1px** |
| 「五」相对第 4 行 | **高 37.4px** |
| 实际格子尺寸 | 13.31 × 13.31px（不是 `--cell:13px`） |
| `.hm .dow` 的 `padding-top` | 22px（魔数） |
| `.hm .dow span` 的 `line-height` | 13px（= `--cell`） |

**根因**
- 星期列（`:589-591`）是一个**独立的 flex 纵向列**，靠 `line-height:var(--cell)` + `gap:var(--gap)` + `padding-top:22px` 手动对齐；而真正的行距由**格子渲染高度**决定，格子高度又是**列宽驱动**的（`aspect-ratio:1/1`，`:600`），实测 13.31px ≠ `--cell` 13px，于是两种行距天然不等（16.31 vs 19）。
- `padding-top:22px` 是为了绕开月份行的实际高度（实测 `months` 高 31px）而写死的补偿值，月份行一变就失效。
- 只要窗口宽度变化 → 列宽变化 → 格子高度变化 → 标签必然继续错位。

**期望行为**
星期标签的行距与网格行距**恒等**，任何宽度下 一/三/五 都与对应行严格对齐。

**实现要点（推荐做法）**
把星期标签并入**同一个 CSS Grid**，而不是旁边再开一个 flex 列：
- 让网格变成 `nc + 1` 列：第 1 列放星期标签、其余放日期列；或
- 让网格用显式行模板 `grid-template-rows: repeat(7, <统一单元高度>)`，星期标签用 `grid-row: 1/2, 3/4, 5/6` 落位在同一网格里；
- 单元高度用一个 CSS 变量同时驱动格子与行模板（如 `--unit: clamp(10px, 1.6vw, 16px)`），避免「列宽决定格高」与「标签固定行高」两套尺寸打架；
- 月份行同理：用同一网格的列定位，不要在外部用 `padding-top` 补偿。

**验收标准**
- 1200 / 1000 / 900 / 820px 四档下，`一`/`三`/`五` 三个标签的行中心与对应格子行中心的偏差 **≤ 1px**。
- 月份标签不遮挡、不换行。

---

### UI-3 缺少时间尺度切换（今日 / 7 天 / 30 天）

**现象**
页面只能显示「最近 30 天」，无法切换到 7 天或今天。参考 UI 顶部有 `今日 / 7天 / 30天` 分段控件。

**证据（实测）**
- `hero` 文案写死为 `"最近 30 天 · tokens 消耗"`（`:789` 用 `(t.days||30)` 渲染）。
- 页面内**没有任何时间尺度控件**：`.tabs` 里只有三个**视图**标签（总体使用量 / 会话使用量 / 上下文使用量），实测 `anyTimeRangeControl = 0`。
- `--days` 只是 CLI 参数（`:1095`，默认 30），只能靠重新生成 HTML 改变；且它**只影响 token 视图的窗口口径数字**（totals / recent），**热力图固定 12 个月不受影响**（`:490` 有注释）。
- payload 里 `DATA.token.days = 30`、`DATA.token.daily.length = 3`（有数据的日期数）。

**期望行为**
页面顶部提供 `今日 / 7 天 / 30 天` 三段切换，切换后**同一页内**即时更新：hero 大字、8 张统计卡、模型块、趋势图；热力图保持 12 个月不变（它是「长期视图」，与参考 UI 一致：参考页的热力图标题固定为「最近 12 个月」）。

**实现要点（关键约束：静态文件，无服务端）**
两条路，必须选一条并写清理由：

- **方案 A（推荐）：payload 内联多窗口聚合，纯前端切换。**
  在 `card_data` 里为 token 视图额外产出 `windows: { "1": {totals, models, recent}, "7": {...}, "30": {...} }`，前端按钮只切换用哪一份。优点：一个文件、切换零延迟、离线可用。代价：HTML 变大（模型列表 ×3），需要限制 `models` 条数。
- **方案 B：生成三个文件**（`--days 1/7/30`），分段控件变成三个 `<a href="…-7d.html">` 链接。
  优点：改动最小、每个文件更小。代价：三份文件、切换有跳转、用户得知道文件名。

无论哪条，都要定义清楚语义并写进界面文案：
- 「今日」= 自然日 00:00 起（**推荐**）还是最近 24 小时？
- 窗口口径只影响 `totals/recent/models`，`totalsAll`、热力图、会话数（`sessions` 为全量）保持不变——这层不一致要在 UI 上讲清楚（例如卡片副标题标注「近 7 天」）。

**验收标准**
- 三段控件存在且可点击；切换后 hero 文案与数字、8 张卡片、模型块、趋势图都随之变化。
- 「30 天」结果与当前默认渲染逐值一致（回归基线）。
- 「今日」在无当天数据时显示 0 且不报错。
- HTML 内仍无 `fetch(`、无外部资源。

---

### UI-4 模型用量应改为单条进度条（总 token / 命中缓存 token）

**现象**
参考 UI 的模型份额是**一条**圆角进度条；而当前实现每个模型堆了**两条**全宽条，看起来像两层错位的横线。

**证据**
- 代码：`modelCard`（`:735-767`）每个模型渲染两条：
  - `:750` `.track2` = 份额条（宽度 = `m.tokens / maxTok`，单色 = `--accent`）；
  - `:752-755` `.track3` = 命中率条（两段：浅蓝命中 + 橙未命中），且**未命中段被强制至少 1.6% 宽**（`:753-754`）。
- 实测（`model-alpha`，828px 宽的两条）：`track2` 宽 828px、`track3` 宽 828px；`track3` 两段 = 命中 2.69%（22.3px）+ 未命中 97.31%（805.8px）。
- 全页 `.mrow` 内共有 **4 条** bar（2 模型 × 2 条）。

**期望行为（对齐参考 UI「30 天模型份额」）**
每个模型**一条**进度条：
- 条长表示该模型 token 在**全部模型中的占比**（即 `m.share`；参考 UI 右侧百分比就是它），或退化为「相对最大模型」，二选一并固定；
- 条内用**较浅的一种颜色**表示**缓存命中（缓存读取）**的那一段，即「总 token / 命中缓存 token」在同一条上可见；
- 条右侧对齐显示占比百分比；模型名下显示 `X tokens · 缓存 Y%`；
- 保留现有的「命中率健康色」语义（≥80% 绿 / ≥50% 黄 / 其他红），但改成作用于文字或 pill，而不是再加一条 bar。

**实现要点**
- 把 `.track2` + `.track3` 合并成一个两层结构：外层 `--track` 底，内层按 `share` 定宽，内层里再叠一段浅色的 cached 段（可用 `linear-gradient` 或内嵌 `<i>`）。
- 需要新数据吗？**不需要**：`models[]` 已含 `tokens / read / input / share`，`缓存比例 = read/(read+input)` 前端可算（现有 `:741-742` 已这么算）。
- 删掉 `:753-754` 的「未命中强制最小 1.6%」技巧——它正是当前条看起来「总有一截橙色」的原因；改成 cached 段用实际比例。
- 同步更新图例文案（参考 UI 写法：`token 份额 · 浅绿表示缓存读取`）。
- 注意 `:606` 附近的 `margin-left:-1.6%` 之类历史补丁已被测试断言禁止（见 §7.3），不要重新引入负数边距。

**验收标准**
- 每个模型恰好 **1 条** bar（`document.querySelectorAll('.mrow .track2, .mrow .track3').length === 2×模型数`，或改造后用新类名断言条数 = 模型数）。
- 「缓存 0%」的模型（如 `model-beta`）条内无缓存段；缓存 91% 的模型条内缓存段长度与比例相符（±1%）。
- 右侧百分比与 `m.share` 一致。
- 条在容器内不溢出、圆角正常、无双条错位。

---

## 4. 参考 UI 对照（MiMo Desktop 内置页面）

用户提供的参考截图来自 MiMo Desktop 自带的用量/状态页，可视为**设计目标**。本次要采纳的是其中的信息架构与视觉语言，不是照搬数据源。

### 4.1 建议本次采纳

| 参考元素 | 说明 |
|---|---|
| 顶部时间尺度分段控件 | `今日 / 7天 / 30天`，选中态为浅色胶囊 → 对应 UI-3 |
| Hero 卡 | `近 30 天` + 超大数字（`2860.9M`）+ `tokens 消耗` 小字 + 右上角「刷新」→ 我们保留「重新生成」按钮，文案对齐 |
| KPI 卡组 | `会话 / 请求 / 工具 / 改动` 四张，数字大、标签小 → 我们的 8 张卡片可考虑分组为「用量 / 效率」两行 |
| TOKEN 拆分卡 | `输入 TOKENS`（副标 `请求 + 缓存读取`）/ `输出 TOKENS`（副标 `响应 + 推理`）/ `缓存读取`（`96%` + `缓存读取占比`），每张左侧有一条彩色竖条 |
| 热力图图例 | `少 → 多`，5 档色块 |
| 模型份额条 | **单条**进度条 + 排名徽章（01/02/…）+ `X tokens · 缓存 Y%` + 右对齐百分比 → 对应 UI-4 |
| 每日趋势线图 | `30 天 TOKENS / 每日 token 总量`，带峰值标注（`峰值 1187.8M`） |
| 每日会话柱图 | `30 天 会话 / 每日活跃会话数`，下方有起止日期轴 |

### 4.2 建议暂缓（另立任务）

- `Token 消耗` 页的「按模型 / 子agent / 工具」三层折叠结构（含每次请求的平均延迟、工具成功率）——需要 `part` 表里更细的字段，超出本次范围。
- `上下文使用情况` 页的**三段式条形图**（已用 / 空闲 / 自动压缩缓冲区）+ `高级 · 用量分类` 树（系统提示词 / 内置工具 / Memory 文件 / Skills / 启动上下文 / 消息，可逐级展开到单个 skill 与 memory 文件）。这一块信息量最大、实现成本最高，建议单独排期。我们的 `contextCard` 目前是占用环，形态不同。
- 工具调用成功率、平均延迟等指标。

### 4.3 术语表（保持与现有界面一致）

`总体使用量 / 会话使用量 / 上下文使用量`、`tokens 消耗`、`缓存读取`、`缓存命中率`、`请求数`、`工具调用`、`会话数`、`平均每次请求`、`按模型的调用情况`、`Token 热力图`、`最近会话`、`上下文占用`。

---

## 5. 需要在 Python 侧配合的数据改动

| # | 改动 | 位置 | 原因 |
|---|---|---|---|
| D1 | `token.daily[]` 从 `{date, tokens}` 扩展为 `{date, tokens, read, input, requests}` | `card_data` `:491` | UI-1 要在提示里显示**当日缓存命中率**；`read_usage` 的 daily 本来就有这些字段（`:149` 初始化、`:273` 处已聚合），只是被裁掉了 |
| D2 | （若选 UI-3 方案 A）新增 `token.windows = {"1":…, "7":…, "30":…}`，每份含 `totals / models / recent` | `card_data` token 分支 | 静态文件下实现窗口切换 |
| D3 | 确认 `daily` 是**全量 12 个月**、`recent` 是**窗口内**，字段语义写进注释 | `:491` / `:493` 已有注释，保持 | 避免后续把两者搞混 |

> D1 会增加 payload 体积：367 天 × 4 个数字字段，可接受（当前 HTML 约 28KB）。若在意体积，可只传 `read` 与 `input` 并让前端算比例。

---

## 6. 非目标

- 不新增「工具成功率 / 平均延迟 / 子 agent 分解 / 上下文用量分类树」（见 §4.2）。
- 不改动 `sessionCard`（最近会话表）与 `contextCard` 的整体形态。
- 不改安装脚本、不改命令模板、不改数据库读取逻辑、不改配置注册逻辑。
- 不引入构建步骤（无打包、无转译、无 npm）。

---

## 7. 验证方式

### 7.1 现有测试（必须保持通过）

```bash
python test/test_python.py        # 聚合口径 + HTML 结构 + 参数白名单 + 注册行为
node  test/runtime-check.mjs      # 在 VM 沙箱里真实执行内联 JS，渲染四种视图
node  test/command-e2e.mjs        # 复刻引擎侧模板处理，端到端生成 HTML
npm test                          # 上面三个的组合
npm pack --dry-run                # 打包清单
powershell -File test/install-smoke.ps1
```

### 7.2 视觉与几何回归（建议新增的自动化）

本次四个问题都能量化成几何断言，建议新写一个 `test/ui-geometry.mjs`（用无头 Chrome + CDP，或复用现有 `test/shot.mjs` 的 CDP 思路）：

| 断言 | 阈值 |
|---|---|
| 热力图无横向滚动 | 视口 1200/1000/900/820px 下 `scrollWidth - clientWidth == 0` |
| 星期标签对齐 | 一/三/五 标签行中心 vs 对应格子行中心，偏差 ≤ 1px（四档宽度都要测） |
| 悬停提示存在 | 每个 `.hm .cell` 都有可读的提示文本（`title` 或自绘层），且内容含日期、星期、tokens、缓存命中率 |
| 模型条数量 | 每个模型恰好 1 条 bar |
| 时间尺度控件 | 存在 今日/7天/30天 三段；点击后 hero 数字变化；30 天与默认基线逐值一致 |
| 无外链 | HTML 内无 `fetch(`、无 `http(s)://` 资源引用 |

### 7.3 会被新改动影响的既有断言（必须同步处理）

改 UI 时这些字符串断言会失效，**不要靠删除断言让测试变绿**，要么保持这些标记，要么在测试里同步更新为等价的更强断言：

- `test_python.py`：`'role="tablist"'`、`'role="tabpanel"'`、`'renderTab(selected, DATA)'`、`'function heatmapCard'`、`'function modelCard'`、`'缓存命中率'`、`'输入 tokens（含缓存读取）'`、`'按模型的调用情况'`、`'track3'` + `'cm-hit-hit'`、`'margin-left:-1.6%' not in html`、`'"daily"' in html`、`'"recent"' in html`、`'最近会话'`、`'上下文占用'`、`'var DATA'`、`'id="regen"'`、`'Token 热力图'`。
- `test/command-e2e.mjs`：`function tokenCard` / `function sessionCard` / `function contextCard` / `function heatmapCard` / `function modelCard` / `id="root"`。
- `test/runtime-check.mjs`：`'Token 热力图'`、`'按模型的调用情况'`、三个视图标签、`aria-selected="true"`、以及会话数两视图一致性回归。

---

## 8. 需要用户先拍板的决策点

1. **UI-3 用方案 A（payload 内联多窗口）还是方案 B（生成 3 个文件）？** 推荐 A。
2. **「今日」的口径**：自然日 00:00 起，还是最近 24 小时？推荐自然日。
3. **UI-4 条长语义**：按「占全部模型的比例」（= `share`，与参考一致）还是「相对最大模型」？二者视觉差异很大，推荐前者。
4. **热力图月份跨度**：窄屏时是「减少到 6 个月」还是「压缩格子保持 12 个月」？推荐保持 12 个月 + 自适应压缩，仅在 <720px 才允许滚动。
5. **是否一并做 §4.2 的上下文用量分类树**（成本最高的一块）？

---

## 附录 A：证据文件

| 内容 | 路径 |
|---|---|
| 当前实拍截图（本文档所依据） | 用户提供：`C:\Users\Martin\AppData\Roaming\Xiaomi MiMo\attachments\images\mimo-paste-1791837*.png`（14 张） |
| 修复前后对照看板 HTML | `C:\Users\Martin\AppData\Local\Temp\mimo-sim-fix-20261013-040646\synthetic-dashboard.html` |
| 几何探针脚本（本文档数值来源） | `C:\Users\Martin\AppData\Local\Temp\mimo-sim-fix-20261013-040646\lib\ui-probe.mjs` |
| 截图脚本（CDP，可复用做视觉回归） | `C:\Users\Martin\AppData\Local\Temp\mimo-sim-fix-20261013-040646\lib\shoot.mjs` |

> 附录 A 的临时目录可能被系统清理；探针脚本思路见 §7.2，可按需重建。

## 附录 B：本文档实测值汇总

```
热力图
  .hm .cols clientWidth / scrollWidth（1200px 视口）= 862 / 862，溢出 0
  900px 视口 → 786 / 848，溢出 62px
  820px 视口 → 706 / 848，溢出 142px
  760px 视口 → 646 / 848，溢出 202px
  网格列数 = 53（约 12 个月）
  计算出的 min-width = 848px，--cell = 13px，--gap = 3px
  .hm .cell 实际尺寸 = 13.31 × 13.31px（由列宽驱动）
  7 行格子中心 y = 705.5 / 721.9 / 738.2 / 754.5 / 770.8 / 787.1 / 803.4（行距 16.31px）
  星期标签中心 y = 695.4(一) / 714.4(三) / 733.4(五)（行距 19.0px）
  .hm .dow padding-top = 22px；.hm .dow span line-height = 13px；月份行高 = 31px
  带 data-tip 的格子 = 367；带 title 的格子 = 0
  样例 data-tip = "2025-10-12 · 无调用"

模型块
  .mrow 内 bar 总数 = 4（2 模型 × 2 条）
  .track2 宽 828.1px / 高 6px，title = "token 总量占最高模型的比例"
  .track3 宽 828.1px / 高 6px，title = "缓存命中 2.7%（命中 2.8K / 未命中 101.2K）"
  .track3 段 = cm-hit-hit 2.69%（22.3px） + cm-hit-miss 97.31%（805.8px）

页面
  hero 文案 = "最近 30 天 · tokens 消耗"
  .tabs 按钮 = 总体使用量 / 会话使用量 / 上下文使用量（均为视图，非时间尺度）
  页面内时间尺度控件数量 = 0
  payload：token.daily[].keys = [date, tokens]；token.days = 30；daily 长度 = 3
```
