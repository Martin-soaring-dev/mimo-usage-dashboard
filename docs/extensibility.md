# MiMo Desktop 扩展面调研

> 结论速览：MiMo **引擎**开源、可扩展；MiMo **桌面应用**闭源，UI 入口只能靠 app.asar 补丁。
> 本文是给"想在 MiMo Desktop 里加用量入口/按钮"这个目标做的接口摸底，所有结论都来自直接读取本机
> `app.asar` 的代码（非猜测）。文末附自查方法。

## 一、开源边界

| 组件 | 是否开源 | 说明 |
|---|---|---|
| **MiMoCode 引擎**（CLI 内核） | ✅ MIT | `github.com/XiaomiMiMo/MiMo-Code`，OpenCode 分支 |
| **Xiaomi MiMo Desktop**（桌面应用） | ❌ 闭源 | 以 MiMoCode 为内核，独立产品，需申请内测 |

仓库 `XiaomiMiMo/MiMo-Code` 的 `packages/` 只有：

```
packages/cli       ← 终端引擎
packages/plugin    ← @mimo-ai/plugin（本仓库插件用的就是这个）
packages/sdk
packages/shared
```

**没有 `packages/desktop`。** 桌面应用不在开源仓库里。

`mimo.xiaomi.com/legal/third-party-licenses` 是**合规声明**（列出闭源桌面 App 打包用到的第三方 OSS 及许可证），
不等于桌面 App 开源。

## 二、可用的扩展入口（实测）

| 机制 | 能贡献什么 | 用户在哪看到 | UI 能力 |
|---|---|---|---|
| **引擎插件 API** `@mimo-ai/plugin` | 工具 + 一组 hooks | 工具调用、对话 | ❌ |
| **MCP 服务器**（stdio / http） | 工具 + **prompts** | composer `/` 菜单、插件页 | ❌ |
| **技能 Skill** | 指令（可带工具/脚本） | composer `/` 菜单、插件页 | ⚠️ |
| **自定义命令 Command** | 提示词 | composer `/` 菜单 | ⚠️ |
| **`sci-widget` 代码块** ★ | 内联交互式 HTML | **对话正文里的活组件** | ✅ |
| **产物 Artifacts / `present_files`** | 文件卡片 / URL | 右侧面板、产物中心 | ✅ |
| **内置浏览器** | 任意带 scheme 的 http URL | 右侧面板浏览器 tab | ✅ |

### 关键代码事实

- **插件只有服务端能力。** `@mimo-ai/plugin` 的 `PluginModule = { id?, server: Plugin; tui?: never }`，
  hooks 全是 `session.*` / `tool.execute.*` / `chat.*` / `actor.*` 之类，没有注册视图/菜单/按钮的接口。
- **右侧面板的 "+" 菜单是硬编码的**（`out/renderer/assets/index-*.js`）：
  ```js
  const gLe = ["review","terminal","browser","files","chat","subagents"];
  function ALe(browserEnabled=true, ...) {
    return gLe.filter(...).map(r => ({ type: r, label: fi[r], kbd: ... }));
  }
  ```
  列表、文案(`fi`)、快捷键(`vLe`)、图标全是常量 + i18n 表；点击 `openTool(type)` 打开的 tab 类型也是固定集合。
  **不扫描任何目录**，加一项只能改 bundle（app.asar）。
- **composer 的 "/" 菜单是目录/配置加载的**：聚合三类来源 `sourceCommand` / `sourceMcp` / `sourceSkill`
  （引擎 `/command` 端点 + 技能目录扫描 + MCP 配置）。
- **`sci-widget`**（`out/renderer/assets/MarkdownImpl-*.js`）：语言为 `sci-widget` 的代码块渲染成
  `<iframe sandbox="allow-scripts" srcdoc={html}>`，配 `postMessage({type:"sci-widget:height"})` 自适应高度
  （最小 80px、上限 560px）。是唯一的内联交互 UI 入口。
- **工具输出不走 Markdown**（`out/renderer/assets/HarnessRow-*.js` 的 `Et()`）：只有 `tool==="skill"` 才用
  Markdown 渲染，其余一律 `<pre>` 纯文本（截断 3000 字）。所以 **sci-widget 必须出现在助手回复正文**里。
- **内置浏览器**（`out/renderer/assets/RpBrowser-*.js`）：URL 规范化时，带 `http://` scheme 的地址原样加载；
  只写 `localhost:8790` 会被错误补成 `https://localhost:8790`。要打开用量面板请用完整地址
  `http://127.0.0.1:8790/`。

## 三、做不到的

工具栏按钮、侧边栏原生 tab、菜单项、快捷键、主动推送通知 —— **桌面 App 没有任何插件接口**，
只能改 app.asar（且官方升级即失效）。

## 四、三条路线

1. **不改 UI（推荐先做）**：用桌面已给的入口——composer `/` 菜单（Command / Skill / MCP）、
   对话内 `sci-widget`、产物 / 内置浏览器。全部走官方插件体系，完全在开源引擎能力范围内。
2. **改 UI**：app.asar 打补丁（脆弱、升级失效，见 `desktop-patch/`）；或向官方仓库提 issue/反馈请求
   加一个原生用量面板。
3. **自建客户端**：基于开源引擎（`packages/cli`）+ `packages/sdk` 自己做一个界面，完全可控，但工作量大。

## 五、两个产品的落点

| 产品 | 不改 UI 的落点 |
|---|---|
| **整体用量统计** | `usage_stats` 工具（Markdown 摘要）+ 命令/技能入口 + 内置浏览器打开 `http://127.0.0.1:8790` |
| **单 session 用量** | 工具读库返回当前会话明细；用 `sci-widget` 内联一个带"切换会话/刷新"按钮的面板，widget 内 `fetch` 本地 API 实时刷新（需给服务加 `Access-Control-Allow-Origin`，因为 widget 是 `allow-scripts` 无同源的不透明源） |

## 六、自查方法

```bash
# 只读打开已安装的 app.asar（补丁存在时用备份的原始包更干净）
# 依赖 @electron/asar（devDependency）
node -e "const {listPackage}=require('@electron/asar'); \
  console.log(listPackage('D:/Program Files/Xiaomi MiMo/resources/app.asar'))"
```

用 `getRawHeader` + 手动切片可读出任意 `out/**` 文件内容（`extractFile` 对补丁后的包会失败）。
关键文件：`out/renderer/assets/index-*.js`（右侧面板菜单）、`MarkdownImpl-*.js`（sci-widget）、
`HarnessRow-*.js`（工具输出渲染）、`RpBrowser-*.js`（内置浏览器）、`out/main/index.mjs`（插件/MCP/命令加载）。
