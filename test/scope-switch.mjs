// test/scope-switch.mjs — 用量范围两档开关与「无重置入口」的真实浏览器回归（Issue #16 / #19）
//
// 为什么需要这个文件：页面上的两档开关（会话用量 / 全局用量）与「页面不再提供重置入口」
// 是纯前端交互，Python 侧只能断言「HTML 里有没有这些元素」，驱动不了点击，
// 也验不到「点完之后激活的是哪个标签、URL 变成什么」。
//
// 这里的断言刻意**验效果而不是验机制**：早期版本我把「重置后 URL 不带 tab」当验收，
// 它在重置实际失效时照样通过（只删了参数，地址栏里显式的 session=<id> 还在）。
// 所以每条都盯用户能看到的最终状态。
//
// 用法：
//   node test/scope-switch.mjs                    # 无浏览器则 SKIP
//   node test/scope-switch.mjs --require-chrome    # 无浏览器必须失败
// 退出码：0 = PASS / 1 = FAIL / 2 = SKIP（未加 --require-chrome 且无浏览器）

import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { spawn, execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { findChrome, openPage, sleep } from "./cdp.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SCRIPT = path.join(HERE, "..", "usage_cli.py");
const PY = process.env.MIMO_USAGE_PY || process.env.PYTHON
  || (process.platform === "win32" ? "python" : "python3");
const REQUIRE_CHROME = process.argv.includes("--require-chrome");

let failed = 0;
const check = (name, ok, extra = "") => {
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${extra ? "  " + extra : ""}`);
  if (!ok) failed++;
};

const work = fs.mkdtempSync(path.join(os.tmpdir(), "mimo-scope-switch-"));
const bail = (code, reason) => {
  try { fs.rmSync(work, { recursive: true, force: true }); } catch { /* ignore */ }
  console.log(code === 2 ? `\n  SKIP  ${reason}` : `\n  FAIL  ${reason}`);
  console.log(code === 2 ? "\nSCOPE SWITCH SKIPPED" : "\nSCOPE SWITCH FAILED");
  process.exit(code);
};

if (!findChrome()) {
  if (REQUIRE_CHROME) bail(1, "--require-chrome：未找到 Chrome/Edge");
  bail(2, "未找到可用 Chrome/Edge（可用 CHROME_PATH 指定）");
}

// ---- 隔离环境：临时数据目录 + 合成库（不碰真实使用记录） ----
const dataDir = path.join(work, "data");
fs.mkdirSync(dataDir, { recursive: true });
const dbFile = path.join(work, "synthetic.db");
process.env.MIMO_USAGE_DATA_DIR = dataDir;

const fixture = [
  "import sys",
  "sys.path.insert(0, " + JSON.stringify(HERE.split(path.sep).join("/")) + ")",
  "from test_python import make_rich_db",
  "make_rich_db(sys.argv[1])",
].join("\n");
execFileSync(PY, ["-c", fixture, dbFile], { stdio: ["ignore", "pipe", "pipe"] });

const TOKEN = "e2e-scope-token-01";
const serve = spawn(PY, [
  SCRIPT, "serve", "--db", dbFile, "--token", TOKEN, "--idle-minutes", "10",
], { stdio: ["ignore", "pipe", "pipe"], env: process.env });

let pageUrl = null;
let port = null;
const waitForServer = async () => {
  const statePath = path.join(dataDir, "live-server.json");
  for (let i = 0; i < 80; i++) {
    await sleep(120);
    try {
      const st = JSON.parse(fs.readFileSync(statePath, "utf8"));
      if (st.token === TOKEN && st.port) {
        port = st.port;
        pageUrl = st.pageUrl;
        const ok = await new Promise((resolve) => {
          const req = http.get(pageUrl + "api/ping", (res) => { res.resume(); resolve(res.statusCode === 200); });
          req.on("error", () => resolve(false));
          req.setTimeout(500, () => { req.destroy(); resolve(false); });
        });
        if (ok) return true;
      }
    } catch { /* 还没写状态文件 */ }
  }
  return false;
};

const shutdown = async () => {
  try {
    await new Promise((resolve) => {
      const req = http.request(pageUrl + "api/shutdown", { method: "POST" }, (res) => { res.resume(); resolve(); });
      req.on("error", () => resolve());
      req.setTimeout(800, () => { req.destroy(); resolve(); });
      req.end();
    });
  } catch { /* ignore */ }
  try { serve.kill(); } catch { /* ignore */ }
};

if (!(await waitForServer())) {
  await shutdown();
  bail(2, "本地实时服务未就绪（可能是环境问题，非产品缺陷）");
}

const page = await openPage({ width: 1280, height: 1400 });
if (!page) { await shutdown(); bail(2, "无法启动浏览器页面"); }

const STATE = `(() => {
  const tabs = [...document.querySelectorAll('[data-tab]')]
    .filter((t) => t.getAttribute('aria-selected') === 'true').map((t) => t.getAttribute('data-tab'));
  const scope = [...document.querySelectorAll('[data-scope]')];
  return {
    activeTabs: tabs,
    pressed: scope.filter((b) => b.getAttribute('aria-pressed') === 'true')
      .map((b) => b.getAttribute('data-scope')),
    scopeCount: scope.length,
    hasRefresh: !!document.getElementById('regen'),
    hasAnyReset: !!document.getElementById('reset-pin')
      || (document.body.textContent || '').indexOf('重置定格') >= 0,
    search: location.search,
  };
})()`;

const click = async (selector) => {
  const box = JSON.parse(await page.eval(
    `(() => { const el = document.querySelector(${JSON.stringify(selector)}); if (!el) return "null";
      el.scrollIntoView({ block: 'center' }); const r = el.getBoundingClientRect();
      return JSON.stringify({ x: r.left + r.width / 2, y: r.top + r.height / 2 }); })()`));
  if (!box) return false;
  await page.click(box.x, box.y);
  await sleep(250);
  return true;
};

// ---------- 初始：会话用量档 ----------
console.log("\n-- 用量范围两档（会话用量 / 全局用量）--");
await page.goto(`${pageUrl}?session=latest&days=30&kind=all&tab=scopeT1&mode=session`, 1000);
let s = await page.eval(STATE);
check("mode=session 时落在「上下文使用量」视图", s.activeTabs.join(",") === "context", `(${s.activeTabs})`);
check("开关显示「会话用量」被选中", s.pressed.join(",") === "session", `(${s.pressed})`);
check("页面上恰好两个档位按钮", s.scopeCount === 2, `(${s.scopeCount})`);
check("页面只留刷新与切换：不再有「重置定格」入口", s.hasAnyReset === false, `(hasAnyReset=${s.hasAnyReset})`);
check("刷新按钮仍在", s.hasRefresh === true);
check("地址栏已写出被定格的会话 id", /[?&]session=(?!latest)[^&]+/.test(s.search), `(${s.search})`);
const pinned = (s.search.match(/[?&]session=([^&]+)/) || [])[1] || "";

// ---------- 切到全局用量 ----------
await click('[data-scope="global"]');
s = await page.eval(STATE);
check("切「全局用量」后落在「总体使用量」视图", s.activeTabs.join(",") === "token", `(${s.activeTabs})`);
check("开关改显「全局用量」被选中", s.pressed.join(",") === "global", `(${s.pressed})`);
check("URL 的 mode 变为 global（刷新可保持）", /[?&]mode=global/.test(s.search), `(${s.search})`);
check("切档位不会把地址栏里的定格会话打回 latest（pin 保留）",
  new RegExp(`[?&]session=${pinned.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`).test(s.search),
  `(期望 session=${pinned}，实得 ${s.search})`);

// ---------- 切回会话用量 ----------
await click('[data-scope="session"]');
s = await page.eval(STATE);
check("切回「会话用量」后回到「上下文使用量」视图", s.activeTabs.join(",") === "context", `(${s.activeTabs})`);
check("URL 的 mode 变回 session", /[?&]mode=session/.test(s.search), `(${s.search})`);

// ---------- 刷新按钮与开关并存、互不干扰 ----------
check("切换后开关仍处于可用状态（未被重建丢失）", s.scopeCount === 2 && s.pressed.length === 1,
  `(count=${s.scopeCount} pressed=${s.pressed})`);

await page.close();
await shutdown();
try { fs.rmSync(work, { recursive: true, force: true }); } catch { /* ignore */ }

console.log(failed === 0 ? "\nSCOPE SWITCH OK" : `\nSCOPE SWITCH FAILED: ${failed}`);
process.exit(failed === 0 ? 0 : 1);
