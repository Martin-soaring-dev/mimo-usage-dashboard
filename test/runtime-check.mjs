// test/runtime-check.mjs — 抽取 usage_cli.py 的 _RUNTIME JS 并真正执行 buildBody，
// 捕捉语法错误 / 运行时异常（Python 测试只查字符串，执行不了 JS）。
// 用法: node test/runtime-check.mjs
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const py = fs.readFileSync(path.join(repoRoot, "usage_cli.py"), "utf8");
const m = py.match(/_RUNTIME = """([\s\S]*?)"""/);
if (!m) { console.error("  FAIL  未找到 _RUNTIME"); process.exit(1); }

const rootElement = {
  clientWidth: 960,
  innerHTML: "",
  addEventListener() {},
  querySelectorAll() { return []; },
};
const panelElement = {
  innerHTML: "",
  setAttribute() {},
};
const sandbox = {
  document: {
    getElementById: (id) => id === "tab-content" ? panelElement : rootElement,
    createElement: () => ({ style: {}, appendChild() {}, classList: { add() {} } }),
  },
  navigator: {},
  console,
};
vm.createContext(sandbox);

let failures = 0;
const check = (name, ok, extra = "") => {
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${extra ? "  " + extra : ""}`);
  if (!ok) failures++;
};

try {
  vm.runInContext(m[1], sandbox, { filename: "_RUNTIME.js" });
  check("_RUNTIME 语法与求值", true);
} catch (e) {
  check("_RUNTIME 语法与求值", false, e.message);
  process.exit(1);
}

const cases = [
  ["all", { kind: "all", token: { totals: { tokens: 100, input: 10, output: 5, reasoning: 2, cacheRead: 80, requests: 3, tools: 4, sessions: 2, cacheShare: .89 }, models: [{ name: "m", providerID: "p", tokens: 100, read: 80, input: 10, requests: 3, tools: 4, share: 1 }], daily: [{ date: "2026-10-10", tokens: 100 }], recent: [{ date: "2026-10-10", tokens: 100 }], days: 30 }, session: { top: [], totals: {}, total: 1, changes: 0, files: 0 }, context: { ok: false, error: "x" } }],
  ["token", { kind: "token", token: { totals: { tokens: 1, input: 1, cacheRead: 1, requests: 1, tools: 0, sessions: 1, cacheShare: .5 }, models: [], daily: [], recent: [], days: 7 } }],
  ["空数据 all", { kind: "all", token: { totals: {}, models: [], daily: [], recent: [], days: 30 }, session: { top: [], totals: {}, total: 0, changes: 0, files: 0 }, context: { ok: false } }],
];

for (const [name, data] of cases) {
  try {
    const out = vm.runInContext(`buildBody(${JSON.stringify(data)})`, sandbox);
    check(`buildBody(${name})`, typeof out === "string" && out.length > 0, `(${out.length} chars)`);
    if (name !== "空数据 all") {
      check(`buildBody(${name}) 含热力图`, out.includes("Token 热力图"));
      check(`buildBody(${name}) 含模型块`, out.includes("按模型的调用情况"));
      if (name === "all") {
        check("all 页包含三种 Tab", out.includes("总体使用量") && out.includes("会话使用量") && out.includes("上下文使用量"));
        check("all 页初始展示总体使用量", out.includes('aria-selected="true"') && out.includes('aria-labelledby="tab-token"'));
      }
    }
  } catch (e) {
    check(`buildBody(${name})`, false, e.message);
  }
}

// ---- 回归：Token 视图的「会话数」必须与 Session 视图的「会话总数」一致 ----
// 之前 token.totals 里没有 sessions 字段，tokenCard 读 undefined -> fmtInt 渲染成 0。
console.log("\n=== 会话数口径（回归） ===");
const tokTotals = { tokens: 100, input: 10, output: 5, reasoning: 2, cacheRead: 80,
                    cacheWrite: 0, requests: 3, tools: 4, cacheShare: .89, sessions: 3 };
const tokenCase = { kind: "token",
                    token: { totals: tokTotals, models: [], daily: [], recent: [], days: 30 } };
const sessionCase = { kind: "session",
                      session: { total: 3, top: [], totals: { tokens: 100, requests: 3, tools: 4 },
                                 changes: 0, files: 0 } };
const numOf = (html, label) => {
  const m = html.match(new RegExp('<div class="num">([^<]*)</div><div class="lb">' + label + '</div>'));
  return m ? m[1] : null;
};
try {
  const tokenHtml = vm.runInContext(`buildBody(${JSON.stringify(tokenCase)})`, sandbox);
  const sessionHtml = vm.runInContext(`buildBody(${JSON.stringify(sessionCase)})`, sandbox);
  const tokenCount = numOf(tokenHtml, "会话数");
  const sessionCount = numOf(sessionHtml, "会话总数");
  check("Token 视图「会话数」渲染为 3（此前恒为 0）", tokenCount === "3", `(${tokenCount})`);
  check("Token 会话数 == Session 会话总数", tokenCount === sessionCount,
        `(${tokenCount} vs ${sessionCount})`);
  // 缺失该字段时仍应退化为 0 而不是报错（向后兼容旧 DATA）
  const legacy = { kind: "token",
                   token: { totals: { ...tokTotals, sessions: undefined }, models: [], daily: [],
                            recent: [], days: 30 } };
  const legacyHtml = vm.runInContext(`buildBody(${JSON.stringify(legacy)})`, sandbox);
  check("旧 DATA 缺 sessions 字段时不抛异常（渲染为 0）",
        numOf(legacyHtml, "会话数") === "0", `(${numOf(legacyHtml, "会话数")})`);
} catch (e) {
  check("会话数口径渲染", false, e.message);
}

console.log(`\n${failures === 0 ? "RUNTIME CHECK OK" : "RUNTIME CHECK FAILED: " + failures}`);
process.exit(failures === 0 ? 0 : 1);
