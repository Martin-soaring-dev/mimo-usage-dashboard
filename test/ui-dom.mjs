// test/ui-dom.mjs — 内联 JS 运行时的行为回归（Node VM，无需浏览器）
//
//   node test/ui-dom.mjs
//
// 与 test/ui-geometry.mjs 的分工（必须分清，不能互相冒充）：
//   * 本文件 = **DOM 行为测试**：在 vm 沙箱里真实执行 _RUNTIME，断言渲染出来的结构、
//     数字口径、缺失/异常字段安全性、以及展开折叠与悬浮提示的处理逻辑。
//     它 **没有排版引擎**，所以不能、也不声称验证任何几何对齐或溢出。
//   * test/ui-geometry.mjs = **真实浏览器几何验证**（无头 Chrome + CDP）。
//
// 覆盖：
//   UI-1 提示字段完整 / 缺失值安全 / 12 个月结构 / 无数据日 / 提示层显隐逻辑
//   UI-2 星期标签与日期格共用同一网格（结构层面；像素级对齐见 ui-geometry）
//   UI-3 三窗口数据齐备且结构一致 / 切换刷新 / 旧 payload 降级 / 不触发网络
//   UI-4 每模型一条 bar / 条长 = share / 浅色段口径 / 0 值、缺失、浮点边界、舍入
//   分类树 分支渲染 / 子项与合计核对 / 空白与异常 / 展开折叠与键盘处理
//   全局 空数据与异常 payload 不抛异常、不渲染 NaN/undefined

import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const py = fs.readFileSync(path.join(ROOT, "usage_cli.py"), "utf8");
const m = py.match(/_RUNTIME = """([\s\S]*?)"""/);
if (!m) { console.error("  FAIL  未找到 _RUNTIME"); process.exit(1); }
const RUNTIME = m[1];

let failed = 0;
const check = (name, ok, extra = "") => {
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${extra ? "  " + extra : ""}`);
  if (!ok) failed++;
};
const section = (t) => console.log(`\n=== ${t} ===`);

// ---------------------------------------------------------------------------
// 极简 DOM 替身：只实现 _RUNTIME 实际用到的接口
// ---------------------------------------------------------------------------
function fakeEl(tag) {
  const el = {
    tagName: String(tag).toUpperCase(),
    style: {},
    attrs: {},
    hidden: undefined,
    textContent: "",
    offsetWidth: 200,
    offsetHeight: 24,
    _listeners: {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    appendChild(c) { this.child = c; return c; },
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    querySelector(sel) { return sel === ".tw" ? this._marker || null : null; },
    querySelectorAll() { return []; },
    removeAttribute(k) { delete this.attrs[k]; },
    getBoundingClientRect() { return { left: 10, top: 20, right: 34, bottom: 44, width: 24, height: 24 }; },
    focus() { this.focused = true; },
    closest() { return null; },
    contains() { return false; },
    fire(type, ev) { (this._listeners[type] || []).forEach((fn) => fn(ev)); },
  };
  return el;
}

function makeVm({ byId = {}, body = null } = {}) {
  const sandbox = {
    document: {
      body: body || fakeEl("body"),
      getElementById: (id) => byId[id] || null,
      createElement: (tag) => fakeEl(tag),
      querySelector: () => null,
      querySelectorAll: () => [],
    },
    navigator: {},
    console,
    window: { innerWidth: 1200, innerHeight: 900 },
  };
  vm.createContext(sandbox);
  vm.runInContext(RUNTIME, sandbox, { filename: "_RUNTIME.js" });
  return sandbox;
}

const call = (sandbox, expr) => vm.runInContext(expr, sandbox);
const build = (sandbox, data) => call(sandbox, `buildBody(${JSON.stringify(data)})`);
// win 默认显式传 null：生产代码里 _WIN 是「粘性」的（切换后保持），测试必须显式指定，
// 否则断言会依赖同一沙箱里上一次 render 留下的状态。粘性行为另有专门用例覆盖。
const render = (sandbox, kind, data, win = null) =>
  call(sandbox, `renderTab(${JSON.stringify(kind)}, ${JSON.stringify(data)}, ${JSON.stringify(win)})`);

// ---- HTML 片段小工具（buildBody 的返回值里不含 _RUNTIME 源码，正则安全） ----
const tagsOf = (html, cls) => [...html.matchAll(new RegExp('<div class="' + cls + '"[^>]*>', "g"))].map((x) => x[0]);
const textsOf = (html, cls) =>
  [...html.matchAll(new RegExp('<div class="' + cls + '"[^>]*>([^<]*)<', "g"))].map((x) => x[1]);
const attrOf = (tag, name) => {
  const mm = tag.match(new RegExp(name + '="([^"]*)"'));
  return mm ? mm[1] : null;
};
const stripTags = (s) => String(s).replace(/<[^>]*>/g, "");

// ---------------------------------------------------------------------------
// 测试数据
// ---------------------------------------------------------------------------
const daily = (n, opts = {}) => {
  const out = [];
  const today = new Date(); today.setHours(0, 0, 0, 0);
  for (let i = n - 1; i >= 0; i--) {
    const d = new Date(today); d.setDate(d.getDate() - i);
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    const key = `${d.getFullYear()}-${mm}-${dd}`;
    const rec = { date: key, tokens: (i % 7 === 3) ? 0 : (i + 1) * 1000 };
    if (opts.rich) { rec.read = (i + 1) * 900; rec.input = (i + 1) * 100; rec.requests = i + 1; }
    if (!opts.skipZero) out.push(rec);
    else if (rec.tokens > 0) out.push(rec);
  }
  return out;
};

const windowOf = (days, tokens, opts = {}) => ({
  days,
  fromDate: "2026-01-01",
  modelTotal: opts.modelTotal !== undefined ? opts.modelTotal : tokens,
  modelCount: opts.modelCount !== undefined ? opts.modelCount : 1,
  totals: {
    tokens, input: Math.round(tokens * 0.2), output: Math.round(tokens * 0.1),
    reasoning: Math.round(tokens * 0.05), cacheRead: Math.round(tokens * 0.6),
    cacheWrite: 0, requests: Math.max(1, Math.round(tokens / 1000)),
    tools: 3, cacheShare: 0.75, sessions: 2,
  },
  models: opts.models || [
    { name: "alpha", providerID: "p", tokens, read: Math.round(tokens * 0.9),
      input: Math.round(tokens * 0.1), requests: 3, tools: 1, share: 1 },
  ],
  recent: Array.from({ length: days }, (_, i) => ({
    date: "2026-01-" + String((i % 28) + 1).padStart(2, "0"), tokens: 100 + i,
  })),
});

const tokenPayload = (days = 30) => ({
  kind: "token",
  token: {
    ...windowOf(days, days * 1000),
    windowKeys: ["1", "7", "30"],
    windows: {
      "1": windowOf(1, 1000),
      "7": windowOf(7, 7000),
      "30": windowOf(30, 30000),
    },
    daily: daily(367, { rich: true, skipZero: true }),
  },
});

// ===========================================================================
section("UI-1 热力图：结构 / 提示字段 / 缺失值安全");
// ===========================================================================
{
  const sb = makeVm();
  const html = render(sb, "token", tokenPayload());
  const cells = tagsOf(html, "cell");
  check("渲染出 12 个月的热力图格子（≥365）", cells.length >= 365, `(${cells.length})`);

  const months = textsOf(html, "mcell");
  check("月份标签唯一值 = 12（12 个月完整）", new Set(months).size === 12,
    `(${new Set(months).size}: ${months.join(",")})`);

  const grid = html.match(/<div class="hm-grid" style="([^"]*)"/);
  check("网格列数 = 1(星期列) + 列数，行模板固定 7 行",
    !!grid && /grid-template-columns:14px repeat\(\d+,minmax\(0,1fr\)\)/.test(grid[1])
    && /grid-template-rows:auto repeat\(7,auto\)/.test(grid[1]), grid ? `(${grid[1]})` : "(无 hm-grid)");

  check("每个格子都有 data-tip", cells.every((c) => (attrOf(c, "data-tip") || "").length > 0));
  check("每个格子都有 aria-label（可访问性）",
    cells.every((c) => attrOf(c, "role") === "img" && (attrOf(c, "aria-label") || "").length > 0));
  check("每个格子都有 data-date 与 data-row(0..6)",
    cells.every((c) => /^\d{4}-\d{2}-\d{2}$/.test(attrOf(c, "data-date") || "")
      && Number(attrOf(c, "data-row")) >= 0 && Number(attrOf(c, "data-row")) <= 6));
  const tips = cells.map((c) => attrOf(c, "data-tip"));
  check("提示一律含 日期 + 星期 + token + 缓存命中率",
    tips.every((t) => /\d{4}-\d{2}-\d{2}/.test(t) && /周[日一二三四五六]/.test(t)
      && /(无调用|tokens)/.test(t) && /缓存命中率/.test(t)));
  const withRate = tips.filter((t) => /缓存命中率 \d/.test(t));
  check("有 read/input 的日子显示真实缓存命中率", withRate.length > 300, `(${withRate.length} 天)`);

  const zeros = cells.filter((c) => attrOf(c, "data-lv") === "0");
  check("无数据的日期渲染为 0 档且提示为「无调用」",
    zeros.length > 0 && zeros.every((c) => /无调用/.test(attrOf(c, "data-tip"))),
    `(${zeros.length} 个空格)`);

  const dow = tagsOf(html, "dlbl");
  const dowText = textsOf(html, "dlbl");
  check("星期标签只有 一/三/五 三个", dow.length === 3 && dowText.join("") === "一三五",
    `(${dowText.join(",")})`);
  check("星期标签落在与日期行相同的网格行上（data-dow/星期序号 + grid-row 一致）",
    dow.every((t) => {
      const d = Number(attrOf(t, "data-dow"));
      const r = Number((t.match(/grid-row:(\d+)/) || [])[1]);
      return [1, 3, 5].includes(d) && r === d + 2;
    }), `(${dow.map((t) => attrOf(t, "data-dow") + "->" + (t.match(/grid-row:(\d+)/) || [])[1]).join(", ")})`);
  check("日期格与标签共用同一网格容器（不再有独立的 dow flex 列）",
    !html.includes('class="dow"') && html.includes('class="hm-grid"'));
}

{
  // 旧 payload：daily 只有 {date, tokens}
  const sb = makeVm();
  const legacy = { kind: "token", token: { ...windowOf(30, 30000), daily: daily(400, { skipZero: true }) } };
  let html = "";
  let threw = null;
  try { html = render(sb, "token", legacy); } catch (e) { threw = e; }
  check("旧 payload（daily 只有 date/tokens）不抛异常", !threw, threw ? `(${threw.message})` : "");
  const tips = tagsOf(html, "cell").map((c) => attrOf(c, "data-tip"));
  check("缺 read/input 时缓存命中率安全显示为「—」且字段仍在",
    tips.length > 0 && tips.every((t) => /缓存命中率 —/.test(t) && !/\d+%/.test(t.split("·")[2] || "")));
}

{
  // 空数据 / 零值 / 缺失字段
  const sb = makeVm();
  const cases = {
    "daily 为空数组": { kind: "token", token: { totals: {}, models: [], daily: [], recent: [], days: 30 } },
    "daily 字段全缺": { kind: "token", token: { totals: {}, models: [], daily: [{}, { date: null }, { date: "bad" }], recent: [] } },
    "tokens 为 null/字符串/负数": { kind: "token", token: { totals: {}, models: [], recent: [], daily: [{ date: "2026-10-12", tokens: null }, { date: "2026-10-13", tokens: "abc" }, { date: "2026-10-14", tokens: -5 }] } },
    "极大数值": { kind: "token", token: { totals: { tokens: 1e15 }, models: [], recent: [], daily: [{ date: "2026-10-13", tokens: 1e15, read: 1e15, input: 1e15 }] } },
    "token 段整个缺失": { kind: "token" },
  };
  for (const [name, data] of Object.entries(cases)) {
    let html = "", err = null;
    try { html = render(sb, "token", data); } catch (e) { err = e; }
    check(`空/异常 payload 不抛异常：${name}`, !err && typeof html === "string" && html.length > 0,
      err ? `(${err.message})` : "");
    if (html) {
      check(`  且不渲染 NaN/undefined：${name}`, !/\bNaN\b/.test(html) && !/>undefined</.test(html));
      check(`  且 12 个月骨架仍完整：${name}`, tagsOf(html, "cell").length >= 365);
    }
  }
  const huge = render(sb, "token", cases["极大数值"]);
  check("极大数值用 T 单位显示（不出现长数字串）", /1\.00T|1,000\.00T|1000\.00T/.test(huge),
    `(${(huge.match(/data-tip="([^"]*)"/) || [])[1]})`);
}

{
  // 提示层的显隐逻辑（真实事件对象 + 极简 DOM 替身）
  const body = fakeEl("body");
  const sb = makeVm({ body });
  const cell = fakeEl("div");
  cell.attrs["data-tip"] = "2026-10-13 周二 · 1.0K tokens · 缓存命中率 90.0%";
  cell.closest = (sel) => (sel === ".hm .cell" ? cell : null);
  const other = fakeEl("div");
  other.closest = () => null;
  const host = fakeEl("div");
  sb.__host = host; sb.__cell = cell; sb.__other = other;
  check("wireHeatmap 挂载成功", call(sb, "wireHeatmap(__host)") === true);
  host.fire("mouseover", { target: cell });
  const tip = body.child;
  check("mouseover 显示提示层并写入 data-tip 文本",
    !!tip && tip.hidden === false && tip.textContent === cell.attrs["data-tip"],
    tip ? `(hidden=${tip.hidden} text=${JSON.stringify(tip.textContent)})` : "(未创建提示层)");
  check("提示层带 role=tooltip、pointer-events:none、position:fixed",
    !!tip && tip.attrs.role === "tooltip"
    && /position:fixed/.test(py) && /pointer-events:none/.test(py));
  host.fire("mouseout", { target: cell });
  check("mouseout 隐藏提示层", tip.hidden === true);
  host.fire("mouseover", { target: other });
  check("悬停非格子元素不会显示提示层", tip.hidden === true);
  check("wireHeatmap 对无 DOM 能力的宿主安全返回 false", call(sb, "wireHeatmap(null)") === false);
}

// ===========================================================================
section("UI-3 时间尺度：三窗口 / 切换刷新 / 兼容性 / 无网络");
// ===========================================================================
{
  const sb = makeVm();
  const payload = tokenPayload(30);
  const html = render(sb, "token", payload);
  const wins = tagsOf(html, "tscale").length ? [...html.matchAll(/data-win="([^"]*)"/g)].map((x) => x[1]) : [];
  check("存在 今日 / 7 天 / 30 天 三段（key=1,7,30）", wins.join(",") === "1,7,30", `(${wins.join(",")})`);
  const pressed = [...html.matchAll(/data-win="([^"]*)" aria-pressed="([^"]*)"/g)]
    .map((x) => ({ k: x[1], p: x[2] }));
  check("默认选中 --days 对应的窗口（30）",
    pressed.filter((x) => x.p === "true").length === 1 && pressed.find((x) => x.p === "true").k === "30",
    JSON.stringify(pressed));
  check("分段控件带可访问标签与状态", html.includes('role="group" aria-label="时间尺度"') && html.includes("aria-pressed"));
  check("hero 文案为「最近 30 天 · tokens 消耗」（回归基线）", html.includes("最近 30 天 · tokens 消耗"));
  check("口径说明写明会话数/热力图不随窗口变化",
    /会话数<\/b>为全部会话总数/.test(html) && /均不随时间尺度变化/.test(html));
  check("会话数卡片带全量口径标注", /title="全部会话总数，不随时间尺度变化"/.test(html));

  const hero = (w) => {
    const h = render(sb, "token", payload, w);
    return {
      label: (h.match(/<div class="label">([^<]*)<\/div>/) || [])[1],
      num: (h.match(/<div class="big">([^<]*)</) || [])[1],
      spark: (h.match(/<div class="spark">([\s\S]*?)<\/div>/) || [, ""])[1].split("<i ").length - 1,
      cards: [...h.matchAll(/<div class="num">([^<]*)<\/div><div class="lb">/g)].map((x) => x[1]).join("|"),
    };
  };
  const h1 = hero("1"), h7 = hero("7"), h30 = hero("30"), hDefault = hero(null);
  check("今日：文案标明自然日 00:00 起", /今日（本地 00:00 起）/.test(h1.label), `(${h1.label})`);
  check("三窗口 hero 数字互不相同", new Set([h1.num, h7.num, h30.num]).size === 3,
    `(今日=${h1.num} 7天=${h7.num} 30天=${h30.num})`);
  check("8 张统计卡随窗口刷新", new Set([h1.cards, h7.cards, h30.cards]).size === 3);
  check("趋势图随窗口刷新（今日 1 根 / 7 天 7 根）", h1.spark === 1 && h7.spark === 7,
    `(今日=${h1.spark} 7天=${h7.spark} 30天=${h30.spark})`);
  check("默认渲染 == 30 天窗口（逐值一致，回归基线）",
    hDefault.label === h30.label && hDefault.num === h30.num && hDefault.cards === h30.cards);
  check("今日无数据时显示 0 且不报错", (() => {
    const p = tokenPayload(30);
    p.token.windows["1"] = windowOf(1, 0, { models: [] });
    const h = render(sb, "token", p, "1");
    return /<div class="big">0</.test(h);
  })());

  // 三份窗口数据必须齐备且结构一致（前端只换数据、不重算、不读库）
  const w = payload.token.windows;
  const shape = (x) => JSON.stringify(Object.keys(x).sort());
  check("三份窗口数据都在 payload 里", ["1", "7", "30"].every((k) => w[k]));
  check("三份窗口结构一致", new Set(["1", "7", "30"].map((k) => shape(w[k]))).size === 1);
  check("三份窗口都带 totals/models/recent", ["1", "7", "30"].every((k) =>
    w[k].totals && Array.isArray(w[k].models) && Array.isArray(w[k].recent)));
  check("窗口 days 与 key 一致", ["1", "7", "30"].every((k) => w[k].days === Number(k)));

  // 兼容与降级
  const legacy = { kind: "token", token: { ...windowOf(15, 1500), daily: daily(367, { skipZero: true }) } };
  const lh = render(sb, "token", legacy);
  check("旧 payload 无 windows：不渲染时间尺度控件", !lh.includes("data-win="));
  check("旧 payload 仍显示「最近 15 天」（不篡改 --days 语义）",
    /最近 15 天 · tokens 消耗/.test(lh),
    `(${(lh.match(/<div class="label">([^<]*)</) || [])[1]})`);
  check("旧 payload 不抛异常且统计卡齐全",
    [...lh.matchAll(/<div class="num">/g)].length === 8);
  check("只有一个窗口时不出控件", !render(sb, "token",
    { kind: "token", token: { ...windowOf(7, 7000), windowKeys: ["7"], windows: { "7": windowOf(7, 7000) } } })
    .includes("data-win="));
  check("--days 非 1/7/30 时补出自定义窗口",
    (() => {
      const p = tokenPayload(15);
      p.token.days = 15;
      p.token.windowKeys = ["1", "7", "15", "30"];
      p.token.windows["15"] = windowOf(15, 15000);
      const h = render(sb, "token", p);
      const keys = [...h.matchAll(/data-win="([^"]*)"/g)].map((x) => x[1]);
      const sel = (h.match(/data-win="([^"]*)" aria-pressed="true"/) || [])[1];
      return keys.join(",") === "1,7,15,30" && sel === "15";
    })());
  check("windowKeys 缺失时按数值升序派生且选择安全",
    (() => {
      const p = { kind: "token", token: { days: 30, totals: {}, models: [], recent: [], daily: [],
        windows: { "30": windowOf(30, 30000), "1": windowOf(1, 1000), "7": windowOf(7, 7000) } } };
      const h = render(sb, "token", p);
      return [...h.matchAll(/data-win="([^"]*)"/g)].map((x) => x[1]).join(",") === "1,7,30";
    })());
  check("_WIN 指向不存在的窗口时安全回退",
    (() => {
      const h = render(sb, "token", tokenPayload(30), "999");
      return /最近 30 天/.test(h) && !h.includes(">undefined<");
    })());
  check("时间尺度切换不引入网络 API；refreshData 内含真实 fetch",
    /function refreshData\(\)/.test(RUNTIME)
    && /fetch\s*\(/.test(RUNTIME.slice(RUNTIME.indexOf("function refreshData")))
    && !/XMLHttpRequest/.test(RUNTIME)
    && !/indexedDB/.test(RUNTIME)
    && !/sqlite/.test(RUNTIME));
  check("切换窗口只走 renderTab（不重新读库、不重新生成 HTML）",
    /_WIN = wbtn.getAttribute\("data-win"\)/.test(py) && /renderTab\(_TAB, DATA\)/.test(py));
  // 生产实现里 _WIN 是粘性的：页面脚本切到 7 天后重绘别的视图，不应被打回默认窗口。
  sb.__DATA = payload;
  render(sb, "token", payload, "7");
  const sticky = call(sb, 'renderTab("token", __DATA)');
  check("_WIN 粘性：切换后不带 win 参数重绘仍保持所选窗口",
    /最近 7 天 · tokens 消耗/.test(sticky) && /data-win="7" aria-pressed="true"/.test(sticky));
}

// ===========================================================================
section("UI-4 模型进度条：单条 / share 口径 / 缓存段 / 边界值");
// ===========================================================================
{
  const sb = makeVm();
  const models = [
    { name: "alpha", providerID: "p1", tokens: 500, read: 450, input: 50, requests: 5, tools: 2, share: 0.5 },
    { name: "beta", providerID: "p1", tokens: 300, read: 0, input: 300, requests: 3, tools: 1, share: 0.3 },
    { name: "beta-2", providerID: "p1", tokens: 200, read: 200, input: 0, requests: 2, tools: 0, share: 0.2 },
  ];
  const html = render(sb, "token", {
    kind: "token", token: { ...windowOf(30, 1000, { models, modelTotal: 1000, modelCount: 3 }), daily: daily(367) },
  });
  const rows = tagsOf(html, "mrow");
  check("模型行数量正确", rows.length === 3, `(${rows.length})`);
  check("每个模型恰好 1 条主进度条", rows.every((r) => (r.match(/class="track"/g) || []).length === 0)
    && (html.match(/<div class="track" /g) || []).length === 3,
    `(tracks=${(html.match(/<div class="track" /g) || []).length})`);
  check("不再出现 track2 / track3（旧的双条结构）", !html.includes("track2") && !html.includes("track3"));
  check("每条 bar 都有可访问名称与 title", (html.match(/class="track" role="img" aria-label="[^"]+" title="[^"]+"/g) || []).length === 3);
  // 注意：热力图的 data-tip 里也含「tokens · 缓存命中率」，所以这里用模型行独有的结构定位。
  check("每行标注 Token 数与缓存命中率（模型名下）",
    (html.match(/ tokens · 缓存命中率 <b style="color:/g) || []).length === rows.length,
    `(${(html.match(/ tokens · 缓存命中率 <b style="color:/g) || []).length}/${rows.length})`);
  check("右侧百分比 = share", [...html.matchAll(/<div class="mval">([^<]*)<\/div>/g)].map((x) => x[1]).join(",")
    === "50.0%,30.0%,20.0%", `(${[...html.matchAll(/<div class="mval">([^<]*)<\/div>/g)].map((x) => x[1]).join(",")})`);
  const fills = [...html.matchAll(/<i class="tf" style="width:([\d.]+)%">/g)].map((x) => Number(x[1]));
  check("条长 = share（50/30/20）", fills.join(",") === "50,30,20", `(${fills.join(",")})`);
  const caches = [...html.matchAll(/<i class="tc" style="width:([\d.]+)%">/g)].map((x) => Number(x[1]));
  check("浅色段 = 缓存读取 / 该模型 token（90% / 100%）", caches.join(",") === "90,100", `(${caches.join(",")})`);
  check("缓存读取为 0 的模型没有浅色段",
    (html.match(/<i class="tc"/g) || []).length === 2);
  check("份额之和 == 100%（分母为全部模型 token）", Math.abs(fills.reduce((a, b) => a + b, 0) - 100) < 0.01);
  check("卡片暴露了 share 的分母，便于核对口径", /data-model-total="1000"/.test(html));

  // 「相对最大模型」是错的口径：alpha 的 share 必须仍是 0.5，而不是 1.0
  check("未把「相对最大模型」当作 share",
    rows[0].includes('data-share="0.500000"') && !rows[0].includes('data-share="1.000000"'));

  // 边界：0 token / 缺失字段 / 浮点越界 / 舍入 / 未知模型
  const edge = [
    { name: "zero", tokens: 0, read: 0, input: 0, share: 0 },
    {},
    { tokens: 100, read: 500, input: 0, share: 3 },          // read > tokens、share > 1
    { tokens: 100, read: 10, input: 20, share: -2 },         // 负 share
    { tokens: 100, read: null, input: "x", share: null },    // 缺字段
    { name: undefined, tokens: 1e15, read: 1, input: 0, share: 0.333333 },
  ];
  let ehtml = "", eerr = null;
  try {
    ehtml = render(sb, "token", {
      kind: "token", token: { ...windowOf(30, 1000, { models: edge, modelTotal: 1000, modelCount: 6 }), daily: daily(367) },
    });
  } catch (e) { eerr = e; }
  check("0 token / 缺失 / 越界 / 未知模型都不抛异常", !eerr, eerr ? `(${eerr.message})` : "");
  check("越界输入不会渲染 NaN/undefined", !/\bNaN\b/.test(ehtml) && !/>undefined</.test(ehtml));
  const efills = [...ehtml.matchAll(/<i class="tf" style="width:([\d.]+)%">/g)].map((x) => Number(x[1]));
  check("share 被夹到 [0,1]（不出现 >100% 或负宽度）",
    efills.length === edge.length && efills.every((v) => v >= 0 && v <= 100), `(${efills.join(",")})`);
  const ecache = [...ehtml.matchAll(/<i class="tc" style="width:([\d.]+)%">/g)].map((x) => Number(x[1]));
  check("缓存段比例被夹到 [0,100]", ecache.length === 0 || ecache.every((v) => v >= 0 && v <= 100),
    `(${ecache.join(",")})`);
  check("read > tokens 时缓存段夹到 100%", ecache.includes(100));
  check("未知模型名不渲染 undefined", /data-model="/.test(ehtml));

  // 舍入：三等分份额之和允许 ±0.1% 误差但不得越界
  const thirds = [1, 2, 3].map((i) => ({ name: "m" + i, tokens: 100, read: 50, input: 50, share: 1 / 3 }));
  const th = render(sb, "token", {
    kind: "token", token: { ...windowOf(30, 300, { models: thirds, modelTotal: 300, modelCount: 3 }), daily: daily(30) },
  });
  const tfills = [...th.matchAll(/<i class="tf" style="width:([\d.]+)%">/g)].map((x) => Number(x[1]));
  check("浮点舍入后份额之和仍在 100%±0.1 内",
    Math.abs(tfills.reduce((a, b) => a + b, 0) - 100) <= 0.1, `(${tfills.join("+")}=${tfills.reduce((a, b) => a + b, 0)})`);

  check("无模型时给出空态且不抛异常",
    /该范围内暂无模型调用/.test(render(sb, "token", { kind: "token", token: { ...windowOf(30, 0, { models: [] }) } })));
}

// ===========================================================================
section("上下文用量分类树：渲染 / 核对 / 异常 / 交互");
// ===========================================================================
const treePayload = (branches) => ({
  kind: "context",
  context: {
    ok: true, sessionId: "s1", title: "t", directory: "D:/p", model: { providerID: "p", modelID: "m" },
    messages: 3, steps: 1, tools: 2, additions: 1, deletions: 2, files: 3, lastActivity: 1,
    tokens: { total: 3000 },
    context: { used: 2000, limit: 8000, ratio: 0.25, source: "step-finish",
      detail: { total: 2000, input: 700, output: 200, reasoning: 100, read: 1000, write: 0 } },
    breakdown: {
      root: "上下文用量分类", rootUnit: "按可核对字段拆分",
      branches,
      notSubdividable: { labels: ["系统提示词", "Memory 文件"], reason: "数据库不存提示词各段落的 token 拆分。" },
    },
  },
});

{
  const sb = makeVm();
  const branches = [
    { label: "当前上下文快照", unit: "tokens", note: "来源 step-finish", total: 2000,
      children: [{ label: "缓存读取", value: 1000 }, { label: "新输入", value: 700 },
        { label: "推理", value: 100 }, { label: "输出", value: 200 }] },
    { label: "工具调用", unit: "次", note: "part.type=tool", total: 3,
      children: [{ label: "bash", value: 2 }, { label: "未知工具", value: 1 }] },
    { label: "消息片段构成", unit: "条", note: "part 行数", total: 5,
      children: [{ label: "tool", value: 3 }, { label: "text", value: 2 }] },
    { label: "未分类示例", unit: "tokens", note: "差额", total: 500,
      children: [{ label: "缓存读取", value: 100 }, { label: "未分类", value: 400 }] },
  ];
  const html = render(sb, "context", treePayload(branches));
  const rows = tagsOf(html, "trow");
  check("分支行渲染四次", rows.filter((r) => attrOf(r, "data-tree-toggle")).length === 4,
    `(${rows.filter((r) => attrOf(r, "data-tree-toggle")).length})`);
  check("树容器使用 role=tree / treeitem / group",
    html.includes('role="tree"') && html.includes('role="treeitem"')
    && (html.match(/role="group"/g) || []).length === 4);
  check("分支行可聚焦并带 aria-expanded + aria-controls",
    rows.filter((r) => attrOf(r, "data-tree-toggle")).every((r) =>
      attrOf(r, "tabindex") === "0" && attrOf(r, "aria-expanded") === "false" && !!attrOf(r, "aria-controls")));
  check("分类树默认全部折叠（aria-expanded=false + 子项 hidden + ▸）",
    rows.filter((r) => attrOf(r, "data-tree-toggle")).every((r) =>
      attrOf(r, "aria-expanded") === "false")
    && (html.match(/class="tkids"[^>]*\bhidden\b/g) || []).length === 4
    && !/class="tw"[^>]*>▾</.test(html));
  check("三种单位分别标注", ["tokens", "次", "条"].every((u) => html.includes('data-unit="' + u + '"')));
  check("子项带 data-leaf/data-value 便于核对",
    (html.match(/data-leaf="/g) || []).length === branches.reduce((a, b) => a + b.children.length, 0));

  const buckets = [...html.matchAll(/data-tree-toggle="([^"]*)" data-branch="([^"]*)" data-unit="([^"]*)" data-total="(\d+)"/g)]
    .map((x) => ({ id: x[1], label: x[2], unit: x[3], total: Number(x[4]) }));
  const sumOf = (id) => {
    const re = new RegExp('id="' + id + '" role="group"(?: hidden)?>([\\s\\S]*?)(?=<div class="trow" role="treeitem" aria-level="1"|$)');
    const seg = (html.match(re) || [, ""])[1];
    return [...seg.matchAll(/data-value="(-?\d+)"/g)].reduce((a, x) => a + Number(x[1]), 0);
  };
  check("每个分支的子项之和 == 分支合计（分类结果可核对）",
    buckets.length === 4 && buckets.every((b) => sumOf(b.id) === b.total),
    `(${buckets.map((b) => b.label + ":" + sumOf(b.id) + "/" + b.total).join(", ")})`);
  check("未知工具被显式标出而不是猜测", /未知工具/.test(html));
  check("「未分类」差额单列", />未分类</.test(html));
  check("无法细分的分类与原因都写明", /无法细分/.test(html) && /不包含提示词各段落|不存提示词各段落/.test(html)
    && /系统提示词/.test(html) && /Memory 文件/.test(html));
  check("说明「次/条」不与 tokens 相加", /不与 tokens 相加/.test(html));
}

{
  // 空 / 缺字段 / 只有根节点 / 异常值
  const sb = makeVm();
  const cases = {
    "breakdown 缺失（旧 payload）": { kind: "context", context: { ok: true, title: "t", model: {}, tokens: {}, context: { used: 1, limit: null, ratio: null, source: "none", detail: null } } },
    "branches 为空": treePayload([]),
    "branch 无子项": treePayload([{ label: "只有根", unit: "tokens", note: "", total: 123, children: [] }]),
    "子项之和 > 合计（数据异常）": treePayload([{ label: "异常", unit: "tokens", note: "", total: 100, children: [{ label: "字段和超出总量", value: 300 }] }]),
    "重复类别被合并": treePayload([{ label: "工具调用", unit: "次", note: "", total: 4, children: [{ label: "bash", value: 4 }] }]),
    "极大数值": treePayload([{ label: "极大", unit: "tokens", note: "", total: 1e15, children: [{ label: "a", value: 1e15 }] }]),
    "子项值为 null/字符串": treePayload([{ label: "脏数据", unit: "条", note: "", total: 5, children: [{ label: "a", value: null }, { label: "b", value: "x" }, { label: "c", value: 5 }] }]),
  };
  for (const [name, data] of Object.entries(cases)) {
    let html = "", err = null;
    try { html = render(sb, "context", data); } catch (e) { err = e; }
    check(`分类树异常场景不抛异常：${name}`, !err && typeof html === "string" && html.length > 0,
      err ? `(${err.message})` : "");
    if (html) check(`  且不渲染 NaN/undefined：${name}`, !/\bNaN\b/.test(html) && !/>undefined</.test(html));
  }
  check("breakdown 缺失时正常显示上下文环，不出现分类树",
    !/role="tree"/.test(render(sb, "context", cases["breakdown 缺失（旧 payload）"]))
    && /上下文占用/.test(render(sb, "context", cases["breakdown 缺失（旧 payload）"])));
  check("branches 为空时给出明确空态",
    /暂无可展示的分类/.test(render(sb, "context", cases["branches 为空"])));
  check("无子项的分支仍可折叠并写明无可细分子项",
    /（无可细分的子项）/.test(render(sb, "context", cases["branch 无子项"])));
  check("子项和超出合计时如实标记异常",
    /字段和超出总量/.test(render(sb, "context", cases["子项之和 > 合计（数据异常）"])));
  check("context.ok=false 时只显示错误卡",
    (() => { const h = render(sb, "context", { kind: "context", context: { ok: false, error: "x" } });
      return h.includes("无法读取上下文") && !h.includes('role="tree"'); })());
}

{
  // 展开 / 折叠 / 键盘
  const byId = {};
  const sb = makeVm({ byId });
  const row = fakeEl("div");
  row.attrs["aria-expanded"] = "false";
  row.attrs["aria-controls"] = "tk-0";
  const marker = fakeEl("span");
  marker.textContent = "▸";
  row._marker = marker;
  const kids = fakeEl("div");
  kids.hidden = true;
  byId["tk-0"] = kids;
  sb.__row = row;

  check("toggleTreeNode 展开：aria-expanded=true + 子项显示 + 标记变 ▾",
    call(sb, "toggleTreeNode(__row)") === true
    && row.attrs["aria-expanded"] === "true" && kids.hidden === false && marker.textContent === "▾");
  check("toggleTreeNode 折叠：回到默认折叠状态",
    call(sb, "toggleTreeNode(__row)") === false
    && row.attrs["aria-expanded"] === "false" && kids.hidden === true && marker.textContent === "▸");
  check("toggleTreeNode 对 null / 缺 aria 的节点安全返回", call(sb, "toggleTreeNode(null)") === null);

  // 事件委托：click / keydown
  const host = fakeEl("div");
  const child = fakeEl("span");
  child.closest = (sel) => (sel === "[data-tree-toggle]" ? row : null);
  host.contains = (n) => n === row;
  sb.__host2 = host; sb.__child = child;
  check("wireTree 挂载成功", call(sb, "wireTree(__host2)") === true);
  check("wireTree 同时监听 click 与 keydown",
    !!host._listeners.click && !!host._listeners.keydown);
  const pressed = (key) => {
    let prevented = false;
    host.fire("keydown", { target: child, key, preventDefault: () => { prevented = true; } });
    return prevented;
  };
  pressed("Enter");
  check("键盘 Enter 展开（默认折叠）", row.attrs["aria-expanded"] === "true" && kids.hidden === false);
  pressed(" ");
  check("键盘 Space 折叠", row.attrs["aria-expanded"] === "false" && kids.hidden === true);
  pressed("ArrowLeft");
  check("键盘 ← 折叠（已折叠则不变）", row.attrs["aria-expanded"] === "false");
  pressed("ArrowLeft");
  check("已折叠时再按 ← 不变化", row.attrs["aria-expanded"] === "false");
  pressed("ArrowRight");
  check("键盘 → 展开", row.attrs["aria-expanded"] === "true" && kids.hidden === false);
  pressed("ArrowRight");
  check("已展开时再按 → 不变化", row.attrs["aria-expanded"] === "true");
  const ignored = pressed("a");
  check("其它按键不触发折叠且不 preventDefault",
    ignored === false && row.attrs["aria-expanded"] === "true");
  host.fire("click", { target: child });
  check("点击分支行折叠", row.attrs["aria-expanded"] === "false" && kids.hidden === true);
  check("wireTree 对无 DOM 能力的宿主安全返回 false", call(sb, "wireTree(null)") === false);
}

// ===========================================================================
section("全局：三视图 × 各类 payload 不抛异常、无 NaN/undefined");
// ===========================================================================
{
  const sb = makeVm();
  const payloads = {
    "完整 all": {
      kind: "all",
      token: { ...windowOf(30, 30000), windowKeys: ["1", "7", "30"],
        windows: { "1": windowOf(1, 1000), "7": windowOf(7, 7000), "30": windowOf(30, 30000) },
        daily: daily(367, { rich: true, skipZero: true }) },
      session: { total: 2, totals: { tokens: 1, requests: 1, tools: 1 }, changes: 3, files: 1, top: [] },
      context: treePayload([{ label: "b", unit: "tokens", note: "", total: 1, children: [{ label: "c", value: 1 }] }]).context,
    },
    "空 all": { kind: "all", token: { totals: {}, models: [], daily: [], recent: [], days: 30 },
      session: { total: 0, totals: {}, changes: 0, files: 0, top: [] }, context: { ok: false } },
    "只有 kind": { kind: "all" },
    "空对象": {},
    "null 字段": { kind: "all", token: null, session: null, context: null },
  };
  for (const [name, data] of Object.entries(payloads)) {
    let err = null, html = "";
    try { html = build(sb, data); } catch (e) { err = e; }
    check(`buildBody 不抛异常：${name}`, !err, err ? `(${err.message})` : "");
    if (html) {
      check(`  无 NaN/undefined：${name}`, !/\bNaN\b/.test(html) && !/>undefined</.test(html) && !/\[object Object\]/.test(html));
      for (const tab of ["token", "session", "context"]) {
        check(`  ${tab} 视图可渲染：${name}`, html.includes(`data-tab="${tab}"`) || name !== "完整 all");
      }
    }
  }
  for (const [name, data] of Object.entries(payloads)) {
    for (const kind of ["token", "session", "context"]) {
      let err = null;
      try { render(sb, kind, data); } catch (e) { err = e; }
      check(`renderTab(${kind}) 不抛异常：${name}`, !err, err ? `(${err.message})` : "");
    }
  }
  check("保留旧的 DOM 契约（role=tablist / tabpanel / renderTab(selected, DATA) / id=root）",
    (() => { const h = build(sb, payloads["完整 all"]);
      return h.includes('role="tablist"') && h.includes('role="tabpanel"')
        && py.includes("renderTab(selected, DATA)") && py.includes('id="root"'); })());
  check("生成期无外部资源引用（无 script src / link href；允许 loopback 模板）",
    !/<script src/.test(py) && !/<link /.test(py)
    && !/https?:\/\/(?!127\.0\.0\.1|localhost|\{LOOPBACK_HOST\})/.test(py));
}

// ===========================================================================
section("实时刷新：fetch 更新数据 / 失败保留旧数据 / 并发闸门");
// ===========================================================================
{
  const byId = {};
  const root = fakeEl("div");
  const regen = fakeEl("button");
  const banner = fakeEl("div");
  const last = fakeEl("span");
  const sub = fakeEl("div");
  byId.root = root;
  byId.regen = regen;
  byId["refresh-banner"] = banner;
  byId["last-ok"] = last;
  const sandbox = makeVm({ byId });
  sandbox.LIVE = { reportUrl: "http://127.0.0.1:9/abc/api/report" };
  sandbox.document.querySelector = (sel) => (sel === ".head .sub" ? sub : null);
  let fetchCalls = 0;
  let fetchOk = true;
  let inflight = 0;
  let maxInflight = 0;
  sandbox.fetch = () => {
    fetchCalls++;
    inflight++;
    maxInflight = Math.max(maxInflight, inflight);
    return Promise.resolve().then(() => {
      inflight--;
      if (!fetchOk) return { ok: false, status: 503 };
      return {
        ok: true,
        status: 200,
        json: () => Promise.resolve({
          kind: "all",
          generatedAt: 2,
          dbLabel: "mimocode.db",
          token: { days: 30, totals: { tokens: 999, sessions: 2, requests: 3, tools: 1,
            input: 0, output: 0, reasoning: 0, cacheRead: 0, cacheWrite: 0, cacheShare: 0 },
            models: [], daily: [], recent: [] },
          session: { total: 2, totals: { tokens: 1, requests: 1, tools: 1 }, changes: 0, files: 0, top: [] },
          context: { ok: false },
        }),
      };
    });
  };
  sandbox.AbortController = class {
    constructor() { this.signal = { aborted: false }; }
    abort() { this.signal.aborted = true; }
  };
  sandbox.setTimeout = () => 0;
  sandbox.clearTimeout = () => {};

  sandbox.DATA = {
    kind: "all",
    token: { days: 30, totals: { tokens: 1, sessions: 1, requests: 1, tools: 0,
      input: 0, output: 0, reasoning: 0, cacheRead: 0, cacheWrite: 0, cacheShare: 0 },
      models: [], daily: [], recent: [] },
    session: { total: 1, totals: { tokens: 1, requests: 1, tools: 0 }, changes: 0, files: 0, top: [] },
    context: { ok: false },
    generatedAt: 1,
    dbLabel: "mimocode.db",
  };
  sandbox._TAB = "token";
  root.innerHTML = build(sandbox, sandbox.DATA);

  const painted = call(sandbox, `__usagePaint({
    kind: "token",
    generatedAt: 2,
    dbLabel: "mimocode.db",
    token: { days: 30, totals: { tokens: 999, sessions: 2, requests: 3, tools: 1,
      input: 0, output: 0, reasoning: 0, cacheRead: 0, cacheWrite: 0, cacheShare: 0 },
      models: [], daily: [], recent: [] },
    session: { total: 2, totals: {}, changes: 0, files: 0, top: [] },
    context: { ok: false }
  })`);
  check("paintData 返回 true 并更新 DATA.generatedAt",
    painted === true && sandbox.DATA.generatedAt === 2);
  check("paintData 更新 header 时间文案", /更新时间/.test(sub.textContent));

  // 成功刷新
  fetchOk = true;
  fetchCalls = 0;
  const ok1 = await call(sandbox, "__usageRefresh()");
  check("成功刷新返回 true 且调用 fetch", ok1 === true && fetchCalls === 1);
  check("成功后隐藏错误条", banner.hidden === true || banner.textContent === "");
  check("按钮恢复可点击文案", regen.textContent === "刷新数据" && regen.disabled !== true);

  // 失败刷新保留旧数据
  const genBefore = sandbox.DATA.generatedAt;
  const tokensBefore = sandbox.DATA.token.totals.tokens;
  fetchOk = false;
  const ok2 = await call(sandbox, "__usageRefresh()");
  check("失败刷新返回 false", ok2 === false);
  check("失败时保留上次成功数据",
    sandbox.DATA.generatedAt === genBefore && sandbox.DATA.token.totals.tokens === tokensBefore);
  check("失败时展示错误条", /刷新失败/.test(banner.textContent) && banner.hidden === false);

  // 恢复
  fetchOk = true;
  const ok3 = await call(sandbox, "__usageRefresh()");
  check("恢复后可再次刷新成功", ok3 === true && sandbox.DATA.token.totals.tokens === 999);

  // 并发：第一次进行中时第二次应被闸门挡住
  fetchCalls = 0;
  maxInflight = 0;
  let release;
  sandbox.fetch = () => {
    fetchCalls++;
    inflight++;
    maxInflight = Math.max(maxInflight, inflight);
    return new Promise((resolve) => {
      release = () => {
        inflight--;
        resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({
            kind: "token",
            generatedAt: 3,
            dbLabel: "mimocode.db",
            token: { days: 30, totals: { tokens: 5, sessions: 1, requests: 1, tools: 0,
              input: 0, output: 0, reasoning: 0, cacheRead: 0, cacheWrite: 0, cacheShare: 0 },
              models: [], daily: [], recent: [] },
            session: { total: 1, totals: {}, changes: 0, files: 0, top: [] },
            context: { ok: false },
          }),
        });
      };
    });
  };
  const pa = call(sandbox, "__usageRefresh()");
  const pb = call(sandbox, "__usageRefresh()");
  check("并发第二次立即短路（fetch 只发一次）", fetchCalls === 1);
  release();
  const [ra, rb] = await Promise.all([pa, pb]);
  check("并发完成后仅第一次执行网络请求结果", ra === true && rb === false);

  // 无 LIVE 配置时明确不可刷新
  sandbox.LIVE = null;
  fetchCalls = 0;
  const offline = await call(sandbox, "__usageRefresh()");
  check("无 LIVE 时不发请求并提示离线", offline === false && fetchCalls === 0
    && /静态快照|实时/.test(banner.textContent));
}

console.log(`\n${failed === 0 ? "UI DOM OK" : "UI DOM FAILED: " + failed}`);
process.exit(failed === 0 ? 0 : 1);
