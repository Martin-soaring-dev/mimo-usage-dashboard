// server.mjs — 本地用量仪表盘服务（仅监听 127.0.0.1）
// 用法：node server.mjs [--port 8790] [--db <path>] [--open]
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { readUsage, usageSummary, DEFAULT_PORT, defaultDbPath } from "./usage-core.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const argv = process.argv.slice(2);
const argVal = (name, def) => {
  const i = argv.indexOf(name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : def;
};

const PORT = Number.parseInt(argVal("--port", String(DEFAULT_PORT)), 10);
const DB = argVal("--db", defaultDbPath());
const OPEN = argv.includes("--open");

function openBrowser(url) {
  try {
    if (process.platform === "win32") spawn("cmd", ["/c", "start", "", url], { detached: true, stdio: "ignore" }).unref();
    else if (process.platform === "darwin") spawn("open", [url], { detached: true, stdio: "ignore" }).unref();
    else spawn("xdg-open", [url], { detached: true, stdio: "ignore" }).unref();
  } catch { /* noop */ }
}

function json(res, code, obj) {
  const body = Buffer.from(JSON.stringify(obj), "utf8");
  res.writeHead(code, { "content-type": "application/json; charset=utf-8", "content-length": body.length });
  res.end(body);
}

const server = http.createServer((req, res) => {
  let url;
  try { url = new URL(req.url, `http://127.0.0.1:${PORT}`); } catch { res.writeHead(400); return res.end("bad request"); }

  if (url.pathname === "/api/ping") return json(res, 200, { ok: true, port: PORT, db: DB });

  if (url.pathname === "/api/usage") {
    try { return json(res, 200, readUsage(DB)); }
    catch (e) { return json(res, 500, { error: e instanceof Error ? e.message : String(e) }); }
  }

  if (url.pathname === "/api/summary") {
    try {
      const days = Number.parseInt(url.searchParams.get("days") || "30", 10);
      const data = readUsage(DB);
      const text = url.searchParams.get("format") === "json"
        ? JSON.stringify({ summary: usageSummary(data, days) })
        : usageSummary(data, days);
      res.writeHead(200, { "content-type": "text/plain; charset=utf-8" });
      return res.end(text);
    } catch (e) { res.writeHead(500); return res.end("error: " + (e instanceof Error ? e.message : String(e))); }
  }

  if (url.pathname === "/" || url.pathname === "/index.html") {
    try {
      const html = fs.readFileSync(path.join(__dirname, "dashboard.html"));
      res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
      return res.end(html);
    } catch (e) { res.writeHead(500); return res.end("dashboard.html 读取失败: " + e.message); }
  }

  res.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
  res.end("not found");
});

server.on("error", (e) => {
  console.error(`[usage-dashboard] 监听失败：${e.message}`);
  process.exit(1);
});

server.listen(PORT, "127.0.0.1", () => {
  const url = `http://127.0.0.1:${PORT}`;
  console.log(`[usage-dashboard] ${url}  (db: ${DB})`);
  if (OPEN) openBrowser(url);
});
