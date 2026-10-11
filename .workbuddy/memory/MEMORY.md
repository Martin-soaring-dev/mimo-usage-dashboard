# MiMo 项目长期备忘

## MiMo Desktop 本机环境（2026-10-11 实测）
- Xiaomi MiMo Desktop 26.1010.101546（Electron），装在 `D:\Program Files\Xiaomi MiMo\`，引擎为 OpenCode 分支，打包在 app.asar 的 out/main/node.mjs。
- 配置 `~/.config/mimocode/mimocode.jsonc`（顶层键 $schema/command/mcp/provider）；数据 `~/.local/share/mimocode/mimocode.db`（WAL，~218MB）。
- 注意：直接读 mimocode.jsonc 原文会触发用户敏感值拒绝；要用 register_command.py 的 load_jsonc 做结构化提取，只看 command/plugin 字段。

## 引擎能力边界（源码级复核，node.mjs 切出验证）
- `command` 机制：模板 `!`…`` 先本地执行插回 stdout，但结果必作为 prompt 发给模型（L580130-580199），还会触发 title() 起标题 → **聊天框 /usage 永远不可能零 token**，只能做到"重活本地、一行复述"。
- 右面板 "+" 菜单是编译常量 ["review","terminal","browser","files","chat","subagents"]，无插件 UI API；deep-link 仅支持远程会话加入。
- 内置浏览器面板（rp-browser webview）存在；present_files 官方描述认可"本地静态服务 + 内置浏览器打开 URL"，但打开动作需 AI 工具调用。
- DB 运行中 mode=ro 只读访问安全（实测 0.01s）。

## 已定位的缓存命中率图表缺陷
- usage_cli.py modelCard：未命中段 width≥1.6% + margin-left:-1.6% 被画进命中区（命中 99.56% 时橙条在 97.96–99.56%，真实未命中端却是底色）；"输入 tokens"卡片显示原始 input 与 README 口径（input+cache.read）不一致。

## 集成方案结论
- 推荐：保留 command 路线 + 页面三 Tab（总体/会话/上下文）+ 修命中率条；刷新用自定义协议（HKCU mimo-usage://）实现零 token 真刷新；内嵌侧栏走"短命本地服务 + present_files"（需实测 present_files 对 http URL 的行为）。
- 详细报告：outputs/integration-research-20261011.md（outputs/ 已 gitignore）。

