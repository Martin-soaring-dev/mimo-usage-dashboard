// MiMo Desktop 用量统计插件
// 提供两个 agent 工具：
//   usage_stats     —— 在对话里返回用量 Markdown 摘要
//   usage_dashboard —— 启动本地仪表盘并在浏览器打开
// 支持两种安装方式：
//   1) npm / GitHub 安装：本插件自带看板文件（同包根目录的 server.mjs 等）
//   2) 脚本安装：看板文件在 ~/.config/mimocode/usage-dashboard，路径写在 usage-dashboard.config.json
import { tool } from "@mimo-ai/plugin";
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import http from "node:http";
import { fileURLToPath } from "node:url";

// 插件所在目录（.../plugin），其父目录即包根，内含 server.mjs / dashboard.html / usage-core.mjs
const HERE = path.dirname(fileURLToPath(import.meta.url));
function bundledDashboardDir() {
  const root = path.resolve(HERE, "..");
  return fs.existsSync(path.join(root, "server.mjs")) ? root : null;
}

const CONFIG_FILE = path.join(os.homedir(), ".config", "mimocode", "usage-dashboard.config.json");
const DEFAULT_PORT = 8790;

function readConfig() {
  try {
    const raw = fs.readFileSync(CONFIG_FILE, "utf8").replace(/^\uFEFF/, "");
    return JSON.parse(raw);
  } catch { return {}; }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function ping(port, timeout = 900) {
  return new Promise((resolve) => {
    const req = http.get({ host: "127.0.0.1", port, path: "/api/ping", timeout }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on("error", () => resolve(false));
    req.on("timeout", () => { req.destroy(); resolve(false); });
  });
}

function firstExisting(paths) {
  for (const p of paths) { try { if (p && fs.existsSync(p)) return p; } catch { /* noop */ } }
  return null;
}

function resolveNode(cfg) {
  if (process.env.MIMO_USAGE_NODE && fs.existsSync(process.env.MIMO_USAGE_NODE)) return process.env.MIMO_USAGE_NODE;
  if (cfg.nodePath && fs.existsSync(cfg.nodePath)) return cfg.nodePath;
  const found = firstExisting([
    "C:\\Program Files\\nodejs\\node.exe",
    path.join(os.homedir(), "AppData", "Local", "Programs", "nodejs", "node.exe"),
    "/usr/local/bin/node", "/usr/bin/node", "/opt/homebrew/bin/node",
  ]);
  if (found) return found;
  if (process.execPath && /(^|[\\/])node(\.exe)?$/i.test(process.execPath)) return process.execPath;
  return "node"; // 退回 PATH
}

async function ensureServer(cfg) {
  const port = Number(cfg.port) || DEFAULT_PORT;
  const url = `http://127.0.0.1:${port}`;
  if (await ping(port)) return { ok: true, port, url };
  const dir = bundledDashboardDir() || cfg.dashboardDir;
  if (!dir) return { ok: false, error: "找不到看板文件（server.mjs）。若是脚本安装，请先运行 install.ps1；若是 npm 安装，请重新安装该包。" };
  const serverPath = path.join(dir, "server.mjs");
  if (!fs.existsSync(serverPath)) return { ok: false, error: `找不到 server.mjs：${serverPath}` };
  const node = resolveNode(cfg);
  try {
    const child = spawn(node, [serverPath, "--port", String(port)], { cwd: dir, detached: true, stdio: "ignore", windowsHide: true });
    child.on("error", () => { /* 在下面的轮询里统一报错 */ });
    child.unref();
  } catch (e) {
    return { ok: false, error: `启动 Node 失败：${e.message}（node=${node}）` };
  }
  for (let i = 0; i < 30; i++) { await sleep(200); if (await ping(port)) return { ok: true, port, url }; }
  return { ok: false, error: `服务启动超时（node=${node}）。请确认已安装 Node ≥ 22 或设置环境变量 MIMO_USAGE_NODE` };
}

function fetchText(url) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, { timeout: 8000 }, (res) => {
      let s = "";
      res.setEncoding("utf8");
      res.on("data", (c) => (s += c));
      res.on("end", () => resolve(s));
    });
    req.on("error", reject);
    req.on("timeout", () => { req.destroy(new Error("timeout")); });
  });
}

function openBrowser(url) {
  try {
    if (process.platform === "win32") spawn("cmd", ["/c", "start", "", url], { detached: true, stdio: "ignore", windowsHide: true }).unref();
    else if (process.platform === "darwin") spawn("open", [url], { detached: true, stdio: "ignore" }).unref();
    else spawn("xdg-open", [url], { detached: true, stdio: "ignore" }).unref();
  } catch { /* noop */ }
}

export default async () => {
  const cfg = readConfig();
  return {
    tool: {
      usage_stats: tool({
        description: [
          "获取 MiMo Desktop 本机的整体用量统计：token 消耗、输入/输出拆分、缓存读取占比、会话数、",
          "请求数、工具调用数、改动量，以及按模型的 token 份额。",
          "当用户询问“我的用量 / 消耗 / token 统计 / 用了多少”时调用它，并把返回的 Markdown 直接呈现给用户。",
        ].join(""),
        args: {
          days: tool.schema.number().int().min(0).max(3650).optional()
            .describe("统计最近多少天；0 表示全部时间。默认 30。"),
        },
        async execute(args) {
          const days = args && typeof args.days === "number" ? args.days : 30;
          const up = await ensureServer(cfg);
          if (!up.ok) return `用量统计不可用：${up.error}`;
          try {
            const text = await fetchText(`${up.url}/api/summary?days=${days}`);
            return `仪表盘：${up.url}\n\n${text}`;
          } catch (e) {
            return `读取用量失败：${e && e.message ? e.message : e}（服务地址 ${up.url}）`;
          }
        },
      }),
      usage_dashboard: tool({
        description: [
          "打开 MiMo Desktop 用量统计仪表盘：包含 12 个月 token 热力图、模型份额、每日 tokens 曲线、",
          "每日活跃会话柱状图与会话明细。会启动本地服务（127.0.0.1）并在默认浏览器打开。",
          "当用户想“看用量仪表盘 / 打开统计页面 / 查看图表”时调用。",
        ].join(""),
        args: {},
        async execute() {
          const up = await ensureServer(cfg);
          if (!up.ok) return `无法启动用量仪表盘：${up.error}`;
          openBrowser(up.url);
          return `用量仪表盘已就绪：${up.url}（已尝试用默认浏览器打开；也可以把该地址粘贴到 MiMo 内置浏览器）`;
        },
      }),
    },
  };
};
