// usage-core.mjs — 读取并聚合 MiMo Desktop(mimocode) 的用量数据
// 数据源：~/.local/share/mimocode/mimocode.db
//   - message.data(JSON)：assistant 消息内含 tokens{input,output,reasoning,cache{read,write}},
//     modelID/providerID,time.created
//   - part.data(JSON)：type==="tool" 的工具调用
//   - session：标题、time_created/updated、summary_additions/deletions/files
import { DatabaseSync } from "node:sqlite";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const DEFAULT_PORT = 8790;

export function defaultDbPath() {
  return path.join(os.homedir(), ".local", "share", "mimocode", "mimocode.db");
}

// 优先实时只读打开；若被占用/无法打开则做一份一致性快照再读。
function openReadOnly(dbPath) {
  try {
    const db = new DatabaseSync(dbPath, { readOnly: true });
    db.prepare("SELECT count(*) AS c FROM sqlite_master").get();
    return db;
  } catch {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "mimo-usage-"));
    const base = path.basename(dbPath);
    for (const suffix of ["", "-wal", "-shm"]) {
      const src = dbPath + suffix;
      if (fs.existsSync(src)) fs.copyFileSync(src, path.join(dir, base + suffix));
    }
    return new DatabaseSync(path.join(dir, base), { readOnly: true });
  }
}

const pad2 = (n) => String(n).padStart(2, "0");
export function localDate(ms) {
  const d = new Date(ms);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

/**
 * 返回聚合后的用量数据：
 * { generatedAt, dbPath, totals, daily[], models[], sessions[] }
 * 其中 daily/models 携带按天数据，前端可据此按任意时间范围过滤。
 */
export function readUsage(dbPath = defaultDbPath()) {
  if (!fs.existsSync(dbPath)) {
    throw new Error(`找不到 MiMo 数据库：${dbPath}`);
  }
  const db = openReadOnly(dbPath);
  try {
    const dailyMap = new Map();
    const getDay = (date) => {
      let x = dailyMap.get(date);
      if (!x) {
        x = { date, tokens: 0, input: 0, output: 0, reasoning: 0, read: 0, write: 0, requests: 0, tools: 0, sessionIds: new Set() };
        dailyMap.set(date, x);
      }
      return x;
    };
    const modelMap = new Map();
    const getModel = (key, providerID, modelID) => {
      let m = modelMap.get(key);
      if (!m) {
        m = { providerID, modelID, name: modelID || key, daily: {}, dailyRead: {}, dailyInput: {}, tokens: 0, input: 0, output: 0, reasoning: 0, read: 0, write: 0, requests: 0 };
        modelMap.set(key, m);
      }
      return m;
    };
    const sessMap = new Map();
    const getSess = (id) => {
      let s = sessMap.get(id);
      if (!s) {
        s = { id, tokens: 0, requests: 0, tools: 0, providerID: null, modelID: null };
        sessMap.set(id, s);
      }
      return s;
    };
    const hourlyMap = new Map();
    const getHour = (dateStr, hour, ts) => {
      const key = `${dateStr} ${pad2(hour)}`;
      let x = hourlyMap.get(key);
      if (!x) {
        x = { key, date: dateStr, hour, ts, tokens: 0, requests: 0, tools: 0, sessionIds: new Set() };
        hourlyMap.set(key, x);
      }
      return x;
    };

    // ---- assistant 消息：token / 请求 / 模型 ----
    const msgRows = db.prepare("SELECT session_id, time_created, data FROM message").all();
    for (const row of msgRows) {
      let d;
      try { d = JSON.parse(row.data); } catch { continue; }
      if (d.role !== "assistant" || !d.tokens) continue;
      const t = d.tokens;
      const input = t.input || 0, output = t.output || 0, reasoning = t.reasoning || 0;
      const read = (t.cache && t.cache.read) || 0, write = (t.cache && t.cache.write) || 0;
      const tokens = input + output + reasoning + read + write;
      const ts = (d.time && d.time.created) || row.time_created;
      const date = localDate(ts);

      const day = getDay(date);
      day.tokens += tokens; day.input += input; day.output += output; day.reasoning += reasoning;
      day.read += read; day.write += write; day.requests++; day.sessionIds.add(row.session_id);

      const hourStart = new Date(ts); hourStart.setMinutes(0, 0, 0);
      const hr = getHour(date, hourStart.getHours(), hourStart.getTime());
      hr.tokens += tokens; hr.requests++; hr.sessionIds.add(row.session_id);

      const providerID = d.providerID || "unknown";
      const modelID = d.modelID || "unknown";
      const key = `${providerID}/${modelID}`;
      const m = getModel(key, providerID, modelID);
      m.daily[date] = (m.daily[date] || 0) + tokens;
      m.dailyRead[date] = (m.dailyRead[date] || 0) + read;
      m.dailyInput[date] = (m.dailyInput[date] || 0) + input;
      m.tokens += tokens; m.input += input; m.output += output; m.reasoning += reasoning;
      m.read += read; m.write += write; m.requests++;

      const s = getSess(row.session_id);
      s.tokens += tokens; s.requests++;
      if (!s.providerID) { s.providerID = providerID; s.modelID = modelID; }
    }

    // ---- 工具调用 ----
    const partRows = db.prepare("SELECT session_id, time_created, data FROM part").all();
    for (const row of partRows) {
      let d;
      try { d = JSON.parse(row.data); } catch { continue; }
      if (d.type !== "tool") continue;
      getDay(localDate(row.time_created)).tools++;
      getSess(row.session_id).tools++;
      const pts = row.time_created;
      if (pts) { const ph = new Date(pts); ph.setMinutes(0, 0, 0); getHour(localDate(pts), ph.getHours(), ph.getTime()).tools++; }
    }

    // ---- 会话表 ----
    const sessRows = db.prepare(
      "SELECT id,title,time_created,time_updated,summary_additions,summary_deletions,summary_files FROM session"
    ).all();
    const sessions = sessRows.map((r) => {
      const a = sessMap.get(r.id) || { tokens: 0, requests: 0, tools: 0, providerID: null, modelID: null };
      return {
        id: r.id,
        title: r.title || "(未命名)",
        timeCreated: r.time_created,
        timeUpdated: r.time_updated,
        additions: r.summary_additions || 0,
        deletions: r.summary_deletions || 0,
        files: r.summary_files || 0,
        tokens: a.tokens,
        requests: a.requests,
        tools: a.tools,
        providerID: a.providerID,
        modelID: a.modelID,
      };
    });
    sessions.sort((x, y) => y.timeUpdated - x.timeUpdated);

    const daily = [...dailyMap.values()]
      .map((x) => ({ ...x, sessions: x.sessionIds.size, sessionIds: [...x.sessionIds] }))
      .sort((x, y) => (x.date < y.date ? -1 : 1));
    const models = [...modelMap.values()]
      .map((m) => ({ ...m, cacheReadShare: m.input + m.read > 0 ? m.read / (m.input + m.read) : 0 }))
      .sort((x, y) => y.tokens - x.tokens);

    const hourly = [...hourlyMap.values()]
      .map((x) => ({ key: x.key, date: x.date, hour: x.hour, ts: x.ts, tokens: x.tokens, requests: x.requests, tools: x.tools, sessions: x.sessionIds.size }))
      .sort((x, y) => x.ts - y.ts);

    const totals = daily.reduce(
      (acc, x) => {
        acc.tokens += x.tokens; acc.input += x.input; acc.output += x.output; acc.reasoning += x.reasoning;
        acc.read += x.read; acc.write += x.write; acc.requests += x.requests; acc.tools += x.tools;
        return acc;
      },
      { tokens: 0, input: 0, output: 0, reasoning: 0, read: 0, write: 0, requests: 0, tools: 0 }
    );
    totals.sessions = sessions.length;

    return { generatedAt: Date.now(), dbPath, totals, daily, hourly, models, sessions };
  } finally {
    try { db.close(); } catch { /* noop */ }
  }
}

// 纯文本/ Markdown 摘要（供插件工具在对话里返回）
export function usageSummary(data, days = 30) {
  const all = !days || days <= 0;
  const fromDate = all ? "0000-00-00" : localDate(Date.now() - (days - 1) * 86400000);
  const inRange = (date) => date >= fromDate;
  const daily = data.daily.filter((d) => inRange(d.date));
  const sum = daily.reduce(
    (a, d) => {
      a.tokens += d.tokens; a.input += d.input; a.output += d.output; a.reasoning += d.reasoning;
      a.read += d.read; a.write += d.write; a.requests += d.requests; a.tools += d.tools;
      return a;
    },
    { tokens: 0, input: 0, output: 0, reasoning: 0, read: 0, write: 0, requests: 0, tools: 0 }
  );
  const fromMs = all ? 0 : Date.now() - (days - 1) * 86400000;
  const sessions = data.sessions.filter((s) => s.timeUpdated >= fromMs);
  const changes = sessions.reduce((a, s) => a + s.additions + s.deletions, 0);
  const files = sessions.reduce((a, s) => a + s.files, 0);

  const inp = sum.input + sum.read;
  const out = sum.output + sum.reasoning;
  const cacheShare = inp > 0 ? (sum.read / inp) * 100 : 0;
  const label = all ? "全部时间" : `最近 ${days} 天`;

  const models = data.models
    .map((m) => {
      let t = 0;
      for (const [date, v] of Object.entries(m.daily)) if (inRange(date)) t += v;
      return { ...m, rangeTokens: t };
    })
    .filter((m) => m.rangeTokens > 0)
    .sort((a, b) => b.rangeTokens - a.rangeTokens);
  const totalModel = models.reduce((a, m) => a + m.rangeTokens, 0) || 1;

  const fmt = (n) => {
    if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
    return String(Math.round(n));
  };

  const lines = [];
  lines.push(`## MiMo Desktop 用量（${label}）`);
  lines.push("");
  lines.push(`- **tokens 消耗**：${fmt(sum.tokens)}（输入 ${fmt(inp)} / 输出 ${fmt(out)}，缓存读取占比 ${cacheShare.toFixed(1)}%）`);
  lines.push(`- **会话**：${sessions.length}　**请求**：${sum.requests}　**工具**：${sum.tools}　**改动**：${fmt(changes)} 行 / ${files} 文件`);
  lines.push("");
  if (models.length) {
    lines.push("### 模型份额");
    for (const m of models.slice(0, 8)) {
      lines.push(`- ${m.name}（${m.providerID}）：${fmt(m.rangeTokens)}（${((m.rangeTokens / totalModel) * 100).toFixed(0)}%）`);
    }
    lines.push("");
  }
  lines.push(`_数据源：${data.dbPath}_`);
  return lines.join("\n");
}
