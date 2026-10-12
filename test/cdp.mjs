// test/cdp.mjs — 无头 Chrome + CDP 的最小封装
//
// 项目原有的 test/shot.mjs 已经用「起 chrome --remote-debugging-port + 连 WebSocket」
// 的方式截图；这里把那段样板抽出来，供几何/交互回归测试复用（不引入任何 npm 依赖，
// 只用到 Node 内置的 http / child_process / 全局 WebSocket）。
//
// 找不到 Chrome 时 findChrome() 返回 null，调用方应自行降级为 SKIP。

import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  process.env.MIMO_CHROME_PATH,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "/usr/bin/google-chrome",
  "/usr/bin/google-chrome-stable",
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
];

export function findChrome() {
  for (const p of CHROME_CANDIDATES) {
    if (!p) continue;
    try { if (fs.existsSync(p)) return p; } catch { /* ignore */ }
  }
  return null;
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function fileUrl(p) {
  return "file:///" + path.resolve(p).replace(/\\/g, "/");
}

/**
 * 启动无头 Chrome 并连到一个页面 target。
 * @returns {Promise<object|null>} null 表示本机没有可用 Chrome（调用方 SKIP）。
 */
export async function openPage({ width = 1280, height = 2600 } = {}) {
  const exe = findChrome();
  if (!exe) return null;

  const port = 9300 + Math.floor(Math.random() * 500);
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "mimo-cdp-"));
  const child = spawn(exe, [
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
    "--no-default-browser-check", "--hide-scrollbars", "--allow-file-access-from-files",
    "--disable-extensions", "--disable-background-networking",
    `--window-size=${width},${height}`,
    "about:blank",
  ], { stdio: "ignore" });

  const getJSON = (p) => new Promise((resolve, reject) => {
    http.get({ host: "127.0.0.1", port, path: p, timeout: 3000 }, (res) => {
      let s = "";
      res.on("data", (c) => (s += c));
      res.on("end", () => { try { resolve(JSON.parse(s)); } catch (e) { reject(e); } });
    }).on("error", reject);
  });

  const kill = () => { try { child.kill(); } catch { /* ignore */ } };
  const cleanup = () => {
    kill();
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch { /* ignore */ }
  };

  let page = null;
  for (let i = 0; i < 80 && !page; i++) {
    await sleep(250);
    try {
      const list = await getJSON("/json/list");
      page = (list || []).find((t) => t.type === "page") || null;
    } catch { /* chrome 还没起来，继续等 */ }
  }
  if (!page) { cleanup(); throw new Error("chrome CDP 未就绪（拿不到 page target）"); }

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let seq = 0;
  const pending = new Map();
  const listeners = [];

  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve);
    ws.addEventListener("error", () => reject(new Error("CDP WebSocket 连接失败")));
  });
  ws.addEventListener("message", (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(msg.method + ": " + JSON.stringify(msg.error)));
      else resolve(msg.result);
      return;
    }
    for (const fn of listeners) fn(msg);
  });

  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++seq;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });

  const on = (fn) => { listeners.push(fn); };

  // 收集页面自身的 console 错误与未捕获异常，用于「不引入 JS 控制台错误」的断言。
  const pageErrors = [];
  on((msg) => {
    if (msg.method === "Runtime.exceptionThrown") {
      const d = msg.params && msg.params.exceptionDetails;
      pageErrors.push("exception: " + ((d && d.exception && d.exception.description) || (d && d.text) || "unknown"));
    } else if (msg.method === "Runtime.consoleAPICalled" && msg.params && msg.params.type === "error") {
      pageErrors.push("console.error: " + (msg.params.args || []).map((a) => a.value ?? a.description ?? "").join(" "));
    }
  });

  await send("Page.enable");
  await send("Runtime.enable");
  await send("Emulation.setDeviceMetricsOverride", {
    width, height, deviceScaleFactor: 1, mobile: false,
  });

  const api = {
    send,
    pageErrors,
    async viewport(w, h = height) {
      await send("Emulation.setDeviceMetricsOverride", {
        width: w, height: h, deviceScaleFactor: 1, mobile: false,
      });
      await sleep(160);
    },
    async goto(url, settleMs = 700) {
      await send("Page.navigate", { url });
      await sleep(settleMs);
    },
    async eval(expression) {
      const r = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
      if (r.exceptionDetails) {
        const d = r.exceptionDetails;
        throw new Error("evaluate 抛错: " + ((d.exception && d.exception.description) || d.text));
      }
      return r.result ? r.result.value : undefined;
    },
    async click(x, y) {
      for (const type of ["mousePressed", "mouseReleased"]) {
        await send("Input.dispatchMouseEvent", {
          type, x, y, button: "left", clickCount: 1,
        });
      }
      await sleep(80);
    },
    async moveMouse(x, y) {
      await send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "none" });
      await sleep(80);
    },
    async pressKey(key, code, vk) {
      const base = { key, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk };
      await send("Input.dispatchKeyEvent", { type: "keyDown", ...base, text: key === " " ? " " : undefined });
      await send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
      await sleep(80);
    },
    async screenshot(outPng) {
      const r = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
      fs.writeFileSync(outPng, Buffer.from(r.data, "base64"));
    },
    async close() {
      try { await send("Page.close"); } catch { /* ignore */ }
      try { ws.close(); } catch { /* ignore */ }
      cleanup();
    },
  };
  return api;
}
