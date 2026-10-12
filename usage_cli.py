#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""usage_cli.py — MiMo Desktop 本机用量统计（确定性本地命令，零 AI 参与）

与旧版（Node 插件 + 常驻 HTTP 服务）不同，本脚本是**纯离线命令行程序**：
  读库 → 聚合 → 填充自包含 HTML 模板 → 写出静态文件（可选打开浏览器）。

不启动任何服务、不监听任何端口、不联网、不写数据库。

用法（通常由 MiMo 的 `command` 字段以 `` !`python usage_cli.py ...` `` 触发）：

    python usage_cli.py                 # 生成总览（token + session + context 三合一）
    python usage_cli.py token           # 只生成 Token 用量页
    python usage_cli.py session         # 只生成 Session 用量页
    python usage_cli.py context         # 只生成上下文占用页
    python usage_cli.py token --days 7  # 最近 7 天
    python usage_cli.py --open          # 生成后用默认浏览器打开
    python usage_cli.py --out D:/x.html # 指定输出路径

仅依赖 Python 标准库（sqlite3 / json / html / argparse）。
"""

from __future__ import annotations

import argparse
import html as _html
import json
import os
import sqlite3
import sys
import webbrowser
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_PORT = 8790  # 仅为兼容旧配置保留，本脚本不再使用

# 已知模型的上下文窗口（tokens）。取自 MiMo 引擎内置模型表；DB 内不存该值。
# 匹配规则：精确 id 优先，其次最长前缀（处理 -0731 / -preview 等后缀变体）。
MODEL_CONTEXT_LIMITS: dict[str, int] = {
    "mimo-v2.6-pro": 1048576,
    "mimo-v2.6-flash": 1048576,
    "mimo-v2.5": 1048576,
    "mimo-v2-pro": 1048576,
    "mimo-v2-flash": 262144,
    "deepseek-v4.1-flash": 131072,
    "deepseek-v4-flash": 131072,
    "deepseek-v4-flash-0731": 131072,
    "deepseek-flash": 131072,
    "glm-5.3": 1000000,
    "claude-sonnet-4-6": 200000,
    "claude-opus-4-8": 200000,
}

KINDS = ("all", "token", "session", "context")

KIND_TITLE = {
    "all": "MiMo Desktop 用量总览",
    "token": "MiMo Desktop Token 用量",
    "session": "MiMo Desktop Session 用量",
    "context": "MiMo Desktop 上下文占用",
}


def default_db_path() -> str:
    """数据源：~/.local/share/mimocode/mimocode.db（可用 MIMO_USAGE_DB 覆盖）。"""
    env = os.environ.get("MIMO_USAGE_DB")
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), ".local", "share", "mimocode", "mimocode.db")


def model_context_limit(model_id: str | None) -> int | None:
    if not model_id:
        return None
    mid = str(model_id).lower()
    if mid in MODEL_CONTEXT_LIMITS:
        return MODEL_CONTEXT_LIMITS[mid]
    best, best_len = None, 0
    for key, val in MODEL_CONTEXT_LIMITS.items():
        if mid.startswith(key) and len(key) > best_len:
            best, best_len = val, len(key)
    return best


# ---------------------------------------------------------------------------
# 数据库（只读；被占用时做一致性快照再读）
# ---------------------------------------------------------------------------


def _open_readonly(db_path: str) -> sqlite3.Connection:
    """优先以只读 URI 打开；失败则复制一份快照（含 -wal/-shm）再读。"""
    uri = "file:" + db_path.replace("\\", "/").replace("?", "%3f").replace("#", "%23") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn
    except sqlite3.Error:
        import shutil
        import tempfile

        tmp = tempfile.mkdtemp(prefix="mimo-usage-")
        base = os.path.basename(db_path)
        for suffix in ("", "-wal", "-shm"):
            src = db_path + suffix
            if os.path.exists(src):
                shutil.copyfile(src, os.path.join(tmp, base + suffix))
        conn = sqlite3.connect(os.path.join(tmp, base), timeout=1.0)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn


def _loads(raw):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _local_date(ms: int) -> str:
    d = datetime.fromtimestamp(ms / 1000.0)
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"


# ---------------------------------------------------------------------------
# 聚合：全量用量（daily / hourly / models / sessions）
# ---------------------------------------------------------------------------


def read_usage(db_path: str) -> dict:
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"找不到 MiMo 数据库：{db_path}")

    conn = _open_readonly(db_path)
    try:
        daily: dict[str, dict] = {}
        hourly: dict[str, dict] = {}
        models: dict[str, dict] = {}
        sess_agg: dict[str, dict] = {}
        # 会话 → 所属模型 key（用于把工具调用归属到模型维度）
        session_model: dict[str, str] = {}

        def day(d: str) -> dict:
            x = daily.get(d)
            if x is None:
                x = {
                    "date": d, "tokens": 0, "input": 0, "output": 0, "reasoning": 0,
                    "read": 0, "write": 0, "requests": 0, "tools": 0, "sessionIds": set(),
                }
                daily[d] = x
            return x

        def hour(d: str, h: int, ts: int) -> dict:
            key = f"{d} {h:02d}"
            x = hourly.get(key)
            if x is None:
                x = {"key": key, "date": d, "hour": h, "ts": ts,
                     "tokens": 0, "requests": 0, "tools": 0, "sessionIds": set()}
                hourly[key] = x
            return x

        def model(key: str, provider_id: str, model_id: str) -> dict:
            m = models.get(key)
            if m is None:
                m = {
                    "providerID": provider_id, "modelID": model_id, "name": model_id or key,
                    "daily": {}, "dailyRead": {}, "dailyInput": {}, "dailyReq": {}, "dailyTools": {},
                    "tokens": 0, "input": 0, "output": 0, "reasoning": 0,
                    "read": 0, "write": 0, "requests": 0, "tools": 0,
                }
                models[key] = m
            return m

        def sess(sid: str) -> dict:
            s = sess_agg.get(sid)
            if s is None:
                s = {"id": sid, "tokens": 0, "requests": 0, "tools": 0,
                     "providerID": None, "modelID": None}
                sess_agg[sid] = s
            return s

        # ---- assistant 消息：token / 请求 / 模型 ----
        for row in conn.execute("SELECT session_id, time_created, data FROM message"):
            d = _loads(row[2])
            if not isinstance(d, dict) or d.get("role") != "assistant":
                continue
            t = d.get("tokens")
            if not t:
                continue
            inp = int(t.get("input") or 0)
            out = int(t.get("output") or 0)
            rea = int(t.get("reasoning") or 0)
            cache = t.get("cache") or {}
            rd = int(cache.get("read") or 0)
            wr = int(cache.get("write") or 0)
            toks = inp + out + rea + rd + wr
            ts = int((d.get("time") or {}).get("created") or row[1])
            ds = _local_date(ts)

            dd = day(ds)
            dd["tokens"] += toks; dd["input"] += inp; dd["output"] += out
            dd["reasoning"] += rea; dd["read"] += rd; dd["write"] += wr
            dd["requests"] += 1; dd["sessionIds"].add(row[0])

            hh = hour(ds, datetime.fromtimestamp(ts / 1000.0).hour,
                      int(datetime.fromtimestamp(ts / 1000.0).replace(minute=0, second=0, microsecond=0).timestamp() * 1000))
            hh["tokens"] += toks; hh["requests"] += 1; hh["sessionIds"].add(row[0])

            pid = d.get("providerID") or "unknown"
            mid = d.get("modelID") or "unknown"
            key = f"{pid}/{mid}"
            m = model(key, pid, mid)
            m["daily"][ds] = m["daily"].get(ds, 0) + toks
            m["dailyRead"][ds] = m["dailyRead"].get(ds, 0) + rd
            m["dailyInput"][ds] = m["dailyInput"].get(ds, 0) + inp
            m["dailyReq"][ds] = m["dailyReq"].get(ds, 0) + 1
            m["tokens"] += toks; m["input"] += inp; m["output"] += out
            m["reasoning"] += rea; m["read"] += rd; m["write"] += wr; m["requests"] += 1
            session_model[row[0]] = key

            s = sess(row[0])
            s["tokens"] += toks; s["requests"] += 1
            if not s["providerID"]:
                s["providerID"], s["modelID"] = pid, mid

        # ---- 工具调用 ----
        for row in conn.execute("SELECT session_id, time_created, data FROM part"):
            d = _loads(row[2])
            if not isinstance(d, dict) or d.get("type") != "tool":
                continue
            ts = int(row[1] or 0)
            day(_local_date(ts))["tools"] += 1
            sess(row[0])["tools"] += 1
            mk = session_model.get(row[0])
            if mk is not None:
                models[mk]["tools"] += 1
                models[mk]["dailyTools"][_local_date(ts)] = \
                    models[mk]["dailyTools"].get(_local_date(ts), 0) + 1
            if ts:
                dtt = datetime.fromtimestamp(ts / 1000.0)
                hour(_local_date(ts), dtt.hour,
                     int(dtt.replace(minute=0, second=0, microsecond=0).timestamp() * 1000))["tools"] += 1

        # ---- 会话表 ----
        sessions = []
        for r in conn.execute(
            "SELECT id,title,time_created,time_updated,summary_additions,"
            "summary_deletions,summary_files FROM session"
        ):
            a = sess_agg.get(r[0]) or {"tokens": 0, "requests": 0, "tools": 0,
                                       "providerID": None, "modelID": None}
            sessions.append({
                "id": r[0],
                "title": r[1] or "(未命名)",
                "timeCreated": r[2],
                "timeUpdated": r[3],
                "additions": r[4] or 0,
                "deletions": r[5] or 0,
                "files": r[6] or 0,
                "tokens": a["tokens"],
                "requests": a["requests"],
                "tools": a["tools"],
                "providerID": a["providerID"],
                "modelID": a["modelID"],
            })
        sessions.sort(key=lambda s: s["timeUpdated"] or 0, reverse=True)

        daily_list = []
        for x in sorted(daily.values(), key=lambda v: v["date"]):
            y = dict(x)
            y["sessions"] = len(x["sessionIds"])
            y["sessionIds"] = sorted(x["sessionIds"])
            daily_list.append(y)

        model_list = []
        for m in models.values():
            denom = m["input"] + m["read"]
            y = dict(m)
            y["cacheReadShare"] = (m["read"] / denom) if denom > 0 else 0.0
            model_list.append(y)
        model_list.sort(key=lambda m: m["tokens"], reverse=True)

        hourly_list = []
        for x in hourly.values():
            y = dict(x)
            y["sessions"] = len(x["sessionIds"])
            del y["sessionIds"]
            hourly_list.append(y)
        hourly_list.sort(key=lambda v: v["ts"])

        totals = {"tokens": 0, "input": 0, "output": 0, "reasoning": 0,
                  "read": 0, "write": 0, "requests": 0, "tools": 0}
        for x in daily_list:
            for k in ("tokens", "input", "output", "reasoning", "read", "write", "requests", "tools"):
                totals[k] += x[k]
        totals["sessions"] = len(sessions)

        return {
            "generatedAt": int(datetime.now().timestamp() * 1000),
            "dbPath": db_path,
            "totals": totals,
            "daily": daily_list,
            "hourly": hourly_list,
            "models": model_list,
            "sessions": sessions,
        }
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


# ---------------------------------------------------------------------------
# 聚合：单会话上下文占用
# ---------------------------------------------------------------------------


def session_context(db_path: str, session_id: str | None = None, limit: int | None = None) -> dict:
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"找不到 MiMo 数据库：{db_path}")

    conn = _open_readonly(db_path)
    try:
        sid = session_id
        if sid:
            if conn.execute("SELECT id FROM session WHERE id=?", (sid,)).fetchone() is None:
                sid = None
        if not sid:
            r = conn.execute("SELECT id FROM session ORDER BY time_updated DESC LIMIT 1").fetchone()
            sid = r[0] if r else None
        if not sid:
            return {"ok": False, "error": "没有找到任何会话"}

        srow = conn.execute(
            "SELECT id,title,directory,time_created,time_updated,summary_additions,"
            "summary_deletions,summary_files,context_watermark,time_compacting,"
            "last_checkpoint_message_id FROM session WHERE id=?", (sid,)
        ).fetchone()
        if srow is None:
            return {"ok": False, "error": f"会话不存在：{sid}"}

        agg = {"total": 0, "input": 0, "output": 0, "reasoning": 0, "read": 0, "write": 0}
        messages = 0
        last_msg_tokens = None
        provider_id = model_id = None
        last_activity = int(srow[3] or 0)

        for r in conn.execute(
            "SELECT time_created, data FROM message WHERE session_id=? ORDER BY time_created ASC",
            (sid,),
        ):
            d = _loads(r[1])
            if not isinstance(d, dict) or d.get("role") != "assistant":
                continue
            messages += 1
            last_activity = max(last_activity, int(r[0] or 0))
            if d.get("providerID") and not provider_id:
                provider_id, model_id = d.get("providerID"), d.get("modelID")
            t = d.get("tokens")
            if t:
                inp = int(t.get("input") or 0); out = int(t.get("output") or 0)
                rea = int(t.get("reasoning") or 0)
                cache = t.get("cache") or {}
                rd = int(cache.get("read") or 0); wr = int(cache.get("write") or 0)
                total = int(t["total"]) if isinstance(t.get("total"), (int, float)) else inp + out + rea + rd + wr
                agg["total"] += total; agg["input"] += inp; agg["output"] += out
                agg["reasoning"] += rea; agg["read"] += rd; agg["write"] += wr
                if isinstance(t.get("total"), (int, float)):
                    last_msg_tokens = t

        tools = 0
        steps = 0
        last_step_tokens = None
        for r in conn.execute(
            "SELECT time_created, data FROM part WHERE session_id=? ORDER BY time_created ASC",
            (sid,),
        ):
            d = _loads(r[1])
            if not isinstance(d, dict):
                continue
            if d.get("type") == "tool":
                tools += 1
            elif d.get("type") == "step-finish":
                steps += 1
                t = d.get("tokens")
                if isinstance(t, dict) and isinstance(t.get("total"), (int, float)):
                    last_step_tokens = t
            last_activity = max(last_activity, int(r[0] or 0))

        ctx = last_step_tokens or last_msg_tokens
        used = int(ctx["total"]) if (ctx and isinstance(ctx.get("total"), (int, float))) else 0
        resolved_limit = limit if (limit and limit > 0) else model_context_limit(model_id)

        detail = None
        if ctx:
            cache = ctx.get("cache") or {}
            detail = {
                "total": int(ctx.get("total") or 0),
                "input": int(ctx.get("input") or 0),
                "output": int(ctx.get("output") or 0),
                "reasoning": int(ctx.get("reasoning") or 0),
                "read": int(cache.get("read") or 0),
                "write": int(cache.get("write") or 0),
            }

        return {
            "ok": True,
            "sessionId": sid,
            "title": srow[1] or "(未命名)",
            "directory": srow[2] or "",
            "timeCreated": srow[3],
            "timeUpdated": srow[4],
            "lastActivity": last_activity,
            "model": {"providerID": provider_id or "unknown", "modelID": model_id or "unknown"},
            "messages": messages, "steps": steps, "tools": tools,
            "additions": srow[5] or 0, "deletions": srow[6] or 0, "files": srow[7] or 0,
            "tokens": agg,
            "context": {
                "used": used,
                "limit": resolved_limit,
                "ratio": (used / resolved_limit) if resolved_limit else None,
                "source": "step-finish" if last_step_tokens else ("message" if last_msg_tokens else "none"),
                "detail": detail,
            },
            "watermark": srow[8], "compacting": srow[9], "lastCheckpoint": srow[10],
        }
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


# ---------------------------------------------------------------------------
# 组装卡片数据（与旧版 cardData 同构，便于模板共用）
# ---------------------------------------------------------------------------


def card_data(db_path: str, kind: str = "all", days: int = 30,
              session_id: str | None = None, limit: int | None = None) -> dict:
    kind = kind if kind in KINDS else "all"
    days = days if (isinstance(days, int) and days > 0) else 30
    data = read_usage(db_path)
    out = {"kind": kind, "generatedAt": int(datetime.now().timestamp() * 1000), "dbPath": db_path}

    if kind in ("token", "all"):
        from_date = _local_date(int((datetime.now() - timedelta(days=days - 1)).timestamp() * 1000))
        daily = [d for d in data["daily"] if d["date"] >= from_date]
        sum_ = {"tokens": 0, "input": 0, "output": 0, "reasoning": 0,
                "read": 0, "write": 0, "requests": 0, "tools": 0}
        for d in daily:
            for k in ("tokens", "input", "output", "reasoning", "read", "write", "requests", "tools"):
                sum_[k] += d[k]
        inp = sum_["input"] + sum_["read"]
        out_tok = sum_["output"] + sum_["reasoning"]
        mlist = []
        for m in data["models"]:
            t = sum(v for k, v in m["daily"].items() if k >= from_date)
            if t <= 0:
                continue
            rd = sum(v for k, v in (m.get("dailyRead") or {}).items() if k >= from_date)
            ix = sum(v for k, v in (m.get("dailyInput") or {}).items() if k >= from_date)
            rq = sum(v for k, v in (m.get("dailyReq") or {}).items() if k >= from_date)
            tl = sum(v for k, v in (m.get("dailyTools") or {}).items() if k >= from_date)
            mlist.append({"name": m["name"], "providerID": m["providerID"],
                          "tokens": t, "read": rd, "input": ix,
                          "requests": rq, "tools": tl})
        mlist.sort(key=lambda m: m["tokens"], reverse=True)
        total_model = sum(m["tokens"] for m in mlist) or 1
        out["token"] = {
            "days": days,
            "totals": {
                # 注意字段语义：input/output 是**原始值**（不含缓存），
                # cacheRead 单列；JS 端 tokenCard 按同名取用。
                "tokens": sum_["tokens"],
                "input": sum_["input"], "output": sum_["output"],
                "reasoning": sum_["reasoning"],
                "cacheRead": sum_["read"], "cacheWrite": sum_["write"],
                "requests": sum_["requests"], "tools": sum_["tools"],
                "cacheShare": (sum_["read"] / inp) if inp > 0 else 0.0,
                # 与 session.total 用同一表达式：会话数是全量会话行数（不受 --days 影响），
                # 这样 Token 视图的「会话数」与 Session 视图的「会话总数」不会漂移。
                "sessions": len(data["sessions"]),
            },
            "totalsAll": data["totals"],
            "models": [{**m, "share": m["tokens"] / total_model} for m in mlist[:8]],
            # 热力图覆盖最近 12 个月，不受 --days 影响
            "daily": [{"date": d["date"], "tokens": d["tokens"]} for d in data["daily"]],
            # 顶部迷你趋势仍按 --days 范围
            "recent": [{"date": d["date"], "tokens": d["tokens"]} for d in daily],
        }

    if kind in ("session", "all"):
        out["session"] = {
            "total": len(data["sessions"]),
            "totals": {"tokens": data["totals"]["tokens"],
                       "requests": data["totals"]["requests"],
                       "tools": data["totals"]["tools"]},
            "changes": sum((s["additions"] + s["deletions"]) for s in data["sessions"]),
            "files": sum(s["files"] for s in data["sessions"]),
            "top": [{
                "id": s["id"], "title": s["title"], "modelID": s["modelID"] or "-",
                "providerID": s["providerID"] or "", "tokens": s["tokens"],
                "requests": s["requests"], "tools": s["tools"],
                "additions": s["additions"], "deletions": s["deletions"],
                "files": s["files"], "timeUpdated": s["timeUpdated"],
            } for s in data["sessions"][:10]],
        }

    if kind in ("context", "all"):
        out["context"] = session_context(db_path, session_id=session_id, limit=limit)

    return out


# ---------------------------------------------------------------------------
# 渲染：自包含 HTML
# ---------------------------------------------------------------------------

_STYLE = """
:root{
  --bg:#f5f6f8; --panel:#ffffff; --panel2:#f7f8fa; --border:#e8eaed; --text:#1f2329;
  --muted:#8a9099; --accent:#3b6ef6; --accent-soft:#eaf0ff; --track:#eef0f3;
  --good:#16a34a; --warn:#d97706; --bad:#dc2626;
  --shadow:0 1px 2px rgba(16,24,40,.04),0 1px 3px rgba(16,24,40,.06);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--text);
  font:13.5px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",Roboto,Helvetica,Arial,sans-serif}
.page{max-width:960px;margin:0 auto;padding:26px 20px 60px}
.head{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin-bottom:18px;flex-wrap:wrap}
.head h1{font-size:21px;margin:0;font-weight:730;letter-spacing:-.2px}
.head .sub{color:var(--muted);font-size:12px;margin-top:4px}
.head .acts{display:flex;gap:8px;align-items:center}
.btn{font:inherit;font-size:12px;color:var(--accent);background:var(--accent-soft);
  border:0;border-radius:999px;padding:6px 14px;cursor:pointer;text-decoration:none;display:inline-block}
.btn:hover{filter:brightness(.96)}
.tabs{display:flex;gap:6px;padding:5px;background:var(--panel2);border:1px solid var(--border);border-radius:12px;margin:0 0 14px;overflow-x:auto}
.tab{flex:1;min-width:max-content;border:0;border-radius:8px;padding:9px 14px;background:transparent;color:var(--muted);font:inherit;font-weight:600;cursor:pointer}
.tab[aria-selected="true"]{background:var(--panel);color:var(--accent);box-shadow:var(--shadow)}
.tab:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.wrap{padding:0}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow);padding:16px 18px}
.hero{padding:20px 22px;background:linear-gradient(180deg,var(--accent-soft),var(--panel))}
.label{color:var(--muted);font-size:11.5px;margin-bottom:5px}
.big{font-size:40px;font-weight:760;letter-spacing:-.8px;line-height:1}
.big span{font-size:14px;font-weight:600;color:var(--muted);margin-left:7px}
.num{font-size:22px;font-weight:720}
.lb{color:var(--muted);font-size:11.5px;margin-top:3px}
.muted{color:var(--muted)}
.small{font-size:11.5px}
h3{font-size:12px;margin:0 0 10px;font-weight:650;color:var(--muted);letter-spacing:.35px}
.bar{height:10px;border-radius:6px;background:var(--track);overflow:hidden;display:flex}
.bar i{display:block;height:100%}
/* 缓存命中率分层（仅模型行使用）：命中=浅蓝（大面积），未命中=橙色（警示）
   早期用 opacity 区分，在浅色卡片上会淡到不可见；这里改用实色。 */
.cm-hit-hit{background:#8fb0fb}   /* 命中：浅蓝 */
.cm-hit-miss{background:#f59e0b}  /* 未命中（新输入）：橙，警示 */
/* 上下文构成用独立配色（语义与命中率不同，不复用上面的类） */
.ctx-cache{background:#8fb0fb}  /* 缓存读取 */
.ctx-input{background:#3b6ef6}   /* 新输入 */
.ctx-reason{background:#a78bfa}  /* 推理 */
.ctx-output{background:#34d399}  /* 输出 */
.mrow{display:grid;grid-template-columns:1fr auto;gap:4px 12px;align-items:center;padding:11px 0;border-bottom:1px solid var(--border)}
.mrow:last-child{border-bottom:0}
.mname{font-weight:600;font-size:13px}
.msub{color:var(--muted);font-size:11px;margin-top:3px}
.mval{font-weight:700;color:var(--accent);font-size:13px;align-self:start;padding-top:1px}
/* 模型行：份额条（单色）+ 命中率条（分层），两条独立，避免语义混淆 */
.track2{height:6px;border-radius:4px;background:var(--track);overflow:hidden;margin-top:7px}
.track2>i{display:block;height:100%;background:var(--accent);border-radius:4px}
.track3{height:6px;border-radius:4px;background:var(--track);overflow:hidden;margin-top:4px;display:flex}
.track3>i{display:block;height:100%;flex:0 0 auto;min-width:0}
.track3>i.cm-hit-hit{border-radius:4px 0 0 4px}   /* 命中段在左 */
.track3>i.cm-hit-miss{border-radius:0 4px 4px 0}  /* 未命中段在右 */
.track3>i:only-child{border-radius:4px}
.legend{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:11px;margin-bottom:8px;flex-wrap:wrap}
.dot{width:9px;height:9px;border-radius:3px;display:inline-block}
/* ---- 日历热力图 ----
   格子用 aspect-ratio 保持正方形；列数由 grid 均分铺满卡片宽度。
   --cell 只用来对齐左侧星期标签的行高。 */
.hm{display:flex;gap:8px;align-items:flex-start;padding:2px 0 0}
.hm .dow{display:flex;flex-direction:column;gap:var(--gap);flex:none;
  color:var(--muted);font-size:9px;padding-top:22px;width:12px}
.hm .dow span{line-height:var(--cell);display:block}
.hm .cols{flex:1;min-width:0;overflow-x:auto;overflow-y:hidden;padding-bottom:2px}
/* 窄屏时保持最小格子宽度，改为横向滚动，而不是把格子压成细条 */
.hm .grid,.hm .months-grid{min-width:var(--minw,560px)}
.hm .months-grid{display:grid;gap:var(--gap);color:var(--muted);font-size:9.5px;
  margin-bottom:4px;line-height:14px}
.hm .months-grid div{white-space:nowrap;overflow:hidden}
.hm .grid{display:grid;gap:var(--gap);aspect-ratio:auto}
.hm .col{display:flex;flex-direction:column;gap:var(--gap)}
.hm .cell{width:100%;aspect-ratio:1/1;border-radius:3px;background:var(--track);
  transition:transform .08s ease;max-height:20px}
.hm .cell:hover{transform:scale(1.35);outline:1px solid var(--accent);outline-offset:1px;position:relative;z-index:2}
.hm .cell[data-lv="1"]{background:#dbe6fe}
.hm .cell[data-lv="2"]{background:#a8c2fb}
.hm .cell[data-lv="3"]{background:#6b93f8}
.hm .cell[data-lv="4"]{background:#2f57d6}
.hm .legend{margin:10px 0 0;justify-content:flex-end;gap:4px}
.hm .legend i{width:11px;height:11px;border-radius:3px;display:inline-block}
.ctx-ring{display:flex;align-items:center;gap:18px}
.ring{width:110px;height:110px;flex:none}
.stat-line{display:flex;justify-content:space-between;gap:14px;padding:7px 0;border-bottom:1px dashed var(--border);font-size:12.5px}
.stat-line:last-child{border-bottom:0}
.stat-line b{font-weight:650;text-align:right}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--border);white-space:nowrap}
th{color:var(--muted);font-weight:500}
td.tt{max-width:260px;overflow:hidden;text-overflow:ellipsis}
.right{text-align:right}
/* 表格容器：窄屏时横向滚动而不是把列挤扁 */
.tw{overflow-x:auto;-webkit-overflow-scrolling:touch}
.spark{display:flex;align-items:flex-end;gap:2px;height:40px;margin-top:10px}
.spark i{flex:1;background:var(--accent);opacity:.55;border-radius:2px 2px 0 0;min-height:2px}
.foot{color:var(--muted);font-size:11px;padding:18px 2px 0;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
.err{margin:0 0 16px;padding:14px 16px;border-radius:12px;background:#fff2f0;border:1px solid #ffccc7;color:#a8071a;font-size:12.5px}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;background:var(--accent-soft);color:var(--accent);font-size:10.5px;font-weight:600}
.tip{background:var(--panel2);border:1px dashed var(--border);border-radius:10px;padding:11px 14px;color:var(--muted);font-size:11.5px;margin-bottom:16px}
@media(max-width:720px){
  .grid4{grid-template-columns:repeat(2,1fr)}
  .grid2{grid-template-columns:1fr}
  .page{padding:16px 12px 40px}
  .big{font-size:32px}
  .mrow{gap:2px 8px}
}
"""

_RUNTIME = """
var fmt = function(n){
  n = Number(n) || 0;
  if (n >= 1e9) return (n/1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n/1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n/1e3).toFixed(1) + "K";
  return String(Math.round(n));
};
var fmtInt = function(n){ return Number(n||0).toLocaleString("en-US"); };
var pct = function(x){ return (isFinite(x) ? (x*100).toFixed(1) : "0.0") + "%"; };
var esc = function(s){ return String(s==null?"":s).replace(/[&<>"]/g, function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]; }); };
var timeCN = function(ms){ try { return new Date(ms).toLocaleString("zh-CN",{hour12:false}); } catch(e){ return "-"; } };
var dateCN = function(ms){ try { return new Date(ms).toLocaleDateString("zh-CN"); } catch(e){ return "-"; } };
// 本地日期 → "YYYY-MM-DD"（不能用 toISOString，那是 UTC，会差一天）
var ds = function(d){
  var m = d.getMonth()+1, day = d.getDate();
  return d.getFullYear() + "-" + (m<10?"0":"") + m + "-" + (day<10?"0":"") + day;
};

// 12 个月日历热力图（GitHub 风格）。daily 只覆盖有数据的日子，其余补 0。
function heatmapCard(t){
  var daily = t.daily || [];
  var map = {};
  daily.forEach(function(d){ map[d.date] = d; });

  // 分位数分档（只对 >0 的值取分位，避免大量 0 拉低阈值）
  var vals = daily.map(function(d){ return d.tokens; }).filter(function(v){ return v > 0; })
                  .sort(function(a,b){ return a-b; });
  var q = function(p){ return vals.length ? vals[Math.min(vals.length-1, Math.floor(vals.length*p))] : 0; };
  var t1 = q(0.25), t2 = q(0.5), t3 = q(0.75);
  var level = function(v){
    if (!v || v <= 0) return 0;
    if (v <= t1) return 1;
    if (v <= t2) return 2;
    if (v <= t3) return 3;
    return 4;
  };

  // 网格：从 364 天前对齐到周日，铺到今天
  var today = new Date(); today.setHours(0,0,0,0);
  var start = new Date(today); start.setDate(start.getDate() - 364);
  start.setDate(start.getDate() - start.getDay());
  var weeks = [], cur = [];
  for (var d = new Date(start); d <= today; d.setDate(d.getDate()+1)){
    cur.push(new Date(d));
    if (cur.length === 7){ weeks.push(cur); cur = []; }
  }
  if (cur.length) weeks.push(cur);

  var gap = 3;
  var lastMonth = -1;
  var monthLabels = [];   // { col, text } —— 标签从该列开始
  var cols = weeks.map(function(wk, ci){
    var m = wk[wk.length-1].getMonth();
    if (m !== lastMonth){ lastMonth = m; monthLabels.push({ col: ci + 1, text: (m+1) + '月' }); }
    var cells = wk.map(function(day){
      var key = ds(day);
      var o = map[key];
      var v = o ? o.tokens : 0;
      var tip = key + ' · ' + (v ? fmt(v) + ' tokens' : '无调用');
      return '<div class="cell" data-lv="' + level(v) + '" data-tip="' + tip + '"></div>';
    }).join("");
    return { c: cells };
  });
  var nc = cols.length;
  // 格子宽高一致（CSS aspect-ratio）；这里只给左侧星期标签一个对齐用的行高。
  var cell = nc > 56 ? 12 : nc > 48 ? 13 : 15;

  var dow = ["", "一", "", "三", "", "五", ""].map(function(x){ return '<span>' + x + '</span>'; }).join("");
  // 月份标签用 grid-column 精确定位到对应列（避免文字宽度撑破单列）
  var months = monthLabels.map(function(l){
    return '<div style="grid-column:' + l.col + ' / span 3">' + l.text + '</div>';
  }).join("");
  var grid = cols.map(function(w){ return '<div class="col">' + w.c + '</div>'; }).join("");

  // 图例色块必须内联写尺寸：这个 div 在 .hm 容器之外，.hm .legend i 选择器匹配不到，
  // 而裸 <i> 是行内元素、没有宽高 —— 之前就是在这里变成「少 [空白] 多」的。
  var sw = function(bg){
    return '<i style="display:inline-block;width:11px;height:11px;border-radius:3px;background:' + bg + '"></i>';
  };
  var legend = '<div class="legend" style="margin:10px 0 0;justify-content:flex-end;gap:4px">少' +
    sw("var(--track)") + sw("#dbe6fe") + sw("#a8c2fb") + sw("#6b93f8") + sw("#2f57d6") + '多</div>';

  var total = daily.reduce(function(a, d){ return a + d.tokens; }, 0);
  var active = daily.filter(function(d){ return d.tokens > 0; }).length;
  var gcols = 'grid-template-columns:repeat(' + nc + ',minmax(0,1fr))';

  return '<div class="card" style="margin-top:12px">' +
    '<h3>Token 热力图 · 最近 12 个月（' + active + ' 天有调用 · 合计 ' + fmt(total) + '）</h3>' +
    '<div class="hm" style="--cell:' + cell + 'px;--gap:' + gap + 'px;--minw:' + (nc * (cell + gap)) + 'px">' +
      '<div class="dow">' + dow + '</div>' +
      '<div class="cols">' +
        '<div class="months-grid" style="' + gcols + '">' + months + '</div>' +
        '<div class="grid" style="' + gcols + '">' + grid + '</div>' +
      '</div>' +
    '</div>' + legend + '</div>';
}

function modelCard(t){
  var models = t.models || [];
  if (!models.length) return '<div class="card" style="margin-top:12px"><h3>按模型的调用情况</h3>' +
    '<div class="muted small">该范围内暂无模型调用</div></div>';
  var maxTok = Math.max.apply(null, [1].concat(models.map(function(m){ return m.tokens; })));
  var rows = models.map(function(m){
    var denom = (m.read||0) + (m.input||0);
    var hit = denom > 0 ? m.read/denom : 0;
    var freshPct = Math.max(0, (1-hit) * 100);
    var cachePct = Math.max(0, hit * 100);
    var hitColor = hit >= .8 ? 'var(--good)' : hit >= .5 ? 'var(--warn)' : 'var(--bad)';
    return '<div class="mrow"><div>' +
      '<div class="mname">' + esc(m.name) + ' <span class="small muted">' + esc(m.providerID||"") + '</span></div>' +
      '<div class="msub">' + fmtInt(m.requests) + ' 次请求 · ' + fmtInt(m.tools||0) + ' 工具 · 占比 ' + pct(m.share) + '</div>' +
      // 份额条（相对最高模型，单色）
      '<div class="track2" title="token 总量占最高模型的比例"><i style="width:' + ((m.tokens/maxTok)*100).toFixed(1) + '%"></i></div>' +
      // 命中率条：浅蓝=命中(左)，橙=未命中(右)。命中率普遍很高，所以未命中段强制最小可见宽度。
      '<div class="track3" title="缓存命中 ' + pct(hit) + '（命中 ' + fmt(m.read) + ' / 未命中 ' + fmt(m.input) + '）">' +
        '<i class="cm-hit-hit" style="width:' + (freshPct > 0 ? 100 - Math.max(freshPct, 1.6) : 100).toFixed(2) + '%"></i>' +
        (freshPct > 0 ? '<i class="cm-hit-miss" style="width:' + Math.max(freshPct, 1.6).toFixed(2) + '%"></i>' : '') +
      '</div>' +
      '<div class="msub" style="margin-top:4px">缓存命中 <b style="color:' + hitColor + '">' + pct(hit) + '</b>' +
        ' <span class="muted">（命中 ' + fmt(m.read) + ' / 新输入 ' + fmt(m.input) + '）</span></div>' +
    '</div><div class="mval">' + fmt(m.tokens) + '</div></div>';
  }).join("");

  return '<div class="card" style="margin-top:12px">' +
    '<h3>按模型的调用情况</h3>' +
    '<div class="legend" style="margin-bottom:6px">' +
      '<span class="dot cm-hit-hit"></span>缓存命中' +
      '<span class="dot cm-hit-miss" style="margin-left:8px"></span>未命中（新输入）' +
    '</div>' + rows + '</div>';
}

function tokenCard(t){
  var tot = t.totals || {};

  // 8 个统计小卡片（两行 × 4）
  // 字段名与 Python card_data 的 totals 严格对应：input/output 为原始值，
  // 缓存在 cacheRead（不是 read —— 之前写错导致「缓存读取 0」）。
  var cards = [
    ['输入 tokens（含缓存读取）', fmt((tot.input||0) + (tot.cacheRead||0))],
    ['输出 tokens', fmt((tot.output||0) + (tot.reasoning||0))],
    ['缓存读取', fmt(tot.cacheRead)],
    ['缓存命中率', pct(tot.cacheShare)],
    ['请求数', fmtInt(tot.requests)],
    ['工具调用', fmtInt(tot.tools)],
    ['会话数', fmtInt(tot.sessions)],
    ['平均每次请求', fmt(tot.requests ? tot.tokens/tot.requests : 0) + ' tok']
  ].map(function(c){
    return '<div class="card"><div class="num">' + c[1] + '</div><div class="lb">' + c[0] + '</div></div>';
  }).join("");

  return '<div class="card hero">' +
    '<div class="label">最近 ' + (t.days||30) + ' 天 · tokens 消耗</div>' +
    '<div class="big">' + fmt(tot.tokens) + '<span>tokens</span></div>' +
    sparkline(t.recent || t.daily) + '</div>' +
    '<div class="grid4" style="margin-top:12px">' + cards + '</div>' +
    heatmapCard(t) +
    modelCard(t);
}

function sparkline(daily){
  var spark = (daily || []).slice(-30);
  var maxD = Math.max.apply(null, [1].concat(spark.map(function(d){ return d.tokens; })));
  var bars = spark.map(function(d){
    return '<i style="height:' + Math.max(3, (d.tokens/maxD)*40).toFixed(1) + 'px" title="' + d.date + ' · ' + fmt(d.tokens) + '"></i>';
  }).join("");
  return '<div class="spark">' + bars + '</div>';
}

function sessionCard(s){
  var rows = (s.top || []).map(function(x){
    return '<tr><td class="tt" title="' + esc(x.title) + '">' + esc(x.title) + '</td>' +
      '<td class="muted">' + esc(x.modelID) + '</td>' +
      '<td class="right">' + fmt(x.tokens) + '</td>' +
      '<td class="right">' + fmtInt(x.requests) + '</td>' +
      '<td class="right">' + fmtInt(x.tools) + '</td>' +
      '<td class="right muted">' + dateCN(x.timeUpdated) + '</td></tr>';
  }).join("") || '<tr><td colspan="6" class="muted" style="padding:14px">暂无会话</td></tr>';
  var tt = s.totals || {};
  return '<div class="grid4">' +
    '<div class="card"><div class="num">' + fmtInt(s.total) + '</div><div class="lb">会话总数</div></div>' +
    '<div class="card"><div class="num">' + fmt(tt.tokens) + '</div><div class="lb">累计 tokens</div></div>' +
    '<div class="card"><div class="num">' + fmtInt(tt.requests) + '</div><div class="lb">请求</div></div>' +
    '<div class="card"><div class="num">' + fmt(s.changes) + '</div><div class="lb">改动行 / ' + fmtInt(s.files) + ' 文件</div></div>' +
    '</div>' +
    '<div class="card" style="margin-top:12px"><h3>最近会话（按活跃排序）</h3>' +
    '<div class="tw"><table><thead><tr><th>会话</th><th>模型</th><th class="right">Tokens</th><th class="right">请求</th><th class="right">工具</th><th class="right">更新</th></tr></thead>' +
    '<tbody>' + rows + '</tbody></table></div></div>';
}

function contextCard(c){
  if (!c || !c.ok) return '<div class="err">无法读取上下文：' + esc((c && c.error) || "未知错误") + '</div>';
  var used = c.context.used;
  var limit = c.context.limit;
  var ratio = limit ? Math.max(0, Math.min(1, c.context.ratio)) : null;
  var h = c.context.detail || { total: used, input: 0, output: 0, reasoning: 0, read: 0 };

  var R = 40, C = 2*Math.PI*R;
  var known = ratio != null;
  var shown = known ? Math.max(0.002, Math.min(1, ratio)) : 0;
  var color = !known ? "var(--muted)" : (ratio < 0.5 ? "var(--good)" : (ratio < 0.8 ? "var(--warn)" : "var(--bad)"));
  var ring = '<svg class="ring" viewBox="0 0 100 100" aria-label="上下文占用">' +
    '<circle cx="50" cy="50" r="' + R + '" fill="none" stroke="var(--track)" stroke-width="10"/>' +
    (known ? '<circle cx="50" cy="50" r="' + R + '" fill="none" stroke="' + color + '" stroke-width="10" stroke-linecap="round" stroke-dasharray="' + (shown*C).toFixed(2) + ' ' + C.toFixed(2) + '" transform="rotate(-90 50 50)"/>' : "") +
    '<text x="50" y="47" text-anchor="middle" font-size="' + (known?15:13) + '" font-weight="700" fill="var(--text)">' + (known ? pct(ratio) : fmt(used)) + '</text>' +
    '<text x="50" y="63" text-anchor="middle" font-size="8" fill="var(--muted)">' + (known ? "已用" : "tokens") + '</text></svg>';

  var parts = [
    { k: "缓存读取", v: h.read, cls: "ctx-cache" },
    { k: "新输入", v: h.input, cls: "ctx-input" },
    { k: "推理", v: h.reasoning, cls: "ctx-reason" },
    { k: "输出", v: h.output, cls: "ctx-output" }
  ];
  var totalParts = parts.reduce(function(a,p){ return a + (p.v||0); }, 0) || 1;
  var segs = parts.map(function(p){
    return '<i class="' + p.cls + '" style="width:' + ((p.v||0)/totalParts*100).toFixed(2) + '%" title="' + p.k + ' ' + fmt(p.v) + '"></i>';
  }).join("");

  return '<div class="card"><div class="ctx-ring">' + ring +
    '<div style="flex:1;min-width:0">' +
    '<div class="label">当前会话上下文占用</div>' +
    '<div style="font-size:20px;font-weight:700">' + fmt(used) + ' <span class="small muted">/ ' + (limit ? fmt(limit) : "上限未知") + ' tokens</span></div>' +
    '<div class="bar" style="margin-top:10px">' + segs + '</div>' +
    '<div class="legend" style="margin:9px 0 0">' +
      '<span class="dot ctx-cache"></span>缓存读取 ' + fmt(h.read) +
      '<span class="dot ctx-input" style="margin-left:10px"></span>新输入 ' + fmt(h.input) +
      '<span class="dot ctx-reason" style="margin-left:10px"></span>推理 ' + fmt(h.reasoning) +
      '<span class="dot ctx-output" style="margin-left:10px"></span>输出 ' + fmt(h.output) + '</div>' +
    '</div></div></div>' +
    '<div class="card" style="margin-top:12px"><h3>会话详情</h3>' +
    '<div class="stat-line"><span class="muted">标题</span><b style="max-width:62%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + esc(c.title) + '">' + esc(c.title) + '</b></div>' +
    '<div class="stat-line"><span class="muted">模型</span><b>' + esc(c.model.modelID) + ' <span class="small muted">' + esc(c.model.providerID) + '</span></b></div>' +
    '<div class="stat-line"><span class="muted">工作目录</span><b class="small" style="max-width:64%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + esc(c.directory) + '">' + esc(c.directory || "-") + '</b></div>' +
    '<div class="stat-line"><span class="muted">消息 / 步 / 工具</span><b>' + fmtInt(c.messages) + ' / ' + fmtInt(c.steps) + ' / ' + fmtInt(c.tools) + '</b></div>' +
    '<div class="stat-line"><span class="muted">本会话累计 tokens</span><b>' + fmt(c.tokens.total) + '</b></div>' +
    '<div class="stat-line"><span class="muted">改动</span><b>+' + fmtInt(c.additions) + ' / -' + fmtInt(c.deletions) + ' · ' + fmtInt(c.files) + ' 文件</b></div>' +
    '<div class="stat-line"><span class="muted">最近活动</span><b>' + timeCN(c.lastActivity) + '</b></div>' +
    '<div class="stat-line"><span class="muted">数据来源</span><b><span class="pill">' + esc(c.context.source) + '</span>' + (c.compacting ? ' <span class="pill">压缩中</span>' : "") + '</b></div>' +
    '</div>';
}

function renderTab(kind, data){
  if (kind === "token") return tokenCard(data.token || { totals:{}, models:[], daily:[], days:30 });
  if (kind === "session") return sessionCard(data.session || { top: [] });
  if (kind === "context") return contextCard(data.context);
  return "";
}

function buildBody(data){
  var kind = data.kind || "all";
  if (kind !== "all") return renderTab(kind, data);
  var tabs = [
    { id: "token", label: "总体使用量" },
    { id: "session", label: "会话使用量" },
    { id: "context", label: "上下文使用量" }
  ];
  var nav = '<div class="tabs" role="tablist" aria-label="用量视图">' + tabs.map(function(tab, i){
    return '<button class="tab" type="button" role="tab" id="tab-' + tab.id +
      '" aria-controls="tab-content" aria-selected="' + (i === 0 ? "true" : "false") +
      '" data-tab="' + tab.id + '">' + tab.label + '</button>';
  }).join("") + '</div>';
  return nav + '<div id="tab-content" role="tabpanel" aria-labelledby="tab-token">' +
    renderTab("token", data) + '</div>';
}

function footer(data){
  return '<div class="foot"><span>MiMo Desktop 本机统计 · 数据不出网 · 无服务、零 AI</span><span>生成于 ' +
    new Date(data.generatedAt || Date.now()).toLocaleString("zh-CN", { hour12: false }) + '</span></div>';
}
"""


def render_html(data: dict, *, cmd: str = "", out_path: str = "") -> str:
    safe = (json.dumps(data, ensure_ascii=False)
            .replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    kind = data.get("kind", "all")
    title = _html.escape(KIND_TITLE.get(kind, "MiMo Desktop 用量"))
    gen = datetime.fromtimestamp((data.get("generatedAt") or 0) / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
    cmd_esc = _html.escape(cmd)

    # 刷新按钮：不请求任何服务，只提示用户重跑命令（并可直接复制）
    tip = ""
    if cmd:
        tip = (
            '<div class="tip">数据为**生成时快照**。要刷新，重新运行：'
            f'<code style="user-select:all;color:var(--text)">{cmd_esc}</code></div>'
        )

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="color-scheme" content="light">
<style>{_STYLE}</style></head>
<body>
<div class="page">
  <div class="head">
    <div>
      <h1>{title}</h1>
      <div class="sub">生成时间 {gen} · 数据源 {_html.escape(data.get("dbPath", ""))}</div>
    </div>
    <div class="acts">
      <button class="btn" id="regen" type="button">重新生成</button>
    </div>
  </div>
  {tip}
  <div class="wrap" id="root"></div>
</div>
<script>
(function(){{
  var DATA = {safe};
  var CMD = {json.dumps(cmd)};
  var OUT = {json.dumps(out_path)};
  {_RUNTIME}
  var root = document.getElementById("root");
  root.innerHTML = buildBody(DATA) + footer(DATA);
  root.addEventListener("click", function(ev){{
    var btn = ev.target && ev.target.closest ? ev.target.closest("[data-tab]") : null;
    if (!btn) return;
    var selected = btn.getAttribute("data-tab");
    root.querySelectorAll("[data-tab]").forEach(function(tab){{
      tab.setAttribute("aria-selected", tab === btn ? "true" : "false");
    }});
    var panel = document.getElementById("tab-content");
    if (panel) {{
      panel.innerHTML = renderTab(selected, DATA);
      panel.setAttribute("aria-labelledby", "tab-" + selected);
    }}
  }});

  document.getElementById("regen").addEventListener("click", function(){{
    var msg = "数据是静态快照，无法在页面内刷新。\\n\\n请重新运行这条命令：\\n" + CMD;
    if (OUT) msg += "\\n\\n输出文件：\\n" + OUT;
    if (navigator.clipboard && CMD) {{
      navigator.clipboard.writeText(CMD).then(function(){{
        alert(msg + "\\n\\n（命令已复制到剪贴板）");
      }}, function(){{ alert(msg); }});
    }} else {{ alert(msg); }}
  }});
}})();
</script>
</body></html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

KNOWN_KINDS = ("all", "token", "session", "context")
_SAFE_FLAGS = {"--open", "--print-path"}
_SAFE_OPTS = {"--days", "--db", "--out", "--session", "--limit"}


def _split_args(text: str) -> list[str]:
    """把 ``--arg-string`` 的整串按引号规则拆成 tokens。

    为什么不用 ``shlex``：Windows 路径含大量反斜杠，``shlex.split(posix=True)``
    会把 ``\\`` 当转义符吞掉（``C:\\Users\\a`` → ``C:Usersa``），
    ``posix=False`` 又会保留引号且不处理嵌套。两种模式都不适用。

    这里手写一个最小拆分器：
      * 单引号 ``'...'`` 与双引号 ``"..."`` 内部原样保留（含反斜杠）；
      * 引号仅用于分组，不进入结果；
      * 引号外的空白为分隔符；
      * 未闭合的引号按「到串尾」处理，不抛异常。

    例：``token --out "C:\\a b\\x.html"`` →
        ``["token", "--out", "C:\\a b\\x.html"]``
    """
    if not text:
        return []
    s = text.strip()
    # 整体被引号包裹时先剥一层（PowerShell 单引号字面串的常见形态）
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1]

    out: list[str] = []
    buf: list[str] = []
    quote = ""
    for ch in s:
        if quote:
            if ch == quote:
                quote = ""
            else:
                buf.append(ch)
        elif ch in ("'", '"'):
            quote = ch
        elif ch.isspace():
            if buf:
                out.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _sanitize_args(argv: list[str]) -> list[str]:
    """Allow-list CLI args. Filesystem paths may contain Windows backslashes.

    This is not a shell-escaping function. Registered MiMo templates must not interpolate
    untrusted arguments into shell text; they now execute a fixed command only.
    """
    out: list[str] = []
    i = 0
    shell_chars = ";|&$" + chr(96) + "><" + chr(10) + chr(13) + chr(92)
    while i < len(argv):
        tok = argv[i]
        stripped = tok.strip().strip(chr(34)).strip(chr(39))
        if any(ch in tok for ch in shell_chars):
            i += 1
            continue
        if stripped in _SAFE_FLAGS:
            out.append(stripped)
        elif stripped in _SAFE_OPTS:
            if i + 1 >= len(argv):
                raise ValueError(f"{stripped} 缺少参数")
            val = argv[i + 1].strip().strip(chr(34)).strip(chr(39))
            if stripped in ("--out", "--db"):
                if not val or any(ch in val for ch in shell_chars.replace(chr(92), "")):
                    raise ValueError(f"{stripped} 路径包含非法字符或为空")
                out.extend((stripped, val))
            elif any(ch in val for ch in shell_chars):
                raise ValueError(f"{stripped} 参数包含非法字符")
            else:
                out.extend((stripped, val))
            i += 1
        elif stripped in KNOWN_KINDS:
            out.append(stripped)
        i += 1
    return out


def default_out_path(kind: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d")
    return os.path.abspath(f"mimo-usage-{kind}-{stamp}.html")


def build_command_hint(kind: str, days: int, out_path: str) -> str:
    parts = ["python", os.path.abspath(__file__)]
    if kind != "all":
        parts.append(kind)
    if kind in ("token", "all") and days != 30:
        parts += ["--days", str(days)]
    if out_path:
        parts += ["--out", out_path]
    return " ".join(f'"{p}"' if " " in p else p for p in parts)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="usage_cli.py",
        description="MiMo Desktop 本机用量统计（离线生成静态 HTML，零 AI 参与）",
    )
    parser.add_argument("kind", nargs="?", default="all",
                        help="生成内容：all（默认）/ token / session / context")
    parser.add_argument("--days", type=int, default=30, help="token 统计最近多少天（默认 30）")
    parser.add_argument("--db", default=None, help="数据库路径（默认 ~/.local/share/mimocode/mimocode.db）")
    parser.add_argument("--out", default=None, help="输出 HTML 路径（默认当前目录 mimo-usage-<kind>-<日期>.html）")
    parser.add_argument("--session", default=None, help="上下文视图指定的会话 ID（默认最近活跃）")
    parser.add_argument("--limit", type=int, default=None, help="上下文上限 tokens（覆盖内置模型表）")
    parser.add_argument("--open", action="store_true", help="生成后用默认浏览器打开")
    parser.add_argument("--print-path", action="store_true", help="只在标准输出打印生成的文件路径")
    parser.add_argument("--arg-string", default=None,
                        help="命令参数整串（由 shell 模板透传，内部按空白拆分后白名单校验）")

    # ---- 命令注入防护：参数可能来自 shell 模板中的 $ARGUMENTS ----
    # 模板写成 `... --arg-string $ARGUMENTS`（**故意不加引号**）：
    #   * 不加引号 → 用户输入不会被 shell 截断（含引号时也不会提前闭合）；
    #   * 代价是 shell 会按空白把它拆成多个 argv 元素，所以这里要把
    #     `--arg-string` **之后的所有元素**重新拼成一整串，再按引号规则解析。
    if argv is None:
        argv = sys.argv[1:]
    argv = [str(a) for a in argv]
    if "--arg-string" in argv:
        i = argv.index("--arg-string")
        joined = " ".join(argv[i + 1:])
        argv = argv[:i] + _split_args(joined)
    try:
        argv = _sanitize_args(argv)
    except ValueError as exc:
        print(f"MiMo 用量：参数错误（{exc}）", file=sys.stderr)
        return 2

    args = parser.parse_args(argv)
    if args.kind not in KINDS:
        args.kind = "all"

    db = args.db or default_db_path()
    out_path = os.path.abspath(args.out) if args.out else default_out_path(args.kind)

    try:
        data = card_data(db, kind=args.kind, days=args.days,
                         session_id=args.session, limit=args.limit)
    except FileNotFoundError as exc:
        print(f"MiMo 用量：{exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"MiMo 用量：读取数据库失败（{exc}）", file=sys.stderr)
        return 3

    cmd_hint = build_command_hint(args.kind, args.days, out_path)
    html_doc = render_html(data, cmd=cmd_hint, out_path=out_path)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html_doc)

    if args.print_path:
        print(out_path)
        return 0

    # 供 `command` 模板内联使用：简洁的一行结果
    print(f"MiMo 用量已生成：{out_path}")
    if args.open:
        try:
            webbrowser.open("file:///" + out_path.replace("\\", "/"))
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
