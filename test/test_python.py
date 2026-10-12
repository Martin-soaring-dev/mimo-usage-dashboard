#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_python.py — usage_cli.py 的端到端测试（纯 Python，无需 Node 子进程）

覆盖：
  1. 聚合口径（用临时 sqlite 造数据，验证 tokens / 命中率 / 工具 / 会话）
  2. 三种视图（token / session / context / all）的 HTML 结构
  3. 命令参数白名单防注入
  4. JSONC 剥离与注释容错
  5. register_command 写入行为（备份唯一、原子写、字段保留、退出码、BOM）

运行：python test/test_python.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import usage_cli as U  # noqa: E402
import register_command as R  # noqa: E402

FAILS = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global FAILS
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  {extra}" if extra else ""))
    if not ok:
        FAILS += 1


# ---------------------------------------------------------------------------
# 造一个最小可用的 mimocode.db
# ---------------------------------------------------------------------------

def make_db(path: str, now_ms: int) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE session (
          id text PRIMARY KEY, project_id text NOT NULL DEFAULT 'p', parent_id text,
          slug text NOT NULL DEFAULT 's', directory text NOT NULL DEFAULT 'D:/proj',
          title text NOT NULL, version text NOT NULL DEFAULT '1', share_url text,
          summary_additions integer, summary_deletions integer, summary_files integer,
          summary_diffs text, revert text, permission text,
          time_created integer NOT NULL, time_updated integer NOT NULL,
          time_compacting integer, time_archived integer, workspace_id text,
          context_from text, context_watermark text, last_checkpoint_message_id text,
          prompt text, auto_worktree_hint_sent integer,
          title_source text NOT NULL DEFAULT 'user', title_revision integer NOT NULL DEFAULT 0
        );
        CREATE TABLE message (
          id text PRIMARY KEY, session_id text NOT NULL, agent_id text NOT NULL DEFAULT 'main',
          time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL
        );
        CREATE TABLE part (
          id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL,
          time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO session (id,title,directory,time_created,time_updated,"
        "summary_additions,summary_deletions,summary_files) VALUES (?,?,?,?,?,?,?,?)",
        ("s1", "测试会话", "D:/proj", now_ms - 3600_000, now_ms, 120, 45, 3),
    )
    conn.execute(
        "INSERT INTO session (id,title,directory,time_created,time_updated,"
        "summary_additions,summary_deletions,summary_files) VALUES (?,?,?,?,?,?,?,?)",
        ("s2", "第二个会话", "D:/proj", now_ms - 7200_000, now_ms - 600_000, 10, 5, 1),
    )

    def msg(mid: str, sid: str, ts: int, tokens: dict, model: str = "mimo-v2.6-flash") -> None:
        data = {"role": "assistant", "providerID": "xiaomi-token-plan-cn",
                "modelID": model, "tokens": tokens, "time": {"created": ts}}
        conn.execute(
            "INSERT INTO message (id,session_id,time_created,time_updated,data) VALUES (?,?,?,?,?)",
            (mid, sid, ts, ts, json.dumps(data)),
        )

    def part(pid: str, mid: str, sid: str, ts: int, data: dict) -> None:
        conn.execute(
            "INSERT INTO part (id,message_id,session_id,time_created,time_updated,data) VALUES (?,?,?,?,?,?)",
            (pid, mid, sid, ts, ts, json.dumps(data)),
        )

    # s1: 两条 assistant；一条 step-finish；两个 tool
    msg("m1", "s1", now_ms - 3500_000,
        {"total": 1000, "input": 400, "output": 100, "reasoning": 50,
         "cache": {"read": 400, "write": 50}})
    msg("m2", "s1", now_ms - 3000_000,
        {"total": 2000, "input": 800, "output": 200, "reasoning": 100,
         "cache": {"read": 900, "write": 0}})
    part("p1", "m2", "s1", now_ms - 3000_000,
         {"type": "step-finish", "tokens": {"total": 2000, "input": 800, "output": 200,
                                            "reasoning": 100, "cache": {"read": 900, "write": 0}}})
    part("p2", "m1", "s1", now_ms - 3400_000, {"type": "tool", "tool": "bash"})
    part("p3", "m2", "s1", now_ms - 2900_000, {"type": "tool", "tool": "read"})
    # s2: 一条 assistant
    msg("m3", "s2", now_ms - 800_000,
        {"total": 500, "input": 200, "output": 50, "reasoning": 0,
         "cache": {"read": 250, "write": 0}})
    conn.commit()
    conn.close()


def main() -> int:
    import time

    now_ms = int(time.time() * 1000)
    tmpdir = tempfile.mkdtemp(prefix="mimo-usage-test-")
    db = os.path.join(tmpdir, "mimocode.db")
    make_db(db, now_ms)

    # ---- 1. 聚合口径 ----
    print("=== 聚合口径 ===")
    data = U.read_usage(db)
    t = data["totals"]
    # m1: 400+100+50+400+50 = 1000 ; m2: 800+200+100+900+0 = 2000 ; m3: 200+50+0+250 = 500
    check("total tokens = 3500", t["tokens"] == 3500, f"({t['tokens']})")
    check("input = 1400", t["input"] == 1400, f"({t['input']})")
    check("output = 350", t["output"] == 350, f"({t['output']})")
    check("cache read = 1550", t["read"] == 1550, f"({t['read']})")
    check("requests = 3", t["requests"] == 3, f"({t['requests']})")
    check("tools = 2", t["tools"] == 2, f"({t['tools']})")
    check("sessions = 2", t["sessions"] == 2, f"({t['sessions']})")
    check("daily 已按日期聚合", len(data["daily"]) >= 1)
    check("models 有 1 个（同 provider/model）", len(data["models"]) == 1, f"({len(data['models'])})")
    m = data["models"][0]
    expect_share = 1550 / (1400 + 1550)
    check("模型缓存命中率口径正确", abs(m["cacheReadShare"] - expect_share) < 1e-9,
          f"({m['cacheReadShare']:.4f} vs {expect_share:.4f})")
    check("会话按活跃排序（s1 最新在前）", data["sessions"][0]["id"] == "s1")
    check("改动统计（s1: 120+45）", data["sessions"][0]["additions"] == 120
          and data["sessions"][0]["deletions"] == 45)

    # ---- 2. 上下文口径 ----
    print("\n=== 上下文口径 ===")
    ctx = U.session_context(db)
    check("ok=true", ctx.get("ok") is True)
    check("取最近活跃会话 s1", ctx["sessionId"] == "s1", f"({ctx.get('sessionId')})")
    check("占用取 step-finish = 2000", ctx["context"]["used"] == 2000, f"({ctx['context']['used']})")
    check("来源标记为 step-finish", ctx["context"]["source"] == "step-finish")
    check("消息数 2 / 步数 1 / 工具 2",
          (ctx["messages"], ctx["steps"], ctx["tools"]) == (2, 1, 2),
          f"({ctx['messages']},{ctx['steps']},{ctx['tools']})")
    check("上限来自内置表（mimo-v2.6-flash=1048576）", ctx["context"]["limit"] == 1048576,
          f"({ctx['context'].get('limit')})")
    check("占用比例可算", ctx["context"]["ratio"] is not None)

    # ---- 3. 四种视图 HTML ----
    all_html = U.render_html(U.card_data(db, kind="all"), cmd="python usage_cli.py")
    check("总览页含三个可访问性 Tab", all(k in all_html for k in
          ('总体使用量', '会话使用量', '上下文使用量', 'role="tablist"', 'role="tabpanel"')))
    check("Tab 点击后渲染对应视图", 'renderTab(selected, DATA)' in all_html)
    print("\n=== 视图渲染 ===")
    for kind in ("all", "token", "session", "context"):
        d = U.card_data(db, kind=kind)
        html = U.render_html(d, cmd="python usage_cli.py", out_path="/tmp/x.html")
        check(f"{kind}: 完整 HTML", html.lstrip().lower().startswith("<!doctype html>"))
        check(f"{kind}: 自包含（无 fetch）", "fetch(" not in html)
        check(f"{kind}: 数据内联", "var DATA" in html)
        check(f"{kind}: 有重新生成按钮", 'id="regen"' in html)
        if kind in ("token", "all"):
            check(f"{kind}: 含热力图渲染器", "function heatmapCard" in html)
            check(f"{kind}: 含模型调用块", "按模型的调用情况" in html)
            check(f"{kind}: 含缓存命中率", "缓存命中率" in html)
            check(f"{kind}: 输入 tokens 卡片包含缓存读取", '输入 tokens（含缓存读取）' in html)
            check(f"{kind}: 模型块含命中率条", "track3" in html and "cm-hit-hit" in html)
            check(f"{kind}: 未命中条不再负 margin 覆盖", "margin-left:-1.6%" not in html)
            # 热力图数据必须覆盖全量（不受 --days 限制）+ 顶部趋势用范围内数据
            check(f"{kind}: token 数据含 daily(热力图)", '"daily"' in html)
            check(f"{kind}: token 数据含 recent(趋势)", '"recent"' in html)
        if kind in ("session", "all"):
            check(f"{kind}: 含最近会话表", "最近会话" in html)
        if kind in ("context", "all"):
            check(f"{kind}: 含上下文环", "上下文占用" in html)

    # days 过滤
    d7 = U.card_data(db, kind="token", days=3650)
    check("days 参数生效（3650 天覆盖全部）", d7["token"]["totals"]["tokens"] == 3500,
          f"({d7['token']['totals']['tokens']})")

    # ---- 会话数口径（回归：Token 视图的「会话数」必须与 Session 视图的「会话总数」一致） ----
    print("\n=== 会话数口径（回归） ===")
    reg = U.card_data(db, kind="all")
    check("Token 卡片含 sessions 字段（不再缺键渲染成 0）",
          isinstance(reg["token"]["totals"].get("sessions"), int),
          f"(sessions={reg['token']['totals'].get('sessions')!r})")
    check("主数据：Token 会话数 == Session 会话总数",
          reg["token"]["totals"].get("sessions") == reg["session"]["total"],
          f"(token={reg['token']['totals'].get('sessions')}, session={reg['session']['total']})")

    # 3 个会话
    db3 = os.path.join(tmpdir, "three.db")
    make_db(db3, now_ms)
    c3 = sqlite3.connect(db3)
    c3.execute("INSERT INTO session (id,title,directory,time_created,time_updated,"
               "summary_additions,summary_deletions,summary_files) VALUES (?,?,?,?,?,?,?,?)",
               ("s3", "第三个会话", "D:/proj", now_ms - 60_000, now_ms - 30_000, 1, 1, 1))
    c3.commit()
    c3.close()
    for kind in ("all", "token", "session"):
        d3 = U.card_data(db3, kind=kind)
        # 注意：card_data 只为 token/all 输出 token 段、为 session/all 输出 session 段，
        # 独立页面各带各的负载，所以按 kind 分别取用。
        if "token" in d3:
            n3 = d3["token"]["totals"].get("sessions")
            check(f"3 会话 / {kind}：Token 会话数 = 3", n3 == 3, f"({n3})")
        if "session" in d3:
            check(f"3 会话 / {kind}：Session 总数 = 3", d3["session"]["total"] == 3,
                  f"({d3['session']['total']})")
        if "token" in d3 and "session" in d3:
            check(f"3 会话 / {kind}：两视图一致",
                  d3["token"]["totals"].get("sessions") == d3["session"]["total"] == 3,
                  f"(token={d3['token']['totals'].get('sessions')}, session={d3['session']['total']})")

    # 空数据集
    db_empty = os.path.join(tmpdir, "empty.db")
    make_db(db_empty, now_ms)
    ce = sqlite3.connect(db_empty)
    ce.executescript("DELETE FROM part; DELETE FROM message; DELETE FROM session;")
    ce.commit()
    ce.close()
    de = U.card_data(db_empty, kind="all")
    check("空数据集：Token 会话数 = 0",
          de["token"]["totals"].get("sessions") == 0,
          f"({de['token']['totals'].get('sessions')!r})")
    check("空数据集：两视图一致且均为 0",
          de["token"]["totals"].get("sessions") == de["session"]["total"] == 0,
          f"(token={de['token']['totals'].get('sessions')}, session={de['session']['total']})")

    # 缺失字段 / 异常字段：会话行本身仍是会话，计数按行数走且不崩
    db_odd = os.path.join(tmpdir, "odd.db")
    make_db(db_odd, now_ms)
    co = sqlite3.connect(db_odd)
    co.execute("INSERT INTO session (id,title,directory,time_created,time_updated) "
               "VALUES (?,?,?,?,?)", ("s-null", "缺字段会话", "D:/proj", now_ms, now_ms))
    for mid, raw in (("m-null", '{"role":"assistant"}'),
                     ("m-onlytotal", '{"tokens":{"total":7}}'),
                     ("m-badjson", '{ broken')):
        co.execute("INSERT INTO message (id,session_id,time_created,time_updated,data) "
                   "VALUES (?,?,?,?,?)", (mid, "s-null", now_ms, now_ms, raw))
    co.commit()
    co.close()
    do = U.card_data(db_odd, kind="all")
    check("缺失/异常字段：两视图一致且等于会话行数 3",
          do["token"]["totals"].get("sessions") == do["session"]["total"] == 3,
          f"(token={do['token']['totals'].get('sessions')}, session={do['session']['total']})")
    check("缺失/异常字段：会话数不受缺 tokens / 非法 JSON 影响",
          isinstance(do["token"]["totals"].get("sessions"), int)
          and do["token"]["totals"]["sessions"] == len(U.read_usage(db_odd)["sessions"]),
          f"(sessions={do['token']['totals'].get('sessions')})")

    # ---- 4. 注入防护 ----
    print("\n=== 参数白名单（注入防护） ===")
    check("拒绝分号拼接", U._sanitize_args(["token", ";", "rm", "-rf", "/"]) == ["token"])
    check("拒绝 && 拼接", U._sanitize_args(["token", "&&", "evil"]) == ["token"])
    check("拒绝反引号", U._sanitize_args(["`whoami`"]) == [])
    check("拒绝 $() ", U._sanitize_args(["$(calc)"]) == [])
    win_db = r"C:\Users\Martin\mimocode.db"
    check("保留 Windows --db 路径", U._sanitize_args(["--db", win_db]) == ["--db", win_db])
    try:
        U._sanitize_args(["--db", "C:\\safe.db; echo pwned"])
        invalid_db_rejected = False
    except ValueError:
        invalid_db_rejected = True
    check("拒绝非法 --db 路径而不静默回退", invalid_db_rejected)
    check("保留合法 kind", U._sanitize_args(["session"]) == ["session"])
    check("保留合法选项与值", U._sanitize_args(["--days", "7"]) == ["--days", "7"])
    check("丢弃未知选项", U._sanitize_args(["--evil", "x"]) == [])
    check("丢弃未知 kind", U._sanitize_args(["hack"]) == [])
    check("--open 白名单放行", U._sanitize_args(["--open"]) == ["--open"])

    # ---- 5. JSONC 解析 ----
    print("\n=== JSONC 解析 ===")
    sample = """{
  // 行注释
  "a": 1, /* 块注释 */
  "b": "含 // 的字符串",
  "c": [1, 2,],
}"""
    parsed = json.loads(R.strip_jsonc(sample))
    check("剥离行注释", parsed["a"] == 1)
    check("剥离块注释", set(parsed.keys()) == {"a", "b", "c"})
    check("保留字符串内的 //", parsed["b"] == "含 // 的字符串")
    check("容忍尾随逗号", parsed["c"] == [1, 2])

    # 模板形状
    import re as _re
    tpl = R.build_usage_template("python.exe", "usage_cli.py")
    shell_cmds = _re.findall(r"!`([^`]+)`", tpl)
    check("模板含且仅含 1 个 shell 命令", len(shell_cmds) == 1, f"({len(shell_cmds)})")
    check("模板不透传用户参数到 shell", bool(shell_cmds) and "--arg-string" not in shell_cmds[0] and "$ARGUMENTS" not in shell_cmds[0])
    check("模板要求原样复述", "原样" in tpl)

    # ---- 6. register_command 写入行为 ----
    print("\n=== 配置注册行为 ===")
    import contextlib
    import io

    reg_root = tempfile.mkdtemp(prefix="mimo-register-test-")
    reg_py = sys.executable
    reg_script = os.path.join(ROOT, "usage_cli.py")

    def run_reg(cfg_path, extra=(), script=None):
        argv = ["--config", cfg_path, "--python", reg_py,
                "--script", script or reg_script] + list(extra)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = R.main(argv)
        return code, out.getvalue(), err.getvalue()

    def new_cfg(name, text, bom=False):
        d = tempfile.mkdtemp(prefix=f"{name}-", dir=reg_root)
        p = os.path.join(d, "mimocode.jsonc")
        data = text.encode("utf-8")
        with open(p, "wb") as fh:
            fh.write(data if not bom else b"\xef\xbb\xbf" + data)
        return d, p

    def backups(d):
        return sorted(n for n in os.listdir(d) if ".bak-" in n)

    def load(path):
        return json.loads(R.strip_jsonc(io.open(path, encoding="utf-8-sig").read()))

    def read(path):
        with open(path, "rb") as fh:
            return fh.read()

    base_cfg = (
        "{\n"
        "  // 用户自己的注释\n"
        '  "command": {\n'
        '    "existing-command": {"description": "keep me", "template": "unchanged"},\n'
        '    "usage": {"description": "old", "template": "old", "agent": "custom-agent"}\n'
        "  },\n"
        '  "plugin": ["usage-dashboard.mjs", "example-user-plugin.mjs"],\n'
        '  "mcp": {}\n'
        "}\n"
    )

    d1, p1 = new_cfg("preserve", base_cfg)
    code1, out1, err1 = run_reg(p1)
    j1 = load(p1)
    check("register 返回 0", code1 == 0, f"(code={code1}, out={out1.strip()})")
    check("无关命令条目保留", j1["command"]["existing-command"]["template"] == "unchanged")
    check("命令条目自定义字段保留", j1["command"]["usage"].get("agent") == "custom-agent")
    check("plugin 默认保留", j1["plugin"] == ["usage-dashboard.mjs", "example-user-plugin.mjs"])
    check("顶层其它键保留", j1["mcp"] == {})
    check("模板不透传用户参数", "$ARGUMENTS" not in j1["command"]["usage"]["template"])
    check("注释被剥离（重写为纯 JSON）", "//" not in io.open(p1, encoding="utf-8").read())
    check("注释丢失在 stderr 明确告警", "注释" in err1 and "JSONC" in err1)
    check("生成 1 个带时间戳备份", len(backups(d1)) == 1, f"({backups(d1)})")
    check("备份与原始字节逐字节一致", read(os.path.join(d1, backups(d1)[0])) == base_cfg.encode("utf-8"))
    check("stderr 记录备份路径", "backup" in err1 and backups(d1)[0] in err1)
    check("顶层键顺序与内容保持（$schema 未凭空新增）", "$schema" not in j1)

    # 同秒重复写入：备份文件名必须唯一，绝不覆盖
    real_datetime = R.datetime

    class _FixedDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 1, 2, 3, 4, 5)

    R.datetime = _FixedDatetime
    try:
        d2, p2 = new_cfg("same-second", base_cfg)
        code2a, _, _ = run_reg(p2)
        with open(p2, "wb") as fh:          # 复位成“需要改动”的状态
            fh.write(base_cfg.encode("utf-8"))
        code2b, _, _ = run_reg(p2)
    finally:
        R.datetime = real_datetime
    b2 = backups(d2)
    check("同秒两次写入都成功", (code2a, code2b) == (0, 0), f"({code2a},{code2b})")
    check("同秒备份唯一、不覆盖", len(b2) == 2, f"({b2})")
    check("第二个同秒备份带序号后缀", any(n.endswith("-1") for n in b2), f"({b2})")
    check("两次备份内容都是原始字节",
          all(read(os.path.join(d2, n)) == base_cfg.encode("utf-8") for n in b2))

    # 解析失败：返回 3，原文件一字不动、不产生备份
    d3, p3 = new_cfg("bad-json", "{ not valid json ")
    before3 = read(p3)
    code3, _, _ = run_reg(p3)
    check("非法 JSON 返回 3", code3 == 3, f"(code={code3})")
    check("解析失败时原文件不变", read(p3) == before3)
    check("解析失败时不产生备份", backups(d3) == [])

    # 配置 / 脚本不存在
    code4, _, _ = run_reg(os.path.join(reg_root, "nope", "mimocode.jsonc"))
    check("配置不存在返回 4", code4 == 4, f"(code={code4})")
    code5, _, _ = run_reg(p1, script=os.path.join(reg_root, "missing_cli.py"))
    check("脚本不存在返回 4", code5 == 4, f"(code={code5})")

    # 无变化：返回 1，不重复写盘
    code6, out6, _ = run_reg(p1)
    check("重复注册返回 1", code6 == 1 and out6.strip() == "already-registered", f"(code={code6})")
    check("无变化时不新增备份", len(backups(d1)) == 1)

    # --no-backup
    d7, p7 = new_cfg("no-backup", base_cfg)
    code7, _, _ = run_reg(p7, extra=["--no-backup"])
    check("--no-backup 不生成备份", code7 == 0 and backups(d7) == [], f"(code={code7})")

    # 旧插件引用只在显式要求时才清理
    d8, p8 = new_cfg("purge", base_cfg)
    code8, _, _ = run_reg(p8, extra=["--purge-legacy-plugin"])
    j8 = load(p8)
    check("--purge-legacy-plugin 生效", code8 == 0 and j8["plugin"] == ["example-user-plugin.mjs"],
          f"({j8.get('plugin')})")

    # --uninstall：只删本项目命令，默认不动 plugin
    code9, _, _ = run_reg(p1, extra=["--uninstall"])
    j9 = load(p1)
    check("--uninstall 返回 0", code9 == 0, f"(code={code9})")
    check("--uninstall 移除本项目命令",
          "usage" not in j9.get("command", {}) and "usage-open" not in j9.get("command", {}))
    check("--uninstall 保留无关命令", j9["command"]["existing-command"]["template"] == "unchanged")
    check("--uninstall 默认保留 plugin",
          j9["plugin"] == ["usage-dashboard.mjs", "example-user-plugin.mjs"])

    # BOM：原文件带 BOM 时写入后仍带 BOM
    d10, p10 = new_cfg("bom", base_cfg, bom=True)
    code10, _, _ = run_reg(p10)
    check("带 BOM 的配置写入后仍保留 BOM", read(p10)[:3] == b"\xef\xbb\xbf", f"(code={code10})")
    check("BOM 配置写入后可解析", load(p10)["command"]["usage"]["template"].startswith("用户请求查看"))

    # 注释计数只统计字符串外的注释
    check("注释计数忽略字符串内的 //",
          R.jsonc_comment_count('{"u":"http://x","a":1}') == 0)
    check("注释计数统计行注释与块注释",
          R.jsonc_comment_count('{ // one\n /* two */ "a":1}') == 2)

    # ---- 收尾 ----
    try:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
        shutil.rmtree(reg_root, ignore_errors=True)
    except OSError:
        pass

    print(f"\n{'PYTHON TESTS OK' if FAILS == 0 else 'PYTHON TESTS FAILED: ' + str(FAILS)}")
    return 0 if FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
