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

## 5. 命令参数安全边界

引擎先对 command 模板做 `$ARGUMENTS` 文本替换，再扫描并执行 `!` 反引号命令。因而，模板中的引号不是可靠的转义层：含单引号的输入可能提前结束 PowerShell 单引号字符串，在 Python 脚本启动前执行额外 shell 语句。

当前注册器采用 fail-closed 设计：

- `/usage` 运行固定的 `usage_cli.py` 总览命令。
- `/usage-open` 运行固定的 `usage_cli.py --open` 命令。
- 两个模板都不包含 `$ARGUMENTS` 或动态参数拼接。
- 需要筛选 `token`、`session`、`context` 或日期范围时，直接运行本地 CLI，而不是把用户输入嵌入 shell 模板。
- CLI 对 `--db` 与 `--out` 路径允许 Windows 反斜杠；遇到非法或缺失的路径参数时明确报错，不能静默丢弃并回退到默认数据库。

旧版本曾用单引号包裹 `$ARGUMENTS`，并根据分号等字符测试注入。这些测试不能排除单引号闭合攻击，因此不能作为通用安全保证。当前固定命令设计刻意牺牲 slash 命令的动态筛选能力，以消除这条 shell 插值攻击面。

测试覆盖：`test/test_python.py` 验证 Windows 数据库路径与非法值拒绝；`test/command-e2e.mjs` 验证注册模板不包含动态参数占位符，并检查固定命令可生成 HTML。真实 MiMo/PowerShell 环境仍应在发布前验收。

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
