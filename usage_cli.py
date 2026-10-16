#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""usage_cli.py — MiMo Desktop 本机用量统计（确定性本地命令，零 AI 参与）

默认路径是纯离线命令行程序：
  读库 → 聚合 → 填充自包含 HTML 模板 → 写出 HTML（可选打开浏览器）。

`--open` 会额外拉起**仅监听 127.0.0.1** 的极小只读服务，让已打开的页面可以
点「刷新数据」重新读库并更新图表（无需回聊天窗重跑 /usage）。服务带随机
path token、Host/Origin 校验、空闲自动退出；默认仍不联网、不写数据库。

用法（通常由 MiMo 的 `command` 字段以 `` !`python usage_cli.py ...` `` 触发）：

    python usage_cli.py                 # 生成总览（token + session + context 三合一）
    python usage_cli.py token           # 只生成 Token 用量页
    python usage_cli.py session         # 只生成 Session 用量页
    python usage_cli.py context         # 只生成上下文占用页
    python usage_cli.py token --days 7  # 最近 7 天
    python usage_cli.py --open          # 启动/复用实时服务并用浏览器打开
    python usage_cli.py --out D:/x.html # 指定输出路径
    python usage_cli.py --shutdown      # 关闭本机实时服务
    python usage_cli.py service-check    # 只读报告实时服务身份（安装器 preflight 用）

仅依赖 Python 标准库（sqlite3 / json / html / argparse / http.server）。
"""

# mimo-usage-dashboard:managed-file:v1
# 上面这一行是"本项目拥有的安装文件"标记，供 install.ps1 在缺少 manifest 时判断归属。
# 没有该标记且没有 manifest 记录的同名文件一律视为用户文件，安装器不会覆盖。

from __future__ import annotations

import argparse
import html as _html
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlencode

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_PORT = 8790  # 旧版固定端口；新版一律用系统分配的临时端口（port=0），不再使用 8790
LOOPBACK_HOST = "127.0.0.1"
DEFAULT_IDLE_MINUTES = 240  # 无请求后自动退出，避免孤儿进程

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

# 实时服务的身份标识：`/{token}/api/ping` 用它证明监听者确实是本项目服务。
# installer.py 出于同样的原因自带一份同名常量（面对的是可能被改动的已安装副本，
# 不能导入本模块）。两份必须一致，由 test/test_python.py 的
# 「身份标识与安装器一致」断言守住。
LIVE_APP_ID = "mimo-usage-dashboard"

KIND_TITLE = {
    "all": "MiMo Desktop 用量总览",
    "token": "MiMo Desktop Token 用量",
    "session": "MiMo Desktop Session 用量",
    "context": "MiMo Desktop 上下文占用",
}


def force_utf8_stdio() -> None:
    """尽力把 stdout/stderr 切到 UTF-8（Windows 默认 GBK/CP936 下中文会乱码）。

    不能只依赖 ``PYTHONUTF8=1``：MiMo 的 shell 替换、管道捕获与子进程不一定带上。
    对不支持 ``reconfigure`` 的流（StringIO、已被包装的测试流等）静默跳过，
    **绝不改变退出码语义**，也不把异常抛给调用方。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def default_db_path() -> str:
    """数据源：~/.local/share/mimocode/mimocode.db（可用 MIMO_USAGE_DB 覆盖）。"""
    env = os.environ.get("MIMO_USAGE_DB")
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), ".local", "share", "mimocode", "mimocode.db")


def default_data_dir() -> str:
    """用户可写数据目录。避免把报告默认写进 MiMo 安装目录 / Program Files。"""
    env = os.environ.get("MIMO_USAGE_DATA_DIR")
    if env:
        return env
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "mimo-usage-dashboard")


def _read_jsonc_config(path: str) -> dict:
    """Read JSONC without modifying the configuration file."""
    with open(path, encoding="utf-8-sig") as stream:
        source = stream.read()
    result = []
    i = 0
    quoted = False
    while i < len(source):
        ch = source[i]
        if quoted:
            result.append(ch)
            if ch == "\\" and i + 1 < len(source):
                result.append(source[i + 1])
                i += 2
                continue
            if ch == '"':
                quoted = False
            i += 1
            continue
        if ch == '"':
            quoted = True
            result.append(ch)
            i += 1
            continue
        if source.startswith("//", i):
            while i < len(source) and source[i] not in "\r\n":
                i += 1
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            if end < 0:
                raise ValueError("unterminated JSONC block comment")
            i = end + 2
            continue
        result.append(ch)
        i += 1
    clean = re.sub(r",(\s*[}\]])", r"\1", "".join(result))
    obj = json.loads(clean)
    if not isinstance(obj, dict):
        raise ValueError("JSONC config root must be an object")
    return obj


def _context_limit_value(config: dict, provider_id: str, model_id: str) -> int | None:
    providers = config.get("provider")
    if not isinstance(providers, dict):
        return None
    provider = providers.get(provider_id)
    if not isinstance(provider, dict):
        return None
    models = provider.get("models")
    if not isinstance(models, dict):
        return None
    model = models.get(model_id)
    if not isinstance(model, dict):
        return None
    limits = model.get("limit")
    if not isinstance(limits, dict):
        return None
    value = limits.get("context")
    return value if type(value) is int and value > 0 else None


def model_context_limit(model_id: str | None, provider_id: str | None = None,
                        directory: str | None = None) -> tuple[int | None, str]:
    """Return (context limit, provenance), never guess across providers.

    A broken configuration is *not* silently replaced with a hard-coded value.
    """
    if not model_id or not provider_id:
        return None, "unknown"
    home = os.path.join(os.path.expanduser("~"), ".config", "mimocode", "mimocode.jsonc")
    paths = [home]
    if directory:
        paths.append(os.path.join(directory, "mimocode.jsonc"))
        paths.append(os.path.join(directory, ".mimocode", "mimocode.jsonc"))
    resolved = None
    for path in paths:
        if not os.path.isfile(path):
            continue
        try:
            config = _read_jsonc_config(path)
        except (OSError, ValueError, UnicodeError):
            return None, "config-error"
        val = _context_limit_value(config, str(provider_id), str(model_id))
        if val is not None:
            resolved = val
    if resolved is not None:
        return resolved, "config"
    # First-party MiMo models keep their built-in window when no config declares one.
    if provider_id == "xiaomi-token-plan-cn":
        known = MODEL_CONTEXT_LIMITS.get(str(model_id).lower())
        if known and str(model_id).lower().startswith("mimo-"):
            return known, "builtin"
    return None, "unknown"


# ---------------------------------------------------------------------------
# 数据库（只读；被占用时做一致性快照再读）
# ---------------------------------------------------------------------------


def _open_readonly(db_path: str):
    """优先以只读 URI 打开；失败则复制一份快照（含 -wal/-shm）再读。

    返回 ``(conn, snapshot_dir)``：正常路径 ``snapshot_dir`` 为 None；快照回退
    时是本次私有的临时目录（``mkdtemp`` 唯一命名）。调用方在 ``conn.close()``
    之后必须删除该目录（见 ``_cleanup_snapshot``）——不删的话，被占用的数百 MB
    数据库每次读取都会在 %TEMP% 留下一份完整拷贝。快照目录只属于本次调用，
    删除不会影响并发的其它读者。
    """
    uri = "file:" + db_path.replace("\\", "/").replace("?", "%3f").replace("#", "%23") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn, None
    except sqlite3.Error:
        import shutil
        import tempfile

        tmp = tempfile.mkdtemp(prefix="mimo-usage-")
        conn = None
        try:
            base = os.path.basename(db_path)
            for suffix in ("", "-wal", "-shm"):
                src = db_path + suffix
                if os.path.exists(src):
                    shutil.copyfile(src, os.path.join(tmp, base + suffix))
            conn = sqlite3.connect(os.path.join(tmp, base), timeout=1.0)
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except BaseException:
            # 初始化半途失败（复制不完整 / 快照打不开）：先关连接（Windows 上
            # 打开着的文件删不掉），再清掉半成品并原样抛出，不在 %TEMP% 留垃圾。
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return conn, tmp


def _cleanup_snapshot(tmp: str) -> None:
    """删除本次调用私有的只读快照目录（best-effort）。

    只作用于 ``_open_readonly`` 自己 ``mkdtemp`` 出来的目录；先清只读位再删，
    与 installer.force_rmtree 同一套 Windows 处理。删不掉就留着（与旧行为一致，
    但正常路径下不会再遗留）。
    """
    import shutil
    import stat

    def _onerror(func, path, _exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass

    try:
        shutil.rmtree(tmp, onerror=_onerror)
    except OSError:
        pass


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

    conn, snap = _open_readonly(db_path)
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
        if snap:
            _cleanup_snapshot(snap)


# ---------------------------------------------------------------------------
# 聚合：单会话上下文占用
# ---------------------------------------------------------------------------

# 上下文用量分类树：只把**数据库里真实存在、可逐项核对**的字段拆成分支。
# 每个分支声明自己的单位（tokens / 次 / 条），不同单位绝不混算。
_TOKENS_BRANCH_FIELDS = (
    ("缓存读取", "read"), ("新输入", "input"), ("推理", "reasoning"),
    ("输出", "output"), ("缓存写入", "write"),
)

# MiMo 数据库只保存每次请求聚合后的 tokens 五项，不保存提示词各段落的拆分，
# 因此下面这些分类在本地只读条件下无法还原，必须如实标注为「无法细分」而不是猜。
_CONTEXT_NOT_SUBDIVIDABLE = (
    "系统提示词", "内置工具清单", "Memory 文件", "Skills", "启动上下文",
)
_CONTEXT_NOT_SUBDIVIDABLE_REASON = (
    "数据库只存每次请求聚合后的 tokens（total/input/output/reasoning/cache），"
    "不存提示词各段落各自的 token 数，因此无法在本地只读条件下还原这部分拆分。"
)


def _branch(label: str, unit: str, note: str, total: int, fields: list) -> dict:
    """构造分类树的一个分支节点。

    ``fields`` 是 ``[(子项名, 数值), …]``，按调用方给定的顺序保留（不做隐含重排）。
    子项之和与 ``total`` 的差额单列一项，不做任何猜测性归并：
      * 差额 > 0 → 「未分类」（有总量但无对应字段）
      * 差额 < 0 → 「字段和超出总量」（原始数据异常，如实标记而不是强行归零）
    """
    children = [{"label": str(k), "value": int(v or 0)} for k, v in fields]
    gap = int(total or 0) - sum(c["value"] for c in children)
    if gap > 0:
        children.append({"label": "未分类", "value": gap})
    elif gap < 0:
        children.append({"label": "字段和超出总量", "value": -gap})
    return {"label": label, "unit": unit, "note": note,
            "total": int(total or 0), "children": children}


def _counted(values, unknown: str) -> list:
    """把一串类别名聚合成长度去重、按次数降序（同次数按名称升序）的 ``[(名, 次), …]``。

    只有**非空字符串**才算一个已知类别；其余（None、空串、纯空白、数字、布尔、字典…）
    一律归入 ``unknown``（例如「未知工具」）并计数，既不丢弃也不猜测其含义。
    同名的多次出现会合并成一项，不会产生重复类别。
    """
    counts: dict[str, int] = {}
    for v in values:
        key = v.strip() if isinstance(v, str) else ""
        if not key:
            key = unknown
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def session_context(db_path: str, session_id: str | None = None, limit: int | None = None) -> dict:
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"找不到 MiMo 数据库：{db_path}")

    conn, snap = _open_readonly(db_path)
    try:
        explicit = session_id is not None
        sid = session_id
        if explicit:
            if not sid or conn.execute("SELECT id FROM session WHERE id=?", (sid,)).fetchone() is None:
                return {"ok": False, "error": f"指定的会话不存在：{sid}",
                        "selectionMode": "fixed", "sessionId": sid}
        else:
            r = conn.execute("SELECT id FROM session ORDER BY time_updated DESC, id DESC LIMIT 1").fetchone()
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
        message_models = {}
        latest_token_model = None
        step_model = None
        last_activity = int(srow[3] or 0)

        for r in conn.execute(
            "SELECT id, time_created, data FROM message WHERE session_id=? ORDER BY time_created ASC",
            (sid,),
        ):
            d = _loads(r[2])
            if not isinstance(d, dict) or d.get("role") != "assistant":
                continue
            messages += 1
            last_activity = max(last_activity, int(r[1] or 0))
            pair = (d.get("providerID"), d.get("modelID"))
            if all(isinstance(x, str) and x for x in pair):
                message_models[r[0]] = pair
                provider_id, model_id = pair
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
                    latest_token_model = message_models.get(r[0])

        tools = 0
        steps = 0
        last_step_tokens = None
        part_types: list = []
        tool_names: list = []
        for r in conn.execute(
            "SELECT time_created, data, message_id FROM part WHERE session_id=? ORDER BY time_created ASC",
            (sid,),
        ):
            d = _loads(r[1])
            if not isinstance(d, dict):
                continue
            ptype = d.get("type")
            part_types.append(ptype if isinstance(ptype, str) else "")
            if d.get("type") == "tool":
                tools += 1
                tool_names.append(d.get("tool"))
            elif d.get("type") == "step-finish":
                steps += 1
                t = d.get("tokens")
                if isinstance(t, dict) and isinstance(t.get("total"), (int, float)):
                    last_step_tokens = t
                    step_model = message_models.get(r[2])
            last_activity = max(last_activity, int(r[0] or 0))

        ctx = last_step_tokens or last_msg_tokens
        used = int(ctx["total"]) if (ctx and isinstance(ctx.get("total"), (int, float))) else 0
        active_model = (step_model if last_step_tokens else latest_token_model)
        if active_model:
            provider_id, model_id = active_model
        if limit is not None and limit > 0:
            resolved_limit, limit_source = limit, "explicit"
        else:
            resolved_limit, limit_source = model_context_limit(model_id, provider_id, srow[2])

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

        # ---- 上下文用量分类树 ----
        # 三个 tokens 口径分支 + 两个计数口径分支。每个分支的合计都能与原始总量逐项核对：
        #   current  → context.used（最新一次 step-finish / assistant 消息的 total）
        #   lifetime → tokens.total（本会话全部 assistant 消息之和）
        #   tools    → context.tools（part.type=tool 的条数）
        #   parts    → 该会话 part 表行数（工具调用是它的真子集，单位不同，不混算）
        src = "step-finish" if last_step_tokens else ("message" if last_msg_tokens else "none")
        snap_total = int((detail or {}).get("total") or 0)

        def _tok_fields(src_dict) -> list:
            d = src_dict if isinstance(src_dict, dict) else {}
            return [(label, int(d.get(key) or 0)) for label, key in _TOKENS_BRANCH_FIELDS]

        branches = [
            _branch("当前上下文快照", "tokens",
                    f"单位 tokens；来源 {src}（最新一次请求的 token 明细）",
                    snap_total, _tok_fields(detail)),
            _branch("本会话累计", "tokens",
                    "单位 tokens；该会话全部 assistant 消息之和（历史累计，不是当前上下文）",
                    agg["total"], _tok_fields(agg)),
            _branch("工具调用", "次",
                    "单位 次；part.type=tool 的条数（与 tokens 口径无关，不能相加）",
                    tools, _counted(tool_names, "未知工具")),
            _branch("消息片段构成", "条",
                    "单位 条；该会话 part 表全部行的 type 分布（工具调用是它的子集）",
                    len(part_types), _counted(part_types, "未知类型")),
        ]

        return {
            "ok": True,
            "sessionId": sid,
            "selectionMode": "fixed" if explicit else "latest",
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
                "limitSource": limit_source,
                "ratio": (used / resolved_limit) if resolved_limit else None,
                "source": src,
                "detail": detail,
            },
            "breakdown": {
                "root": "上下文用量分类",
                "rootUnit": "按可核对字段拆分，不同单位分别标注",
                "branches": branches,
                "notSubdividable": {
                    "labels": list(_CONTEXT_NOT_SUBDIVIDABLE),
                    "reason": _CONTEXT_NOT_SUBDIVIDABLE_REASON,
                },
            },
            "watermark": srow[8], "compacting": srow[9], "lastCheckpoint": srow[10],
        }
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        if snap:
            _cleanup_snapshot(snap)


# ---------------------------------------------------------------------------
# 组装卡片数据（与旧版 cardData 同构，便于模板共用）
# ---------------------------------------------------------------------------


def _window_slice(data: dict, days: int) -> dict:
    """把全量聚合裁剪成某个「自然日窗口」的 token 视图数据。

    窗口口径：[今天 -(days-1) 天 的本地 00:00, 现在]，按本地日期切分（自然日）。
    所以 ``days=1`` 就是「今日」——从当天 00:00 起，不是滚动的最近 24 小时。

    这些切片在**生成时一次算好并内联进 HTML**，浏览器端切换时间尺度只是换一份已经
    存在的数字，不重新读库、不发请求（C1/C2/C4）。
    """
    days = days if (isinstance(days, int) and days > 0) else 30
    from_date = _local_date(int((datetime.now() - timedelta(days=days - 1)).timestamp() * 1000))
    recent = [d for d in data["daily"] if d["date"] >= from_date]

    sum_ = {"tokens": 0, "input": 0, "output": 0, "reasoning": 0,
            "read": 0, "write": 0, "requests": 0, "tools": 0}
    for d in recent:
        for k in sum_:
            sum_[k] += d[k]

    mlist = []
    for m in data["models"]:
        t = sum(v for k, v in m["daily"].items() if k >= from_date)
        if t <= 0:
            continue
        mlist.append({
            "name": m["name"], "providerID": m["providerID"], "tokens": t,
            "read": sum(v for k, v in (m.get("dailyRead") or {}).items() if k >= from_date),
            "input": sum(v for k, v in (m.get("dailyInput") or {}).items() if k >= from_date),
            "requests": sum(v for k, v in (m.get("dailyReq") or {}).items() if k >= from_date),
            "tools": sum(v for k, v in (m.get("dailyTools") or {}).items() if k >= from_date),
        })
    mlist.sort(key=lambda m: m["tokens"], reverse=True)

    denom = sum_["input"] + sum_["read"]
    # share = 该模型 token / 窗口内**全部**模型 token。分母是截断前的总和，
    # 所以模型数 > 8 时这些 share 之和会 < 1（这是正确语义，不是舍入误差）。
    model_total_tokens = sum(m["tokens"] for m in mlist)
    return {
        "days": days,
        "fromDate": from_date,
        # 显式给出分母与模型总数，前端与测试都不必猜 share 的口径。
        "modelTotal": model_total_tokens,
        "modelCount": len(mlist),
        "totals": {
            # 注意字段语义：input/output 是**原始值**（不含缓存），cacheRead 单列；
            # JS 端 tokenCard 按同名取用。
            "tokens": sum_["tokens"],
            "input": sum_["input"], "output": sum_["output"],
            "reasoning": sum_["reasoning"],
            "cacheRead": sum_["read"], "cacheWrite": sum_["write"],
            "requests": sum_["requests"], "tools": sum_["tools"],
            "cacheShare": (sum_["read"] / denom) if denom > 0 else 0.0,
            # 会话数是全量会话行数（不受窗口影响），与 Session 视图的「会话总数」同源，
            # 这样 Token 视图的「会话数」与 Session 视图的「会话总数」不会漂移。
            "sessions": len(data["sessions"]),
        },
        "models": [{**m, "share": m["tokens"] / (model_total_tokens or 1)} for m in mlist[:8]],
        "recent": [{"date": d["date"], "tokens": d["tokens"]} for d in recent],
    }


def card_data(db_path: str, kind: str = "all", days: int = 30,
              session_id: str | None = None, limit: int | None = None) -> dict:
    kind = kind if kind in KINDS else "all"
    days = days if (isinstance(days, int) and days > 0) else 30
    data = read_usage(db_path)
    out = {"kind": kind, "generatedAt": int(datetime.now().timestamp() * 1000), "dbPath": db_path}

    if kind in ("token", "all"):
        # 时间尺度选项固定为「今日 / 7 天 / 30 天」；--days 不是这三者时再补一个自定义窗口，
        # 保证 --days 的语义不被悄悄改掉。
        win_days = sorted({1, 7, 30, days})
        # 顶层字段保持原样：与 --days 对应的那一份（回归基线）。
        out["token"] = {
            **_window_slice(data, days),
            "totalsAll": data["totals"],
            # 热力图覆盖最近 12 个月，不受 --days 影响。这里把当日的 read/input/requests
            # 一并带上，供热力图悬浮提示计算「当日缓存命中率」。
            "daily": [{"date": d["date"], "tokens": d["tokens"], "read": d["read"],
                       "input": d["input"], "requests": d["requests"]} for d in data["daily"]],
            # 时间尺度切换用：今日 / 7 天 / 30 天（外加 --days 不是这三者时的自定义窗口）。
            # 全在生成时算好并内联，浏览器端切换不读库、不发请求、不重新生成 HTML。
            "windowKeys": [str(d) for d in win_days],
            "windows": {str(d): _window_slice(data, d) for d in win_days},
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
/* 上下文构成用独立配色（模型份额条的浅色段语义不同，不复用这里的类） */
.ctx-cache{background:#8fb0fb}  /* 缓存读取 */
.ctx-input{background:#3b6ef6}   /* 新输入 */
.ctx-reason{background:#a78bfa}  /* 推理 */
.ctx-output{background:#34d399}  /* 输出 */
.mrow{display:grid;grid-template-columns:1fr auto;gap:4px 12px;align-items:center;padding:11px 0;border-bottom:1px solid var(--border)}
.mrow:last-child{border-bottom:0}
.mmain{min-width:0}
.mname{font-weight:600;font-size:13px}
.msub{color:var(--muted);font-size:11px;margin-top:3px}
.mval{font-weight:700;color:var(--accent);font-size:13px;align-self:start;padding-top:1px}
.rank{display:inline-block;min-width:16px;margin-right:6px;padding:0 4px;border-radius:5px;
  background:var(--accent-soft);color:var(--accent);font-size:10px;font-weight:700;text-align:center}
/* 每个模型**只有一条**进度条：
   条长 = 该模型 token / 窗口内全部模型 token（= m.share，右对齐百分比就是它）；
   条内浅色段 = 该模型的缓存读取 token，与条长同分母（窗口内全部模型 token），可直接相加。 */
.track{height:6px;border-radius:4px;background:var(--track);overflow:hidden;margin-top:7px}
.track>.tf{display:block;height:100%;background:var(--accent);border-radius:4px;overflow:hidden}
.track>.tf>.tc{display:block;height:100%;background:#8fb0fb;border-radius:4px 0 0 4px}
.legend{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:11px;margin-bottom:8px;flex-wrap:wrap}
.dot{width:9px;height:9px;border-radius:3px;display:inline-block}
/* ---- 日历热力图 ----
   星期标签和日期格在**同一个 CSS Grid** 里：标签占第 1 列、落在与对应日期行相同的
   网格行上，行高由该行的格子决定。这样两者的中心必然重合，不依赖任何 padding-top /
   line-height 补偿，也不会随窗口宽度漂移（UI-2）。
   格子由列宽驱动（aspect-ratio:1/1），所以 12 个月在常规宽度下自动压缩铺满，
   只有视口 <720px 时才由媒体查询给出最小宽度并允许横向滚动（UI-1）。 */
.hm{margin:2px 0 0}
.hm .cols{overflow-x:auto;overflow-y:hidden}
/* margin 是悬停呼吸空间：cell:hover 的 transform:scale 会把变换盒子计入 .cols 的
   可滚动区域，最右侧格子放大会超出容器右缘 ~2.3px，凭空多出横向滚动条。
   左右各留 4px 后，任何视口下悬停边缘格子都不再触发滚动条。 */
.hm .hm-grid{display:grid;gap:var(--gap);min-width:0;margin:0 4px}
.hm .mcell{grid-row:1;white-space:nowrap;overflow:hidden;color:var(--muted);font-size:9.5px;line-height:14px}
.hm .dlbl{grid-column:1;align-self:center;justify-self:center;color:var(--muted);
  font-size:9px;line-height:1}
.hm .cell{width:100%;aspect-ratio:1/1;align-self:center;border-radius:3px;background:var(--track);
  transition:transform .08s ease}
.hm .cell:hover{transform:scale(1.35);outline:1px solid var(--accent);outline-offset:1px;position:relative;z-index:2}
.hm .cell[data-lv="1"]{background:#dbe6fe}
.hm .cell[data-lv="2"]{background:#a8c2fb}
.hm .cell[data-lv="3"]{background:#6b93f8}
.hm .cell[data-lv="4"]{background:#2f57d6}
.hm .legend{margin:10px 0 0;justify-content:flex-end;gap:4px}
.hm .legend i{width:11px;height:11px;border-radius:3px;display:inline-block}
/* 悬浮提示层：position:fixed 自绘，读格子的 data-tip，贴近视口边缘时自动翻转/夹紧 */
.hm-tip{position:fixed;z-index:99;padding:7px 10px;border-radius:8px;
  box-sizing:border-box;max-width:min(320px,calc(100vw - 12px));max-height:calc(100vh - 12px);
  overflow-x:hidden;overflow-y:auto;overflow-wrap:anywhere;white-space:normal;
  background:rgba(31,35,41,.94);color:#fff;font-size:11.5px;line-height:1.5;
  pointer-events:none;box-shadow:0 4px 14px rgba(16,24,40,.22)}
.hm-tip[hidden]{display:none}
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
/* ---- 时间尺度分段控件（今日 / 7 天 / 30 天）---- */
.tscale{display:flex;align-items:center;gap:10px;margin:0 0 12px;flex-wrap:wrap}
.seg{display:inline-flex;gap:4px;padding:4px;background:var(--panel2);border:1px solid var(--border);border-radius:10px;flex:none}
.seg button{font:inherit;font-size:12px;font-weight:600;color:var(--muted);background:transparent;
  border:0;border-radius:7px;padding:6px 13px;cursor:pointer;min-width:max-content}
.seg button[aria-pressed="true"]{background:var(--panel);color:var(--accent);box-shadow:var(--shadow)}
.seg button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.scope{color:var(--muted);font-size:11px;flex:1 1 300px;min-width:0}
/* ---- 上下文用量分类树 ----
   分支节点是 role=treeitem 的按钮（tabindex=0），Enter/Space/←/→ 均可展开折叠（见 wireTree）。 */
.tree{margin:2px 0 0}
.trow{display:grid;grid-template-columns:13px minmax(0,1fr) auto auto;gap:2px 8px;align-items:center;
  padding:6px;border-radius:8px;font-size:12.5px}
.trow[data-tree-toggle]{cursor:pointer}
.trow[data-tree-toggle]:hover{background:var(--panel2)}
.trow:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.trow .tw{color:var(--muted);font-size:10px;line-height:1;justify-self:center}
.trow .tl{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.trow .tu{justify-self:end}
.trow .tv{justify-self:end;font-weight:650;font-variant-numeric:tabular-nums;min-width:74px;text-align:right}
.tkids{margin:0 0 4px 13px;border-left:1px dashed var(--border);padding-left:8px}
.tkids[hidden]{display:none}
.tchild{color:var(--text)}
.tdisc{color:var(--muted);font-size:11px;line-height:1.65;margin-top:10px;padding-top:9px;border-top:1px dashed var(--border)}
@media(max-width:720px){
  .grid4{grid-template-columns:repeat(2,1fr)}
  .grid2{grid-template-columns:1fr}
  .page{padding:16px 12px 40px}
  .big{font-size:32px}
  .mrow{gap:2px 8px}
  .trow{grid-template-columns:13px minmax(0,1fr) auto auto;font-size:12px}
}
/* 只有视口 <720px 才允许热力图横向滚动；≥720px 一律自适应铺满 12 个月，不出横向滚动条。 */
@media(max-width:719.98px){
  .hm .hm-grid{min-width:var(--minw,560px)}
}
"""

_RUNTIME = """
// 缺失字段 / NaN / Infinity 一律当 0：旧版 payload 少字段时不能让 NaN 渲染进页面。
var num = function(n){ var v = Number(n); return isFinite(v) ? v : 0; };
var fmt = function(n){
  n = num(n);
  if (n >= 1e12) return (n/1e12).toFixed(2) + "T";
  if (n >= 1e9) return (n/1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n/1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n/1e3).toFixed(1) + "K";
  return String(Math.round(n));
};
var fmtInt = function(n){ return num(n).toLocaleString("en-US"); };
var pct = function(x){ x = Number(x); return (isFinite(x) ? (x*100).toFixed(1) : "0.0") + "%"; };
var esc = function(s){ return String(s==null?"":s).replace(/[&<>"]/g, function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]; }); };
var timeCN = function(ms){ try { return new Date(ms).toLocaleString("zh-CN",{hour12:false}); } catch(e){ return "-"; } };
var dateCN = function(ms){ try { return new Date(ms).toLocaleDateString("zh-CN"); } catch(e){ return "-"; } };
// 本地日期 → "YYYY-MM-DD"（不能用 toISOString，那是 UTC，会差一天）
var ds = function(d){
  var m = d.getMonth()+1, day = d.getDate();
  return d.getFullYear() + "-" + (m<10?"0":"") + m + "-" + (day<10?"0":"") + day;
};
var DOW_CN = ["周日","周一","周二","周三","周四","周五","周六"];
// "YYYY-MM-DD" → 本地 00:00 的 Date。不能写 new Date("YYYY-MM-DD")：那按 UTC 解析，
// 负时区会整体差一天，星期也会错。
var parseDay = function(key){
  var p = String(key == null ? "" : key).split("-");
  if (p.length !== 3) return null;
  var y = Number(p[0]), m = Number(p[1]), d = Number(p[2]);
  if (!isFinite(y) || !isFinite(m) || !isFinite(d)) return null;
  var dt = new Date(y, m - 1, d);
  return isNaN(dt.getTime()) ? null : dt;
};
var dowOf = function(key){
  var dt = parseDay(key);
  return dt ? DOW_CN[dt.getDay()] : "星期未知";
};
// 当日缓存命中率 = 缓存读取 / (缓存读取 + 新输入)。
// 旧版 payload 的 daily 只有 {date, tokens}，缺 read/input 时返回 null（显示「—」），不猜测。
var dayHit = function(rec){
  if (!rec || typeof rec !== "object") return null;
  var rd = Number(rec.read), ix = Number(rec.input);
  if (!isFinite(rd) || !isFinite(ix)) return null;
  var denom = rd + ix;
  return denom > 0 ? rd / denom : null;
};
var optPct = function(x){ return x == null ? "—" : pct(x); };
// 热力图格子的悬浮文案：日期 + 星期 + token 用量 + 缓存命中率（有数据时再补明细）。
var hmTip = function(rec, key){
  var v = rec ? num(rec.tokens) : 0;
  var out = [String(key == null ? "" : key) + " " + dowOf(key),
             v > 0 ? fmt(v) + " tokens" : "无调用",
             "缓存命中率 " + optPct(dayHit(rec))];
  if (rec && v > 0){
    var rd = Number(rec.read), ix = Number(rec.input);
    if (isFinite(rd) || isFinite(ix))
      out.push("缓存读取 " + fmt(isFinite(rd)?rd:0) + " / 新输入 " + fmt(isFinite(ix)?ix:0));
    if (isFinite(Number(rec.requests))) out.push("请求 " + fmtInt(rec.requests) + " 次");
  }
  return out.join(" · ");
};

// 12 个月日历热力图（GitHub 风格）。daily 只覆盖有数据的日子，其余补 0。
// 星期标签与日期格共用同一个 CSS Grid（第 1 列放标签），所以标签行中心与对应日期行中心
// 由布局本身保证重合，不靠 padding-top / line-height 补偿（UI-2）。
function heatmapCard(t){
  t = t || {};
  var daily = t.daily || [];
  var map = {};
  daily.forEach(function(d){ if (d && d.date) map[d.date] = d; });

  // 分位数分档（只对 >0 的值取分位，避免大量 0 拉低阈值）
  var vals = daily.map(function(d){ return num(d && d.tokens); })
                  .filter(function(v){ return v > 0; })
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
  var monthLabels = [];   // { col, text }；col 是 grid 列号（第 1 列留给星期标签）
  var cells = [];
  weeks.forEach(function(wk, ci){
    var m = wk[wk.length-1].getMonth();
    if (m !== lastMonth){ lastMonth = m; monthLabels.push({ col: ci + 2, text: (m+1) + "月" }); }
    wk.forEach(function(day, ri){
      var key = ds(day);
      var rec = map[key];
      var tip = hmTip(rec, key);
      cells.push('<div class="cell" role="img" data-date="' + key + '" data-row="' + ri +
        '" data-lv="' + level(rec ? num(rec.tokens) : 0) + '" aria-label="' + esc(tip) +
        '" data-tip="' + esc(tip) + '" style="grid-column:' + (ci + 2) +
        ';grid-row:' + (ri + 2) + '"></div>');
    });
  });
  var nc = weeks.length;
  // 格子尺寸由列宽驱动；这个下限只在 <720px 的横向滚动模式下生效。
  var cellMin = nc > 56 ? 12 : nc > 48 ? 13 : 15;

  // 周一/三/五 → 星期序号 1/3/5 → 日期行 ri 对应 grid-row ri+2（第 1 行是月份行）。
  var dow = [[1,"一"],[3,"三"],[5,"五"]].map(function(p){
    return '<div class="dlbl" aria-hidden="true" data-dow="' + p[0] +
      '" style="grid-row:' + (p[0] + 2) + '">' + p[1] + '</div>';
  }).join("");
  // span 必须夹在网格内，否则 grid 会生成隐式列，把宽度撑出去（历史实现的潜在溢出源）。
  var months = monthLabels.map(function(l){
    return '<div class="mcell" style="grid-column:' + l.col + ' / span ' +
      Math.max(1, Math.min(3, nc + 2 - l.col)) + '">' + l.text + '</div>';
  }).join("");

  // 图例色块必须内联写尺寸：这个 div 在 .hm 容器之外，.hm .legend i 选择器匹配不到。
  var sw = function(bg){
    return '<i style="display:inline-block;width:11px;height:11px;border-radius:3px;background:' + bg + '"></i>';
  };
  var legend = '<div class="legend" style="margin:10px 0 0;justify-content:flex-end;gap:4px">少' +
    sw("var(--track)") + sw("#dbe6fe") + sw("#a8c2fb") + sw("#6b93f8") + sw("#2f57d6") + '多</div>';

  var total = daily.reduce(function(a, d){ return a + num(d && d.tokens); }, 0);
  var active = daily.filter(function(d){ return d && num(d.tokens) > 0; }).length;

  return '<div class="card" style="margin-top:12px">' +
    '<h3>Token 热力图 · 最近 12 个月（' + active + ' 天有调用 · 合计 ' + fmt(total) + '）</h3>' +
    '<div class="hm" style="--gap:' + gap + 'px;--minw:' + (nc * (cellMin + gap)) + 'px">' +
      '<div class="cols">' +
        '<div class="hm-grid" style="grid-template-columns:14px repeat(' + nc +
          ',minmax(0,1fr));grid-template-rows:auto repeat(7,auto)">' +
          months + dow + cells.join("") +
        '</div>' +
      '</div>' +
    '</div>' + legend + '</div>';
}

function modelCard(t){
  var models = (t && t.models) || [];
  if (!models.length) return '<div class="card" style="margin-top:12px"><h3>按模型的调用情况</h3>' +
    '<div class="muted small">该范围内暂无模型调用</div></div>';
  var rows = models.map(function(m, i){
    m = m || {};
    var tok = num(m.tokens);
    var rd = num(m.read);
    var ix = num(m.input);
    var denom = rd + ix;
    // 缓存命中率 = 缓存读取 /（缓存读取 + 新输入）；分母为 0 时按 0 显示。
    var hit = denom > 0 ? rd / denom : 0;
    var hitColor = hit >= .8 ? 'var(--good)' : hit >= .5 ? 'var(--warn)' : 'var(--bad)';
    // 条长 = m.share（该模型 token / **窗口内全部模型** token），不退化为「相对最大模型」。
    // 缺失 / NaN / 越界一律夹到 [0,1]，避免 0 token 或异常数据把布局撑破。
    var share = num(m.share);
    if (share < 0) share = 0;
    if (share > 1) share = 1;
    // 浅色段 = 该模型的缓存读取 token。它与条长同分母（窗口内全部模型 token），
    // 所以它在整条上的实际宽度 = share × (缓存读取 / 该模型 token)。0 时不渲染该段。
    var cacheFrac = (tok > 0 && rd > 0) ? Math.min(1, rd / tok) : 0;
    var seg = cacheFrac > 0
      ? '<i class="tc" style="width:' + (cacheFrac * 100).toFixed(2) + '%"></i>' : "";
    var rank = (i + 1) < 10 ? "0" + (i + 1) : String(i + 1);
    var label = String(m.name == null ? "" : m.name) + "：token 份额 " + pct(share) +
      (cacheFrac > 0 ? "，其中缓存读取 " + pct(cacheFrac) : "，无缓存读取");
    return '<div class="mrow" data-model="' + esc(m.name) + '" data-share="' + share.toFixed(6) +
        '" data-cache-frac="' + cacheFrac.toFixed(6) + '" data-tokens="' + tok +
        '" data-read="' + rd + '" data-input="' + ix + '">' +
      '<div class="mmain">' +
        '<div class="mname"><span class="rank" aria-hidden="true">' + rank + '</span>' + esc(m.name) +
          ' <span class="small muted">' + esc(m.providerID || "") + '</span></div>' +
        '<div class="msub">' + fmt(tok) + ' tokens · 缓存命中率 <b style="color:' + hitColor + '">' +
          pct(hit) + '</b> <span class="muted">（命中 ' + fmt(rd) + ' / 新输入 ' + fmt(ix) +
          ' · ' + fmtInt(m.requests) + ' 次请求 · ' + fmtInt(m.tools || 0) + ' 工具）</span></div>' +
        // 每个模型**只有这一条**进度条：<i class="tf"> 是份额段，里面再叠浅色的缓存读取段。
        '<div class="track" role="img" aria-label="' + esc(label) + '" title="' + esc(label) + '">' +
          '<i class="tf" style="width:' + (share * 100).toFixed(2) + '%">' + seg + '</i>' +
        '</div>' +
      '</div>' +
      '<div class="mval">' + pct(share) + '</div></div>';
  }).join("");

  // data-model-total 是 share 的分母（窗口内全部模型 token，含未展示的模型），
  // 便于回归测试直接核对 share 的口径，而不是「相对最大模型」。
  var shownTotal = models.reduce(function(a, m){ return a + num(m && m.tokens); }, 0);
  return '<div class="card" style="margin-top:12px" data-model-total="' +
      num(t && t.modelTotal !== undefined ? t.modelTotal : shownTotal) +
      '" data-model-count="' + num(t && t.modelCount !== undefined ? t.modelCount : models.length) +
      '" data-model-shown="' + models.length + '"><h3>按模型的调用情况</h3>' +
    '<div class="legend" style="margin-bottom:6px">' +
      '<span class="dot" style="background:var(--accent)"></span>token 份额（分母 = 全部模型 token）' +
      '<span class="dot" style="background:#8fb0fb;margin-left:8px"></span>浅色段 = 该模型的缓存读取' +
    '</div>' + rows + '</div>';
}

// ---- 时间尺度（今日 / 7 天 / 30 天）----
// 三份窗口数据在生成时就算好并内联进 DATA.token.windows。切换只是换一份**已经存在**的
// 数字：不重新读库、不发请求、不重新生成 HTML、不依赖服务端（C1/C2/C4）。
var _WIN = null;   // 当前选中的窗口 key；null = 用 payload 默认（--days 对应的那一份）

function windowKeys(t){
  var w = t && t.windows;
  if (!w || typeof w !== "object") return [];
  var keys = t.windowKeys;
  if (!keys || !keys.length) keys = Object.keys(w);
  return keys.filter(function(k){ return k && w[k]; })
             .sort(function(a, b){ return num(a) - num(b); });
}
function winLabel(k){
  var n = num(k);
  if (n === 1) return "今日";
  return n > 0 ? n + " 天" : String(k);
}
// 界面上的一句话口径。30 天保持与原实现逐字一致（回归基线）。
function winScope(k, days){
  if (k == null || k === "") return "最近 " + (num(days) || 30) + " 天";
  var n = num(k);
  if (n === 1) return "今日（本地 00:00 起）";
  return n > 0 ? "最近 " + n + " 天" : String(k);
}
// 当前要显示的窗口。旧版 payload 没有 windows 时退回顶层字段（受 --days 限制），安全降级。
function currentWindow(t){
  t = t || {};
  var keys = windowKeys(t);
  var legacy = { days: t.days, totals: t.totals, models: t.models, recent: t.recent };
  if (!keys.length) return { key: null, win: legacy };
  var pick = (_WIN != null && t.windows[_WIN]) ? _WIN : null;
  if (pick == null){
    var d = String(num(t.days) || 30);
    pick = t.windows[d] ? d : (t.windows["30"] ? "30" : keys[keys.length - 1]);
  }
  return { key: pick, win: t.windows[pick] || legacy };
}
function winControl(keys, sel){
  return '<div class="tscale"><div class="seg" role="group" aria-label="时间尺度">' +
    keys.map(function(k){
      return '<button type="button" data-win="' + esc(k) + '" aria-pressed="' +
        (k === sel ? "true" : "false") + '">' + winLabel(k) + '</button>';
    }).join("") + '</div>' +
    // 不同口径必须写清楚：会话数与热力图不随时间尺度变化，避免被当成窗口内的数字。
    '<div class="scope">口径：' + esc(winScope(sel, null)) +
      '，按本地自然日 00:00 切分；<b>会话数</b>为全部会话总数，<b>热力图</b>为最近 12 个月，二者均不随时间尺度变化。' +
    '</div></div>';
}

function tokenCard(t){
  t = t || {};
  var cur = currentWindow(t);
  var w = cur.win || {};
  var tot = w.totals || {};
  var keys = windowKeys(t);

  // 8 个统计小卡片（两行 × 4），全部是**当前时间窗口**口径。
  // 字段名与 Python card_data 的 totals 严格对应：input/output 为原始值，缓存在 cacheRead。
  var cards = [
    { k: '输入 tokens（含缓存读取）', v: fmt(num(tot.input) + num(tot.cacheRead)) },
    { k: '输出 tokens', v: fmt(num(tot.output) + num(tot.reasoning)) },
    { k: '缓存读取', v: fmt(tot.cacheRead) },
    { k: '缓存命中率', v: pct(tot.cacheShare) },
    { k: '请求数', v: fmtInt(tot.requests) },
    { k: '工具调用', v: fmtInt(tot.tools) },
    // 会话数来自 session 表全量行数，不随窗口变化；这里显式标注，避免与窗口内数字混淆。
    { k: '会话数', v: fmtInt(tot.sessions), t: '全部会话总数，不随时间尺度变化' },
    { k: '平均每次请求（tokens）', v: fmt(num(tot.requests) ? num(tot.tokens) / num(tot.requests) : 0) }
  ].map(function(c){
    return '<div class="card"' + (c.t ? ' title="' + esc(c.t) + '"' : '') + '><div class="num">' +
      c.v + '</div><div class="lb">' + c.k + '</div></div>';
  }).join("");

  return (keys.length >= 2 ? winControl(keys, cur.key) : "") +
    '<div class="card hero">' +
    '<div class="label">' + esc(winScope(cur.key, w.days)) + ' · tokens 消耗</div>' +
    '<div class="big">' + fmt(tot.tokens) + '<span>tokens</span></div>' +
    sparkline(w.recent || t.daily) + '</div>' +
    '<div class="grid4" style="margin-top:12px">' + cards + '</div>' +
    heatmapCard(t) +
    modelCard(w);
}

function sparkline(daily){
  var spark = (daily || []).slice(-30);
  var maxD = Math.max.apply(null, [1].concat(spark.map(function(d){ return num(d && d.tokens); })));
  var bars = spark.map(function(d){
    d = d || {};
    return '<i style="height:' + Math.max(3, (num(d.tokens)/maxD)*40).toFixed(1) + 'px" title="' +
      esc(d.date) + ' ' + esc(dowOf(d.date)) + ' · ' + fmt(d.tokens) + ' tokens"></i>';
  }).join("");
  return '<div class="spark">' + bars + '</div>';
}

// ---- 热力图悬浮提示层（UI-1）----
// 自绘固定定位层，读格子的 data-tip。贴近视口右/下边缘时自动夹紧或翻转到格子下方。
function tipLayer(){
  try {
    var el = document.getElementById("hm-tip");
    if (el) return el;
    if (!document.body || typeof document.createElement !== "function") return null;
    el = document.createElement("div");
    el.id = "hm-tip";
    el.className = "hm-tip";
    el.setAttribute("role", "tooltip");
    el.hidden = true;
    document.body.appendChild(el);
    return el;
  } catch (e) { return null; }
}
function placeTip(el, cell){
  try {
    var r = cell.getBoundingClientRect();
    var bw = num(el.offsetWidth) || 220, bh = num(el.offsetHeight) || 24;
    var vw = (typeof window !== "undefined" && num(window.innerWidth)) || 1024;
    var vh = (typeof window !== "undefined" && num(window.innerHeight)) || 768;
    var x = r.left + r.width / 2 - bw / 2;
    if (x + bw > vw - 6) x = vw - bw - 6;
    if (x < 6) x = 6;
    var y = r.top - bh - 8;
    if (y < 6) y = r.bottom + 8;
    // Prefer a fully visible tooltip; allow a clipped viewport only when
    // the viewport itself is smaller than the reserved 12px safety margin.
    y = Math.max(6, Math.min(y, Math.max(6, vh - bh - 6)));
    el.style.left = x.toFixed(1) + "px";
    el.style.top = y.toFixed(1) + "px";
  } catch (e) { /* 拿不到布局时静默跳过：提示层本身照常显示 */ }
}
// 事件委托挂在 root 上，所以切换视图/时间尺度导致的 innerHTML 重建不会丢监听。
function wireHeatmap(host){
  if (!host || typeof host.addEventListener !== "function") return false;
  var tip = null;
  var cellOf = function(node){
    return (node && typeof node.closest === "function") ? node.closest(".hm .cell") : null;
  };
  var show = function(ev){
    var cell = cellOf(ev.target);
    if (!cell){ if (tip) tip.hidden = true; return; }
    if (!tip) tip = tipLayer();
    if (!tip) return;
    tip.textContent = cell.getAttribute("data-tip") || "";
    tip.hidden = false;
    placeTip(tip, cell);
  };
  host.addEventListener("mouseover", show);
  host.addEventListener("focusin", show);
  host.addEventListener("mousemove", function(ev){
    if (!tip || tip.hidden) return;
    var cell = cellOf(ev.target);
    if (cell) placeTip(tip, cell);
  });
  host.addEventListener("mouseout", function(ev){
    if (tip && cellOf(ev.target)) tip.hidden = true;
  });
  return true;
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

// ---- 上下文用量分类树 ----
// 只展示数据库里真实存在、可逐项核对的字段。单位不同的分支分开标注（tokens / 次 / 条），
// 不把输入 token、缓存读取 token、输出 token、请求次数或会话数混为一谈，也不做任何
// 「按比例分摊」的猜测；库中还原不出来的分类明确列出并说明原因。
function treeKids(branch, path, collapsed){
  var kids = (branch && branch.children) || [];
  var total = num(branch && branch.total);
  var unit = String((branch && branch.unit) || "");
  var hid = collapsed ? " hidden" : "";
  // 统计口径说明属于对应分支的折叠内容，不能成为树节点的常驻兄弟行。
  var note = branch && branch.note ? '<div class="tnote">' + esc(branch.note) + '</div>' : '';
  if (!kids.length){
    return '<div class="tkids" id="' + path + '" role="group"' + hid + '>' +
      note + '<div class="tnote">（无可细分的子项）</div></div>';
  }
  return '<div class="tkids" id="' + path + '" role="group"' + hid + '>' + note + kids.map(function(c){
    var v = num(c && c.value);
    return '<div class="trow tchild" role="treeitem" aria-level="2" data-leaf="' +
        esc(c && c.label) + '" data-value="' + v + '">' +
      '<span class="tw" aria-hidden="true"></span>' +
      '<span class="tl">' + esc(c && c.label) + '</span>' +
      '<span class="tu muted small">' + (total > 0 ? pct(v / total) : "—") + '</span>' +
      '<span class="tv">' + fmt(v) + ' ' + esc(unit) + '</span></div>';
  }).join("") + '</div>';
}
function contextTree(bd){
  var branches = (bd && bd.branches) || [];
  if (!branches.length){
    return '<div class="card" style="margin-top:12px"><h3>上下文用量分类</h3>' +
      '<div class="muted small">可细分的上下文数据不足，暂无可展示的分类</div></div>';
  }
  var rows = branches.map(function(b, i){
    var path = "tk-" + i;
    var kids = (b && b.children) || [];
    var open = kids.length > 0 ? false : false; // 默认全部折叠（含无子项分支），保证 a11y 一致
    var mark = kids.length ? (open ? "▾" : "▸") : "·";
    return '<div class="trow" role="treeitem" aria-level="1" aria-expanded="' + (open ? "true" : "false") + '"' +
        ' aria-controls="' + path + '" tabindex="0" data-tree-toggle="' + path + '"' +
        ' data-branch="' + esc(b.label) + '" data-unit="' + esc(b.unit) +
        '" data-total="' + num(b.total) + '">' +
      '<span class="tw" aria-hidden="true">' + mark + '</span>' +
      '<span class="tl">' + esc(b.label) + '</span>' +
      '<span class="tu muted small">' + esc(b.unit) + '</span>' +
      '<span class="tv">' + fmt(b.total) + '</span></div>' +
      treeKids(b, path, !open);
  }).join("");
  var na = (bd && bd.notSubdividable) || {};
  var labels = (na.labels || []).map(function(x){ return esc(x); }).join(" / ");
  return '<div class="card" style="margin-top:12px"><h3>上下文用量分类</h3>' +
    '<div class="tree" role="tree" aria-label="上下文用量分类">' + rows + '</div>' +
    '<div class="tdisc"><b>可核对</b>：每个分支的合计等于其子项之和（差额单列为「未分类」）；' +
      '「次」与「条」是计数口径，不与 tokens 相加。<br>' +
      '<b>无法细分</b>：' + (labels || "—") + '。<br>' + esc(na.reason || "") + '</div></div>';
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
    '<div class="label">' + (c.selectionMode === "fixed" ? "已选择会话上下文占用" : "最近活跃会话上下文占用") + '</div>' +
    '<div class="small muted">会话：' + esc(c.title) + ' · ' + esc((c.sessionId || "").slice(0, 12)) + '</div>' +
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
    '</div>' +
    contextTree(c.breakdown);
}

// ---- 分类树的展开 / 折叠 + 键盘操作 ----
// 分支行是 tabindex=0 的 role=treeitem：Enter / Space 切换，← 折叠，→ 展开。
function toggleTreeNode(node){
  if (!node || typeof node.getAttribute !== "function") return null;
  var id = node.getAttribute("aria-controls") || node.getAttribute("data-tree-toggle");
  var next = node.getAttribute("aria-expanded") === "false";
  node.setAttribute("aria-expanded", next ? "true" : "false");
  try {
    var kids = id ? document.getElementById(id) : null;
    if (kids) kids.hidden = !next;
  } catch (e) { /* 折叠容器缺失时状态属性仍然更新，可访问性语义不受影响 */ }
  try {
    var mk = typeof node.querySelector === "function" ? node.querySelector(".tw") : null;
    if (mk) mk.textContent = next ? "▾" : "▸";
  } catch (e) { /* 同上 */ }
  return next;
}
function wireTree(host){
  if (!host || typeof host.addEventListener !== "function") return false;
  var nodeOf = function(node){
    if (!node || typeof node.closest !== "function") return null;
    var n = node.closest("[data-tree-toggle]");
    if (!n) return null;
    if (typeof host.contains === "function" && !host.contains(n)) return null;
    return n;
  };
  host.addEventListener("click", function(ev){
    var n = nodeOf(ev.target);
    if (n) toggleTreeNode(n);
  });
  host.addEventListener("keydown", function(ev){
    var n = nodeOf(ev.target);
    if (!n) return;
    var k = ev.key, open = n.getAttribute("aria-expanded") !== "false";
    if (k === "ArrowRight"){ if (open) return; }
    else if (k === "ArrowLeft"){ if (!open) return; }
    else if (k !== "Enter" && k !== " " && k !== "Spacebar") return;
    if (ev.preventDefault) ev.preventDefault();
    toggleTreeNode(n);
  });
  return true;
}

function renderTab(kind, data, win){
  // 显式传 win 时切换当前窗口（内部与测试用）；只传两个参数时沿用当前选择。
  if (win !== undefined) _WIN = win;
  if (kind === "token") return tokenCard((data && data.token) || { totals:{}, models:[], daily:[], days:30 });
  if (kind === "session") return sessionCard((data && data.session) || { top: [] });
  if (kind === "context") return contextCard(data && data.context);
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
  // Issue #16 切片 2：用量范围两档开关。「会话用量」= 看被定格的会话；
  // 「全局用量」= 看全库聚合（与会话无关）。控件只负责切标签 + 把选择写进 URL 的
  // mode 参数（读写都在页内 glue 里完成），因此状态可见、且刷新后保持。
  var scope = '<div class="seg" role="group" aria-label="用量范围" ' +
    'style="margin-top:10px;display:inline-flex">' +
    '<button type="button" data-scope="session" aria-pressed="false">会话用量</button>' +
    '<button type="button" data-scope="global" aria-pressed="false">全局用量</button>' +
    '</div>' +
    '<button class="btn" type="button" id="reset-pin" style="margin-left:8px">重置定格</button>';
  return nav + scope + '<div id="tab-content" role="tabpanel" aria-labelledby="tab-token">' +
    renderTab("token", data) + '</div>';
}

function footer(data){
  var gen = new Date(data.generatedAt || Date.now()).toLocaleString("zh-CN", { hour12: false });
  var live = (typeof LIVE !== "undefined" && LIVE) ? LIVE.reportUrl : "";
  var stamp = '<span id="last-ok">最近成功更新：' + gen + '</span>';
  var mode = live
    ? '<span class="muted" id="live-mode">实时模式 · 可点击右上角刷新</span>'
    : '<span class="muted" id="live-mode">静态快照 · 无页内刷新服务</span>';
  return '<div class="foot"><span>MiMo Desktop 本机统计 · 数据不出网 · 仅本地只读' +
    (live ? ' · loopback' : '') + '</span>' + mode + stamp + '</div>';
}

// ---- 页内刷新（仅当页面由本地只读服务携带 LIVE 配置时可用）----
// 刷新语义：fetch 最新 card_data JSON → 整页重建 tab/图表 DOM。
// 失败时**保留上次成功数据**，只显示错误条，绝不假装成功。
var _REFRESHING = false;
function showRefreshBanner(msg, isErr){
  var el = document.getElementById("refresh-banner");
  if (!el) return;
  if (!msg){ el.hidden = true; el.textContent = ""; el.className = "tip"; return; }
  el.hidden = false;
  el.textContent = msg;
  el.className = isErr ? "err" : "tip";
}
function paintData(next){
  if (!next || typeof next !== "object") throw new Error("空数据");
  DATA = next;
  if (DATA.kind && DATA.kind !== "all") _TAB = DATA.kind;
  var sub = document.querySelector(".head .sub");
  if (sub){
    var gen = new Date(DATA.generatedAt || Date.now()).toLocaleString("zh-CN", { hour12: false });
    sub.textContent = "更新时间 " + gen + " · 数据源 " + (DATA.dbLabel || "mimocode.db");
  }
  var rootEl = document.getElementById("root");
  if (!rootEl) throw new Error("缺少 #root");
  rootEl.innerHTML = buildBody(DATA) + footer(DATA);
  wireHeatmap(rootEl);
  wireTree(rootEl);
  var last = document.getElementById("last-ok");
  if (last){
    last.textContent = "最近成功更新：" +
      new Date(DATA.generatedAt || Date.now()).toLocaleString("zh-CN", { hour12: false });
  }
  return true;
}
function refreshData(){
  if (_REFRESHING) return Promise.resolve(false);
  var btn = document.getElementById("regen");
  var url = (typeof LIVE !== "undefined" && LIVE && LIVE.reportUrl) ? LIVE.reportUrl : "";
  if (!url){
    showRefreshBanner("当前页面没有连接本地刷新服务（静态快照）。请用 /usage-open 或 python usage_cli.py --open 打开实时看板。", true);
    return Promise.resolve(false);
  }
  _REFRESHING = true;
  if (btn){ btn.disabled = true; btn.textContent = "刷新中…"; btn.setAttribute("aria-busy", "true"); }
  showRefreshBanner("正在读取最新用量…", false);
  var ctl = (typeof AbortController !== "undefined") ? new AbortController() : null;
  var timer = setTimeout(function(){ try { if (ctl) ctl.abort(); } catch (e) {} }, 8000);
  return fetch(url, {
    method: "GET",
    cache: "no-store",
    credentials: "omit",
    signal: ctl ? ctl.signal : undefined
  }).then(function(res){
    clearTimeout(timer);
    if (!res || !res.ok) throw new Error("HTTP " + (res ? res.status : 0));
    return res.json();
  }).then(function(json){
    paintData(json);
    showRefreshBanner("", false);
    var st = document.getElementById("refresh-status");
    if (st) st.textContent = "";
    return true;
  }).catch(function(err){
    clearTimeout(timer);
    var msg = "刷新失败：无法连接本地统计服务或请求超时。已保留上一次成功的数据。";
    if (err && err.message && err.message !== "HTTP 0") msg += "（" + err.message + "）";
    showRefreshBanner(msg + " 可稍后重试；若服务已退出，请重新运行 --open。", true);
    return false;
  }).then(function(ok){
    _REFRESHING = false;
    if (btn){ btn.disabled = false; btn.textContent = "刷新数据"; btn.removeAttribute("aria-busy"); }
    return ok;
  });
}
// 暴露给自动化测试（真实浏览器与 jsdom 均可用）
try {
  if (typeof globalThis !== "undefined") {
    globalThis.__usageRefresh = refreshData;
    globalThis.__usagePaint = paintData;
    // 只读快照：CDP/e2e 无法直接读 IIFE 内的 DATA/LIVE
    globalThis.__usageSnapshot = function(){
      return { data: DATA, live: LIVE, tab: _TAB, refreshing: _REFRESHING };
    };
  }
} catch (e) { /* file:// 下也可能可用；失败可忽略 */ }
"""


def _public_payload(data: dict) -> dict:
    """给网页/API 用的脱敏副本：不暴露数据库绝对路径等内部信息。"""
    out = json.loads(json.dumps(data, ensure_ascii=False))
    db_path = out.pop("dbPath", "") or ""
    out["dbLabel"] = os.path.basename(db_path.replace("\\", "/")) if db_path else "mimocode.db"
    return out


def render_html(data: dict, *, cmd: str = "", out_path: str = "", live: dict | None = None) -> str:
    public = _public_payload(data)
    safe = (json.dumps(public, ensure_ascii=False)
            .replace("<", "\\u003c").replace(" ", "\\u2028").replace(" ", "\\u2029"))
    kind = public.get("kind", "all")
    title = _html.escape(KIND_TITLE.get(kind, "MiMo Desktop 用量"))
    gen = datetime.fromtimestamp((public.get("generatedAt") or 0) / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
    cmd_esc = _html.escape(cmd)
    db_label = _html.escape(public.get("dbLabel") or "mimocode.db")

    live_js = "null"
    btn_label = "重新生成"
    if live:
        live_js = json.dumps({
            "reportUrl": live["reportUrl"],
            "pageUrl": live.get("pageUrl", ""),
        }, ensure_ascii=False)
        btn_label = "刷新数据"
        tip = '<div class="tip" id="refresh-banner" hidden></div>'
    else:
        tip = (
            '<div class="tip" id="refresh-banner">静态快照：页面内无法直连数据库。'
            '需要页内刷新时请用 <code style="user-select:all;color:var(--text)">'
            f'{cmd_esc or "python usage_cli.py --open"}</code> 打开实时看板。</div>'
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
      <div class="sub">更新时间 {gen} · 数据源 {db_label}</div>
    </div>
    <div class="acts">
      <button class="btn" id="regen" type="button">{btn_label}</button>
    </div>
  </div>
  {tip}
  <div class="wrap" id="root"></div>
</div>
<script>
(function(){{
  var DATA = {safe};
  var LIVE = {live_js};
  var CMD = {json.dumps(cmd)};
  var OUT = {json.dumps(out_path)};
  {_RUNTIME}
  var root = document.getElementById("root");
  // 当前视图 / 当前时间尺度。_WIN 为 null 时用 payload 的默认窗口（--days 对应的那一份）。
  var _TAB = (DATA.kind && DATA.kind !== "all") ? DATA.kind : "token";
  root.innerHTML = buildBody(DATA) + footer(DATA);
  // Issue #16：把定格到的会话写进地址栏，便于复制或收藏复用（保留 tab 令牌）。
  // 只在带 tab 令牌、且地址里仍写着 session=latest 时改写；静态快照（LIVE 为 null）不动；
  // 拿不到 history / URL API 时静默跳过，页面照常显示。
  (function(){{
    try {{
      if (!LIVE || !LIVE.pageUrl) return;
      if (typeof history === "undefined" || !history.replaceState) return;
      if (typeof URL === "undefined") return;
      var sid = (DATA.context && DATA.context.sessionId) || "";
      if (!sid) return;
      var u = new URL(LIVE.pageUrl);
      if (!u.searchParams.get("tab")) return;
      if (u.searchParams.get("session") === sid) return;
      u.searchParams.set("session", sid);
      history.replaceState(null, "", u.pathname + u.search);
    }} catch (e) {{ /* 布局/历史 API 不可用时静默跳过 */ }}
  }})();
  // 事件委托挂 root：切换视图或时间尺度后 innerHTML 被重建，监听依然有效。
  wireHeatmap(root);
  wireTree(root);
  root.addEventListener("click", function(ev){{
    var btn = ev.target && ev.target.closest ? ev.target.closest("[data-tab]") : null;
    if (!btn) {{
      // 时间尺度切换：只换用已经内联进 DATA 的另一份窗口数据。
      // 不读数据库、不发请求、不重新生成 HTML。
      var wbtn = ev.target && ev.target.closest ? ev.target.closest("[data-win]") : null;
      if (wbtn) {{
        _WIN = wbtn.getAttribute("data-win");
        var host = document.getElementById("tab-content");
        if (host) {{ host.innerHTML = renderTab(_TAB, DATA); }}
        else {{ root.innerHTML = buildBody(DATA) + footer(DATA); }}
      }}
      return;
    }}
    var selected = btn.getAttribute("data-tab");
    _TAB = selected;
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
    if (LIVE) {{
      refreshData();
      return;
    }}
    var msg = "当前是静态快照，没有连接本地刷新服务。\\n\\n请重新打开实时看板：\\n" +
      "python usage_cli.py --open";
    if (OUT) msg += "\\n\\n快照文件：\\n" + OUT;
    alert(msg);
  }});

  // ---- Issue #16 切片 2：用量范围两档（会话用量 / 全局用量）----
  // 必须放在 root 的 click 监听注册**之后**：切换标签直接复用既有的事件委托。
  (function(){{
    try {{
      var btns = root.querySelectorAll("[data-scope]");
      var rst = document.getElementById("reset-pin");
      if (!btns.length) return;
      var pageUrl = (typeof LIVE !== "undefined" && LIVE) ? (LIVE.pageUrl || "") : "";
      var hasTab = false;
      var curMode = "session";
      try {{
        if (pageUrl) {{
          var u0 = new URL(pageUrl);
          hasTab = !!u0.searchParams.get("tab");
          if (hasTab) curMode = (u0.searchParams.get("mode") === "global") ? "global" : "session";
        }}
      }} catch (e0) {{ /* 没有 URL API：退化为静态行为 */ }}
      // 没有定格就没什么可重置：隐藏按钮而不是给一个点了没反应的入口。
      if (rst && !hasTab) rst.hidden = true;
      var paint = function(mode){{
        btns.forEach(function(b){{
          b.setAttribute("aria-pressed", b.getAttribute("data-scope") === mode ? "true" : "false");
        }});
      }};
      var goTab = function(id){{
        var t = root.querySelector('[data-tab="' + id + '"]');
        if (t) t.click();
      }};
      btns.forEach(function(b){{
        b.addEventListener("click", function(){{
          var m = b.getAttribute("data-scope");
          paint(m);
          goTab(m === "global" ? "token" : "context");
          try {{
            if (pageUrl && typeof location !== "undefined" && location.href) {{
              // 以**当前地址栏**为基准改，而不是用服务端最初给的 pageUrl ——
              // 否则会把已经写进地址栏的定格会话（?session=<id>）打回 latest。
              var u = new URL(location.href);
              u.searchParams.set("mode", m);
              history.replaceState(null, "", u.pathname + u.search);
            }}
          }} catch (e1) {{ /* 位置栏不变，页面仍按选择切换 */ }}
        }});
      }});
      if (rst) {{
        rst.addEventListener("click", function(){{
          try {{
            var u2 = new URL((typeof location !== "undefined" && location.href) ? location.href : pageUrl);
            u2.searchParams.delete("tab");
            u2.searchParams.delete("mode");
            // 关键：地址栏里可能已经被写入了显式的 session=<定格到的 id>（见上面的
            // replaceState）。只删 tab 不足以解除定格 —— 必须把它放回 latest。
            u2.searchParams.set("session", "latest");
            location.assign(u2.pathname + u2.search);
          }} catch (e2) {{ /* ignore */ }}
        }});
      }}
      if (hasTab) {{ paint(curMode); goTab(curMode === "global" ? "token" : "context"); }}
    }} catch (e) {{ /* 控件不可用时静默跳过，页面照常显示 */ }}
  }})();
}})();
</script>
</body></html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

KNOWN_KINDS = ("all", "token", "session", "context")
# "serve" / "service-check" 仅供本机进程间使用（安装器 preflight、测试、调试）；
# 不进入用户 slash 命令白名单语义，但允许 CLI 直接调用。
_INTERNAL_KINDS = ("serve", "service-check", "selfcheck")
_SAFE_FLAGS = {"--open", "--print-path", "--shutdown", "--help", "-h"}
_SAFE_OPTS = {"--days", "--db", "--out", "--session", "--limit",
              "--token", "--idle-minutes", "--port", "--wait-seconds"}


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


def _sanitize_args(argv: list[str], dropped_out: list | None = None) -> list[str]:
    """Allow-list CLI args. Filesystem paths may contain Windows backslashes.

    This is not a shell-escaping function. Registered MiMo templates must not interpolate
    untrusted arguments into shell text; they now execute a fixed command only.

    被丢弃的**选项**（以 ``-`` 开头）会在 stderr 明确提示。静默丢弃曾经造成过真实事故：
    ``usage_cli.py --help`` 里的 ``--help`` 被丢掉，于是变成无参调用——既读了真实数据库、
    又往用户数据目录写了一份报告，还会在没有数据库的机器上直接失败。这类"静默变形"必须
    是可见的。
    """
    out: list[str] = []
    dropped: list[str] = []
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
        elif stripped in KNOWN_KINDS or stripped in _INTERNAL_KINDS:
            out.append(stripped)
        elif stripped.startswith("-"):
            dropped.append(stripped)
        i += 1
    if dropped:
        print(f"提示：已忽略无法识别的选项 {' '.join(dropped)}"
              f"（本命令只接受固定参数集；查看用法请用 --help）", file=sys.stderr)
    if dropped_out is not None:
        dropped_out.extend(dropped)
    return out


def default_out_path(kind: str) -> str:
    """默认写到用户可写数据目录，避免落在 Program Files / 安装目录 / 任意 CWD。"""
    stamp = datetime.now().strftime("%Y%m%d")
    return os.path.abspath(os.path.join(default_data_dir(), f"mimo-usage-{kind}-{stamp}.html"))


def build_command_hint(kind: str, days: int, out_path: str) -> str:
    parts = ["python", os.path.abspath(__file__)]
    if kind != "all":
        parts.append(kind)
    if kind in ("token", "all") and days != 30:
        parts += ["--days", str(days)]
    if out_path:
        parts += ["--out", out_path]
    return " ".join(f'"{p}"' if " " in p else p for p in parts)


# ---------------------------------------------------------------------------
# 本地只读刷新服务（loopback-only）
# ---------------------------------------------------------------------------
#
# 为什么需要它：静态 file:// HTML 无法在页内重新读 SQLite；宿主插件接口没有
# 「刷新已打开页面」的 API（见 docs/plugin-interface.md §7）。因此在用户显式
# --open 时，拉起一个只监听 127.0.0.1 的极小只读 HTTP 服务：
#   GET  /{token}/              → 自包含 HTML（嵌入 LIVE.reportUrl）
#   GET  /{token}/api/report    → card_data JSON（无 dbPath）
#   GET  /{token}/api/ping      → 固定身份标识（不读数据库；身份探测专用）
#   POST /{token}/api/shutdown  → 退出服务（需 token + loopback Host）
# 安全：随机 path token、仅 loopback、Host/Origin 校验、GET-only 数据面、
# 空闲自动退出、不暴露数据库绝对路径。


def server_state_path() -> str:
    return os.path.join(default_data_dir(), "live-server.json")


def _host_is_loopback(host_header: str | None) -> bool:
    if not host_header:
        return False
    host = host_header.strip().lower()
    # strip port
    if host.startswith("["):
        # [::1]:port
        host = host.split("]")[0] + "]"
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    return host in ("127.0.0.1", "localhost", "[::1]", "::1")


def _origin_allowed(origin: str | None, port: int) -> bool:
    """Origin 校验：缺失时放行（同源导航/部分 CLI），其余必须是本服务精确同源。

    明确拒绝：
    - ``null`` / ``file://``（沙箱 iframe、data:、本地文件页——Origin: null 不可信）
    - 非 http(s)、非 loopback 主机
    - 端口不一致（含省略端口，避免被 ``http://127.0.0.1`` 默认 80 口 CSRF）
    - https（本服务只提供 http）
    """
    if not origin:
        return True  # 同源导航 / 部分浏览器不带 Origin / CLI
    o = origin.strip()
    if not o or o.lower() in ("null", "file://"):
        return False
    try:
        u = urlparse(o)
    except ValueError:
        return False
    if u.scheme != "http":
        return False
    host = (u.hostname or "").lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        return False
    # 必须是显式端口且与本服务一致
    return u.port == port


class _UsageHandler(BaseHTTPRequestHandler):
    server_version = "MimoUsage/1.0"
    protocol_version = "HTTP/1.1"

    # 挂在 server 上的上下文
    #   token / db / lock / last / idle_seconds —— 请求处理要用
    #   kind —— 只被 /api/ping 的身份应答读取
    # 会话口径**不在**这里：它由每次请求的 query 决定（见 _request_options / _load）。
    # 早前还挂着 days / session_id / limit，改成请求级参数后它们已无任何读取方，
    # 故移除（Issue #19），避免后来者误以为设置它能生效。

    def log_message(self, fmt: str, *args) -> None:  # 静音默认 stderr 访问日志
        return

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _touch(self) -> None:
        self.server.last = time.time()  # type: ignore[attr-defined]

    def _drain_body(self) -> None:
        """读掉请求体，避免 HTTP/1.1 keep-alive 下未读 body 造成连接错位。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return
        n = min(n, 1 << 20)  # 上限 1 MiB，防滥用
        remaining = n
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass

    def _path_parts(self):
        raw = self.path or "/"
        if "?" in raw:
            raw = raw.split("?", 1)[0]
        parts = [p for p in raw.split("/") if p]
        return parts

    def do_GET(self) -> None:  # noqa: N802
        self._touch()
        if not _host_is_loopback(self.headers.get("Host")):
            self._json(403, {"error": "forbidden_host"})
            return
        origin = self.headers.get("Origin")
        port = self.server.server_address[1]  # type: ignore[attr-defined]
        if not _origin_allowed(origin, port):
            self._json(403, {"error": "forbidden_origin"})
            return
        token = self.server.token  # type: ignore[attr-defined]
        parts = self._path_parts()
        if not parts or parts[0] != token:
            self._json(404, {"error": "not_found"})
            return
        rest = parts[1:]
        try:
            if not rest or rest == ["index.html"]:
                data = self._load()
                # 只回显「已校验参数重新编码」后的 query，不回显原始 query：
                # 这段文本会被 json.dumps 嵌进页内 <script> 的 var LIVE，
                # 而 json.dumps 不转义 < > /，原始 "</script>" 会提前闭合脚本元素。
                suffix = ""
                if self.path.partition("?")[2]:
                    options = self._request_options()
                    spec = {
                        "session": options["session_id"] if options["session_id"] is not None else "latest",
                        "days": options["days"],
                        "kind": options["kind"],
                    }
                    if options["limit"] is not None:
                        spec["limit"] = options["limit"]
                    if options.get("tab"):
                        spec["tab"] = options["tab"]
                    if options.get("mode"):
                        spec["mode"] = options["mode"]
                    suffix = "?" + urlencode(spec)
                live = {
                    "reportUrl": f"http://{LOOPBACK_HOST}:{port}/{token}/api/report" + suffix,
                    "pageUrl": f"http://{LOOPBACK_HOST}:{port}/{token}/" + suffix,
                }
                html = render_html(data, live=live)
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                return
            if rest == ["api", "report"]:
                data = self._load()
                self._json(200, _public_payload(data))
                return
            if rest == ["api", "ping"]:
                # 常量时间响应：不读数据库、不返回路径、不回显 token。
                # 身份探测必须与数据库规模无关 —— /api/report 每次请求都做全量聚合，
                # 大库上远超探测超时，健康实例会被判为不可用（曾导致 --open 把自己
                # 刚启动的子进程杀掉）。所以探测统一走这里。
                self._json(200, {"app": LIVE_APP_ID, "kind": self.server.kind, "requestSelection": 1})  # type: ignore[attr-defined]
                return
        except ValueError:
            self._json(400, {"error": "invalid_query"})
            return
        except FileNotFoundError:
            # 不回传 str(exc)：其中含数据库绝对路径，token 持有者也不应拿到
            self._json(503, {"error": "db_missing"})
            return
        except sqlite3.Error:
            self._json(503, {"error": "db_busy"})
            return
        except Exception:
            self._json(500, {"error": "internal"})
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        self._touch()
        self._drain_body()
        if not _host_is_loopback(self.headers.get("Host")):
            self._json(403, {"error": "forbidden_host"})
            return
        origin = self.headers.get("Origin")
        port = self.server.server_address[1]  # type: ignore[attr-defined]
        if not _origin_allowed(origin, port):
            self._json(403, {"error": "forbidden_origin"})
            return
        token = self.server.token  # type: ignore[attr-defined]
        parts = self._path_parts()
        if not parts or parts[0] != token:
            self._json(404, {"error": "not_found"})
            return
        if parts[1:] == ["api", "shutdown"]:
            self._json(200, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()  # type: ignore[attr-defined]
            return
        self._json(404, {"error": "not_found"})

    def _request_options(self) -> dict:
        """Each dashboard URL owns its query; no mutable process-wide session selection."""
        raw = self.path.partition("?")[2]
        params = parse_qs(raw, keep_blank_values=True, max_num_fields=8)
        if any(k not in ("session", "days", "limit", "kind", "tab", "mode") or len(v) != 1
               for k, v in params.items()):
            raise ValueError("无效或重复的看板查询参数")
        session = params.get("session", ["latest"])[0]
        if not session or len(session) > 256 or any(ord(c) < 32 for c in session):
            raise ValueError("无效的会话 ID")
        # tab：一次 /usage-open 的定格令牌。字符集收紧，便于安全回显与去重。
        tab = params.get("tab", [""])[0]
        if tab and (len(tab) > 64 or any(c not in _BINDING_CHARS for c in tab)):
            raise ValueError("无效的看板令牌")
        # mode：用量范围两档。「session」= 用已定格的会话；「global」= 全库聚合，绕过定格。
        mode = params.get("mode", [""])[0]
        if mode not in ("", "session", "global"):
            raise ValueError("无效的用量范围")
        kind = params.get("kind", ["all"])[0]
        if kind not in KINDS:
            raise ValueError("无效视图")
        try:
            days = int(params.get("days", ["30"])[0])
            limit_raw = params.get("limit", [""])[0]
            limit = int(limit_raw) if limit_raw else None
        except ValueError as exc:
            raise ValueError("统计参数必须为整数") from exc
        if not (1 <= days <= 36500) or (limit is not None and not (1 <= limit <= 10**12)):
            raise ValueError("统计参数超出范围")
        return dict(kind=kind, days=days,
                    session_id=None if session == "latest" else session, limit=limit,
                    tab=tab or None, mode=mode or None)

    def _load(self) -> dict:
        options = self._request_options()
        tab = options.pop("tab", None)
        mode = options.pop("mode", None)
        # mode=global 是「全库聚合」档：与会话无关，**故意不查定格**，
        # 这样即使别的会话随后活动，全局视图也不受定格影响。
        if tab and mode != "global" and options["session_id"] is None:
            frozen = frozen_session(self.server.db, tab)  # type: ignore[attr-defined]
            if frozen:
                options["session_id"] = frozen
        return card_data(self.server.db, **options)  # type: ignore[attr-defined]


def create_usage_server(db: str, *, kind: str = "all", days: int = 30,
                        session_id: str | None = None, limit: int | None = None,
                        token: str | None = None, idle_minutes: int = DEFAULT_IDLE_MINUTES,
                        port: int = 0) -> ThreadingHTTPServer:
    """创建只监听 loopback 的 ThreadingHTTPServer（尚未 serve_forever）。"""
    token = token or secrets.token_urlsafe(16)
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token):
        raise ValueError("token 格式非法")
    httpd = ThreadingHTTPServer((LOOPBACK_HOST, int(port or 0)), _UsageHandler)
    httpd.daemon_threads = True
    httpd.token = token  # type: ignore[attr-defined]
    httpd.db = db  # type: ignore[attr-defined]
    httpd.kind = kind if kind in KINDS else "all"  # type: ignore[attr-defined]
    httpd.last = time.time()  # type: ignore[attr-defined]
    httpd.idle_seconds = max(1, int(idle_minutes)) * 60  # type: ignore[attr-defined]
    httpd.lock = threading.Lock()  # type: ignore[attr-defined]

    real_shutdown = httpd.shutdown

    def _idle_watchdog():
        while True:
            time.sleep(15)
            try:
                idle = time.time() - httpd.last  # type: ignore[attr-defined]
                if idle >= httpd.idle_seconds:  # type: ignore[attr-defined]
                    real_shutdown()
                    return
            except Exception:
                return

    threading.Thread(target=_idle_watchdog, daemon=True).start()
    return httpd


def live_urls(httpd: ThreadingHTTPServer) -> dict:
    port = httpd.server_address[1]
    token = httpd.token  # type: ignore[attr-defined]
    page = f"http://{LOOPBACK_HOST}:{port}/{token}/"
    return {
        "pageUrl": page,
        "reportUrl": f"http://{LOOPBACK_HOST}:{port}/{token}/api/report",
        "port": port,
        "token": token,
    }


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong(0)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 服务身份校验（安装器 preflight 与复用逻辑各自实现同一套判据）
# ---------------------------------------------------------------------------
#
# 只凭「端口上有东西在听」就认定是本项目服务是不安全的：可能被其它程序占用，
# 也可能是被篡改的状态文件。因此必须同时满足：
#   1) 状态文件里的 token 合法、port 是 1..65535 的整数；
#   2) GET /{token}/api/ping 返回 200；
#   3) 响应体带**本项目身份标识**（app == LIVE_APP_ID 且 kind 属于已知视图）。
# 三条全过才认为是「自己的服务」，才允许发 shutdown / 复用。
# 探测走 ping 而不是 /api/report：后者每次请求都做全量聚合，大库上会超过探测
# 超时，把健康实例误判为不可达。installer.py 出于版本错配的考虑不能导入本模块，
# 它自带一份同样的判据——两边的端点与身份标识必须保持一致。


def _is_loopback_host(host: str) -> bool:
    return (host or "").strip().lower() in ("127.0.0.1", "localhost", "::1", "[::1]")


def _url_is_loopback(url: str) -> bool:
    """双保险：任何对外请求前都先确认目标是 loopback http。"""
    try:
        u = urlparse(url)
    except ValueError:
        return False
    return u.scheme == "http" and _is_loopback_host(u.hostname or "")


def _fetch_json(url: str, timeout: float = 1.0):
    """GET 一个 loopback URL 并解析 JSON。返回 (payload, error)；成功时 error 为 None。"""
    import urllib.error
    import urllib.request
    if not _url_is_loopback(url):
        return None, "refused_non_loopback"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None, f"http_{resp.status}"
            body = resp.read(1 << 20)
    except urllib.error.HTTPError as exc:
        return None, f"http_{exc.code}"
    except Exception as exc:
        return None, f"unreachable_{exc.__class__.__name__}"
    try:
        return json.loads(body.decode("utf-8")), None
    except (ValueError, UnicodeDecodeError):
        return None, "payload_not_json"


def _looks_like_our_ping(payload) -> bool:
    """ping 响应必须同时命中项目标识与已知 kind。

    只看"端口上有 HTTP 服务在应答"不足以证明归属：共用一个端口号或恰好监听
    同一端口的其它进程必须被判为**非本项目**，否则会去关掉别人的服务。
    """
    if not isinstance(payload, dict):
        return False
    if payload.get("app") != LIVE_APP_ID:
        return False
    return payload.get("kind") in KINDS


def _live_urls(port: int, token: str) -> dict:
    """始终由 (port, token) 本地重建 URL，绝不采信状态文件里的 URL 文本。"""
    base = f"http://{LOOPBACK_HOST}:{int(port)}/{token}"
    return {"pageUrl": base + "/", "reportUrl": base + "/api/report",
            "port": int(port), "token": token}


def _valid_state_fields(st: dict):
    """返回 (port, token) 或 None。只接受严格形态，不做类型强制转换。"""
    token = st.get("token")
    port = st.get("port")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token):
        return None
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        return None
    return port, token


def _service_probe() -> dict:
    """内部探针：与 ``service_status()`` 相同，但额外带上完整 token（不外泄）。"""
    path = server_state_path()
    status = {
        "stateFile": path,
        "statePresent": False,
        "stateValid": False,
        "reachable": False,
        "owned": False,
        "port": None,
        "token": None,
        "pid": None,
        "reason": "no_state",
    }
    if not os.path.exists(path):
        return status
    status["statePresent"] = True
    try:
        with open(path, "r", encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        status["reason"] = "state_unreadable"
        return status
    if not isinstance(st, dict):
        status["reason"] = "state_not_object"
        return status
    fields = _valid_state_fields(st)
    if fields is None:
        status["reason"] = "token_or_port_invalid"
        return status
    port, token = fields
    status["stateValid"] = True
    status["port"] = port
    status["token"] = token
    pid = st.get("pid")
    status["pid"] = pid if (isinstance(pid, int) and not isinstance(pid, bool)) else None
    payload, err = _fetch_json(_live_urls(port, token)["pageUrl"] + "api/ping", timeout=1.2)
    if err:
        status["reason"] = err
        return status
    status["reachable"] = True
    if _looks_like_our_ping(payload):
        status["owned"] = True
        status["reason"] = "ok"
    else:
        status["reason"] = "payload_not_ours"
    return status


def service_status() -> dict:
    """公开报告：只暴露 token 前 6 位，避免完整凭据进入日志/对话记录。"""
    status = _service_probe()
    token = status.pop("token", None)
    status["tokenPrefix"] = token[:6] if token else None
    return status


def cmd_service_check() -> int:
    """`service-check` 内部子命令：向 stdout 输出 JSON 报告。"""
    print(json.dumps(service_status(), ensure_ascii=False, indent=2))
    return 0


def cmd_selfcheck() -> int:
    """`selfcheck` 内部子命令：**不读数据库、不写任何文件**的环境自检。

    安装器用它验证"这份 usage_cli.py 能被目标解释器启动且自身完整"。刻意不读库、
    不建目录、不写文件：早起版本曾用 ``--help`` 承担这个职责，而 ``--help`` 会被参数
    白名单丢弃 → 变成无参调用 → 读真实数据库 + 往用户数据目录写报告 + 在没有数据库的
    机器上直接失败。任何副作用在这里都是不可接受的。
    """
    db = default_db_path()
    info = {
        "ok": True,
        "python": "%d.%d.%d" % tuple(sys.version_info[:3]),
        "supported": sys.version_info >= (3, 10),
        "self": os.path.abspath(__file__),
        "dataDir": default_data_dir(),
        "dbPath": db,
        "dbExists": os.path.exists(db),
        "kinds": list(KINDS),
        "internalKinds": list(_INTERNAL_KINDS),
        "hasServer": callable(create_usage_server),
    }
    info["ok"] = bool(info["supported"] and info["hasServer"])
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0 if info["ok"] else 3


def read_server_state() -> dict | None:
    try:
        with open(server_state_path(), "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        if isinstance(obj, dict) and obj.get("port") and obj.get("token"):
            return obj
    except Exception:
        return None
    return None


def write_server_state(obj: dict) -> None:
    path = server_state_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    # 尽量收紧权限。注意：Windows 上 os.chmod 主要只支持只读位，0o600 不能当作
    # ACL 已正确配置的证明；真正的边界是 %LOCALAPPDATA% 默认用户配置文件 ACL
    # （同机其它用户默认不可读）。失败则忽略，不因此拒绝启动。
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def clear_server_state() -> None:
    try:
        os.unlink(server_state_path())
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 标签页会话定格（Issue #16）
# 每次 /usage-open 生成一个独立的 tab 令牌，首次带该令牌的请求把「此刻最近活跃的会话」
# 定格下来，之后同一令牌一律用它 —— 于是同一个标签页刷新不再漂移。
# 每个 tab 一个独立文件，避免读-改-写竞争；**绝不写入用量数据库**。
# ---------------------------------------------------------------------------

_BINDING_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def session_bindings_dir() -> str:
    return os.path.join(default_data_dir(), "session-bindings")


def _binding_path(tab: str) -> str:
    return os.path.join(session_bindings_dir(), tab + ".json")


def read_binding(tab: str) -> str | None:
    """读某个 tab 已定格的会话 id；不存在或不可读时返回 None。"""
    try:
        with open(_binding_path(tab), "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        return None
    sid = obj.get("session_id") if isinstance(obj, dict) else None
    return sid if isinstance(sid, str) and sid else None


def write_binding(tab: str, session_id: str) -> None:
    """原子写。失败不抛：定格失败只让这一页退回「最近活跃」，不该让看板不可用。"""
    try:
        path = _binding_path(tab)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + f".tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"session_id": session_id, "frozenAt": int(time.time() * 1000)},
                      fh, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass
    prune_bindings()


def prune_bindings(max_age_days: int = 30) -> None:
    """回收过期绑定，避免每个 tab 一个文件无限累积（生命周期）。

    只在写入路径上顺带做一次，目录很小、代价可忽略；失败静默（不影响定格本身）。
    """
    cutoff = time.time() - max(1, int(max_age_days)) * 86400
    try:
        for name in os.listdir(session_bindings_dir()):
            if not name.endswith(".json"):
                continue
            full = os.path.join(session_bindings_dir(), name)
            try:
                if os.path.getmtime(full) < cutoff:
                    os.unlink(full)
            except OSError:
                continue
    except OSError:
        return


def frozen_session(db_path: str, tab: str) -> str | None:
    """该 tab 定格的会话；若尚未定格，则把「此刻最近活跃」的会话定格下来。

    注意语义：定格的是**打开那一刻的最近活跃会话**，不是「发起命令的会话」——
    斜杠命令拿不到会话身份（见 Issue #17 的结论）。
    """
    known = read_binding(tab)
    if known:
        return known
    conn, snap = _open_readonly(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM session ORDER BY time_updated DESC, id DESC LIMIT 1").fetchone()
        sid = row[0] if row else None
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        if snap:
            _cleanup_snapshot(snap)
    if not sid:
        return None
    write_binding(tab, str(sid))
    return str(sid)


def _lock_path() -> str:
    return os.path.join(default_data_dir(), "live-server.lock")


def _lock_holder_pid(path: str) -> int | None:
    try:
        with open(path, "r", encoding="ascii", errors="ignore") as fh:
            raw = (fh.read() or "").strip().splitlines()[:1]
        if not raw:
            return None
        return int(raw[0])
    except (OSError, ValueError):
        return None


def _acquire_start_lock(timeout: float = 12.0) -> bool:
    """跨进程互斥：并发 --open 时只允许一个父进程 spawn serve 子进程。

    创建方式是 O_CREAT|O_EXCL 原子创建；失败方等待。陈旧锁回收同时看
    mtime 与锁文件内记录的持有者 PID 是否仍存活——持有者已死时不必等满 30s。
    """
    path = _lock_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(path)
                stale = age > 30
                if not stale and age > 0.4:
                    holder = _lock_holder_pid(path)
                    # 持有者进程已退出 → 立即视为陈旧（崩溃后的快速回收）
                    if holder is not None and holder > 0 and not _pid_alive(holder):
                        stale = True
                    elif holder is None and age > 2.0:
                        # 锁文件空/损坏且已存在一小会儿
                        stale = True
                if stale:
                    os.unlink(path)
                    continue
            except OSError:
                pass
            time.sleep(0.05)
    return False


def _release_start_lock() -> None:
    try:
        os.unlink(_lock_path())
    except OSError:
        pass


def _state_pid(st: dict) -> int:
    pid = st.get("pid")
    return pid if (isinstance(pid, int) and not isinstance(pid, bool)) else 0


def _request_shutdown(port: int, token: str, timeout: float = 1.0) -> bool:
    """用本项目协议请求关闭；url 由 (port, token) 本地重建，不采信状态文件文本。"""
    import urllib.request
    url = _live_urls(port, token)["pageUrl"] + "api/shutdown"
    if not _url_is_loopback(url):
        return False
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def try_reuse_server(db: str) -> dict | None:
    """复用已有实例。只有身份三态全部通过才复用，绝不用状态文件里的 URL。"""
    status = _service_probe()
    if not (status["stateValid"] and status["reachable"] and status["owned"]):
        if status["stateValid"] and status["reachable"] and not status["owned"]:
            print("提示：live-server.json 指向的监听者不是本项目服务，已忽略该状态文件（未做任何强制动作）。",
                  file=sys.stderr)
        return None
    # v0.2.0 servers do not understand request-scoped session selection.
    # Only shut down an instance proven to be ours; otherwise a new caller could
    # silently receive another session's context from the old process.
    ping, ping_err = _fetch_json(_live_urls(status["port"], status["token"])["pageUrl"] + "api/ping", timeout=1.2)
    if ping_err or not isinstance(ping, dict) or ping.get("requestSelection") != 1:
        if not ping_err and _looks_like_our_ping(ping):
            _request_shutdown(status["port"], status["token"])
        return None
    st = read_server_state() or {}
    recorded_db = st.get("db")
    if recorded_db and os.path.abspath(str(recorded_db)) != os.path.abspath(db):
        # 库路径不同：当前实例不服务本次请求。用本项目协议优雅关掉，避免它空转到 idle。
        _request_shutdown(status["port"], status["token"])
        return None
    return {**_live_urls(status["port"], status["token"]),
            "pid": status["pid"] or 0, "reused": True}


def spawn_serve_child(db: str, *, kind: str = "all", days: int = 30,
                      session_id: str | None = None, limit: int | None = None,
                      token: str, idle_minutes: int = DEFAULT_IDLE_MINUTES) -> int:
    """在 detached 子进程中启动 serve 循环；返回 pid。"""
    script = os.path.abspath(__file__)
    argv = [
        sys.executable, script, "serve",
        "--db", db,
        "--token", token,
        "--idle-minutes", str(int(idle_minutes)),
    ]
    if kind != "all":
        argv += []  # serve 始终服务 all 视图数据；kind 由页面切换
    if days and days != 30:
        argv += ["--days", str(int(days))]
    if session_id:
        argv += ["--session", str(session_id)]
    if limit:
        argv += ["--limit", str(int(limit))]

    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "cwd": default_data_dir() if os.path.isdir(default_data_dir()) else os.path.expanduser("~"),
    }
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv, **kwargs)
    return proc.pid


def wait_for_server(token: str, timeout: float = 8.0) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = read_server_state()
        if st and st.get("token") == token:
            fields = _valid_state_fields(st)
            if fields is not None:
                port, tok = fields
                payload, err = _fetch_json(_live_urls(port, tok)["pageUrl"] + "api/ping", timeout=1.2)
                if err is None and _looks_like_our_ping(payload):
                    return {**_live_urls(port, tok),
                            "pid": _state_pid(st), "reused": False}
        time.sleep(0.12)
    return None


def ensure_live_server(db: str, *, kind: str = "all", days: int = 30,
                       session_id: str | None = None, limit: int | None = None,
                       idle_minutes: int = DEFAULT_IDLE_MINUTES) -> dict | None:
    reused = try_reuse_server(db)
    if reused:
        return reused
    if not _acquire_start_lock():
        # 拿不到锁：另一进程正在启动。锁超时后仍短暂轮询复用，避免刚好
        # 对方刚写完 state、自己却直接返回 None。
        for _ in range(8):
            time.sleep(0.12)
            reused = try_reuse_server(db)
            if reused:
                return reused
        return None
    try:
        # 拿到锁后再查一次，避免排队期间实例已就绪
        reused = try_reuse_server(db)
        if reused:
            return reused
        token = secrets.token_urlsafe(16)
        try:
            pid = spawn_serve_child(
                db, kind=kind, days=days, session_id=session_id, limit=limit,
                token=token, idle_minutes=idle_minutes,
            )
        except Exception:
            return None
        live = wait_for_server(token)
        if live:
            live["pid"] = live.get("pid") or pid
            return live
        # 超时：尝试杀掉可能半启动的子进程
        try:
            if pid:
                os.kill(pid, 9 if os.name == "nt" else 15)
                time.sleep(0.1)
                if _pid_alive(pid):
                    os.kill(pid, 9)
        except Exception:
            pass
        return None
    finally:
        _release_start_lock()


def run_serve_loop(db: str, *, kind: str = "all", days: int = 30,
                   session_id: str | None = None, limit: int | None = None,
                   token: str | None = None,
                   idle_minutes: int = DEFAULT_IDLE_MINUTES,
                   port: int = 0) -> int:
    """前台/子进程入口：阻塞 serve_forever，直到 idle 或 shutdown。"""
    httpd = create_usage_server(
        db, kind=kind, days=days, session_id=session_id, limit=limit,
        token=token, idle_minutes=idle_minutes, port=port,
    )
    urls = live_urls(httpd)
    write_server_state({
        "pid": os.getpid(),
        "port": urls["port"],
        "token": urls["token"],
        "pageUrl": urls["pageUrl"],
        "reportUrl": urls["reportUrl"],
        "db": os.path.abspath(db),
        "kind": kind,
        "days": days,
        "startedAt": int(time.time() * 1000),
        "idleMinutes": idle_minutes,
    })
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass
        # 只清理自己写下的 state
        st = read_server_state()
        if st and st.get("token") == urls["token"]:
            clear_server_state()
    return 0


# subprocess 顶层导入（spawn 子进程服务用）

def main(argv=None) -> int:
    force_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="usage_cli.py",
        description="MiMo Desktop 本机用量统计（静态 HTML + 可选 loopback 实时刷新服务）",
    )
    parser.add_argument("kind", nargs="?", default="all",
                        help="生成内容：all（默认）/ token / session / context / serve（内部）")
    parser.add_argument("--days", type=int, default=30, help="token 统计最近多少天（默认 30）")
    parser.add_argument("--db", default=None, help="数据库路径（默认 ~/.local/share/mimocode/mimocode.db）")
    parser.add_argument("--out", default=None,
                        help="输出 HTML 路径（默认用户数据目录 mimo-usage-<kind>-<日期>.html）")
    parser.add_argument("--session", default=None, help="上下文视图指定的会话 ID（默认最近活跃）")
    parser.add_argument("--limit", type=int, default=None, help="上下文上限 tokens（覆盖模型配置；不提供时自动解析）")
    parser.add_argument("--open", action="store_true", help="启动/复用本地实时服务并用浏览器打开")
    parser.add_argument("--print-path", action="store_true", help="只在标准输出打印生成的文件路径")
    parser.add_argument("--shutdown", action="store_true", help="请求关闭本机实时服务后退出")
    parser.add_argument("--token", default=None, help="serve 模式 path token（内部/测试）")
    parser.add_argument("--idle-minutes", type=int, default=DEFAULT_IDLE_MINUTES,
                        help="serve 空闲退出分钟数（默认 240）")
    parser.add_argument("--port", type=int, default=0, help="serve 监听端口（0=系统分配；仅 loopback）")
    parser.add_argument("--wait-seconds", type=int, default=0,
                        help="--shutdown 时等待服务真正退出的秒数（默认 0，只发请求不等）")
    parser.add_argument("--arg-string", default=None,
                        help="命令参数整串（由 shell 模板透传，内部按空白拆分后白名单校验）")

    if argv is None:
        argv = sys.argv[1:]
    argv = [str(a) for a in argv]
    if "--arg-string" in argv:
        i = argv.index("--arg-string")
        joined = " ".join(argv[i + 1:])
        argv = argv[:i] + _split_args(joined)
    dropped_flags: list = []
    try:
        argv = _sanitize_args(argv, dropped_flags)
    except ValueError as exc:
        print(f"MiMo 用量：参数错误（{exc}）", file=sys.stderr)
        return 2
    # 收口：用户给了参数，但一个都没被识别 —— 绝不能"按默认行为跑一遍"。
    # 这正是 `--help` 被丢弃后变成无参调用（读库 + 写报告）那条事故路径。
    if dropped_flags and not argv:
        print(f"MiMo 用量：参数错误（无法识别这些选项：{' '.join(dropped_flags)}）；"
              f"未执行任何操作。查看用法请用 --help", file=sys.stderr)
        return 2

    # 内部子命令（serve / service-check）由显式词触发（固定 CLI，非 $ARGUMENTS）
    raw_kind = argv[0] if argv and not argv[0].startswith("-") else "all"
    is_internal = raw_kind in _INTERNAL_KINDS
    args = parser.parse_args(argv)
    if is_internal:
        args.kind = raw_kind
    elif args.kind not in KINDS:
        args.kind = "all"

    db = args.db or default_db_path()
    out_path = os.path.abspath(args.out) if args.out else default_out_path(
        args.kind if args.kind in KINDS else "all")

    # ---- 关闭实时服务 ----
    # 三态身份校验通过才发 shutdown；不通过一律不猜、不强制结束。
    if args.shutdown:
        # 必须用内部探针：公开报告刻意不暴露完整 token
        status = _service_probe()
        if not status["statePresent"]:
            print("无运行中的实时服务", file=sys.stderr)
            return 1
        if not status["stateValid"]:
            print(f"状态文件无效（{status['reason']}）：未发送关闭请求，也未删除该文件", file=sys.stderr)
            return 3
        if not status["reachable"]:
            print(f"状态文件存在但服务不可达（{status['reason']}）：未删除该文件。"
                  f"若确有残留，可直接重新运行 --open 覆盖状态。", file=sys.stderr)
            return 1
        if not status["owned"]:
            print("状态文件指向的监听者不是本项目服务：已拒绝发送关闭请求"
                  "（不会按端口或 PID 强制结束任何进程）", file=sys.stderr)
            return 3
        if not _request_shutdown(status["port"], status["token"], timeout=2.0):
            print("关闭实时服务失败", file=sys.stderr)
            return 3
        stopped = not service_status()["reachable"]
        waited = 0.0
        if args.wait_seconds > 0 and not stopped:
            start = time.time()
            while time.time() < start + args.wait_seconds:
                if not service_status()["reachable"]:
                    stopped = True
                    break
                time.sleep(0.2)
            waited = time.time() - start
        if not stopped:
            print(f"已发送关闭请求，但服务在 {args.wait_seconds} 秒内仍未退出。"
                  f"未做任何强制结束；请稍后重试或手动检查。", file=sys.stderr)
            return 3
        clear_server_state()
        print("已关闭实时服务" + (f"（等待 {waited:.1f}s 退出）" if args.wait_seconds > 0 else ""))
        return 0

    # ---- service-check（内部，只读） ----
    if args.kind == "service-check":
        return cmd_service_check()

    # ---- selfcheck（内部：不读库、不写文件） ----
    if args.kind == "selfcheck":
        return cmd_selfcheck()

    # ---- serve 循环（内部） ----
    if args.kind == "serve":
        if not args.token:
            print("serve 模式必须提供 --token", file=sys.stderr)
            return 2
        try:
            return run_serve_loop(
                db, days=args.days, session_id=args.session, limit=args.limit,
                token=args.token, idle_minutes=args.idle_minutes, port=args.port,
            )
        except OSError as exc:
            print(f"MiMo 用量：无法监听 loopback 端口（{exc}）", file=sys.stderr)
            return 3

    try:
        data = card_data(db, kind=args.kind, days=args.days,
                         session_id=args.session, limit=args.limit)
    except FileNotFoundError as exc:
        print(f"MiMo 用量：{exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"MiMo 用量：读取数据库失败（{exc}）", file=sys.stderr)
        return 3

    cmd_hint = build_command_hint(args.kind if args.kind in KINDS else "all", args.days, out_path)

    live = None
    opened_url = None
    if args.open:
        live = ensure_live_server(
            db, kind=args.kind if args.kind in KINDS else "all",
            days=args.days, session_id=args.session, limit=args.limit,
        )

    html_doc = render_html(data, cmd=cmd_hint, out_path=out_path, live=live)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html_doc)

    if args.open and live:
        query = {"session": args.session if args.session is not None else "latest",
                 "days": args.days, "kind": args.kind if args.kind in KINDS else "all"}
        if args.limit is not None:
            query["limit"] = args.limit
        # 每次 /usage-open 一个独立令牌（Issue #16）：URL 互不相同，可在不同标签页并存；
        # 该页首次打开时服务端把「此刻最近活跃的会话」定格下来，之后刷新不再漂移。
        query["tab"] = secrets.token_urlsafe(9)
        # 默认落在「会话用量」档（上下文使用量视图，用上面那个定格会话）。
        query["mode"] = "session"
        opened_url = live["pageUrl"] + "?" + urlencode(query)
        try:
            webbrowser.open(opened_url)
        except Exception:
            pass

    if args.print_path:
        print(out_path)
        return 0

    # 供 `command` 模板内联使用：始终保持**一行**输出（MiMo 会原样复述）
    if opened_url:
        print(f"MiMo 用量已生成：{opened_url}（快照 {out_path}）")
    else:
        print(f"MiMo 用量已生成：{out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
