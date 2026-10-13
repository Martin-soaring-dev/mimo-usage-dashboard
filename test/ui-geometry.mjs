// test/ui-geometry.mjs — 真实浏览器（无头 Chrome + CDP）里的 UI 几何与交互回归
//
//   node test/ui-geometry.mjs            # 本机有 Chrome 时执行真实布局断言
//   CHROME_PATH=/path/to/chrome node …   # 指定浏览器
//   node test/ui-geometry.mjs --require-chrome   # 无浏览器时**必须失败**（CI 用）
//   node test/ui-geometry.mjs --shots DIR  # 额外存几张截图（仅合成数据，可人工复核）
//
// 退出码（三态，缺浏览器绝不等于通过）：
//   0 = PASS（真实浏览器跑完且全部断言通过）
//   1 = FAIL（断言失败；或 --require-chrome 时找不到/起不来浏览器）
//   2 = SKIP（未加 --require-chrome 且本机无可用浏览器——明确跳过，不是成功）
//
// 为什么单独一个文件、且不挂进 `npm test`：
//   `npm test` 必须在没有浏览器的机器/CI 上也能确定性通过，而这里的断言**只能在真实
//   布局引擎里做**（模拟 DOM 没有排版，量不出 offsetHeight / scrollWidth）。
//   本机手动运行允许 SKIP 降级；CI 必须加 --require-chrome，把缺依赖变成显式失败。
//
// 覆盖（对应 spec §7.2）：
//   UI-1 12 个月在 1200/1000/900/820/720px 下完整可见且无横向溢出；<720px 才允许滚动
//   UI-1 悬浮提示字段完整（日期 + 星期 + token + 缓存命中率），无数据日安全显示
//   UI-2 一/三/五 标签行中心与对应日期行中心偏差 ≤ 1px
//   UI-3 今日/7天/30天 三段可切换、hero 随之变化、切换不产生任何网络请求
//   UI-4 每个模型恰好 1 条进度条，条长 = m.share，浅色段 = 该模型的缓存读取
//   全局 无 JS 控制台错误、无外部资源请求、分类树展开折叠与键盘可操作

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { fileUrl, findChrome, openPage } from "./cdp.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const SCRIPT = path.join(ROOT, "usage_cli.py");
const PY = process.env.MIMO_USAGE_PY || process.env.PYTHON
  || (process.platform === "win32" ? "python" : "python3");
const REQUIRE_CHROME = process.argv.includes("--require-chrome");

// 常规视口 + 边界：≥720 必须无溢出；1093/768 是真实使用中出现过问题/常用宽度的抽样；
// 1199 是「刚窄于 1200」的边界；719 是「刚窄于 720」，从这里开始才允许热力图内部横向滚动。
const VIEWPORTS = [1200, 1093, 1000, 900, 820, 768, 720, 1199, 719];
const NO_SCROLL_VIEWPORTS = VIEWPORTS.filter((w) => w >= 720);

let failed = 0;
const check = (name, ok, extra = "") => {
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${extra ? "  " + extra : ""}`);
  if (!ok) failed++;
};

const shotsIdx = process.argv.indexOf("--shots");
const SHOT_DIR = shotsIdx >= 0 ? process.argv[shotsIdx + 1] : null;

// ---- 生成合成库 + 页面（绝不触碰真实 MiMo 数据库/配置） ----
const work = fs.mkdtempSync(path.join(os.tmpdir(), "mimo-geometry-"));
const dbFile = path.join(work, "synthetic.db");
const htmlFile = path.join(work, "synthetic-dashboard.html");

const fixture = [
  "import sys",
  "sys.path.insert(0, " + JSON.stringify(HERE.split(path.sep).join("/")) + ")",
  "from test_python import make_rich_db",
  "make_rich_db(sys.argv[1])",
].join("\n");
execFileSync(PY, ["-c", fixture, dbFile], { stdio: ["ignore", "pipe", "pipe"] });
execFileSync(PY, [SCRIPT, "--db", dbFile, "--out", htmlFile], { stdio: ["ignore", "pipe", "pipe"] });

const html = fs.readFileSync(htmlFile, "utf8");
console.log("=== 静态（生成期）断言 ===");
check("生成期 HTML 无外部资源引用；静态页 LIVE=null", !html.includes("var LIVE = {") && html.includes("var LIVE = null"));
check("生成期 HTML 无 XMLHttpRequest", !html.includes("XMLHttpRequest"));
check("生成期 HTML 无外部 http(s) 资源", !/https?:\/\//.test(html.replace(/https?:\/\/[^"]*json-schema\.org[^"]*/g, "")));

const PROBE = `(() => {
  const q = (s) => document.querySelector(s);
  const qa = (s) => Array.from(document.querySelectorAll(s));
  const cy = (el) => { const r = el.getBoundingClientRect(); return r.top + r.height / 2; };
  const cells = qa('.hm .cell');
  const months = qa('.hm .mcell');
  const labels = qa('.hm .dlbl');
  const rowBuckets = {};
  for (const c of cells) {
    const k = String(c.getAttribute('data-row'));
    (rowBuckets[k] = rowBuckets[k] || []).push(cy(c));
  }
  const rowCenter = {};
  for (const k of Object.keys(rowBuckets)) {
    const a = rowBuckets[k];
    rowCenter[k] = a.reduce((x, y) => x + y, 0) / a.length;
  }
  const labelRows = labels.map((l) => ({ dow: Number(l.getAttribute('data-dow')), center: cy(l) }));
  const dowDev = labelRows.map((l) => {
    const rc = rowCenter[String(l.dow)];
    return rc === undefined ? null : Math.abs(l.center - rc);
  });
  const tips = cells.map((c) => c.getAttribute('data-tip') || '');
  const aria = cells.map((c) => c.getAttribute('aria-label') || '');
  const models = qa('.mrow').map((row) => {
    const track = row.querySelector('.track');
    const tf = row.querySelector('.tf');
    const tc = row.querySelector('.tc');
    return {
      name: row.getAttribute('data-model'),
      share: Number(row.getAttribute('data-share')),
      cacheFrac: Number(row.getAttribute('data-cache-frac')),
      tokens: Number(row.getAttribute('data-tokens')),
      read: Number(row.getAttribute('data-read')),
      input: Number(row.getAttribute('data-input')),
      tracks: row.querySelectorAll('.track').length,
      legacyTracks: row.querySelectorAll('.track2, .track3').length,
      trackW: track ? track.getBoundingClientRect().width : 0,
      fillW: tf ? tf.getBoundingClientRect().width : 0,
      cacheW: tc ? tc.getBoundingClientRect().width : 0,
      hasCacheSeg: !!tc,
      sharePct: row.querySelector('.mval') ? row.querySelector('.mval').textContent.trim() : '',
    };
  });
  const modelCardEl = q('[data-model-total]');
  const docEl = document.documentElement;
  return JSON.stringify({
    viewportW: window.innerWidth,
    colsClient: q('.hm .cols') ? q('.hm .cols').clientWidth : -1,
    colsScroll: q('.hm .cols') ? q('.hm .cols').scrollWidth : -1,
    colsOverflowX: q('.hm .cols') ? getComputedStyle(q('.hm .cols')).overflowX : '',
    docClient: docEl.clientWidth,
    docScroll: docEl.scrollWidth,
    cellCount: cells.length,
    cellW: cells.length ? cells[0].getBoundingClientRect().width : -1,
    monthCount: new Set(months.map((m) => m.textContent.trim())).size,
    months: months.map((m) => m.textContent.trim()),
    cellLabelRows: Object.keys(rowCenter).length,
    labelRows,
    dowDev,
    cellsWithTip: tips.filter((t) => t.length > 0).length,
    cellsWithAria: aria.filter((t) => t.length > 0).length,
    tipsComplete: tips.filter((t) => /\\d{4}-\\d{2}-\\d{2}/.test(t) && /周[日一二三四五六]|星期未知/.test(t)
      && /(无调用|tokens)/.test(t) && /缓存命中率/.test(t)).length,
    zeroCells: cells.filter((c) => c.getAttribute('data-lv') === '0').length,
    heroLabel: q('.hero .label') ? q('.hero .label').textContent : '',
    heroNum: q('.hero .big') ? q('.hero .big').textContent : '',
    winButtons: qa('[data-win]').map((b) => ({
      key: b.getAttribute('data-win'),
      pressed: b.getAttribute('aria-pressed'),
      label: b.textContent.trim(),
    })),
    scopeText: q('.tscale .scope') ? q('.tscale .scope').textContent : '',
    sessionCardTitle: (qa('.grid4 .card[title]')[0] || {}).getAttribute
      ? qa('.grid4 .card[title]')[0].getAttribute('title') : '',
    modelRows: models.length,
    models,
    modelTotal: modelCardEl ? Number(modelCardEl.getAttribute('data-model-total')) : -1,
    modelCount: modelCardEl ? Number(modelCardEl.getAttribute('data-model-count')) : -1,
    modelShown: modelCardEl ? Number(modelCardEl.getAttribute('data-model-shown')) : -1,
    externalResources: performance.getEntriesByType('resource').map((e) => e.name).filter((n) => /^(https?|ftp):/.test(n)),
  });
})()`;

const chrome = findChrome();

// 缺依赖时的三态出口：--require-chrome（CI）→ FAIL(1)；否则明确 SKIP(2)，绝不静默成功。
const bailNoBrowser = (reason) => {
  try { fs.rmSync(work, { recursive: true, force: true }); } catch { /* ignore */ }
  if (REQUIRE_CHROME) {
    console.log(`\n  FAIL  --require-chrome：${reason}`);
    console.log("\nUI GEOMETRY FAILED: 浏览器依赖缺失（不允许静默跳过）");
    process.exit(1);
  }
  console.log(`\n  SKIP  ${reason}（未指定 --require-chrome，按 SKIP 处理）`);
  console.log("        注意：本文件的断言全部依赖真实布局引擎，模拟 DOM 无法替代。");
  console.log("\nUI GEOMETRY SKIPPED");
  process.exit(2);
};

if (!chrome) {
  bailNoBrowser("未找到可用 Chrome/Edge（可用 CHROME_PATH 指定）→ 跳过真实浏览器几何验证");
}
console.log(`\n=== 真实浏览器几何（${chrome}）===`);

let page = null;
try {
  page = await openPage({ width: 1200, height: 2600 });
} catch (err) {
  bailNoBrowser(`无法启动浏览器（${err && err.message}）→ 跳过真实浏览器几何验证`);
}
if (!page) bailNoBrowser("无法启动浏览器 → 跳过真实浏览器几何验证");

const probes = {};
try {
  await page.goto(fileUrl(htmlFile), 900);

  for (const w of VIEWPORTS) {
    await page.viewport(w, 2600);
    probes[w] = JSON.parse(await page.eval(PROBE));
  }

  // ---------- UI-1：12 个月 + 溢出 ----------
  console.log("\n-- UI-1 热力图布局 --");
  const base = probes[1200];
  check("热力图渲染了 12 个月标签", base.monthCount === 12, `(${base.monthCount}: ${base.months.join(",")})`);
  const counts = new Set(VIEWPORTS.map((w) => probes[w].cellCount));
  check("各视口日期格数量一致（12 个月完整、无裁剪）", counts.size === 1 && base.cellCount >= 365,
    `(${[...counts].join(",")} 格)`);
  check("7 行日期行全部有格子", base.cellLabelRows === 7, `(${base.cellLabelRows})`);
  check("存在无数据的空格子（用于验证无调用提示）", base.zeroCells > 0, `(${base.zeroCells})`);

  for (const w of VIEWPORTS) {
    const p = probes[w];
    const over = p.colsScroll - p.colsClient;
    if (w >= 720) {
      check(`${w}px: .hm .cols 无横向溢出`, over === 0, `(client=${p.colsClient} scroll=${p.colsScroll} 溢出=${over})`);
    } else {
      // 窄屏（<720px）：媒体查询给出 JS 计算的 min-width，允许**并且预期**出现滚动区。
      // 两条断言都必须有意义：
      //   (a) 溢出确实发生 —— 证明 min-width 生效（否则 12 个月会被压扁成 0 宽而测不出来）；
      //   (b) 溢出必须可滚动到达 —— overflow-x 必须是 auto/scroll，被 hidden/clip 裁掉 = 内容不可达。
      check(`${w}px: 窄屏 min-width 生效（滚动区确实存在）`, over > 0, `(client=${p.colsClient} scroll=${p.colsScroll} 溢出=${over})`);
      check(`${w}px: 溢出内容可滚动到达`, p.colsOverflowX === "auto" || p.colsOverflowX === "scroll",
        `(overflow-x=${p.colsOverflowX})`);
    }
    check(`${w}px: 页面本身不出现横向滚动条`, p.docScroll - p.docClient <= 0,
      `(doc client=${p.docClient} scroll=${p.docScroll})`);
  }

  // ---------- UI-1：提示字段 ----------
  console.log("\n-- UI-1 悬浮提示 --");
  check("每个日期格都有 data-tip", base.cellsWithTip === base.cellCount, `(${base.cellsWithTip}/${base.cellCount})`);
  check("每个日期格都有 aria-label", base.cellsWithAria === base.cellCount, `(${base.cellsWithAria}/${base.cellCount})`);
  check("提示含 日期+星期+token+缓存命中率", base.tipsComplete === base.cellCount,
    `(${base.tipsComplete}/${base.cellCount})`);

  await page.viewport(1200, 2600);
  const hover = async (selector) => {
    const box = JSON.parse(await page.eval(
      `(() => { const el = ${selector}; if (!el) return "null";
        const r = el.getBoundingClientRect();
        return JSON.stringify({ x: r.left + r.width / 2, y: r.top + r.height / 2 }); })()`));
    if (!box) return null;
    await page.moveMouse(box.x, box.y);
    return JSON.parse(await page.eval(
      `(() => { const t = document.getElementById('hm-tip');
        if (!t) return JSON.stringify({ exists: false });
        const r = t.getBoundingClientRect();
        return JSON.stringify({ exists: true, hidden: t.hidden, text: t.textContent,
          left: r.left, right: r.right, top: r.top, bottom: r.bottom,
          vw: window.innerWidth, vh: window.innerHeight }); })()`));
  };

  const filled = await hover("document.querySelector('.hm .cell[data-lv]:not([data-lv=\"0\"])')");
  check("悬停有数据的格子显示提示层", !!filled && filled.exists && !filled.hidden,
    filled ? `(text=${JSON.stringify(filled.text)})` : "(no tip layer)");
  if (filled && filled.exists) {
    check("提示内容含日期 + 星期", /\d{4}-\d{2}-\d{2}/.test(filled.text) && /周[日一二三四五六]/.test(filled.text),
      `(${JSON.stringify(filled.text)})`);
    check("提示内容含 token 用量", /tokens/.test(filled.text));
    check("提示内容含缓存命中率", /缓存命中率/.test(filled.text));
    check("提示层未跑出视口", filled.left >= 0 && filled.right <= filled.vw + 1, `(right=${filled.right} vw=${filled.vw})`);
  }
  const empty = await hover("document.querySelector('.hm .cell[data-lv=\"0\"]')");
  check("悬停无数据格子显示「无调用」且字段齐全", !!empty && empty.exists && !empty.hidden
    && /无调用/.test(empty.text) && /缓存命中率/.test(empty.text) && /周[日一二三四五六]/.test(empty.text),
    empty ? `(${JSON.stringify(empty.text)})` : "(no tip layer)");
  const lastCell = await hover("document.querySelectorAll('.hm .cell')[6]");
  check("贴近视口右侧/顶部时提示层自动夹紧", !!lastCell && lastCell.exists
    && lastCell.left >= 0 && lastCell.right <= lastCell.vw + 1 && lastCell.top >= 0,
    lastCell ? `(l=${lastCell.left.toFixed(1)} r=${lastCell.right.toFixed(1)} t=${lastCell.top.toFixed(1)} vw=${lastCell.vw})` : "");

  // 悬停**最右侧**格子：transform:scale 会把格子的变换盒子计入 .cols 的可滚动区域，
  // 若超出容器右缘就会凭空多出横向滚动条（真实用户在数据最右端悬停时必现）。
  // 这里把该行为钉死：任何视口下，悬停最右侧格子都不得让 .cols 出现横向溢出。
  const rightBox = JSON.parse(await page.eval(
    `(() => { const cells = document.querySelectorAll('.hm .cell');
      const el = cells[cells.length - 1]; if (!el) return "null";
      const r = el.getBoundingClientRect();
      return JSON.stringify({ x: r.left + r.width / 2, y: r.top + r.height / 2 }); })()`));
  let hoverOver = null;
  if (rightBox) {
    await page.moveMouse(rightBox.x, rightBox.y);
    hoverOver = JSON.parse(await page.eval(
      `(() => { const c = document.querySelector('.hm .cols');
        const last = document.querySelectorAll('.hm .cell');
        const el = last[last.length - 1];
        return JSON.stringify({ client: c.clientWidth, scroll: c.scrollWidth,
          over: c.scrollWidth - c.clientWidth, hovered: el.matches(':hover') }); })()`));
  }
  check("悬停最右侧格子不触发 .cols 横向溢出",
    !!hoverOver && hoverOver.hovered && hoverOver.over === 0,
    hoverOver ? `(hovered=${hoverOver.hovered} client=${hoverOver.client} scroll=${hoverOver.scroll} 溢出=${hoverOver.over})` : "(no cell)");
  await page.moveMouse(4, 4);

  // ---------- UI-2：星期标签对齐 ----------
  console.log("\n-- UI-2 星期标签对齐（偏差阈值 1px）--");
  for (const w of VIEWPORTS) {
    const p = probes[w];
    const devs = p.dowDev.filter((d) => d !== null);
    const maxDev = devs.length ? Math.max(...devs) : -1;
    check(`${w}px: 一/三/五 与对应行中心偏差 ≤ 1px`, p.labelRows.length === 3 && devs.length === 3 && maxDev <= 1,
      `(labels=${p.labelRows.length} 偏差=${devs.map((d) => d.toFixed(2)).join("/")}px)`);
  }

  // ---------- UI-4：模型进度条 ----------
  console.log("\n-- UI-4 模型进度条 --");
  const p30 = probes[1200];
  const m30 = p30.models;
  check("模型行已渲染", m30.length >= 4, `(${m30.length} 个模型)`);
  check("每个模型恰好 1 条主进度条", m30.every((m) => m.tracks === 1) && p30.modelRows === m30.length,
    `(rows=${p30.modelRows} tracks=${m30.map((m) => m.tracks).join(",")})`);
  check("旧的第二条 bar 已消失", m30.every((m) => m.legacyTracks === 0));

  // share 的口径必须是「该模型 token / 窗口内全部模型 token」——不是「相对最大模型」。
  const totalModel = p30.modelTotal;
  const shareVsTotal = m30.map((m) => Math.abs(m.share - m.tokens / totalModel));
  check("m.share == 该模型 token / 窗口内全部模型 token（分母来自 payload）",
    totalModel > 0 && Math.max(...shareVsTotal) < 1e-6,
    `(modelTotal=${totalModel} 最大偏差=${Math.max(...shareVsTotal).toExponential(2)})`);
  const maxTok = Math.max(...m30.map((m) => m.tokens));
  check("share 不是「相对最大模型」的比例",
    m30.some((m) => Math.abs(m.share - m.tokens / maxTok) > 0.05),
    `(max=${maxTok} 相对最大模型的份额=${(m30.find((m) => m.tokens === maxTok).share * 100).toFixed(1)}%)`);
  const shareErr = m30.map((m) => Math.abs(m.fillW - m.trackW * m.share));
  check("条长 = m.share（像素级 ±1px）", Math.max(...shareErr) <= 1,
    `(最大偏差 ${Math.max(...shareErr).toFixed(2)}px)`);
  const cacheFracErr = m30.map((m) => Math.abs(m.cacheFrac - (m.tokens > 0 ? m.read / m.tokens : 0)));
  check("浅色段比例 == 缓存读取 / 该模型 token（字段口径一致，±1e-6）",
    Math.max(...cacheFracErr) < 1e-6, `(最大偏差=${Math.max(...cacheFracErr).toExponential(2)})`);
  const cacheErr = m30.map((m) => Math.abs(m.cacheW - (m.trackW * m.read) / totalModel));
  check("浅色段像素宽 == 条宽 × 缓存读取 / 全部模型 token（同分母，±1px）", Math.max(...cacheErr) <= 1,
    `(最大偏差 ${Math.max(...cacheErr).toFixed(2)}px)`);
  const noCache = m30.filter((m) => m.cacheFrac === 0);
  check("无缓存读取的模型条内没有浅色段", noCache.length > 0
    && noCache.every((m) => !m.hasCacheSeg && m.cacheW === 0),
    `(${noCache.map((m) => m.name + ":seg=" + m.hasCacheSeg).join(", ")})`);
  const hasCache = m30.filter((m) => m.cacheFrac > 0);
  check("有缓存读取的模型条内有浅色段且窄于份额段",
    hasCache.length > 0 && hasCache.every((m) => m.hasCacheSeg && m.cacheW > 0 && m.cacheW <= m.fillW + 1),
    `(${hasCache.map((m) => m.name + ":" + m.cacheW.toFixed(1) + "/" + m.fillW.toFixed(1) + "px").join(", ")})`);
  check("右侧百分比与 m.share 一致",
    m30.every((m) => m.sharePct === (Math.min(1, Math.max(0, m.share)) * 100).toFixed(1) + "%"),
    `(${m30.map((m) => m.name + "=" + m.sharePct).join(", ")})`);
  check("份额条不溢出容器", m30.every((m) => m.fillW <= m.trackW + 0.5 && m.cacheW <= m.fillW + 0.5),
    `(max fill/track=${Math.max(...m30.map((m) => m.fillW / m.trackW)).toFixed(4)})`);
  console.log("  明细: " + m30.map((m) => `${m.name} share=${m.share.toFixed(4)} fill=${m.fillW.toFixed(1)}px` +
    ` cacheFrac=${m.cacheFrac.toFixed(4)} cache=${m.cacheW.toFixed(1)}px`).join(" | "));

  // ---------- UI-3：时间尺度切换 ----------
  console.log("\n-- UI-3 时间尺度切换 --");
  const winLabels = base.winButtons.map((b) => b.label).join("/");
  check("存在 今日 / 7 天 / 30 天 三段", base.winButtons.length === 3
    && base.winButtons.map((b) => b.key).join(",") === "1,7,30", `(${winLabels})`);
  check("默认选中 30 天（与 --days 默认一致）",
    base.winButtons.find((b) => b.pressed === "true")?.key === "30",
    `(selected=${base.winButtons.find((b) => b.pressed === "true")?.key})`);
  check("hero 文案标出当前口径", /最近 30 天/.test(base.heroLabel), `(${base.heroLabel})`);
  check("口径说明标注了会话数/热力图不随窗口变化",
    /会话数/.test(base.scopeText) && /热力图/.test(base.scopeText), `(${base.scopeText.slice(0, 60)}…)`);
  check("会话数卡片标注了全量口径", /全部会话/.test(base.sessionCardTitle || ""), `(${base.sessionCardTitle})`);

  // 注入计数器：切换期间不得出现任何网络请求
  await page.eval(`(() => {
    window.__net = 0;
    const f = window.fetch;
    window.fetch = function () { window.__net++; return f ? f.apply(this, arguments) : Promise.reject(new Error('blocked')); };
    const XO = window.XMLHttpRequest && window.XMLHttpRequest.prototype.open;
    if (XO) { window.XMLHttpRequest.prototype.open = function () { window.__net++; return XO.apply(this, arguments); }; }
    return true;
  })()`);

  const readHero = () => page.eval(
    `JSON.stringify({ label: document.querySelector('.hero .label').textContent,
      num: document.querySelector('.hero .big').textContent,
      cards: Array.from(document.querySelectorAll('.grid4 .num')).map((n) => n.textContent).join('|'),
      pressed: Array.from(document.querySelectorAll('[data-win]')).map((b) => b.getAttribute('aria-pressed')).join(','),
      spark: document.querySelectorAll('.spark i').length })`);

  const hero30 = JSON.parse(await readHero());
  await page.eval(`(() => { document.querySelector('[data-win="1"]').click(); return true; })()`);
  const hero1 = JSON.parse(await readHero());
  await page.eval(`(() => { document.querySelector('[data-win="7"]').click(); return true; })()`);
  const hero7 = JSON.parse(await readHero());
  await page.eval(`(() => { document.querySelector('[data-win="30"]').click(); return true; })()`);
  const hero30b = JSON.parse(await readHero());

  check("切到「今日」后 hero 文案与数字都变化",
    /今日/.test(hero1.label) && hero1.num !== hero30.num, `(${hero1.label} ${hero1.num} ← ${hero30.num})`);
  check("切到「7 天」后 hero 文案与数字都变化",
    /最近 7 天/.test(hero7.label) && hero7.num !== hero1.num && hero7.num !== hero30.num,
    `(${hero7.label} ${hero7.num})`);
  check("8 张统计卡随之刷新", hero1.cards !== hero30.cards && hero7.cards !== hero30.cards);
  check("趋势图随之刷新（今日 1 根 / 7 天 7 根）",
    hero1.spark === 1 && hero7.spark === 7, `(今日=${hero1.spark} 7天=${hero7.spark} 30天=${hero30.spark})`);
  check("选中态互斥且只有一个 aria-pressed",
    hero1.pressed.split(",").filter((p) => p === "true").length === 1
    && hero7.pressed.split(",").filter((p) => p === "true").length === 1,
    `(今日=${hero1.pressed} 7天=${hero7.pressed})`);
  check("30 天结果与默认基线逐值一致（回归）",
    hero30b.label === hero30.label && hero30b.num === hero30.num && hero30b.cards === hero30.cards,
    `(${hero30b.label} ${hero30b.num})`);
  check("切换过程零网络请求（fetch / XHR 均未触发）",
    (await page.eval("window.__net")) === 0, `(net=${await page.eval("window.__net")})`);
  check("页面运行期无外部资源请求", base.externalResources.length === 0,
    `(${base.externalResources.join(", ")})`);

  // 模型块在窗口切换后同样刷新（30 天 4 个模型 / 今日 1 个）
  const mCount = async () => page.eval("document.querySelectorAll('.mrow').length");
  await page.eval(`(() => { document.querySelector('[data-win="1"]').click(); return true; })()`);
  const todayModels = await mCount();
  await page.eval(`(() => { document.querySelector('[data-win="30"]').click(); return true; })()`);
  const monthModels = await mCount();
  check("模型块随窗口刷新", todayModels < monthModels && todayModels >= 1,
    `(今日=${todayModels} 30天=${monthModels})`);

  // ---------- 分类树 ----------
  console.log("\n-- 上下文用量分类树 --");
  await page.eval(`(() => { document.querySelector('[data-tab="context"]').click(); return true; })()`);
  const tree = JSON.parse(await page.eval(`JSON.stringify(
    Array.from(document.querySelectorAll('[data-tree-toggle]')).map((n) => {
      const kids = document.getElementById(n.getAttribute('aria-controls'));
      const leaves = kids ? Array.from(kids.querySelectorAll('[data-leaf]')) : [];
      return {
        label: n.getAttribute('data-branch'), unit: n.getAttribute('data-unit'),
        total: Number(n.getAttribute('data-total')), tabindex: n.getAttribute('tabindex'),
        expanded: n.getAttribute('aria-expanded'),
        kidHidden: kids ? kids.hidden : null,
        sum: leaves.reduce((a, l) => a + Number(l.getAttribute('data-value')), 0),
        count: leaves.length,
        leaves: leaves.map((l) => l.getAttribute('data-leaf') + '=' + l.getAttribute('data-value')),
      };
    }))`));
  check("分类树渲染了 4 个分支（3 个 tokens/次/条 口径）", tree.length === 4,
    `(${tree.map((b) => b.label + "[" + b.unit + "]").join(", ")})`);
  check("每个分支的子项之和 == 分支合计（可核对）",
    tree.every((b) => b.count > 0 && b.sum === b.total),
    `(${tree.map((b) => b.label + " " + b.sum + "/" + b.total).join(", ")})`);
  check("tokens / 次 / 条 三种单位分开标注",
    new Set(tree.map((b) => b.unit)).size === 3, `(${tree.map((b) => b.unit).join(",")})`);
  check("分支行可键盘聚焦（tabindex=0）", tree.every((b) => b.tabindex === "0"));
  check("默认全部折叠（aria-expanded=false + 子项 hidden）",
    tree.every((b) => b.expanded === "false" && b.kidHidden === true));
  check("未知工具被显式归入「未知工具」",
    tree.some((b) => b.unit === "次" && b.leaves.some((l) => l.startsWith("未知工具="))),
    `(${(tree.find((b) => b.unit === "次") || {}).leaves?.join(", ")})`);
  check("无法细分的分类被如实列出",
    /系统提示词/.test(html) && /无法细分/.test(html) && /不存提示词各段落/.test(html));

  // 鼠标展开 / 折叠（默认折叠 → 点击展开）
  const toggleByClick = async () => JSON.parse(await page.eval(`(() => {
    const n = document.querySelector('[data-tree-toggle]');
    const kids = document.getElementById(n.getAttribute('aria-controls'));
    return JSON.stringify({ expanded: n.getAttribute('aria-expanded'), hidden: kids.hidden }); })()`));
  await page.eval(`(() => { document.querySelector('[data-tree-toggle]').click(); return true; })()`);
  const expanded = await toggleByClick();
  check("点击分支行可展开", expanded.expanded === "true" && expanded.hidden === false,
    JSON.stringify(expanded));
  await page.eval(`(() => { document.querySelector('[data-tree-toggle]').click(); return true; })()`);
  const collapsed = await toggleByClick();
  check("再次点击可折叠", collapsed.expanded === "false" && collapsed.hidden === true, JSON.stringify(collapsed));

  // 键盘：Enter / Space / ← / →（从默认折叠状态开始）
  const focusFirst = () => page.eval(
    "(() => { const n = document.querySelector('[data-tree-toggle]'); n.focus(); return document.activeElement === n; })()");
  check("分支行可获得焦点", (await focusFirst()) === true);
  await page.pressKey("Enter", "Enter", 13);
  const kEnter = await toggleByClick();
  check("键盘 Enter 展开（默认折叠）", kEnter.expanded === "true" && kEnter.hidden === false, JSON.stringify(kEnter));
  await page.pressKey(" ", "Space", 32);
  const kSpace = await toggleByClick();
  check("键盘 Space 折叠", kSpace.expanded === "false" && kSpace.hidden === true, JSON.stringify(kSpace));
  await page.pressKey("ArrowRight", "ArrowRight", 39);
  const kRight = await toggleByClick();
  check("键盘 → 展开", kRight.expanded === "true" && kRight.hidden === false, JSON.stringify(kRight));
  await page.pressKey("ArrowLeft", "ArrowLeft", 37);
  const kLeft = await toggleByClick();
  check("键盘 ← 折叠", kLeft.expanded === "false" && kLeft.hidden === true, JSON.stringify(kLeft));

  // ---------- 全局 ----------
  console.log("\n-- 全局 --");
  check("页面无 JS 控制台错误 / 未捕获异常", page.pageErrors.length === 0,
    page.pageErrors.length ? `(${page.pageErrors.join(" | ")})` : "");

  // 三个视图都能渲染且互不干扰
  const views = [];
  for (const id of ["token", "session", "context"]) {
    await page.eval(`(() => { document.querySelector('[data-tab="${id}"]').click(); return true; })()`);
    views.push(JSON.parse(await page.eval(
      `JSON.stringify({ id: "${id}", len: document.getElementById('tab-content').innerHTML.length,
        selected: document.querySelector('[data-tab="${id}"]').getAttribute('aria-selected') })`)));
  }
  check("三个视图都能渲染且 aria-selected 正确",
    views.every((v) => v.len > 200 && v.selected === "true"), JSON.stringify(views));
  check("视图切换后仍无控制台错误", page.pageErrors.length === 0,
    page.pageErrors.length ? `(${page.pageErrors.join(" | ")})` : "");

  if (SHOT_DIR) {
    fs.mkdirSync(SHOT_DIR, { recursive: true });
    for (const w of [1200, 900, 720]) {
      await page.viewport(w, 2600);
      await page.eval(`(() => { document.querySelector('[data-tab="token"]').click(); return true; })()`);
      await page.screenshot(path.join(SHOT_DIR, `token-${w}.png`));
    }
    await page.viewport(1200, 2600);
    await page.eval(`(() => { document.querySelector('[data-tab="context"]').click(); return true; })()`);
    await page.screenshot(path.join(SHOT_DIR, "context-1200.png"));
    console.log(`\n  截图已写入 ${SHOT_DIR}（仅本项目合成数据，无真实库内容）`);
  }
} catch (err) {
  failed++;
  console.log(`  FAIL  几何测试执行中断: ${err && err.message}`);
} finally {
  await page.close();
  try { fs.rmSync(work, { recursive: true, force: true }); } catch { /* ignore */ }
}

console.log(`\n${failed === 0 ? "UI GEOMETRY OK" : "UI GEOMETRY FAILED: " + failed}`);
process.exit(failed === 0 ? 0 : 1);
