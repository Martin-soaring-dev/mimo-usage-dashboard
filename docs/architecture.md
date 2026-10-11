# 架构与引擎机制实证

> 本文记录 v0.2 重构的技术依据。所有结论均来自**直接读取本机 `app.asar`**
> （`out/main/node.mjs`，约 37MB，含完整开源引擎），非猜测。
> 复核方法见文末。

## 0. 结论速览

`/usage` 现在是一个**零 AI 参与的本地命令**，走 MiMo 引擎的 `command` 机制 + 内置 shell 替换：

```
用户输入 /usage token --days 7
   │
   ├─ 引擎取 cfg.command.usage.template
   ├─ 替换 $ARGUMENTS / $1 $2 …          （node.mjs @31186707）
   ├─ SHELL_REGEX 扫描 !`…`              （src/config/markdown.ts）
   ├─ 本地执行 → stdout 替换回文本
   └─ 结果作为 prompt 发给模型（模型只复述）
```

脚本本身完全离线：读库 → 聚合 → 渲染 HTML → 写文件。

## 1. 命令系统（`Command.state`，node.mjs @18768600）

引擎的命令表由四个来源合并，**后者不覆盖前者**：

```js
// 简化自引擎源码
if (!DISABLE_CRON) { commands3[LOOPS] = { source: "command", … }; }

for (const [name, command] of Object.entries(cfg.command ?? {})) {
  commands3[name] = {                        // ← ① 配置 command 字段
    name, agent: command.agent, model: command.model,
    description: command.description,
    source: "command",
    get template() { return command.template; },
    subtask: command.subtask,
  };
}

for (const [name, prompt] of Object.entries(await mcp.prompts())) {
  commands3[name] = { source: "mcp", … };     // ← ② MCP prompts
}

for (const item of await skill.all()) {
  if (commands3[item.name]) continue;         // ← ③ 技能：不覆盖已有命令
  commands3[item.name] = { source: "skill", get template() { return item.content; } };
}
```

**关键推论**：只要在配置里注册 `command.usage`，就**优先于**同名技能 `usage`。
这正是 v0.2 用来接管 `/usage` 的机制 —— 无需删除技能，命令自动胜出。

## 2. 模板替换与 shell 执行（node.mjs @31186707）

```js
const raw2  = input.arguments.match(argsRegex) ?? [];
const args3 = raw2.map((a) => a.replace(quoteTrimRegex, ""));     // 参数分词
const templateCommand = await cmd2.template;

let template;
if (cmd2.source === "skill") {
  template = input.arguments;                                     // 技能：整段参数当 prompt
} else {
  // ① 位置参数 $1 $2 …（末位收尾吃掉剩余）
  const withArgs = templateCommand.replaceAll(placeholderRegex, (_, idx) => { … });
  // ② $ARGUMENTS 整体替换
  template = withArgs.replaceAll("$ARGUMENTS", input.arguments);
  // ③ 无占位符且有参数 → 追加到末尾
  if (placeholders.length === 0 && !usesArgumentsPlaceholder && input.arguments.trim())
    template = template + "\n\n" + input.arguments;
}

// ④ shell 替换：先扫描，再本地执行，再插回
const shellMatches = markdown.shell(template);
if (shellMatches.length > 0) {
  const sh = shell.preferred();
  const results = await Promise.all(shellMatches.map(async ([, cmd]) =>
    (await process.text([cmd], { shell: sh, nothrow: true })).text));
  template = template.replace(bashRegex, () => results[i++]);
}
```

**顺序要点**：`$ARGUMENTS` 替换发生在 shell 扫描**之前**
⇒ 用户输入会被拼进 shell 命令行 ⇒ **存在命令注入面**（见 §5）。

因此模板对 `$ARGUMENTS` 的包裹方式**直接决定安全性**，见 §5.1。

**执行环境**：`shell.preferred()` = `lazy(() => select(process.env.SHELL))`，
Windows 下选中 PowerShell 并加 UTF-8 前缀（`POWERSHELL_UTF8_PREFIX`），
通过 `process.text([cmd], { shell })` 执行（`nothrow: true`，失败不抛错）。

### 2.1 shell 语法定义（src/config/markdown.ts）

```js
const SHELL_REGEX = /!`([^`]+)`/g;
function shell(template) { return Array.from(template.matchAll(SHELL_REGEX)); }
function files(template) { return Array.from(template.matchAll(FILE_REGEX)); }
```

注意 `shell()` 对**整个模板原文**做正则，**不排除代码块** ——
所以模板里不能出现任何示例性的 `` !`…` ``，否则会被当真命令执行。

## 3. 为什么最终发给模型的仍是 prompt

```js
} else if (cmd2.source === "skill") {
  const visibleText = `/${input.command} ${input.arguments}`;
  parts4 = [{ type: "text", text: visibleText }, …];    // 技能：只显示命令名
} else {
  parts4 = [...templateParts, ...input.parts ?? []];    // command：模板（含脚本输出）发给模型
}
```

**所以 `command` 源无法做到「模型完全不运行」** —— 它保证的是：
**繁重的读库 / 计算 / 渲染全在本地脚本完成**，模型只收到一行结果并复述。
这就是 v0.2 方案与 qwen code `/update` 的等价之处：**确定性本地函数 + 极低成本转述**。

若需要模型完全不参与，则只能走离线路径：直接 `python usage_cli.py --open`
（或双击 `usage.cmd`）—— 脚本自己开浏览器，引擎毫不知情。

## 4. 为什么弃用常驻服务与对话内卡片

### 4.1 插件 API 无 UI 能力

`@mimo-ai/plugin` 的 `PluginModule = { id?, server: Plugin, tui?: never }`，
hooks 全是 `session.*` / `tool.*` / `chat.*` / `actor.*`，
**没有注册视图 / 菜单 / 按钮的接口**。右侧面板的 "+" 菜单是编译进 bundle 的常量数组，
**不扫描任何目录**。

### 4.2 `sci-widget` 的硬约束

`sci-widget` 代码块渲染为 `<iframe sandbox="allow-scripts" srcdoc=…>`，强制注入 CSP：

```
default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';
img-src data: blob:; font-src data:; connect-src 'none'
```

- `connect-src 'none'` ⇒ 卡片内**不能 fetch** ⇒ 数据必须内联
- `sandbox="allow-scripts"`（无 same-origin）⇒ iframe 是不透明源
- 高度上限 560px
- **流式占位**：`isSciWidget(lang) ? (streaming ? <Loading/> : <Iframe/>)`
  ⇒ 只要 HTML 由 AI 逐字输出，就会看到「正在生成交互演示…」

### 4.3 结论

放弃 `sci-widget` 与常驻服务，改为**静态 HTML 文件**：
输出稳定、零 token、可反复打开、可归档、不占端口。

## 5. 命令注入防护

因为 `$ARGUMENTS` 会被拼进 shell 命令行，采用**双层防护**。

### 5.1 第一层：模板用 PowerShell 单引号包裹

模板写成：

```
!`<python> <script> --arg-string '$ARGUMENTS'`
```

`` `$ARGUMENTS` `` **必须**用**单引号**包裹。三种写法的实测差异：

| 写法 | 用户输入 `token ; echo x` | 用户输入 `--out "C:\a b\x.html"` |
|---|---|---|
| 裸接 `$ARGUMENTS` | ❌ PowerShell 把 `;` 当语法，**整行解析失败** | ✅ 可用 |
| 双引号 `"$ARGUMENTS"` | ✅ 拦住 | ❌ 内层 `"` 提前闭合，**参数被截断** |
| **单引号 `'$ARGUMENTS'`** | ✅ **字面文本，shell 层不解释** | ✅ **内部双引号原样保留** |

Windows 上 `shell.preferred()` 选中 PowerShell，而 PowerShell 的 `'...'` 是
**字面字符串**（不做变量展开、不解释分号与 `&&`），所以注入面在 shell 层即被堵死。

> ⚠️ **踩坑记录**：早期版本用 `--arg-string "$ARGUMENTS"`，在
> `--out "C:\Users\x\a b\y.html"` 这类输入下引号会提前闭合，`--out` 被静默丢弃、
> 回落到默认路径。裸接则会让含 `;` 的输入直接令 PowerShell 报语法错。
> 只有单引号同时满足两侧。

### 5.2 第二层：脚本白名单（`_sanitize_args`）

`--arg-string` 之后的所有 argv 元素拼回整串，再用**自写的引号拆分器**解析
（不用 `shlex` —— `posix=True` 会把 Windows 路径的 `\` 当转义符吞掉，
`posix=False` 又不处理嵌套引号），随后逐个过滤：

- 含 `; | & $ \` > < \n \r \` 的 token 一律丢弃
- 只放行已知 kind（`all|token|session|context`）与已知选项
  （`--days --db --out --session --limit --open --print-path`）
- **例外**：`--out` 的值允许含 `\`（Windows 路径必需），但仍拒绝 `; | & $ \` >`
- 未知选项 / 未知 kind 全部丢弃

实测：输入 `token ; echo pwned` → 白名单后只剩 `['token']`，恶意部分全部丢弃。

测试见：
- `test/test_python.py`「参数白名单」（9 项断言）
- `test/e2e_live.py`「注入防护（真实路径）」（真实跑一遍 PowerShell）

### 5.3 已知限制

PowerShell 在把参数传给外部程序时**会剥掉内层双引号**，因此
`--arg-string 'token --out "C:\a b\x.html"'` 到达脚本时双引号已消失，
含空格路径会散架。实际场景不受影响：

- 用户通常**不传** `--out`，走 `default_out_path()`（默认当前目录）；
- 默认路径与 `~/.config/mimocode/` 均不含空格。

## 6. 复核方法

```bash
# 只读列出 asar 内文件（不需解包）
node -e "const {listPackage}=require('@electron/asar');
  console.log(listPackage('D:/Program Files/Xiaomi MiMo/resources/app.asar')
    .map(f=>f.replace(/\\\\/g,'/')).filter(f=>f.startsWith('/out/main')).join('\n'))"

# 读取 node.mjs 任意片段（getRawHeader + 按 offset 切片）
# 关键位置：
#   @18768600  Command.state 命令表构建
#   @31186707  模板替换 + shell 执行
#   @10112741  src/config/markdown.ts（SHELL_REGEX）
#   @15007564  src/shell/shell.ts（preferred / POWERSHELL_UTF8_PREFIX）
```

> `extractFile` 对补丁过的包会失败；用 `getRawHeader` 拿 `{header, headerSize}`，
> 数据区起点 = `8 + headerSize`，按 header 条目的 `offset`/`size` 切片最稳。
