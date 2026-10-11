# MiMo Desktop 插件接口调研（完整版）

> 目标：找出 **MiMo Desktop** 可用的插件接口，用于本项目的用量统计插件。
> 方法：直接读取本机已安装的 `app.asar`（含打包在内的**引擎源码**）+ `@mimo-ai/plugin` 官方类型定义 + 开源仓库文档。
> 所有结论均给出**文件路径/行号**作为依据。**未验证处已明确标注**。

---

## 0. 结论速览

| 能力 | 是否可扩展 | 入口 |
|---|---|---|
| 注册 **agent 工具**（tool） | ✅ | `@mimo-ai/plugin` 的 `tool()`，经 `plugin` 数组加载 |
| 监听/改写 **会话与工具执行**（20+ hooks） | ✅ | 同上，返回 `Hooks` 对象 |
| 自定义 **认证 / Provider 模型** | ✅ | `Hooks.auth` / `Hooks.provider` |
| 在对话里返回 **Markdown 摘要** | ✅ | 工具返回值（`skill` 工具走 Markdown 渲染） |
| **注入 UI**（侧边栏 tab、菜单项、按钮） | ❌ | 桌面 App 无任何插件接口，只能改 app.asar |
| 对话内**内联交互组件** | ⚠️ | 只能靠助手回复正文里的 ```sci-widget 代码块 |
| 本机 **HTTP 面板** | ✅ | 工具启动本地服务 + 内置浏览器打开 |

本项目 `plugin/usage-dashboard.mjs` 用的正是 ✅ 那一行——**注册工具**。本报告补充完整的接口面。

---

## 1. 插件接口的官方来源

两个 SDK 包都在 `node_modules/@mimo-ai/`（本工作区已安装）：

| 包 | 版本 | 作用 | 依据 |
|---|---|---|---|
| `@mimo-ai/plugin` | 0.1.15 | 插件编写 SDK（`tool` / 类型 / hooks） | `node_modules/@mimo-ai/plugin/package.json:3` |
| `@mimo-ai/sdk` | 0.1.15 | 引擎 HTTP API 客户端（插件入参 `client`） | `node_modules/@mimo-ai/plugin/package.json:44` |

`@mimo-ai/plugin` 的 `exports` 只暴露三个子路径（`package.json:30-40`）：
- `.` → `dist/index.js`（全部 hooks 与类型）
- `./tool` → `dist/tool.js`（`tool()` 工厂）
- `./tui` → `dist/tui.js`（TUI，**插件不可用**）

> 引擎侧实现打包在桌面应用的 `app.asar` 内：`out/main/node.mjs`（37MB，即开源引擎的打包产物）。

---

## 2. 插件如何被发现与加载（代码级）

### 2.1 配置里的 `plugin` 数组

配置来源（`out/main/node.mjs:273340-273341`，引擎内嵌文档）：
- **项目级**：`.mimocode/mimocode.json` 或 `.jsonc`
- **全局级**：`~/.config/mimocode/mimocode.jsonc`（推荐）或 `.json`
- **路径根**：受 `MIMOCODE_HOME` 控制，否则走 XDG（Windows 为 `%LOCALAPPDATA%\mimocode\`）（`node.mjs:81266,273379`）

`plugin` 字段的类型定义（`node.mjs:254411`）：

```js
Spec = Union([ String, Tuple([ String, Options ]) ])
Options = Record(String, Unknown)
```

即每一项可以是：
1. `"mimo-usage-dashboard"`（字符串）
2. `["mimo-usage-dashboard", { ...任意选项 }]`（带 options 的元组）

归一化逻辑（`node.mjs:730572-730581`）：`plugin` 数组各项会被包装为
`{ spec, source, scope }` 存进内部 `plugin_origins`，再由
`PluginLoader.loadExternal({ items: cfg.plugin_origins, kind: "server" })` 加载（`node.mjs:610702-610742`）。

> 这正是本项目 `install.ps1` / `register-plugin.mjs` 往 `plugin` 数组写 `file://.../usage-dashboard.mjs` 的原因。

### 2.2 支持哪些 spec 形式

`isPathPluginSpec`（`node.mjs:254207-254209`）：
```js
spec.startsWith("file://") || spec.startsWith(".") || isAbsolutePath(spec)
```

`resolvePluginTarget`（`node.mjs:254245-254251`）：
- **路径型**（`file://`、`./`、绝对路径）→ `resolvePathPluginTarget`：
  - 指向文件 → 直接用
  - 指向目录 → 要求有 `package.json`，否则找 index 文件（`node.mjs:254210-254226`）
- **其余** → 当作 **npm 包**：`npm.add(pkg)` 安装到引擎缓存（`node.mjs:254248-254251`）

因此支持的写法（README 已验证）：
```
"mimo-usage-dashboard"                                  // npm
"github:Martin-soaring-dev/mimo-usage-dashboard"        // github（npm 支持 spec）
"https://github.com/.../main.tar.gz"                    // tarball URL
"file:///abs/path/plugin/usage-dashboard.mjs"           // 本地文件
```

### 2.3 入口如何解析（npm 包）

`resolvePackageEntrypoint`（`node.mjs:254138-254150`）：
1. 若有 `package.json.exports`，取 `exports["./server"]`（`server` 是 kind）
2. 否则回退 `exports.main` / `main` 字段
3. 本地目录型：找 `index.js/ts/mjs` 等（`INDEX_FILES`，`node.mjs:254158-254163`）

> ⚠️ **重要**：kind 为 `"server"` 时优先读 `exports["./server"]`。本项目 `package.json` 只定义了 `"."`，没有 `"./server"`，因此回退到 `main: "index.mjs"`（`package.json:9`）——**能工作但属回退路径**。若要更规范，建议加 `"exports": { ".": "./index.mjs", "./server": "./index.mjs" }`。

### 2.4 插件模块的导出形状

`readV1Plugin` / `readPluginModule`（`node.mjs:254285-254310`）：
- **v1 形状**：`export default { id?, server: Plugin, tui?: never }`（对应类型 `PluginModule`，`@mimo-ai/plugin/dist/index.d.ts:52-56`）
- **函数形状**：`export default async (input, options) => Hooks`（本项目用的就是这种）

> 本项目 `index.mjs` 只 `export { default } from "./plugin/usage-dashboard.mjs"`（`index.mjs:3`），导出的是**函数**，匹配第二种。

### 2.5 依赖自动安装

`config4.waitForDependencies()`（`node.mjs:610706-610707`）：当 `plugin` 数组非空时，引擎会先确保依赖（npm 安装）就绪，再加载插件。这解释了 README 所说"引擎会自动 `npm install` 该包"。

---

## 3. 插件能拿到什么（Plugin 入参）

类型定义 `PluginInput`（`@mimo-ai/plugin/dist/index.d.ts:36-46`）：

| 字段 | 类型 | 用途 |
|---|---|---|
| `client` | `createOpencodeClient()` | **引擎 HTTP API 客户端**（可用 SDK 全部接口） |
| `project` | `Project` | 当前项目 |
| `directory` | `string` | 会话工作目录 |
| `worktree` | `string` | 工作树根 |
| `experimental_workspace` | `{ register }` | 注册 workspace 适配器（实验） |
| `serverUrl` | `URL` | 引擎内部服务地址 |
| `$` | `BunShell` | Bun shell（Node 环境下为 `undefined`） |

引擎实现处 `node.mjs:610645-610659` 与此一致，注意 `$`：`typeof Bun === "undefined" ? undefined : Bun.$` —— **桌面 Node 环境下 `$` 为 undefined**。

---

## 4. Hooks 全清单

`Hooks` 接口（`@mimo-ai/plugin/dist/index.d.ts:383-625`）。按用途分组：

### 4.1 工具与配置
| Hook | 作用 |
|---|---|
| `tool` | **注册工具**（`{ [name]: ToolDefinition }`） |
| `config` | 读取/修改最终配置 |
| `tool.definition` | 改写发给 LLM 的工具描述/参数 |
| `command.execute.before` | 命令执行前注入 parts |
| `tool.execute.before` | **改写工具参数 / 取消执行**（output 有 `cancel`） |
| `tool.execute.after` | 改写工具输出 |
| `shell.env` | 注入 shell 环境变量 |

### 4.2 会话与对话
| Hook | 作用 |
|---|---|
| `chat.message` | 收到新用户消息 |
| `chat.params` | 改写采样参数（temperature/topP/…） |
| `chat.headers` | 注入 LLM 请求头 |
| `session.pre` / `session.post` | 会话 runLoop 起止（post 带完整 trajectory） |
| `session.userQuery.pre` / `post` | 每一步 LLM 调用前后 |
| `session.llm.request` | 请求发往模型网关前 |
| `experimental.chat.messages.transform` | 改写消息历史 |
| `experimental.chat.system.transform` | 改写 system prompt |
| `experimental.text.complete` | 文本完成回调 |

### 4.3 权限 / 认证 / Provider
| Hook | 作用 |
|---|---|
| `permission.ask` | 允许/拒绝/询问权限 |
| `auth` | **自定义登录方式**（oauth / api key 两种） |
| `provider` | **动态提供模型列表** |

### 4.4 Actor / 压缩（较新）
| Hook | 作用 |
|---|---|
| `actor.preStop` / `actor.postStop` | 子代理交付前后（可续跑一轮） |
| `experimental.session.compacting` | 自定义压缩 prompt |
| `experimental.compaction.autocontinue` | 压缩后是否自动续跑 |

### 4.5 事件
| Hook | 作用 |
|---|---|
| `event` | 订阅引擎所有事件总线消息 |

> 引擎加载 hook 的顺序（`node.mjs:610757-610773`）：先调用每个 hook 的 `config(cfg)`，再 `bus.subscribeAll()` 把事件推给所有 `hook.event`。

---

## 5. 工具的完整契约

`ToolDefinition`（`@mimo-ai/plugin/dist/tool.d.ts:40-52`）：

```ts
tool<Args extends z.ZodRawShape>(input: {
  description: string;
  args: Args;                                       // zod raw shape
  execute(args: z.infer<z.ZodObject<Args>>, context: ToolContext): Promise<ToolResult>;
}): { description; args; execute }
tool.schema  // === zod（v4，@mimo-ai/plugin 的依赖）
```

`ToolContext`（`tool.d.ts:3-33`）——**execute 的第二个参数**，本项目当前**未使用**：

| 字段 | 用途 |
|---|---|
| `sessionID` / `messageID` / `agent` | 当前会话上下文 |
| `directory` / `worktree` | 解析相对路径（优于 `process.cwd()`） |
| `abort` | `AbortSignal` |
| `metadata({title, metadata})` | **上报工具卡片标题/元数据** |
| `ask(input): Effect` | **主动请求权限**（permission/patterns/always） |

`ToolResult`（`tool.d.ts:34-39`）：`string` 或 `{ output, metadata }`。

> **本项目可改进点**：`execute(args)` 只用了第一个参数（`plugin/usage-dashboard.mjs:121-131`），可加 `metadata({title:"用量统计"})` 让工具卡片更清晰；也可用 `context.directory`。

---

## 6. 桌面应用**内建**的额外工具（非插件，但可参考）

桌面自带插件 `out/main/desktop-tools-plugin.mjs`（45KB）注册了一批内置工具，其中**部分能力可被插件通过 globalThis 桥复用**：

| 内置工具 | 桥（globalThis） | 说明 |
|---|---|---|
| `present_files` | — | 展示文件卡片（本项目正在用同类能力） |
| `image_gen` / `image_edit` | `__mimoImageGen` | AI 生图/改图 |
| `tts_speech` / `asr_transcribe` | `__mimoAudioBridge` | 语音合成/识别 |
| `automation_update` | `__mimoAutomationUpdate` | 定时任务 |
| `get/set_desktop_setting` | `__mimoDesktopSettings` | 读写桌面外观设置 |
| `contacts` / `talk_to_session` / `create_session` | `__mimoSessionChat` | 跨会话通信 |
| `list/read_external_session` | `__mimoSessionChat` | 读取外部 agent（Claude Code）会话 |

依据：`out/main/desktop-tools-plugin.mjs:113`（工具表）+ 各桥调用点。

> ⚠️ 这些桥是**桌面应用注入的**，插件是否一定可见取决于注入时机；**未在本报告中验证**（需运行时确认 `globalThis.__mimo*` 是否存在）。

---

## 7. UI 能力的真相（为什么本项目的面板是"网页 + app.asar 补丁"）

桌面 App 是**闭源**的（`docs/extensibility.md:12`）。对本机 `app.asar` 的实测结论：

- **插件 API 只有服务端能力**：`PluginModule = { id?, server, tui?: never }`，hooks 全是 `session.*` / `tool.*` / `chat.*` / `actor.*`，**无注册视图/菜单/按钮的接口**（`@mimo-ai/plugin/dist/index.d.ts:52-56`，与 `docs/extensibility.md:42-43` 一致）。
- **右侧面板 "+" 菜单是硬编码常量**，不扫描目录（`docs/extensibility.md:44-52`）。
- **composer 的 "/" 菜单**才走目录/配置加载（Command / Skill / MCP 三类，`docs/extensibility.md:53-54`）。
- 唯一的内联 UI 入口是 **`sci-widget`** 代码块 → `<iframe sandbox="allow-scripts">`（`docs/extensibility.md:55-57`）。
- **工具输出不走 Markdown**（只有 `tool === "skill"` 才渲染 Markdown），所以图文富文本必须出现在**助手回复正文**（`docs/extensibility.md:58-59`）。

> 外网交叉验证：第三方项目也确认 "MiMo Code 与 opencode 使用同一套 plugin SDK"，插件目录 `~/.config/mimocode/plugins/`，配置路径 `~/.config/mimocode/mimocode.json`（rtk-ai/rtk issue #2380）。

---

## 8. 插件目录扫描（另一条发现路径）

除 `plugin` 数组外，引擎还会扫描**文件型 hook**（`node.mjs:610551,610783`）：

```
FILE_HOOK_GLOB = "{hook,hooks}/*.{js,ts}"
```
- 扫描位置：`config4.directories()` 中**除 `~/.mimocode` 外**的所有目录
- 匹配文件用 `Bun.build` 编译后 `import`（`node.mjs:610792-610806`）
- 有 500ms 变更检查（`FILE_HOOK_CHECK_INTERVAL_MS = 500`）

> 另有一个传闻中的 `{plugin,plugins}/*.{ts,js}` 目录约定（README 与第三方均提及，`~/.config/mimocode/plugins/`）。**本项目源码里我只直接读到了 `{hook,hooks}` 的 glob**；`{plugin,plugins}` 的目录扫描**未在桌面 App 的打包产物中直接读到**——README 的说法**未能在本机代码中完全证实**，建议以 `plugin` 数组注册为准（这是已验证可用的路径）。

---

## 9. 给本项目的具体建议

1. **补 `exports."./server"`**（`package.json`）：让入口解析走首选路径而非 `main` 回退（见 §2.3）。
2. **用 `ToolContext`**：`execute(args, ctx)` 里加 `ctx.metadata({ title: "用量统计" })`，并优先用 `ctx.directory` 而非 `process.cwd()`（`plugin/usage-dashboard.mjs:121,140`）。
3. **`$` 在 Node 环境为 undefined**：插件里不要用 Bun shell；需要执行命令用 `node:child_process`（README 已如此做，正确）。
4. **注册时优先 npm 包名**而非 `file://`：npm 路径有依赖自动安装 + 版本管理；`file://` 仅适合本地开发（见 §2.2）。
5. **若想要"入口按钮"**：官方唯一路径是 app.asar 补丁（已实现于 `desktop-patch/`），或用 composer `/` 菜单的 Skill / MCP prompts。

---

## 10. 依据文件清单

| 主题 | 文件 / 位置 |
|---|---|
| 插件类型、Hooks、PluginInput | `node_modules/@mimo-ai/plugin/dist/index.d.ts` |
| 工具契约、ToolContext | `node_modules/@mimo-ai/plugin/dist/tool.d.ts` |
| SDK 包元信息 | `node_modules/@mimo-ai/plugin/package.json`、`@mimo-ai/sdk` |
| 插件 spec/加载/入口解析 | `app.asar` → `out/main/node.mjs` L254075-254532 |
| 插件服务与 hook 装配 | `app.asar` → `out/main/node.mjs` L610625-610775 |
| plugin_origins 归一化 | `app.asar` → `out/main/node.mjs` L730572-730581 |
| 配置路径与内嵌文档 | `app.asar` → `out/main/node.mjs` L273340-273379 |
| 桌面内置工具与桥 | `app.asar` → `out/main/desktop-tools-plugin.mjs` |
| UI 扩展面调研 | `docs/extensibility.md` |

> 复现命令（只读，不改 App）：
> ```bash
> node -e "const {listPackage}=require('@electron/asar'); \
>   console.log(listPackage('D:/Program Files/Xiaomi MiMo/resources/app.asar').filter(f=>/plugin/i.test(f)))"
> ```
> 读取具体文件内容用 `getRawHeader` + 按 offset 切片（`extractFile` 对补丁过的包会失败，见 `docs/extensibility.md:93`）。
