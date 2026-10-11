// 用 CDP 给本地 HTML 截图（沙箱里 chrome --screenshot 不可用时的替代方案）
// 用法: node test/shot.mjs <file.html> <out.png> [width] [height]
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import http from "node:http";

const [htmlPath, outPng, w = "1000", h = "2600"] = process.argv.slice(2);
if (!htmlPath || !outPng) {
  console.error("usage: node test/shot.mjs <file.html> <out.png> [w] [h]");
  process.exit(2);
}

const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9333;
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "shot-"));

const child = spawn(CHROME, [
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profile}`,
  "--headless=new", "--disable-gpu", "--no-sandbox",
  "--hide-scrollbars", "--allow-file-access-from-files",
  "--no-first-run", "--no-default-browser-check",
  `--window-size=${w},${h}`,
  "about:blank",
], { stdio: "ignore", detached: false });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const getJSON = (p) => new Promise((resolve, reject) => {
  http.get({ host: "127.0.0.1", port: PORT, path: p, timeout: 2000 }, (res) => {
    let s = ""; res.on("data", (c) => (s += c)); res.on("end", () => { try { resolve(JSON.parse(s)); } catch (e) { reject(e); } });
  }).on("error", reject);
});

let wsUrl = null;
for (let i = 0; i < 40; i++) {
  await sleep(250);
  try {
    const v = await getJSON("/json/version");
    wsUrl = v.webSocketDebuggerUrl;
    if (wsUrl) break;
  } catch { /* retry */ }
}
if (!wsUrl) { console.error("chrome CDP 未就绪"); child.kill(); process.exit(3); }

// 打开目标页（/json/new 需要 PUT；GET 会 405，这里直接用已有 about:blank 页导航）
const fileUrl = "file:///" + path.resolve(htmlPath).replace(/\\/g, "/");
const list = await getJSON("/json/list");
const page = (list || []).find((t) => t.type === "page");
if (!page) { console.error("找不到 page target"); child.kill(); process.exit(4); }

// 简易 WebSocket 客户端（Node 22 内置 WebSocket）
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0;
const pending = new Map();
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const mid = ++id;
  pending.set(mid, { resolve, reject });
  ws.send(JSON.stringify({ id: mid, method, params }));
});

await new Promise((r) => ws.addEventListener("open", r));
ws.addEventListener("message", (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
  }
});

await send("Page.enable");
await send("Page.navigate", { url: fileUrl });
await sleep(1500);
const res = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
fs.writeFileSync(outPng, Buffer.from(res.data, "base64"));
console.log(`shot ok: ${outPng}`);
ws.close();
child.kill();
process.exit(0);
