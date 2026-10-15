// test/refresh-e2e.mjs — 真实浏览器 + 真实 loopback 服务 的页内刷新端到端
//
//   node test/refresh-e2e.mjs                 # 本机有 Chrome 时执行
//   node test/refresh-e2e.mjs --require-chrome # 无浏览器时必须失败（CI 可选）
//
// 退出码与 ui-geometry.mjs 相同：
//   0 = PASS / 1 = FAIL / 2 = SKIP（未加 --require-chrome 且无浏览器）
//
// 证明的产品行为（不是「HTTP 200」就过关）：
//   1. 打开实时页后，DOM 里的会话总数等指标来自内联初值
//   2. 临时库数据变化后，点击「刷新数据」→ 指标变成新值（不只是时间戳）
//   3. 服务停止后再点刷新 → 保留旧数据 + 错误条
//   4. 快速连点不会把界面搞乱
//
// 绝不触碰真实 mimocode.db / mimocode.jsonc：库与 MIMO_USAGE_DATA_DIR 都在临时目录。

import { spawn, execFileSync } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { findChrome, openPage, sleep } from "./cdp.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const SCRIPT = path.join(ROOT, "usage_cli.py");
const PY = process.env.MIMO_USAGE_PY || process.env.PYTHON
  || (process.platform === "win32" ? "python" : "python3");
const REQUIRE_CHROME = process.argv.includes("--require-chrome");

let failed = 0;
const check = (name, ok, extra = "") => {
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${extra ? "  " + extra : ""}`);
  if (!ok) failed++;
};

const bail = (code, reason) => {
  try { fs.rmSync(work, { recursive: true, force: true }); } catch { /* ignore */ }
  console.log(code === 2 ? `\n  SKIP  ${reason}` : `\n  FAIL  ${reason}`);
  console.log(code === 2 ? "\nREFRESH E2E SKIPPED" : "\nREFRESH E2E FAILED");
  process.exit(code);
};

if (!findChrome()) {
  if (REQUIRE_CHROME) bail(1, "--require-chrome：未找到 Chrome/Edge");
  bail(2, "未找到可用 Chrome/Edge（可用 CHROME_PATH 指定）");
}

// ---- 临时环境 ----
const work = fs.mkdtempSync(path.join(os.tmpdir(), "mimo-refresh-e2e-"));
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

const TOKEN = "e2e-refresh-token-01";
const serve = spawn(PY, [
  SCRIPT, "serve", "--db", dbFile, "--token", TOKEN, "--idle-minutes", "10",
], { stdio: ["ignore", "pipe", "pipe"], env: process.env });

let pageUrl = null;
let reportUrl = null;
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
        reportUrl = st.reportUrl;
        // 真正 ping 通才算就绪
        const ok = await new Promise((resolve) => {
          const req = http.get(reportUrl, (res) => { res.resume(); resolve(res.statusCode === 200); });
          req.on("error", () => resolve(false));
          req.setTimeout(500, () => { req.destroy(); resolve(false); });
        });
        if (ok) return true;
      }
    } catch { /* not ready yet */ }
  }
  return false;
};

const postJson = (url) => new Promise((resolve, reject) => {
  const u = new URL(url);
  const req = http.request({
    hostname: u.hostname, port: u.port, path: u.pathname, method: "POST",
    headers: { "Content-Length": 0 },
  }, (res) => { res.resume(); resolve(res.statusCode); });
  req.on("error", reject);
  req.end();
});

const addSession = (id, title) => {
  const py = `
import sqlite3
c = sqlite3.connect(${JSON.stringify(dbFile)})
c.execute(
  "INSERT INTO session (id,title,directory,time_created,time_updated,"
  "summary_additions,summary_deletions,summary_files) VALUES (?,?,?,?,?,?,?,?)",
  (${JSON.stringify(id)}, ${JSON.stringify(title)}, "D:/proj", 1, 2, 1, 1, 1),
)
c.commit(); c.close()
`;
  execFileSync(PY, ["-c", py], { stdio: ["ignore", "pipe", "pipe"] });
};

let page = null;
try {
  if (!(await waitForServer())) {
    serve.kill();
    bail(REQUIRE_CHROME ? 1 : 2, "serve 子进程未能就绪（超时）");
  }
  console.log(`\n=== 实时刷新 e2e（${pageUrl}）===`);

  try {
    page = await openPage({ width: 1280, height: 1600 });
  } catch (err) {
    serve.kill();
    bail(REQUIRE_CHROME ? 1 : 2, `无法启动浏览器（${err && err.message}）`);
  }
  if (!page) { serve.kill(); bail(2, "无法启动浏览器"); }

  await page.goto(pageUrl, 900);
  const boot = await page.eval(`(() => {
    const s = (typeof __usageSnapshot === "function") ? __usageSnapshot() : null;
    const btn = document.getElementById("regen") || {};
    return {
      hasSnap: !!s,
      live: !!(s && s.live && s.live.reportUrl),
      btn: btn.textContent || "",
      sessions: (s && s.data && s.data.session) ? s.data.session.total : -1,
      tokens: (s && s.data && s.data.token) ? s.data.token.totals.tokens : -1,
      gen: (s && s.data) ? s.data.generatedAt : -1,
    };
  })()`);
  check("实时页已加载且 LIVE 配置存在", boot.live === true, JSON.stringify(boot));
  check("按钮为「刷新数据」", /刷新数据/.test(boot.btn), boot.btn);
  check("初值来自内联 DATA（会话总数 >0）", boot.sessions > 0, `sessions=${boot.sessions}`);

  // ---- 改库后点击刷新：指标必须变，不只是时间戳 ----
  addSession("s-e2e-refresh", "E2E 刷新会话");
  const genBefore = boot.gen;
  const clickResult = await page.eval(`(async () => {
    const btn = document.getElementById("regen");
    if (!btn || typeof __usageRefresh !== "function" || typeof __usageSnapshot !== "function") {
      return { ok: false, why: "missing-hooks" };
    }
    const ok = await __usageRefresh();
    const snap = __usageSnapshot();
    const banner = document.getElementById("refresh-banner");
    return {
      ok: ok === true,
      sessions: snap.data.session.total,
      tokens: snap.data.token.totals.tokens,
      gen: snap.data.generatedAt,
      btnText: btn.textContent,
      bannerHidden: banner ? banner.hidden : null,
      bannerText: banner ? banner.textContent : "",
      lastOk: (document.getElementById("last-ok") || { textContent: "" }).textContent,
    };
  })()`);
  check("点击刷新后会话总数增加（新数据，不只是时间戳）",
    clickResult.ok && clickResult.sessions >= boot.sessions + 1,
    `(${boot.sessions} → ${clickResult.sessions})`);
  check("刷新后 generatedAt 更新", clickResult.gen > genBefore,
    `(${genBefore} → ${clickResult.gen})`);
  check("刷新后显示最近成功更新", /最近成功更新/.test(clickResult.lastOk || ""),
    clickResult.lastOk);

  // ---- 服务停止后刷新：保留旧数据 + 错误条 ----
  const sessionsKept = clickResult.sessions;
  await postJson(reportUrl.replace("/api/report", "/api/shutdown"));
  await sleep(400);
  const offline = await page.eval(`(async () => {
    const ok = await (typeof __usageRefresh === "function" ? __usageRefresh() : Promise.resolve(false));
    const banner = document.getElementById("refresh-banner");
    const snap = (typeof __usageSnapshot === "function") ? __usageSnapshot() : null;
    return {
      refreshReturned: ok === true,
      sessions: snap && snap.data ? snap.data.session.total : -1,
      bannerHidden: banner ? banner.hidden : null,
      bannerText: banner ? banner.textContent : "",
      tokens: snap && snap.data ? snap.data.token.totals.tokens : -1,
    };
  })()`);
  check("服务离线后刷新返回失败", offline.refreshReturned === false, JSON.stringify(offline));
  check("服务离线后刷新失败时保留上次数据",
    offline.sessions === sessionsKept, `(${sessionsKept} → ${offline.sessions})`);
  check("服务离线后展示可理解错误条",
    offline.bannerHidden === false && /刷新失败|无法连接|超时|服务/.test(offline.bannerText || ""),
    offline.bannerText);

  // ---- 快速连点（服务已离线，仍不应炸）----
  const double = await page.eval(`(async () => {
    const btn = document.getElementById("regen");
    const p1 = typeof __usageRefresh === "function" ? __usageRefresh() : Promise.resolve(false);
    const p2 = typeof __usageRefresh === "function" ? __usageRefresh() : Promise.resolve(false);
    const p3 = typeof __usageRefresh === "function" ? __usageRefresh() : Promise.resolve(false);
    await Promise.all([p1, p2, p3]);
    const snap = (typeof __usageSnapshot === "function") ? __usageSnapshot() : null;
    return {
      sessions: snap && snap.data ? snap.data.session.total : -1,
      btnText: btn.textContent,
      disabled: btn.disabled,
    };
  })()`);
  check("快速连点后数据仍保留", double.sessions === sessionsKept, `(${double.sessions})`);
  check("快速连点后按钮恢复可用文案", /刷新数据/.test(double.btnText || ""), double.btnText);

  // ---- 服务恢复后可再次刷新成功（同 token + 同端口重启）----
  // 页面内嵌的 LIVE.reportUrl 在生成时已固定；若换端口则浏览器仍指向旧地址。
  serve.kill();
  await sleep(200);
  addSession("s-e2e-recover", "恢复后刷新会话");
  const serve2 = spawn(PY, [
    SCRIPT, "serve", "--db", dbFile, "--token", TOKEN, "--idle-minutes", "10",
    "--port", String(port),
  ], { stdio: ["ignore", "pipe", "pipe"], env: process.env });
  try {
    if (!(await waitForServer())) {
      serve2.kill();
      bail(REQUIRE_CHROME ? 1 : 2, "恢复后的 serve 未能就绪（超时）");
    }
    check("恢复后仍监听同一端口并复用同一 token",
      port === Number(new URL(pageUrl).port) && stTokenMatches(TOKEN),
      `(port=${port}, pageUrl=${pageUrl})`);
    const recovered = await page.eval(`(async () => {
      const ok = await (typeof __usageRefresh === "function" ? __usageRefresh() : Promise.resolve(false));
      const snap = (typeof __usageSnapshot === "function") ? __usageSnapshot() : null;
      const banner = document.getElementById("refresh-banner");
      return {
        ok: ok === true,
        sessions: snap && snap.data ? snap.data.session.total : -1,
        bannerHidden: banner ? banner.hidden : null,
        bannerText: banner ? banner.textContent : "",
      };
    })()`);
    check("服务恢复后刷新成功", recovered.ok === true, JSON.stringify(recovered));
    check("恢复后读到新库数据（会话数再次增加）",
      recovered.sessions >= sessionsKept + 1,
      `(${sessionsKept} → ${recovered.sessions})`);
    check("恢复成功后错误条消失", recovered.bannerHidden === true || recovered.bannerText === "",
      recovered.bannerText);
  } finally {
    try { serve2.kill(); } catch { /* ignore */ }
  }

  check("页面无未捕获异常",
    (page.pageErrors || []).filter((e) => !/Failed to load resource|net::ERR/i.test(e)).length === 0,
    (page.pageErrors || []).slice(0, 3).join(" | "));
} finally {
  try { if (page) await page.close(); } catch { /* ignore */ }
  try { serve.kill(); } catch { /* ignore */ }
  // 子进程可能已自己退出；再试 --shutdown 清理残留
  try {
    execFileSync(PY, [SCRIPT, "--shutdown"], {
      env: process.env, stdio: "ignore", timeout: 5000,
    });
  } catch { /* ignore */ }
  try { fs.rmSync(work, { recursive: true, force: true }); } catch { /* ignore */ }
}

function stTokenMatches(token) {
  try {
    const st = JSON.parse(fs.readFileSync(path.join(dataDir, "live-server.json"), "utf8"));
    return st.token === token;
  } catch {
    return false;
  }
}

console.log(`\n${failed === 0 ? "REFRESH E2E OK" : "REFRESH E2E FAILED: " + failed}`);
process.exit(failed === 0 ? 0 : 1);
