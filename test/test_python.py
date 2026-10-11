#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_python.py — usage_cli.py 的端到端测试（纯 Python，无需 Node 子进程）

覆盖：
  1. 聚合口径（用临时 sqlite 造数据，验证 tokens / 命中率 / 工具 / 会话）
  2. 三种视图（token / session / context / all）的 HTML 结构
  3. 命令参数白名单防注入
  4. JSONC 剥离与注释容错

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
            check(f"{kind}: 模型块含命中率条", "track3" in html and "cm-hit-hit" in html)
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

    # ---- 4. 注入防护 ----
    print("\n=== 参数白名单（注入防护） ===")
    check("拒绝分号拼接", U._sanitize_args(["token", ";", "rm", "-rf", "/"]) == ["token"])
    check("拒绝 && 拼接", U._sanitize_args(["token", "&&", "evil"]) == ["token"])
    check("拒绝反引号", U._sanitize_args(["`whoami`"]) == [])
    check("拒绝 $() ", U._sanitize_args(["$(calc)"]) == [])
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
    check("shell 命令经 --arg-string 透传", "--arg-string" in shell_cmds[0] if shell_cmds else False)
    check("模板要求原样复述", "原样" in tpl)

    # ---- 收尾 ----
    try:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    except OSError:
        pass

    print(f"\n{'PYTHON TESTS OK' if FAILS == 0 else 'PYTHON TESTS FAILED: ' + str(FAILS)}")
    return 0 if FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
