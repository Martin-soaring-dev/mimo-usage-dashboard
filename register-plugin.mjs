// register-plugin.mjs — 把插件注册进 ~/.config/mimocode/mimocode.jsonc 的 plugin 数组
// 用法：node register-plugin.mjs <configPath> <pluginSpec>
// 说明：引擎的插件目录扫描只匹配 {plugin,plugins}/*.{ts,js}，不含 .mjs；
//       因此 .mjs 插件需通过配置的 plugin 数组注册（与桌面自带插件一致）。
import fs from "node:fs";

const [configPath, spec] = process.argv.slice(2);
if (!configPath || !spec) {
  console.error("usage: node register-plugin.mjs <configPath> <pluginSpec>");
  process.exit(2);
}

let raw = "";
try { raw = fs.readFileSync(configPath, "utf8"); }
catch { console.error(`CONFIG_NOT_FOUND ${configPath}`); process.exit(4); }

fs.writeFileSync(`${configPath}.bak-usage-dashboard`, raw);

let cfg;
try { cfg = JSON.parse(raw.replace(/^\uFEFF/, "")); }
catch { console.error("CONFIG_PARSE_FAILED"); process.exit(3); }

const list = Array.isArray(cfg.plugin) ? cfg.plugin : [];
const already = list.some((p) => JSON.stringify(p).includes("usage-dashboard"));
if (!already) list.push(spec);
cfg.plugin = list;
fs.writeFileSync(configPath, JSON.stringify(cfg, null, 2) + "\n");
console.log(already ? "already-registered" : "registered");
