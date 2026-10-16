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
import time as time_module

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import usage_cli as U  # noqa: E402
import register_command as R  # noqa: E402
import installer as I  # noqa: E402

FAILS = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global FAILS
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  {extra}" if extra else ""))
    if not ok:
        FAILS += 1


def cleanup_temp(path: str, label: str, attempts: int = 3) -> None:
    """删除临时目录；删不掉就**有界重试**，最终仍失败则把残留如实打印出来。

    刻意不用 `shutil.rmtree(..., ignore_errors=True)`：Windows 上它删不掉被占用的
    文件却把错误咽掉，"删掉了"和"没删掉"返回值一样 —— 几十 MB 的大库 fixture 会
    在 %TEMP% 里无声累积。这里用 installer.force_rmtree（清只读位重试、返回真实
    存在性）拿真实结果。

    重试是**有界**的：失败通常只是句柄还没释放（例如后台请求线程刚跑完一次大库
    聚合），等一小会儿就能删；真的删不掉就打印残留 —— 只报告，既不把原本失败的
    测试改绿，也不给通过的测试引入新的抖动。
    """
    if not os.path.isdir(path):
        return
    for attempt in range(attempts):
        if I.force_rmtree(path):
            return
        if attempt + 1 < attempts:
            time_module.sleep(0.5)
    print(f"  WARN  {label}未能清理，残留：{path}")


# ---------------------------------------------------------------------------
# 造一个最小可用的 mimocode.db
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
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


def make_db(path: str, now_ms: int) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
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


# ---------------------------------------------------------------------------
# 「贴近真实形态」的大库：跨 12+ 个月、多模型、多 part 类型、含无数据日
# 供 UI 几何 / 行为回归测试使用（几何断言需要真实的月份跨度与列数）。
# ---------------------------------------------------------------------------

RICH_MODELS = (
    # providerID, modelID, 单条 assistant 消息的 tokens 组成
    ("xiaomi-token-plan-cn", "mimo-v2.6-pro",
     {"input": 800, "output": 200, "reasoning": 100, "cache": {"read": 900, "write": 0}}),
    ("xiaomi-token-plan-cn", "mimo-v2.6-flash",
     {"input": 5000, "output": 300, "reasoning": 50, "cache": {"read": 100, "write": 0}}),
    ("deepseek", "deepseek-v4-flash",
     {"input": 0, "output": 700, "reasoning": 0, "cache": {"read": 0, "write": 0}}),
    ("glm", "glm-5.3",
     {"input": 200, "output": 100, "reasoning": 0, "cache": {"read": 9800, "write": 0}}),
)
RICH_TOOLS = ("bash", "read", "write", "edit")
RICH_GAP_EVERY = 11   # 每 11 天留一天没有调用 → 热力图里必须出现「无调用」格子


def _local_noon_ms(days_ago: int) -> int:
    """当天的本地 12:00 —— 避开时区/夏令时把行算到相邻日期。"""
    from datetime import datetime as _dt, timedelta as _td
    d = (_dt.now() - _td(days=days_ago)).replace(hour=12, minute=0, second=0, microsecond=0)
    return int(d.timestamp() * 1000)


def make_rich_db(path: str, days: int = 420) -> None:
    """跨 ``days`` 天、多模型、多 part 类型的最小真实形态库（纯合成数据）。

    - 每天 1 条 assistant 消息，轮流使用 RICH_MODELS 的 4 个模型；
    - 每天 1 个 step-finish（上下文占用取最新一条）、1 个 text、3 个 tool
      （其中 1 个故意不带 tool 名，用于验证「未知工具」归类）；
    - 每 RICH_GAP_EVERY 天跳过一天，让热力图里存在真正的「无调用」日期；
    - 近 1 / 7 / 30 天的量级差 50 / 10 / 3 倍，保证时间尺度切换后数字必然变化。
    """
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO session (id,title,directory,time_created,time_updated,"
        "summary_additions,summary_deletions,summary_files,context_watermark) VALUES (?,?,?,?,?,?,?,?,?)",
        ("sess-rich", "UI 回归会话", "D:/proj/ui", _local_noon_ms(days - 1),
         _local_noon_ms(0), 421, 137, 12, "wm-1"),
    )

    msg_n = 0
    part_n = 0
    for i in range(days):
        if i > 0 and i % RICH_GAP_EVERY == 7:
            continue
        if i == 0:
            factor = 50          # 今日
        elif i < 7:
            factor = 10          # 近 7 天
        elif i < 30:
            factor = 3           # 近 30 天
        else:
            factor = 1           # 更早
        prov, model, base = RICH_MODELS[i % len(RICH_MODELS)]
        ts = _local_noon_ms(i)
        msg_n += 1
        msg_id = "m%d" % msg_n
        tokens = {k: v * factor for k, v in base.items() if k != "cache"}
        tokens["cache"] = {k: v * factor for k, v in base["cache"].items()}
        tokens["total"] = (tokens["input"] + tokens["output"] + tokens["reasoning"]
                           + tokens["cache"]["read"] + tokens["cache"]["write"])
        conn.execute(
            "INSERT INTO message (id,session_id,time_created,time_updated,data) VALUES (?,?,?,?,?)",
            (msg_id, "sess-rich", ts, ts,
             json.dumps({"role": "assistant", "providerID": prov, "modelID": model,
                         "time": {"created": ts}, "tokens": tokens})),
        )

        def _part(data: dict) -> None:
            nonlocal part_n
            part_n += 1
            conn.execute(
                "INSERT INTO part (id,message_id,session_id,time_created,time_updated,data) "
                "VALUES (?,?,?,?,?,?)",
                ("p%d" % part_n, msg_id, "sess-rich", ts, ts, json.dumps(data)),
            )

        _part({"type": "step-finish", "tokens": tokens})
        _part({"type": "text", "text": "…"})
        for t in range(3):
            if t == 2:
                _part({"type": "tool"})                      # 缺 tool 名 → 未知工具
            else:
                _part({"type": "tool", "tool": RICH_TOOLS[(i + t) % len(RICH_TOOLS)]})
    conn.commit()
    conn.close()


def make_big_db(path: str, messages: int = 28000) -> None:
    """造一个 20MB 以上的合成库（纯合成数据，只用于"大库"回归）。

    存在的理由：实时服务的身份探测曾经走 `/api/report`，而它**每次请求都做全量
    聚合** —— 库越大越慢，20MB 左右就超过探测的 1.2s 超时，健康实例会被判成
    "不可达/陈旧状态"（详见「大库下的实时服务启动」一节）。这个 fixture 让回归
    测试跑在确实会触发原故障的规模上，而不是小库上"看起来没问题"。
    """
    tools = ("read", "write", "bash", "edit", "grep")
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    now_ms = int(time_module.time() * 1000)
    conn.execute(
        "INSERT INTO session (id,title,directory,time_created,time_updated,"
        "summary_additions,summary_deletions,summary_files) VALUES (?,?,?,?,?,?,?,?)",
        ("s-big", "大库会话", "D:/proj/big", now_ms - 86_400_000, now_ms, 10, 4, 1),
    )
    for i in range(messages):
        mid = f"bm{i}"
        ts = now_ms - i * 1000
        tokens = {"input": 400, "output": 120, "reasoning": 30,
                  "cache": {"read": 900, "write": 40}}
        tokens["total"] = (tokens["input"] + tokens["output"] + tokens["reasoning"]
                           + tokens["cache"]["read"] + tokens["cache"]["write"])
        conn.execute(
            "INSERT INTO message (id,session_id,time_created,time_updated,data) "
            "VALUES (?,?,?,?,?)",
            (mid, "s-big", ts, ts, json.dumps(
                {"role": "assistant", "providerID": "xiaomi-token-plan-cn",
                 "modelID": "mimo-v2.6-flash", "time": {"created": ts}, "tokens": tokens})),
        )
        for p in range(5):
            data = ({"type": "step-finish", "tokens": tokens} if p == 0
                    else {"type": "tool", "tool": tools[p % len(tools)]})
            conn.execute(
                "INSERT INTO part (id,message_id,session_id,time_created,time_updated,data) "
                "VALUES (?,?,?,?,?,?)",
                (f"bp{i}-{p}", mid, "s-big", ts, ts, json.dumps(data)),
            )
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
    # 上限解析会去读 ~/.config/mimocode/mimocode.jsonc。把 ~ 临时指向空目录，让
    # 「内置表兜底」这条断言不依赖运行机器上是否存在该配置 —— CI 上没有它、开发者
    # 机器上往往有，不隔离的话同一份代码在两种环境下会得出相反结果。
    home_tmp = tempfile.mkdtemp(prefix="mimo-usage-home-")
    real_expanduser = os.path.expanduser
    U.os.path.expanduser = lambda p: home_tmp if p == "~" else real_expanduser(p)
    try:
        ctx = U.session_context(db)
    finally:
        U.os.path.expanduser = real_expanduser
        cleanup_temp(home_tmp, "隔离 home 目录")
    # Issue #4: explicit selection must never silently switch to the newest session.
    check("Issue #4：默认模式为 latest", ctx.get("selectionMode") == "latest")
    fixed = U.session_context(db, session_id="s2")
    check("Issue #4：指定 s2 保持固定模式",
          fixed.get("sessionId") == "s2" and fixed.get("selectionMode") == "fixed")
    missing = U.session_context(db, session_id="does-not-exist")
    check("Issue #4：无效显式会话不回退到最新",
          missing.get("ok") is False and missing.get("selectionMode") == "fixed"
          and missing.get("sessionId") == "does-not-exist")
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
        check(f"{kind}: 静态 LIVE=null", "var LIVE = null" in html)
        check(f"{kind}: 数据内联", "var DATA" in html)
        check(f"{kind}: 有刷新按钮", 'id="regen"' in html)
        if kind in ("token", "all"):
            check(f"{kind}: 含热力图渲染器", "function heatmapCard" in html)
            check(f"{kind}: 含模型调用块", "按模型的调用情况" in html)
            check(f"{kind}: 含缓存命中率", "缓存命中率" in html)
            check(f"{kind}: 输入 tokens 卡片包含缓存读取", '输入 tokens（含缓存读取）' in html)
            check(f"{kind}: 模型块为单条进度条", 'class="track"' in html)
            # UI-4：第二条 bar（track2/track3）与「未命中强制最小宽度」都必须消失。
            # 注意 HTML 是 JS 外壳，模型行要到运行期才由 buildBody 渲染，
            # 所以「每个模型恰好 1 条 bar」的计数断言在 test/ui-dom.mjs 里做。
            check(f"{kind}: 旧的第二条 bar 已移除", "track2" not in html and "track3" not in html
                  and "cm-hit-hit" not in html and "cm-hit-miss" not in html)
            check(f"{kind}: 缓存读取改用浅色段", 'class="tc"' in html and 'class="tf"' in html)
            check(f"{kind}: 不再按「相对最大模型」定条长", "相对最高模型" not in html and "maxTok" not in html)
            check(f"{kind}: 未命中条不再强制最小宽度", "Math.max(freshPct, 1.6)" not in html)
            check(f"{kind}: 未命中条不再负 margin 覆盖", "margin-left:-1.6%" not in html)
            # 热力图数据必须覆盖全量（不受 --days 限制）+ 顶部趋势用范围内数据
            check(f"{kind}: token 数据含 daily(热力图)", '"daily"' in html)
            check(f"{kind}: token 数据含 recent(趋势)", '"recent"' in html)
        if kind in ("session", "all"):
            check(f"{kind}: 含最近会话表", "最近会话" in html)
        if kind in ("context", "all"):
            check(f"{kind}: 含上下文环", "上下文占用" in html)
            check(f"{kind}: 运行时默认折叠分支", 'aria-expanded="' in html and "(open ? \"true\" : \"false\")" in html)

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

    # ---- 3b. 时间尺度窗口（UI-3 的数据层） ----
    print("\n=== 时间尺度窗口 ===")
    from datetime import datetime as _dt, timedelta as _td
    tk = U.card_data(db, kind="token", days=30)["token"]
    check("windowKeys 固定包含 1/7/30", tk["windowKeys"] == ["1", "7", "30"], f"({tk['windowKeys']})")
    check("三份窗口都内联进 payload", sorted(tk["windows"]) == ["1", "30", "7"], f"({sorted(tk['windows'])})")
    shape30 = sorted(tk["windows"]["30"])
    check("三份窗口结构一致", all(sorted(w) == shape30 for w in tk["windows"].values())
          and {"days", "fromDate", "modelTotal", "modelCount", "totals", "models", "recent"} <= set(shape30),
          f"({shape30})")
    check("窗口 days 与 key 一致", all(w["days"] == int(k) for k, w in tk["windows"].items()))
    today = U._local_date(now_ms)
    exp29 = U._local_date(int((_dt.now() - _td(days=29)).timestamp() * 1000))
    check("「今日」按自然日：fromDate = 今天（不是滚动 24 小时）",
          tk["windows"]["1"]["fromDate"] == today,
          f"({tk['windows']['1']['fromDate']} vs {today})")
    check("「30 天」fromDate = 今天 - 29 天", tk["windows"]["30"]["fromDate"] == exp29,
          f"({tk['windows']['30']['fromDate']} vs {exp29})")
    check("顶层字段 == windows['30'] 逐值一致（回归基线）",
          tk["totals"] == tk["windows"]["30"]["totals"]
          and tk["recent"] == tk["windows"]["30"]["recent"]
          and tk["models"] == tk["windows"]["30"]["models"])
    check("--days 非 1/7/30 时补出自定义窗口（不篡改 --days 语义）",
          U.card_data(db, kind="token", days=15)["token"]["windowKeys"] == ["1", "7", "15", "30"])

    dbw = os.path.join(tmpdir, "windows.db")
    make_rich_db(dbw)
    wt = U.card_data(dbw, kind="token", days=30)["token"]
    t1 = wt["windows"]["1"]["totals"]["tokens"]
    t7 = wt["windows"]["7"]["totals"]["tokens"]
    t30 = wt["windows"]["30"]["totals"]["tokens"]
    check("今日 < 7 天 < 30 天（窗口口径独立，未互相污染）", 0 < t1 < t7 < t30, f"({t1} < {t7} < {t30})")
    check("请求数同样随时间窗口递增",
          wt["windows"]["1"]["totals"]["requests"] < wt["windows"]["7"]["totals"]["requests"]
          < wt["windows"]["30"]["totals"]["requests"])
    check("会话数在各窗口一致（全量口径，不随时间窗口漂移）",
          len({w["totals"]["sessions"] for w in wt["windows"].values()}) == 1)
    check("daily[] 带 read/input/requests（供热力图算当日缓存命中率）",
          wt["daily"] and all({"date", "tokens", "read", "input", "requests"} <= set(d) for d in wt["daily"]))
    check("daily 覆盖全量（不受 --days 限制），recent 只覆盖窗口",
          len(wt["daily"]) > len(wt["windows"]["30"]["recent"]),
          f"(daily={len(wt['daily'])} recent30={len(wt['windows']['30']['recent'])})")
    check("模型 share 以窗口内全部模型 token 为分母",
          abs(sum(m["share"] for m in wt["windows"]["30"]["models"]) - 1.0) < 1e-9,
          f"({sum(m['share'] for m in wt['windows']['30']['models'])})")
    check("modelTotal == 窗口内模型 token 之和",
          wt["windows"]["30"]["modelTotal"] == sum(m["tokens"] for m in wt["windows"]["30"]["models"]))

    # ---- 3c. 上下文用量分类树（数据层） ----
    print("\n=== 上下文用量分类树 ===")
    cx = U.session_context(dbw)
    bd = cx["breakdown"]
    byname = {b["label"]: b for b in bd["branches"]}
    check("4 个分支：快照 / 累计 / 工具调用 / 消息片段",
          [b["label"] for b in bd["branches"]] == ["当前上下文快照", "本会话累计", "工具调用", "消息片段构成"],
          f"({[b['label'] for b in bd['branches']]})")
    check("单位分别标注（tokens/tokens/次/条），不同口径不混算",
          [b["unit"] for b in bd["branches"]] == ["tokens", "tokens", "次", "条"])
    check("每个分支的子项之和 == 分支合计（分类结果可核对）",
          all(sum(c["value"] for c in b["children"]) == b["total"] for b in bd["branches"]),
          f"({[(b['label'], sum(c['value'] for c in b['children']), b['total']) for b in bd['branches']]})")
    check("快照分支合计 == context.used", byname["当前上下文快照"]["total"] == cx["context"]["used"])
    detail = cx["context"]["detail"]
    check("快照子项与 context.detail 逐项一致",
          {c["label"]: c["value"] for c in byname["当前上下文快照"]["children"]} ==
          {"缓存读取": detail["read"], "新输入": detail["input"], "推理": detail["reasoning"],
           "输出": detail["output"], "缓存写入": detail["write"]})
    check("累计分支合计 == 本会话 assistant 消息 tokens 之和",
          byname["本会话累计"]["total"] == cx["tokens"]["total"],
          f"({byname['本会话累计']['total']} vs {cx['tokens']['total']})")
    check("累计口径与快照口径是两个不同的数字（未被混为一谈）",
          byname["本会话累计"]["total"] != byname["当前上下文快照"]["total"])

    conn_w = sqlite3.connect(dbw)
    part_rows = [json.loads(r[0]) for r in conn_w.execute("SELECT data FROM part").fetchall()]
    n_parts = len(part_rows)
    n_tools = sum(1 for d in part_rows if isinstance(d, dict) and d.get("type") == "tool")
    n_unknown_tools = sum(1 for d in part_rows
                          if isinstance(d, dict) and d.get("type") == "tool"
                          and not (isinstance(d.get("tool"), str) and d["tool"].strip()))
    conn_w.close()
    check("工具分支合计 == 库里 part.type=tool 的行数（独立核对）",
          byname["工具调用"]["total"] == n_tools == cx["tools"], f"({n_tools})")
    check("片段分支合计 == part 表行数（独立核对）",
          byname["消息片段构成"]["total"] == n_parts, f"({n_parts})")
    tool_kids = {c["label"]: c["value"] for c in byname["工具调用"]["children"]}
    check("缺 tool 名的调用归入「未知工具」而不是被丢弃",
          tool_kids.get("未知工具", 0) == n_unknown_tools > 0, f"({tool_kids})")
    check("子项按次数降序排列（同次数按名称升序）",
          [c["value"] for c in byname["工具调用"]["children"]] ==
          sorted([c["value"] for c in byname["工具调用"]["children"]], reverse=True))
    check("无法细分的分类被如实列出并给出原因",
          set(bd["notSubdividable"]["labels"]) >= {"系统提示词", "内置工具清单", "Memory 文件", "Skills"}
          and len(bd["notSubdividable"]["reason"]) > 20)
    check("_branch：差额单列为「未分类」",
          [c["label"] for c in U._branch("x", "tokens", "", 100, [("a", 40), ("b", 30)])["children"]]
          == ["a", "b", "未分类"])
    check("_branch：字段和超出总量时如实标记异常（不强行归零）",
          U._branch("x", "tokens", "", 10, [("a", 40)])["children"][-1]
          == {"label": "字段和超出总量", "value": 30})
    check("_branch：空输入得到只有根节点的分支",
          U._branch("x", "次", "", 0, []) == {"label": "x", "unit": "次", "note": "",
                                             "total": 0, "children": []})
    check("_counted：合并重复类别并按次数降序",
          U._counted(["bash", "bash", "read"], "未知工具") == [("bash", 2), ("read", 1)])
    check("_counted：空值/空白/非字符串一律归入未知类别",
          U._counted([None, "", "  ", 0, True, {}, "bash"], "未知工具")
          == [("未知工具", 6), ("bash", 1)])
    check("_counted：空输入返回空列表", U._counted([], "未知工具") == [])
    db_nomsg = os.path.join(tmpdir, "nomsg.db")
    make_db(db_nomsg, now_ms)
    cn = sqlite3.connect(db_nomsg)
    cn.executescript("DELETE FROM part; DELETE FROM message;")
    cn.commit()
    cn.close()
    nb = U.session_context(db_nomsg)["breakdown"]["branches"]
    check("有会话但无消息/片段：分支合计全为 0 且不抛异常",
          len(nb) == 4 and all(b["total"] == 0 and sum(c["value"] for c in b["children"]) == 0 for b in nb),
          f"({[(b['label'], b['total'], len(b['children'])) for b in nb]})")
    check("完全没有会话时安全返回 ok=False（不抛异常）",
          U.session_context(db_empty).get("ok") is False,
          f"({U.session_context(db_empty).get('error')})")

    # ---- 3d. 只读保证（C5） ----
    print("\n=== 只读保证 ===")
    import hashlib
    import inspect

    def _digest(p):
        with open(p, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    before = _digest(dbw)
    U.read_usage(dbw)
    U.card_data(dbw, kind="all")
    U.session_context(dbw)
    after = _digest(dbw)
    check("聚合与渲染全程不修改数据库文件（C5）", before == after, f"({before[:12]}… vs {after[:12]}…)")
    check("不产生 WAL/SHM 旁文件",
          not os.path.exists(dbw + "-wal") and not os.path.exists(dbw + "-shm"))
    check("连接使用只读 URI（mode=ro）", "mode=ro" in inspect.getsource(U._open_readonly))
    check("源码中不存在写库语句（INSERT/UPDATE/DELETE/CREATE/ATTACH）",
          not any(k in inspect.getsource(U) for k in
                  ("INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE TABLE", "ATTACH", "PRAGMA")))

    # ---- 3e. 只读快照回退的临时副本生命周期（Release Gate 回归） ----
    # 旧实现：_open_readonly 失败回退时 mkdtemp 整库拷贝到 %TEMP%，**从不删除**
    # —— 被占用的库每次读取都永久漏一份（本机库可达数百 MB）。现在必须：
    #   * 正常直读不产生任何快照目录；
    #   * 走快照回退时，调用方关闭连接后目录被删除；
    #   * 初始化半途失败不留半成品；
    #   * 只删本次调用私有的 mkdtemp 目录。
    print("\n=== 只读快照回退（临时副本生命周期） ===")
    import shutil as _shutil

    snap_root = os.path.join(tmpdir, "snap-tmp")
    os.makedirs(snap_root, exist_ok=True)
    old_tempdir = tempfile.tempdir
    tempfile.tempdir = snap_root
    real_connect = sqlite3.connect
    try:
        def _snap_leftovers():
            return [n for n in os.listdir(snap_root) if n.startswith("mimo-usage-")]

        # 正常路径：直读成功，不得产生快照目录
        U.read_usage(dbw)
        check("正常只读路径不产生快照目录", _snap_leftovers() == [], f"({_snap_leftovers()})")

        # 强制走快照回退：第一次 connect（只读 URI）模拟"database is locked"，
        # 之后的真实 connect 打开快照副本。比拿 EXCLUSIVE 锁可靠（Windows 上
        # 强制字节锁会让快照复制本身也失败）。
        snap_db = os.path.join(tmpdir, "snap.db")
        make_db(snap_db, int(time_module.time() * 1000))
        fail_once = {"armed": True}

        def _connect_first_fails(*a, **kw):
            if fail_once["armed"]:
                fail_once["armed"] = False
                raise sqlite3.OperationalError("simulated: database is locked")
            return real_connect(*a, **kw)

        sqlite3.connect = _connect_first_fails
        try:
            conn_s, snap_s = U._open_readonly(snap_db)
        finally:
            sqlite3.connect = real_connect
        check("锁冲突时走快照回退并返回快照目录", snap_s is not None)
        if snap_s is not None:
            n_master = conn_s.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
            check("快照连接可查询", isinstance(n_master, int))
            check("快照目录落在受控临时根下",
                  os.path.isdir(snap_s) and os.path.abspath(snap_s).startswith(os.path.abspath(snap_root)),
                  f"({snap_s})")
            check("快照内含数据库副本",
                  os.path.isfile(os.path.join(snap_s, os.path.basename(snap_db))))
            try:
                conn_s.close()
            except sqlite3.Error:
                pass
            U._cleanup_snapshot(snap_s)
            check("关闭连接并清理后快照目录已删除", not os.path.exists(snap_s))
        check("清理后不留任何 mimo-usage-* 目录", _snap_leftovers() == [], f"({_snap_leftovers()})")

        # 调用方路径：read_usage 走快照回退后，finally 必须删掉快照目录
        fail_once["armed"] = True
        sqlite3.connect = _connect_first_fails
        try:
            usage_via_snap = U.read_usage(dbw)
        finally:
            sqlite3.connect = real_connect
        check("read_usage 经快照回退仍返回完整数据",
              isinstance(usage_via_snap, dict) and usage_via_snap.get("totals", {}).get("total", 0) >= 0)
        check("read_usage 结束后快照目录已清理", _snap_leftovers() == [], f"({_snap_leftovers()})")

        # 初始化半途失败：主库是垃圾字节 → 快照副本打不开 → 清掉半成品再抛
        garbage_db = os.path.join(tmpdir, "garbage.db")
        with open(garbage_db, "wb") as fh:
            fh.write(b"this is not a sqlite database at all")
        fail_once["armed"] = True
        raised = None
        sqlite3.connect = _connect_first_fails
        try:
            U._open_readonly(garbage_db)
        except sqlite3.Error as exc:
            raised = exc
        except OSError:
            raised = None  # 复制阶段失败同样必须已清理
        finally:
            sqlite3.connect = real_connect
        check("垃圾数据库经快照回退后如实抛错（不静默吞掉）", raised is not None)
        check("初始化失败后不遗留半成品快照目录", _snap_leftovers() == [], f"({_snap_leftovers()})")
    finally:
        tempfile.tempdir = old_tempdir
        sqlite3.connect = real_connect

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

    # P0 回归（2026-10-14 真实部署暴露）：--help 曾被白名单**静默丢弃**，于是
    # `usage_cli.py --help` 变成无参调用 —— 读了真实数据库、往用户数据目录写了报告，
    # 还会在没有数据库的机器上让安装器的自检直接失败。三条断言把这条路封住。
    check("--help 白名单放行（不再被静默丢弃）", U._sanitize_args(["--help"]) == ["--help"])
    check("-h 白名单放行", U._sanitize_args(["-h"]) == ["-h"])
    _dropped_flags = []
    check("未知选项被丢弃时回传给调用方（便于报错而非静默变形）",
          U._sanitize_args(["--bogus"], _dropped_flags) == [] and _dropped_flags == ["--bogus"],
          f"(dropped={_dropped_flags})")
    import contextlib as _cl
    import io as _io_guard
    _o_g, _e_g = _io_guard.StringIO(), _io_guard.StringIO()
    try:
        with _cl.redirect_stdout(_o_g), _cl.redirect_stderr(_e_g):
            _guard_code = U.main(["--bogus"])
    except SystemExit as _se:
        _guard_code = _se.code
    check("参数一个都没被识别时必须退出码 2（不静默按默认行为执行）",
          _guard_code == 2, f"(code={_guard_code})")

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

    # 既有 usage 条目必须是"本项目以前写进去的"（template 里含脚本名 usage_cli.py），
    # 否则会被归属护栏按"用户的同名命令"拒绝改动 —— 那正是下面 ownership 一节在测的事。
    base_cfg = (
        "{\n"
        "  // 用户自己的注释\n"
        '  "command": {\n'
        '    "existing-command": {"description": "keep me", "template": "unchanged"},\n'
        '    "usage": {"description": "old", "template": "old usage_cli.py old", "agent": "custom-agent"}\n'
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
    # Release Hardening：改为就地修改，注释与用户排版必须逐字节保留
    raw1 = read(p1).decode("utf-8")
    check("注释被保留（不再重写整个文件）", R.jsonc_comment_count(raw1) == 1, f"(count={R.jsonc_comment_count(raw1)})")
    check("注释原文仍在文件里", "// 用户自己的注释" in raw1)
    check("stderr 不再声称注释会丢失", "不会保留" not in err1)
    check("command 段之前的字节完全未变",
          base_cfg.split('"command"', 1)[0] == raw1.split('"command"', 1)[0])
    check("command 段之后的字节完全未变",
          base_cfg[base_cfg.rindex("}"):] == raw1[raw1.rindex("}"):])
    check("mcp/plugin 行原文未变",
          '"mcp": {}' in raw1 and '"plugin": ["usage-dashboard.mjs", "example-user-plugin.mjs"],' in raw1)
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

    # 只清理"明确属于本项目旧版"的条目：仅含同名子串的用户插件必须保留
    d8b, p8b = new_cfg("purge-scoped",
                       '{"command": {}, "plugin": ["my-usage-dashboard-helper.mjs", "usage-dashboard.mjs"]}\n')
    code8b, _, _ = run_reg(p8b, extra=["--purge-legacy-plugin"])
    j8b = load(p8b)
    check("--purge-legacy-plugin 只删本项目旧插件",
          code8b == 0 and j8b["plugin"] == ["my-usage-dashboard-helper.mjs"],
          f"(code={code8b}, {j8b.get('plugin')})")

    # --uninstall：只删本项目命令，默认不动 plugin
    code9, _, _ = run_reg(p1, extra=["--uninstall"])
    j9 = load(p1)
    check("--uninstall 返回 0", code9 == 0, f"(code={code9})")
    check("--uninstall 移除本项目命令",
          "usage" not in j9.get("command", {}) and "usage-open" not in j9.get("command", {}))
    check("--uninstall 保留无关命令", j9["command"]["existing-command"]["template"] == "unchanged")
    check("--uninstall 默认保留 plugin",
          j9["plugin"] == ["usage-dashboard.mjs", "example-user-plugin.mjs"])

    # 回归（2026-10-14 新增 uninstall 流程时暴露）：注销曾经按名字逐个规划移除，
    # 于是同一个分隔逗号被算进两个相邻区间 → "内部错误：修改区间重叠"。
    # 下面三种形态分别覆盖：只有一个目标成员、两个连续成员、两个不连续成员。
    d11, p11 = new_cfg("uninstall-single",
                       '{"command": {"usage": {"description": "a", "template": "a usage_cli.py"}}}\n')
    code11, out11, err11 = run_reg(p11, extra=["--uninstall"])
    check("注销单个 usage 能成功", code11 == 0, f"(code={code11}, err={err11.strip()[:80]})")
    check("注销后 command 为空", load(p11)["command"] == {})

    d12, p12 = new_cfg("uninstall-both-empty", '{"command": {}}\n')
    check("空 command 注册两个命令", run_reg(p12)[0] == 0 and set(load(p12)["command"]) == {"usage", "usage-open"})
    code12, out12, err12 = run_reg(p12, extra=["--uninstall"])
    check("注销两个连续成员能成功（回归：区间重叠）", code12 == 0,
          f"(code={code12}, err={err12.strip()[:120]})")
    check("注销后 command 为空且文件可解析", load(p12)["command"] == {})

    d13, p13 = new_cfg(
        "uninstall-split",
        '{"command": {"usage": {"description": "a", "template": "a usage_cli.py"}, '
        '"middle": {"description": "m", "template": "m"}, '
        '"usage-open": {"description": "c", "template": "c usage_cli.py"}}}\n')
    code13, out13, err13 = run_reg(p13, extra=["--uninstall"])
    j13 = load(p13)
    check("注销两个不连续成员能成功（回归：区间重叠）", code13 == 0,
          f"(code={code13}, err={err13.strip()[:120]})")
    check("中间的用户命令条目原样保留", list(j13["command"]) == ["middle"]
          and j13["command"]["middle"]["template"] == "m")

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

    # ---- 7. Release Hardening：就地修改与 preflight（T5 / T6） ----
    print("\n=== 就地修改（最小范围）与 preflight ===")

    def masked_raw(text, keys):
        """把指定顶层成员整段折叠掉，用于逐字节比较"其余部分是否原样"。"""
        return R._without_members(text, set(keys))

    def write_fixture(name, text, bom=False, newline="\n"):
        d, p = new_cfg(name, text, bom=bom)
        if newline != "\n":
            with open(p, "wb") as fh:
                fh.write((("\ufeff" if bom else "") + text).replace("\n", newline).encode("utf-8"))
        return d, p

    def apply_and_compare(name, before_text, bom=False, newline="\n", keys=("command",), extra=()):
        d, p = write_fixture(name, before_text, bom=bom, newline=newline)
        raw_before = read(p)
        code, out, err = run_reg(p, extra=extra)
        raw_after = read(p)
        text_before = raw_before.decode("utf-8-sig")
        text_after = raw_after.decode("utf-8-sig")
        ok = (masked_raw(text_before, keys) == masked_raw(text_after, keys))
        check(f"{name}: 除目标成员外逐字节未变", ok, f"(code={code})")
        if not ok:
            check(f"{name}: 差异",
                  False, f"({masked_raw(text_before, keys)[:200]!r} != {masked_raw(text_after, keys)[:200]!r})")
        return code, raw_before, raw_after, text_after

    # T6：注释 / CRLF / BOM / 制表符缩进 / 嵌套 / 既有同名条目字段
    # 注意这里的 usage 是**用户自己写的**（template "mine" 里没有本项目脚本名），
    # 所以默认必须 fail closed、一字不动；只有显式 --force 才允许覆盖 ——
    # 覆盖时下面的字节保留断言全部照旧成立。
    t6 = (
        "{\r\n"
        "\t// 制表符缩进 + CRLF + 注释\r\n"
        '\t"$schema": "https://x/s.json",\r\n'
        '\t"command": {\r\n'
        '\t\t/* 块注释 */\r\n'
        '\t\t"other": { "description": "d", "template": "t", "agent": "keep" },\r\n'
        '\t\t"usage": { "description": "用户自己写的", "template": "mine", "agent": "my-agent" }\r\n'
        "\t},\r\n"
        '\t"mcp": { "servers": { "a": { "url": "http://x" } } }\r\n'
        "}\r\n"
    )
    d6r, p6r = write_fixture("t6-refuse", t6, bom=True, newline="\r\n")
    raw6r = read(p6r)
    code6r, _o6r, err6r = run_reg(p6r)
    check("T6: 用户自建同名命令 → 默认 fail closed（退出码 7）", code6r == 7, f"(code={code6r})")
    check("T6: 拒绝时说明冲突原因与越权开关",
          "FOREIGN_COMMAND" in err6r and "不指向本项目" in err6r and "--force" in err6r)
    check("T6: 拒绝时文件逐字节未变", read(p6r) == raw6r)
    check("T6: 拒绝时不产生备份", backups(d6r) == [])

    code6, rb6, ra6, ta6 = apply_and_compare("t6-jsonc", t6, bom=True, newline="\r\n",
                                             extra=("--force",))
    check("T6: 显式 --force 后退出码 0", code6 == 0, f"(code={code6})")
    check("T6: BOM 保留", ra6[:3] == b"\xef\xbb\xbf")
    check("T6: CRLF 保留", b"\r\n" in ra6 and b"\n" not in ra6.replace(b"\r\n", b""))
    check("T6: 制表符缩进保留", b"\t\t\"other\"" in ra6 or b"\t\t\"usage\"" in ra6)
    check("T6: 行注释保留", "制表符缩进 + CRLF + 注释".encode("utf-8") in ra6)
    check("T6: 块注释保留", b"/* \xe5\x9d\x97\xe6\xb3\xa8\xe9\x87\x8a */" in ra6)
    check("T6: 嵌套 mcp 段逐字节保留", '"mcp": { "servers": { "a": { "url": "http://x" } } }'.encode() in ra6)
    check("T6: 用户自加字段 agent 保留", '"agent": "my-agent"'.encode() in ra6)
    check("T6: 邻居条目 agent 保留", '"agent": "keep"'.encode() in ra6)
    j6 = json.loads(R.strip_jsonc(ta6))
    check("T6: usage 描述已更新", j6["command"]["usage"]["description"].startswith("MiMo Desktop 本机用量"))
    check("T6: usage-open 已插入", "usage-open" in j6["command"])

    # 单行对象 / 无 command 段 / 空对象
    apply_and_compare("inline", '{ "mcp": {}, "other": 1 }\n')
    apply_and_compare("no-command", '{\n  // top\n  "mcp": {}\n}\n')
    d_e, p_e = new_cfg("empty", "{}\n")
    code_e, _, _ = run_reg(p_e)
    je = load(p_e)
    check("空对象配置也能注册", code_e == 0 and set(je["command"]) == {"usage", "usage-open"},
          f"(code={code_e})")

    # 无变化时不得新增备份、不得写盘
    d_i, p_i = new_cfg("idem", '{\n  "command": {}\n}\n')
    run_reg(p_i)
    n_bak_1 = len(backups(d_i))
    raw_i = read(p_i)
    code_i, out_i, _ = run_reg(p_i)
    check("重复注册返回 1", code_i == 1 and out_i.strip() == "already-registered", f"(code={code_i})")
    check("重复注册不新增备份", len(backups(d_i)) == n_bak_1)
    check("重复注册不改变字节", read(p_i) == raw_i)

    # ---- Release Hardening：命令归属护栏（usage command ownership regression） ----
    # 回归目标：**直接运行本脚本**时，绝不能覆盖或删除"不是本项目写的"
    # command.usage / command.usage-open。这条判断以前只存在于 preflight，只有走
    # 安装器时才生效，直接调用本脚本会把用户的同名命令静默改掉。
    print("\n=== 命令归属护栏（usage command ownership regression）===")

    foreign_cfg = (
        "{\n"
        "  // 用户自己的注释\n"
        '  "command": {\n'
        '    "usage": {"description": "我的", "template": "run-my-own-thing", "agent": "my-agent"},\n'
        '    "mine-open": {"description": "keep", "template": "keep"}\n'
        "  },\n"
        '  "myField": 7\n'
        "}\n"
    )

    # 1) 用户已有 usage 命令 → 注册必须失败
    d_o1, p_o1 = new_cfg("own-foreign", foreign_cfg)
    before_o1 = read(p_o1)
    code_o1, _out_o1, err_o1 = run_reg(p_o1)
    check("归属: 用户自己的 usage → 注册 fail closed（退出码 7）", code_o1 == 7, f"(code={code_o1})")
    check("归属: 不覆盖用户命令（文件逐字节未变）", read(p_o1) == before_o1)
    check("归属: 不删除用户命令", "usage" in load(p_o1)["command"])
    check("归属: 错误信息点名冲突成员与原因",
          "command.usage" in err_o1 and "不指向本项目脚本" in err_o1,
          f"({err_o1.strip()[:90]})")
    check("归属: 拒绝时不产生备份", backups(d_o1) == [])

    # 2) 已有本项目 usage（template 指向 usage_cli.py）→ 允许更新
    d_o2, p_o2 = new_cfg(
        "own-ours",
        "{\n"
        '  "command": {\n'
        '    "usage": {"description": "旧", "template": "x usage_cli.py y", "agent": "keep-me"}\n'
        "  }\n"
        "}\n")
    code_o2, _out_o2, _err_o2 = run_reg(p_o2)
    check("归属: 本项目的 usage → 允许更新（退出码 0）", code_o2 == 0, f"(code={code_o2})")
    j_o2 = load(p_o2)
    check("归属: 更新后模板指向本项目",
          j_o2["command"]["usage"]["template"].startswith("用户请求查看"),
          f"({j_o2['command']['usage']['template'][:24]})")
    check("归属: 无关字段 agent 保留", j_o2["command"]["usage"].get("agent") == "keep-me")

    # 3) 用户自定义字段保留（--force 之后也不许丢）
    d_o3, p_o3 = new_cfg("own-force", foreign_cfg)
    code_o3, _out_o3, _err_o3 = run_reg(p_o3, extra=["--force"])
    j_o3 = load(p_o3)
    check("归属: --force 才允许覆盖（退出码 0）", code_o3 == 0, f"(code={code_o3})")
    check("归属: --force 后用户新增字段仍保留",
          j_o3["command"]["usage"].get("agent") == "my-agent")
    check("归属: --force 后邻居命令与顶层字段保留",
          j_o3["command"]["mine-open"]["template"] == "keep" and j_o3["myField"] == 7)
    check("归属: --force 后注释仍逐字节保留",
          "// 用户自己的注释" in read(p_o3).decode("utf-8-sig"))
    check("归属: --force 时先写了备份", len(backups(d_o3)) == 1)

    # 4) 注销同样不许删用户的同名命令
    d_o4, p_o4 = new_cfg("own-uninstall", foreign_cfg)
    before_o4 = read(p_o4)
    code_o4, _out_o4, err_o4 = run_reg(p_o4, extra=["--uninstall"])
    check("归属: 注销用户自己的 usage → fail closed（退出码 7）", code_o4 == 7, f"(code={code_o4})")
    check("归属: 注销被拒后文件逐字节未变", read(p_o4) == before_o4)
    check("归属: 注销被拒时给出归属原因", "FOREIGN_COMMAND" in err_o4)
    code_o4b, _out_o4b, _err_o4b = run_reg(p_o4, extra=["--uninstall", "--force"])
    check("归属: --uninstall --force 才移除", code_o4b == 0, f"(code={code_o4b})")
    check("归属: 注销不误删邻居命令",
          "usage" not in load(p_o4)["command"]
          and load(p_o4)["command"]["mine-open"]["template"] == "keep")

    # 5) 结构问题优先报 6：不能把"结构坏了"说成"归属不明"
    for nm, txt in (("usage.template 不是字符串", '{"command": {"usage": {"template": {}}}}\n'),
                    ("usage 无 template", '{"command": {"usage": {"description": []}}}\n')):
        _d_s, p_s = new_cfg("own-struct", txt)
        code_s, _out_s, err_s = run_reg(p_s)
        check(f"归属: {nm} → 仍按结构错误报 6（不报归属冲突）",
              code_s == 6 and "FOREIGN_COMMAND" not in err_s, f"(code={code_s})")

    # fail closed：重复键 / 非对象 / 无法定位 —— 必须退出码 6 或 3，且文件与备份都不变
    print("\n--- fail closed（禁止猜测）---")
    fc_cases = [
        ("重复 command 键", '{"command": {}, "command": {}}\n'),
        ("重复 usage 键", '{"command": {"usage": {}, "usage": {}}}\n'),
        ("重复 usage-open 键", '{"command": {"usage-open": {}, "usage-open": {}}}\n'),
        ("command 非对象", '{"command": "oops"}\n'),
        ("command 是数组", '{"command": [1, 2]}\n'),
        ("usage 非对象", '{"command": {"usage": "oops"}}\n'),
        ("usage-open 非对象", '{"command": {"usage-open": 5}}\n'),
        ("usage.template 是对象", '{"command": {"usage": {"template": {}}}}\n'),
        ("usage.description 是数组", '{"command": {"usage": {"description": []}}}\n'),
        ("重复 template 键", '{"command": {"usage": {"template": "a", "template": "b"}}}\n'),
        ("字符串未闭合", '{"command": {"usage": "abc\n'),
        ("块注释未闭合", '{"command": /* oops }\n'),
        ("尾随多余内容", '{"command": {}} trailing\n'),
        ("单引号字符串", "{'command': {}}\n"),
        ("顶层不是对象", "[1, 2, 3]\n"),
    ]
    for name, text in fc_cases:
        dfc, pfc = new_cfg("failclosed", text)
        raw_before = read(pfc)
        code_fc, _, err_fc = run_reg(pfc)
        check(f"fail closed: {name}",
              code_fc in (3, 6) and read(pfc) == raw_before and backups(dfc) == [],
              f"(code={code_fc})")

    # 校验器负向测试：人为构造"改动跑到目标区间外"必须被拦下
    good = '{\n  "command": {},\n  "mcp": {}\n}\n'
    bad = '{\n  "command": {},\n  "mcq": {}\n}\n'   # 偷偷改了 mcp 的键名
    try:
        R.verify_minimal_change(good, bad, ignore_top=set(), ignore_in_command={"usage", "usage-open"})
        check("校验器拦下越界改动", False, "(未抛错)")
    except R.JsoncStructureError:
        check("校验器拦下越界改动", True)
    try:
        R.verify_minimal_change(good, good.replace('"mcp"', '"mcp2"'),
                                ignore_top=set(), ignore_in_command=set())
        check("校验器拦下 command 内非目标成员改动", False, "(未抛错)")
    except R.JsoncStructureError:
        check("校验器拦下 command 内非目标成员改动", True)

    # preflight 报告（T5：manifest 缺失时的安全行为）
    print("\n--- preflight 报告 ---")

    def preflight(cfg_path, install_dir, extra=()):
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = R.main(["--preflight", "--config", cfg_path,
                           "--install-dir", install_dir] + list(extra))
        finally:
            sys.stdout, sys.stderr = old
        return code, json.loads(out.getvalue())

    pf_root = tempfile.mkdtemp(prefix="preflight-", dir=reg_root)
    pf_cfg = os.path.join(pf_root, "mimocode.jsonc")
    pf_inst = os.path.join(pf_root, "installed")
    os.makedirs(pf_inst)
    with open(pf_cfg, "w", encoding="utf-8") as fh:
        fh.write('{"command": {}}\n')

    code_pf, rep_pf = preflight(pf_cfg, pf_inst)
    check("T5: 全新环境 preflight 通过（码 0）", code_pf == 0, f"(code={code_pf})")
    check("T5: mode=fresh", rep_pf["mode"] == "fresh")
    check("T5: 管理文件全部 absent",
          rep_pf["managedFiles"] == {"usage_cli.py": "absent", "register_command.py": "absent"})
    check("T5: 全新环境不需要 -Force", rep_pf["requiresForce"] is False)

    # 无 manifest 但含本项目标记 → 可平滑升级
    with open(os.path.join(pf_inst, "usage_cli.py"), "w", encoding="utf-8") as fh:
        fh.write("#!/usr/bin/env python3\n# " + R.OWNERSHIP_MARKER + "\n")
    code_pf, rep_pf = preflight(pf_cfg, pf_inst)
    check("T5: 含标记 → marker-only（认作本项目旧版）",
          rep_pf["managedFiles"]["usage_cli.py"] == "marker-only")
    check("T5: marker-only 不需 -Force", rep_pf["requiresForce"] is False, f"({rep_pf['forceReasons']})")
    check("T5: mode=adopt", rep_pf["mode"] == "adopt")

    # T5-b：无 manifest 且无标记 → 归属不明 → 必须要求显式确认
    with open(os.path.join(pf_inst, "register_command.py"), "w", encoding="utf-8") as fh:
        fh.write("# 用户自己的同名文件\n")
    code_pf, rep_pf = preflight(pf_cfg, pf_inst)
    check("T5: 无标记文件 → unknown", rep_pf["managedFiles"]["register_command.py"] == "unknown")
    check("T5: unknown → preflight 退出码 7", code_pf == 7, f"(code={code_pf})")
    check("T5: unknown → requiresForce", rep_pf["requiresForce"] is True)
    check("T5: 说明归属不明的原因", any("归属不明" in r for r in rep_pf["forceReasons"]),
          f"({rep_pf['forceReasons']})")
    code_pf_force, rep_pf_force = preflight(pf_cfg, pf_inst, extra=("--force",))
    check("T5: --force 后 preflight 通过", code_pf_force == 0, f"(code={code_pf_force})")

    # manifest 记录哈希 → owned / owned-modified
    import hashlib as _hl
    _dg = _hl.sha256(open(os.path.join(pf_inst, "usage_cli.py"), "rb").read()).hexdigest()
    with open(os.path.join(pf_inst, "install-manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"product": R.PRODUCT, "version": "0.2.0",
                   "files": {"usage_cli.py": {"sha256": _dg}}}, fh)
    code_pf, rep_pf = preflight(pf_cfg, pf_inst)
    check("T5: manifest 匹配 → owned", rep_pf["managedFiles"]["usage_cli.py"] == "owned")
    check("T5: manifestPresent=True", rep_pf["manifestPresent"] is True)
    with open(os.path.join(pf_inst, "usage_cli.py"), "a", encoding="utf-8") as fh:
        fh.write("# 用户改过这一行\n")
    code_pf, rep_pf = preflight(pf_cfg, pf_inst)
    check("T5: manifest 不匹配 → owned-modified", rep_pf["managedFiles"]["usage_cli.py"] == "owned-modified")
    check("T5: owned-modified → 需要 -Force", rep_pf["requiresForce"] is True)
    check("T5: 说明已被修改", any("已被修改" in r for r in rep_pf["forceReasons"]))

    # 配置形态异常时 preflight 必须 fail closed
    for name, text, want in (("重复 command", '{"command": {}, "command": {}}', 6),
                             ("command 非对象", '{"command": 1}', 6),
                             ("无法解析", '{ oops', 3)):
        d_pf, p_pf = new_cfg("preflight-shape", text)
        code_pf, rep_pf = preflight(p_pf, pf_inst)
        check(f"preflight: {name} → 退出码 {want}", code_pf == want, f"(code={code_pf})")

    # 存在指向别处的同名命令 → 认作他人注册，需要 -Force
    d_pf2, p_pf2 = new_cfg("preflight-foreign",
                           '{"command": {"usage": {"template": "echo hi"}}}\n')
    code_pf, rep_pf = preflight(p_pf2, pf_inst)
    check("preflight: 同名但非本项目命令 → 需要 -Force",
          rep_pf["requiresForce"] is True and any("不指向本项目" in r for r in rep_pf["forceReasons"]),
          f"({rep_pf['forceReasons']})")

    # ---- 本地实时刷新服务（loopback） ----
    print("\n=== 本地只读刷新服务 ===")
    import threading as _th
    import urllib.error as _uerr
    import urllib.request as _ureq

    data_dir = os.path.join(tmpdir, "usage-data")
    os.makedirs(data_dir, exist_ok=True)
    old_data_env = os.environ.get("MIMO_USAGE_DATA_DIR")
    os.environ["MIMO_USAGE_DATA_DIR"] = data_dir
    try:
        # default_out_path 改为用户可写目录
        check("default_out_path 落在数据目录而非 CWD",
              os.path.abspath(U.default_out_path("all")).startswith(os.path.abspath(data_dir)),
              f"({U.default_out_path('all')})")

        # UTF-8 stdio：reconfigure 不可用时不抛异常
        U.force_utf8_stdio()
        check("force_utf8_stdio 对普通流安全", True)

        # 管道/替换流
        class _NoReconf:
            def write(self, *_a, **_k):
                return 0
        import io as _io
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = _io.StringIO()
        sys.stderr = _io.StringIO()
        try:
            U.force_utf8_stdio()
            check("StringIO 无 reconfigure 时静默成功", True)
        finally:
            sys.stdout, sys.stderr = old_out, old_err

        payload0 = U.card_data(db, kind="all")
        check("payload 含 dbPath（内部）", "dbPath" in payload0)
        pub = U._public_payload(payload0)
        check("公开 payload 不含 dbPath", "dbPath" not in pub)
        check("公开 payload 有 dbLabel", bool(pub.get("dbLabel")))

        httpd = U.create_usage_server(db, idle_minutes=10)
        urls = U.live_urls(httpd)
        check("服务仅绑定 127.0.0.1", httpd.server_address[0] in ("127.0.0.1", "::1"),
              f"({httpd.server_address})")
        t = _th.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        t.start()
        try:
            with _ureq.urlopen(urls["reportUrl"], timeout=3) as resp:
                got = json.loads(resp.read().decode("utf-8"))
            check("API 返回统计字段", all(k in got for k in ("kind", "token", "session", "context")))
            check("API 不泄露 dbPath", "dbPath" not in got)

            # Issue #4: concurrent dashboard URLs must not inherit one global session.
            from urllib.parse import urlencode as _qencode
            def _report_for(**query):
                with _ureq.urlopen(urls["reportUrl"] + "?" + _qencode(query), timeout=3) as rsp:
                    return json.loads(rsp.read().decode("utf-8"))
            a = _report_for(session="s1", days=7)
            b = _report_for(session="s2", days=30)
            a_again = _report_for(session="s1", days=7)
            check("Issue #4：两个页面固定不同会话",
                  a["context"]["sessionId"] == "s1" and b["context"]["sessionId"] == "s2"
                  and a_again["context"]["sessionId"] == "s1")
            check("Issue #4：各请求保留 days", a["token"]["windowKeys"] and "7" in a["token"]["windowKeys"])
            check("Issue #4：固定查询带 selectionMode",
                  a["context"]["selectionMode"] == b["context"]["selectionMode"] == "fixed")
            invalid = _report_for(session="unknown-session")
            check("Issue #4：API 缺失会话不降级",
                  invalid["context"].get("ok") is False)
            with _ureq.urlopen(urls["pageUrl"] + "?session=s1&days=7", timeout=3) as resp:
                fixed_html = resp.read().decode("utf-8")
            check("Issue #4：页面刷新 URL 保留会话选择",
                  "session=s1&amp;days=7" in fixed_html or "session=s1&days=7" in fixed_html)

            with _ureq.urlopen(urls["pageUrl"], timeout=3) as resp:
                page = resp.read().decode("utf-8")
            check("页面嵌入 LIVE.reportUrl", urls["reportUrl"] in page)
            check("页面按钮为「刷新数据」", "刷新数据" in page)

            # ---- Issue #16：唯一 URL + 首次打开定格 ----
            # 用独立 fixture（5 个会话），模拟「每个会话各触发一次 /usage-open」：
            # 每次触发前先把该会话置为最新活跃，再带一个全新的 tab 令牌请求。
            from urllib.parse import urlencode as _qencode16
            freeze_db = os.path.join(tmpdir, "freeze.db")
            fcon = sqlite3.connect(freeze_db)
            fcon.executescript(SCHEMA_SQL)
            fnow = int(time_module.time() * 1000)
            for _i in range(5):
                _ts = fnow - (5 - _i) * 60_000
                fcon.execute(
                    "INSERT INTO session (id,title,directory,time_created,time_updated,"
                    "summary_additions,summary_deletions,summary_files) VALUES (?,?,?,?,?,0,0,0)",
                    (f"t{_i}", f"会话{_i}", f"D:/p/{_i}", _ts - 60_000, _ts))
            fcon.commit()
            fcon.close()

            fhttpd = U.create_usage_server(freeze_db, idle_minutes=10)
            furls = U.live_urls(fhttpd)
            _th.Thread(target=fhttpd.serve_forever, kwargs={"poll_interval": 0.05},
                       daemon=True).start()
            try:
                def _report16(tab, **q):
                    q.setdefault("session", "latest")
                    q["tab"] = tab
                    try:
                        with _ureq.urlopen(furls["reportUrl"] + "?" + _qencode16(q), timeout=3) as rsp:
                            return json.loads(rsp.read().decode("utf-8"))["context"]["sessionId"]
                    except _uerr.HTTPError as exc:
                        # 把状态码带回来，让断言失败可诊断（而不是抛栈中断整套测试）
                        return f"HTTP {exc.code}"

                def _touch16(sid, ms):
                    c16 = sqlite3.connect(freeze_db)
                    c16.execute("UPDATE session SET time_updated=? WHERE id=?", (ms, sid))
                    c16.commit()
                    c16.close()

                seen = []
                for _i in range(5):
                    # 会话 t<i> 刚活动过 → 它就是「此刻最近活跃」→ 它触发 /usage-open
                    _touch16(f"t{_i}", int(time_module.time() * 1000) + _i + 1)
                    seen.append(_report16(f"tab{_i}"))
                check("Issue #16：5 个会话各触发一次 → 定格到 5 个不同会话（不全是同一个）",
                      len(set(seen)) == 5 and seen == [f"t{_i}" for _i in range(5)], f"({seen})")

                # 定格之后让别的会话变成最新：同一 tab 刷新必须**不漂移**
                _touch16("t4", int(time_module.time() * 1000) + 9_999_999)
                again = _report16("tab0")
                check("Issue #16：定格后刷新不漂移（同一 tab 仍是原会话）",
                      again == seen[0] and not again.startswith("HTTP"),
                      f"(首次={seen[0]} 现在={again})")
                fresh = _report16("tabFresh")
                check("Issue #16：新 tab 取当前最近活跃（与旧 tab 不同）",
                      fresh == "t4" and fresh != seen[0], f"(fresh={fresh} 旧={seen[0]})")

                with _ureq.urlopen(
                        furls["pageUrl"] + "?session=latest&days=30&kind=all&tab=pageTab16",
                        timeout=3) as resp:
                    fpage = resp.read().decode("utf-8")
                check("Issue #16：页面把 tab 令牌带进 LIVE（刷新不会丢掉定格）",
                      "tab=pageTab16" in fpage)
                check("Issue #16：页面带地址栏定格脚本（把 session 写实，便于复制复用）",
                      "replaceState" in fpage)

                try:
                    _ureq.urlopen(furls["reportUrl"] + "?tab=bad%20tab", timeout=3)
                    check("Issue #16：非法 tab 令牌被拒（400）", False, "(未拒绝)")
                except _uerr.HTTPError as e16:
                    check("Issue #16：非法 tab 令牌被拒（400）", e16.code == 400, f"(code={e16.code})")
            finally:
                try:
                    _ureq.urlopen(_ureq.Request(
                        furls["reportUrl"].rsplit("/api/", 1)[0] + "/api/shutdown",
                        method="POST", data=b""), timeout=3)
                except Exception:
                    pass
                fhttpd.shutdown()
                fhttpd.server_close()

            # 错误 token → 404
            bad = urls["reportUrl"].replace(urls["token"], "AAAAAAAAAAAAAAAAAAAAAA")
            try:
                _ureq.urlopen(bad, timeout=3)
                check("错误 token 被拒绝", False)
            except _uerr.HTTPError as e:
                check("错误 token 被拒绝", e.code == 404, f"(code={e.code})")

            # 不可信 Origin → 403
            req = _ureq.Request(urls["reportUrl"], headers={"Origin": "http://evil.example"})
            try:
                _ureq.urlopen(req, timeout=3)
                check("不可信 Origin 被拒绝", False)
            except _uerr.HTTPError as e:
                check("不可信 Origin 被拒绝", e.code == 403, f"(code={e.code})")

            # 合法 Origin（同 loopback）允许
            req_ok = _ureq.Request(urls["reportUrl"], headers={"Origin": f"http://127.0.0.1:{urls['port']}"})
            with _ureq.urlopen(req_ok, timeout=3) as resp:
                check("同源 loopback Origin 允许", resp.status == 200)

            # ---- Origin / Host 安全边界（直接调用纯函数 + 真实 HTTP）----
            check("Origin 缺失放行（同源导航/CLI）",
                  U._origin_allowed(None, urls["port"]) is True)
            check("Origin:null 被拒绝（沙箱 iframe / file://）",
                  U._origin_allowed("null", urls["port"]) is False)
            check("Origin:file:// 被拒绝",
                  U._origin_allowed("file://", urls["port"]) is False)
            check("Origin 省略端口被拒绝（防默认 80 口 CSRF）",
                  U._origin_allowed("http://127.0.0.1", urls["port"]) is False)
            check("Origin 端口不一致被拒绝",
                  U._origin_allowed(f"http://127.0.0.1:{urls['port'] + 1}", urls["port"]) is False)
            check("Origin:https 被拒绝",
                  U._origin_allowed(f"https://127.0.0.1:{urls['port']}", urls["port"]) is False)
            check("Origin 同端口 loopback 放行",
                  U._origin_allowed(f"http://localhost:{urls['port']}", urls["port"]) is True)

            # 真实 HTTP：Origin:null → 403
            req_null = _ureq.Request(urls["reportUrl"], headers={"Origin": "null"})
            try:
                _ureq.urlopen(req_null, timeout=3)
                check("HTTP Origin:null 被拒绝", False)
            except _uerr.HTTPError as e:
                check("HTTP Origin:null 被拒绝", e.code == 403, f"(code={e.code})")

            # 真实 HTTP：非 loopback Host → 403（raw socket，绕过 urllib 重写 Host）
            import socket as _sock
            raw = _sock.create_connection(("127.0.0.1", urls["port"]), 2)
            try:
                path = f"/{urls['token']}/api/report"
                raw.sendall(
                    f"GET {path} HTTP/1.1\r\nHost: evil.example:{urls['port']}\r\n"
                    f"Connection: close\r\n\r\n".encode("ascii")
                )
                resp_raw = b""
                while True:
                    chunk = raw.recv(4096)
                    if not chunk:
                        break
                    resp_raw += chunk
                check("非 loopback Host 被拒绝", b" 403 " in resp_raw.split(b"\r\n", 1)[0],
                      f"({resp_raw.split(b'\\r\\n', 1)[0]!r})")
            finally:
                raw.close()

            # 缺失 Host → 403
            raw2 = _sock.create_connection(("127.0.0.1", urls["port"]), 2)
            try:
                path = f"/{urls['token']}/api/report"
                raw2.sendall(f"GET {path} HTTP/1.1\r\nConnection: close\r\n\r\n".encode("ascii"))
                resp_raw = b""
                while True:
                    chunk = raw2.recv(4096)
                    if not chunk:
                        break
                    resp_raw += chunk
                check("缺失 Host 被拒绝", b" 403 " in resp_raw.split(b"\r\n", 1)[0])
            finally:
                raw2.close()

            # Issue #18：原始 query 不得未转义回显进页内 <script>。
            # 首页会把 LIVE 用 json.dumps 嵌进 <script>，而 json.dumps 不转义 < > /，
            # 所以回显里一旦出现原始 "</script>"，浏览器就会提前闭合脚本元素。
            # 这里必须用裸 socket 发送原始字符 —— urllib 会先做百分号编码，构造不出该请求。
            raw3 = _sock.create_connection(("127.0.0.1", urls["port"]), 2)
            try:
                path = f"/{urls['token']}/?session=</script><b>x</b>"
                raw3.sendall(
                    f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{urls['port']}\r\n"
                    f"Connection: close\r\n\r\n".encode("ascii")
                )
                resp_raw = b""
                while True:
                    chunk = raw3.recv(4096)
                    if not chunk:
                        break
                    resp_raw += chunk
                page_raw = resp_raw.decode("utf-8", "replace")
                live_seg = (page_raw.split("var LIVE =", 1)[1].split("};", 1)[0]
                            if "var LIVE =" in page_raw else "")
                check("Issue #18：回显的 LIVE 片段不含未转义的 '<'",
                      bool(live_seg) and "<" not in live_seg, f"(segment={live_seg[:110]!r})")
                check("Issue #18：回显的 query 被重新编码（出现 %3C）",
                      "%3C" in live_seg or "%3c" in live_seg, f"(segment={live_seg[:110]!r})")
            finally:
                raw3.close()

            # shutdown 不接受跨源 Origin=null
            req_sd_bad = _ureq.Request(
                urls["reportUrl"].rsplit("/api/", 1)[0] + "/api/shutdown",
                method="POST", data=b"", headers={"Origin": "null"},
            )
            try:
                _ureq.urlopen(req_sd_bad, timeout=3)
                check("Origin:null 的 shutdown 被拒绝", False)
            except _uerr.HTTPError as e:
                check("Origin:null 的 shutdown 被拒绝", e.code == 403, f"(code={e.code})")

            # 数据变化后的 API 更新（真正动态刷新，不是旧 HTML 重载）
            c = sqlite3.connect(db)
            c.execute(
                "INSERT INTO session (id,title,directory,time_created,time_updated,"
                "summary_additions,summary_deletions,summary_files) VALUES (?,?,?,?,?,?,?,?)",
                ("s-refresh", "刷新新增会话", "D:/proj", now_ms, now_ms + 1, 1, 1, 1),
            )
            c.commit()
            c.close()
            with _ureq.urlopen(urls["reportUrl"], timeout=3) as resp:
                got2 = json.loads(resp.read().decode("utf-8"))
            check("刷新后会话总数增加",
                  got2["session"]["total"] >= got["session"]["total"] + 1,
                  f"({got['session']['total']} → {got2['session']['total']})")

            # 并发刷新：多个同时 GET 不应 500
            codes = []
            def _hit():
                try:
                    with _ureq.urlopen(urls["reportUrl"], timeout=5) as resp:
                        codes.append(resp.status)
                except Exception as ex:
                    codes.append(str(ex))
            threads = [_th.Thread(target=_hit) for _ in range(8)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=5)
            check("并发刷新全部成功", len(codes) == 8 and all(c == 200 for c in codes), f"({codes})")

            # shutdown
            req_sd = _ureq.Request(urls["reportUrl"].rsplit("/api/", 1)[0] + "/api/shutdown", method="POST", data=b"")
            with _ureq.urlopen(req_sd, timeout=3) as resp:
                check("shutdown 接受请求", resp.status == 200)
        finally:
            httpd.shutdown()
            httpd.server_close()
            t.join(timeout=2)

        # ---- db_missing：503 且不泄露绝对路径 ----
        missing_db = os.path.join(tmpdir, "definitely-missing.db")
        httpd_m = U.create_usage_server(missing_db, idle_minutes=10)
        urls_m = U.live_urls(httpd_m)
        tm = _th.Thread(target=httpd_m.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        tm.start()
        try:
            try:
                _ureq.urlopen(urls_m["reportUrl"], timeout=3)
                check("库缺失返回 503", False)
            except _uerr.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                check("库缺失返回 503", e.code == 503, f"(code={e.code})")
                check("503 响应不泄露数据库绝对路径",
                      missing_db not in body and "definitely-missing" not in body, f"({body})")
        finally:
            httpd_m.shutdown()
            httpd_m.server_close()
            tm.join(timeout=2)

        # ---- 启动锁：陈旧锁可被回收，当前锁可被释放 ----
        lock = U._lock_path()
        check("无锁时 acquire 成功", U._acquire_start_lock(timeout=1) is True)
        check("已有锁时 acquire 超时失败", U._acquire_start_lock(timeout=0.2) is False)
        U._release_start_lock()
        check("release 后锁文件消失", not os.path.exists(lock))
        # 陈旧锁（mtime 很旧）应被回收
        os.makedirs(os.path.dirname(lock) or ".", exist_ok=True)
        with open(lock, "w", encoding="utf-8") as fh:
            fh.write("999999\n")
        os.utime(lock, (0, 0))
        check("陈旧锁可被回收并重新 acquire", U._acquire_start_lock(timeout=1) is True)
        U._release_start_lock()

        # 持有者已死的近期锁：不应等满 30s，应按 PID 存活回收
        import subprocess as _sp
        dead = _sp.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=10)
        dead_pid = dead.pid
        with open(lock, "w", encoding="utf-8") as fh:
            fh.write(f"{dead_pid}\n")
        # mtime 保持“刚刚写入”——只靠年龄不够，必须靠 PID 判死
        got_dead = U._acquire_start_lock(timeout=2)
        check("持有者已死的近期锁可被快速回收", got_dead is True)
        if got_dead:
            U._release_start_lock()

        # ---- 真并发 --open：多进程同时 ensure_live_server，只允许一个健康实例 ----
        print("\n=== 并发启动锁（多进程 ensure_live_server）===")
        conc_data = os.path.join(tmpdir, "usage-data-conc")
        os.makedirs(conc_data, exist_ok=True)
        conc_db = os.path.join(tmpdir, "conc.db")
        make_db(conc_db, int(time_module.time() * 1000))
        worker = f"""
import json, os, sys
sys.path.insert(0, {json.dumps(ROOT)})
import usage_cli as U
live = U.ensure_live_server(sys.argv[1], idle_minutes=5)
print(json.dumps(live, ensure_ascii=False))
"""
        conc_env = os.environ.copy()
        conc_env["MIMO_USAGE_DATA_DIR"] = conc_data
        # 父测试进程也切到同一数据目录，便于读 live-server.json / 清理
        old_data_env_conc = os.environ.get("MIMO_USAGE_DATA_DIR")
        os.environ["MIMO_USAGE_DATA_DIR"] = conc_data
        try:
            procs = [
                _sp.Popen(
                    [sys.executable, "-c", worker, conc_db],
                    stdout=_sp.PIPE, stderr=_sp.PIPE,
                    env=conc_env, text=True, encoding="utf-8", errors="replace",
                )
                for _ in range(4)
            ]
            outs = []
            codes = []
            for p in procs:
                try:
                    o, e = p.communicate(timeout=25)
                except _sp.TimeoutExpired:
                    p.kill()
                    o, e = p.communicate(timeout=5)
                    o = (o or "") + "\nTIMEOUT"
                codes.append(p.returncode)
                outs.append((o or "").strip())
            check("4 个并发 ensure_live_server 均退出 0", all(c == 0 for c in codes), f"({codes})")
            lives = []
            for raw in outs:
                line = next((ln for ln in raw.splitlines() if ln.startswith("{")), "")
                try:
                    lives.append(json.loads(line) if line else None)
                except json.JSONDecodeError:
                    lives.append(None)
            check("4 个并发调用均拿到 live 对象", all(isinstance(x, dict) and x.get("token") for x in lives),
                  f"({lives})")
            tokens = {(x or {}).get("token") for x in lives}
            ports = {(x or {}).get("port") for x in lives}
            check("并发结果收敛到同一 token（无错配）", len(tokens) == 1 and None not in tokens, f"({tokens})")
            check("并发结果收敛到同一 port", len(ports) == 1 and None not in ports, f"({ports})")
            st_conc = U.read_server_state()
            check("状态文件 token 与并发结果一致",
                  bool(st_conc) and st_conc.get("token") in tokens
                  and int(st_conc.get("port") or 0) in ports,
                  f"(state={st_conc})")
            live0 = next((x for x in lives if isinstance(x, dict)), None)
            if live0:
                try:
                    with _ureq.urlopen(live0["reportUrl"], timeout=3) as resp:
                        check("收敛后的唯一实例可访问 /api/report", resp.status == 200)
                except Exception as ex:
                    check("收敛后的唯一实例可访问 /api/report", False, f"({ex})")
                # 只应有一个本库健康实例：再次并发 ensure 应全部复用同一 token
                procs2 = [
                    _sp.Popen(
                        [sys.executable, "-c", worker, conc_db],
                        stdout=_sp.PIPE, stderr=_sp.PIPE,
                        env=conc_env, text=True, encoding="utf-8", errors="replace",
                    )
                    for _ in range(3)
                ]
                tokens2 = set()
                reuse_ok = True
                for p in procs2:
                    try:
                        o, _e = p.communicate(timeout=20)
                    except _sp.TimeoutExpired:
                        p.kill()
                        o, _e = p.communicate(timeout=5)
                        reuse_ok = False
                    line = next((ln for ln in (o or "").splitlines() if ln.startswith("{")), "")
                    try:
                        obj = json.loads(line) if line else {}
                    except json.JSONDecodeError:
                        obj = {}
                    tokens2.add(obj.get("token"))
                    if p.returncode != 0 or not obj.get("reused"):
                        reuse_ok = False
                check("第二波并发全部复用同一实例（reused=true）",
                      reuse_ok and tokens2 == tokens, f"(tokens={tokens2}, reused_ok={reuse_ok})")
            # 清理：用状态文件里的 token 请求 shutdown，并验证身份后再清 state
            try:
                if st_conc and st_conc.get("token"):
                    req_sd = _ureq.Request(
                        f"http://{U.LOOPBACK_HOST}:{int(st_conc['port'])}/{st_conc['token']}/api/shutdown",
                        method="POST", data=b"",
                    )
                    with _ureq.urlopen(req_sd, timeout=3) as resp:
                        check("并发实例 shutdown 接受请求", resp.status == 200)
                    time_module.sleep(0.3)
                    st_after = U.read_server_state()
                    check("shutdown 后状态文件已清理或 token 不再匹配",
                          (st_after is None) or (st_after.get("token") != st_conc.get("token")),
                          f"(after={st_after})")
            except Exception as ex:
                check("并发实例 shutdown 接受请求", False, f"({ex})")
            finally:
                U.clear_server_state()
        finally:
            if old_data_env_conc is None:
                os.environ.pop("MIMO_USAGE_DATA_DIR", None)
            else:
                os.environ["MIMO_USAGE_DATA_DIR"] = old_data_env_conc
    finally:
        if old_data_env is None:
            os.environ.pop("MIMO_USAGE_DATA_DIR", None)
        else:
            os.environ["MIMO_USAGE_DATA_DIR"] = old_data_env

    # ---- P0 回归：大库下的实时服务启动 ----
    #  曾经：ensure_live_server / _service_probe 用 `/api/report` 判身份，而该端点
    #  每次请求都做全量聚合 —— 20MB 左右的库聚合就要约 2s，远超探测的 1.2s 超时，
    #  于是健康实例被判成"不可达/陈旧状态"：刚 spawn 的子进程被 kill、返回 None，
    #  并留下指向死进程的 live-server.json。现在身份探测走 `/{token}/api/ping`
    #  （常量时间、不读库），所以下面的断言与库规模无关。
    print("\n=== 大库下的实时服务启动（回归） ===")
    import http.server as _hs
    import socket as _bsock

    big_dir = os.path.join(tmpdir, "big-live")
    os.makedirs(big_dir, exist_ok=True)
    big_db = os.path.join(big_dir, "bigcode.db")
    make_big_db(big_db)
    big_mb = os.path.getsize(big_db) / (1 << 20)
    check("大库 fixture 超过 20MB（覆盖原故障规模）", big_mb > 20, f"({big_mb:.1f}MB)")
    check("身份标识与安装器一致（协议常量不漂移）",
          U.LIVE_APP_ID == I.LIVE_APP_ID,
          f"(usage_cli={U.LIVE_APP_ID}, installer={I.LIVE_APP_ID})")

    # 替身：数 card_data 被调用几次。用"探测有没有做聚合"来证明它是常量时间，
    # 而不是断言毫秒级耗时（耗时随机器漂移，也会掩盖真正的机制）。
    _real_card_data = U.card_data
    _agg = {"n": 0}

    def _counting_card_data(*args, **kwargs):
        _agg["n"] += 1
        return _real_card_data(*args, **kwargs)

    U.card_data = _counting_card_data

    old_data_big = os.environ.get("MIMO_USAGE_DATA_DIR")
    old_db_big = os.environ.get("MIMO_USAGE_DB")
    big_data = os.path.join(tmpdir, "big-live-data")
    os.makedirs(big_data, exist_ok=True)
    big_spawn_data = os.path.join(tmpdir, "big-live-spawn")
    os.makedirs(big_spawn_data, exist_ok=True)
    os.environ["MIMO_USAGE_DATA_DIR"] = big_data
    # 本段所有调用都显式传库路径，但仍把默认库环境变量钉在合成库上：将来若有人
    # 在这里加了不带路径的调用（例如 U.main([])），它才不会落到真实用户库。
    os.environ["MIMO_USAGE_DB"] = big_db
    bhttpd = None
    bt = None
    e2e_live = None
    try:
        # ---- A. 端点契约：ping 给出固定身份，且不回显 token / 不含库路径 ----
        bhttpd = U.create_usage_server(big_db, idle_minutes=10)
        burls = U.live_urls(bhttpd)
        bping_url = burls["pageUrl"] + "api/ping"
        bt = _th.Thread(target=bhttpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        bt.start()

        with _ureq.urlopen(bping_url, timeout=5) as resp:
            ping_text = resp.read().decode("utf-8")
        ping = json.loads(ping_text)
        check("ping 返回 200 且带固定 app 标识", ping.get("app") == U.LIVE_APP_ID, f"({ping})")
        check("ping 报告已知 kind", ping.get("kind") in U.KINDS, f"({ping.get('kind')})")
        check("ping 只返回身份与能力字段（没有敏感信息）",
              set(ping) == {"app", "kind", "requestSelection"}
              and type(ping.get("requestSelection")) is int
              and ping["requestSelection"] == 1, f"({ping})")
        check("ping 不回显 token", burls["token"] not in ping_text, f"({ping_text})")
        check("ping 不含数据库路径",
              "dbPath" not in ping_text and big_db not in ping_text and "bigcode" not in ping_text,
              f"({ping_text})")

        # ---- B. 关键证明：ping 不做全量聚合；/api/report 才是聚合路径 ----
        _agg["n"] = 0
        with _ureq.urlopen(bping_url, timeout=5) as resp:
            resp.read()
        check("ping 不触发全量聚合（card_data 调用 0 次）", _agg["n"] == 0, f"(calls={_agg['n']})")

        _agg["n"] = 0
        with _ureq.urlopen(burls["reportUrl"], timeout=120) as resp:
            resp.read()
        check("（对照）/api/report 确实触发聚合（替身可观测）", _agg["n"] >= 1, f"(calls={_agg['n']})")

        # ---- C. 身份探测在 20MB+ 库上仍能确认实例，且不聚合 ----
        U.write_server_state({
            "pid": os.getpid(), "port": burls["port"], "token": burls["token"],
            "pageUrl": burls["pageUrl"], "reportUrl": burls["reportUrl"],
            "db": big_db, "kind": "all", "days": 30,
            "startedAt": int(time_module.time() * 1000), "idleMinutes": 10,
        })
        _agg["n"] = 0
        probe = U._service_probe()
        check("大库实例：身份三态全过（stateValid/reachable/owned）",
              probe["stateValid"] and probe["reachable"] and probe["owned"], f"({probe})")
        check("身份探测本身不触发全量聚合", _agg["n"] == 0, f"(calls={_agg['n']})")

        _agg["n"] = 0
        wlive = U.wait_for_server(burls["token"])
        check("大库实例：wait_for_server 能确认身份",
              isinstance(wlive, dict) and wlive.get("port") == burls["port"], f"({wlive})")
        check("wait_for_server 不触发全量聚合", _agg["n"] == 0, f"(calls={_agg['n']})")

        # 安装器自带一份探针，同样不能聚合
        _agg["n"] = 0
        ist = I.service_status()
        check("安装器探测：大库实例判为 owned",
              ist["stateValid"] and ist["reachable"] and ist["owned"], f"({ist})")
        check("安装器探测不触发全量聚合", _agg["n"] == 0, f"(calls={_agg['n']})")

        # ---- D. ping 的安全边界与数据面一致（不是绕过鉴权的公开端点）----
        bad_tok = bping_url.replace(burls["token"], "AAAAAAAAAAAAAAAAAAAAAA")
        for label, url in (("ping：错误 token 被拒绝", bad_tok),
                           ("ping：缺失 token 被拒绝",
                            f"http://{U.LOOPBACK_HOST}:{burls['port']}/api/ping")):
            try:
                _ureq.urlopen(url, timeout=5)
                check(label, False)
            except _uerr.HTTPError as e:
                check(label, e.code == 404, f"(code={e.code})")

        for origin in ("null", "http://evil.example",
                       f"http://127.0.0.1:{burls['port'] + 1}"):
            req_bad_origin = _ureq.Request(bping_url, headers={"Origin": origin})
            try:
                _ureq.urlopen(req_bad_origin, timeout=5)
                check(f"ping：Origin {origin} 被拒绝", False)
            except _uerr.HTTPError as e:
                check(f"ping：Origin {origin} 被拒绝", e.code == 403, f"(code={e.code})")

        req_ok_origin = _ureq.Request(
            bping_url, headers={"Origin": f"http://127.0.0.1:{burls['port']}"})
        with _ureq.urlopen(req_ok_origin, timeout=5) as resp:
            check("ping：同源 loopback Origin 放行", resp.status == 200)

        # 非 loopback Host → 403（raw socket，绕过 urllib 对 Host 的重写）
        raw_b = _bsock.create_connection((U.LOOPBACK_HOST, burls["port"]), 2)
        try:
            raw_b.sendall(
                (f"GET /{burls['token']}/api/ping HTTP/1.1\r\n"
                 f"Host: evil.example:{burls['port']}\r\nConnection: close\r\n\r\n").encode("ascii")
            )
            resp_raw = b""
            while True:
                chunk = raw_b.recv(4096)
                if not chunk:
                    break
                resp_raw += chunk
            check("ping：非 loopback Host 被拒绝",
                  b" 403 " in resp_raw.split(b"\r\n", 1)[0],
                  f"({resp_raw.split(b'\r\n', 1)[0]!r})")
        finally:
            raw_b.close()

        # POST ping → 404：数据面仍然只认 GET
        req_post_ping = _ureq.Request(bping_url, method="POST", data=b"")
        try:
            _ureq.urlopen(req_post_ping, timeout=5)
            check("ping：POST 被拒绝（数据面仍为 GET-only）", False)
        except _uerr.HTTPError as e:
            check("ping：POST 被拒绝（数据面仍为 GET-only）", e.code == 404, f"(code={e.code})")

        # ---- E. 陌生监听者：可访问但不是本项目 → 不认领、不关闭、不杀 ----
        foreign_seen: list = []

        class _ForeignHandler(_hs.BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def _reply(self):
                foreign_seen.append(self.path)
                body = json.dumps({"hello": "not-the-dashboard"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = _reply
            do_POST = _reply

        fhttpd = _hs.ThreadingHTTPServer((U.LOOPBACK_HOST, 0), _ForeignHandler)
        fport = fhttpd.server_address[1]
        ft = _th.Thread(target=fhttpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        ft.start()
        try:
            U.write_server_state({
                "pid": os.getpid(), "port": fport, "token": "FOREIGNTOKEN000000",
                "pageUrl": f"http://{U.LOOPBACK_HOST}:{fport}/FOREIGNTOKEN000000/",
                "reportUrl": f"http://{U.LOOPBACK_HOST}:{fport}/FOREIGNTOKEN000000/api/report",
                "db": big_db, "kind": "all", "days": 30,
                "startedAt": int(time_module.time() * 1000), "idleMinutes": 10,
            })
            fprobe = U._service_probe()
            check("陌生监听者：可访问但 owned=False（不凭端口认领）",
                  fprobe["stateValid"] and fprobe["reachable"] and not fprobe["owned"],
                  f"({fprobe})")
            check("陌生监听者：try_reuse_server 拒绝复用", U.try_reuse_server(big_db) is None)
            check("陌生监听者：未被本项目协议关闭（没收到任何 shutdown 请求）",
                  not any("shutdown" in p for p in foreign_seen), f"({foreign_seen})")
        finally:
            fhttpd.shutdown()
            fhttpd.server_close()
            ft.join(timeout=2)

        # ---- F. 端到端：大库上真实 spawn 子进程并复用（独立数据目录）----
        bhttpd.shutdown()
        bhttpd.server_close()
        bt.join(timeout=2)
        bhttpd = None
        os.environ["MIMO_USAGE_DATA_DIR"] = big_spawn_data

        e2e_live = U.ensure_live_server(big_db, idle_minutes=5)
        check("大库：ensure_live_server 返回 live（原代码此处为 None）",
              isinstance(e2e_live, dict) and bool(e2e_live.get("token")), f"({e2e_live})")
        if isinstance(e2e_live, dict):
            check("大库：启动的子进程仍存活（没有被自己杀掉）",
                  U._pid_alive(int(e2e_live["pid"] or 0)), f"(pid={e2e_live.get('pid')})")
            with _ureq.urlopen(e2e_live["reportUrl"], timeout=120) as resp:
                check("大库：/api/report 数据面不受影响", resp.status == 200)
            reuse = U.ensure_live_server(big_db, idle_minutes=5)
            check("大库：第二次调用复用同一实例（不重复 spawn）",
                  isinstance(reuse, dict) and reuse.get("reused") is True
                  and reuse.get("token") == e2e_live.get("token")
                  and reuse.get("pid") == e2e_live.get("pid"), f"({reuse})")

        # ---- G. 启动失败路径：锁与状态文件仍被正确清理 ----
        fail_data = os.path.join(tmpdir, "big-live-fail")
        os.makedirs(fail_data, exist_ok=True)
        os.environ["MIMO_USAGE_DATA_DIR"] = fail_data
        orig_spawn = U.spawn_serve_child
        try:
            def _boom_spawn(*_a, **_kw):
                raise OSError("simulated spawn failure")

            U.spawn_serve_child = _boom_spawn
            failed = U.ensure_live_server(big_db, idle_minutes=5)
            check("spawn 失败时返回 None", failed is None, f"({failed})")
            check("spawn 失败后启动锁已释放", not os.path.exists(U._lock_path()),
                  f"({U._lock_path()})")
            check("spawn 失败后没有留下状态文件",
                  not os.path.exists(os.path.join(fail_data, "live-server.json")))
        finally:
            U.spawn_serve_child = orig_spawn
    finally:
        U.card_data = _real_card_data
        # 关掉本段的同进程服务，并**有界**等待其线程收尾：请求线程若还在跑 24MB
        # 库的聚合，DB 句柄不释放，紧接着删目录就会失败。
        if bhttpd is not None:
            try:
                bhttpd.shutdown()
                bhttpd.server_close()
            except Exception:
                pass
        if bt is not None:
            bt.join(timeout=5)
        # 收掉本节点起来的子进程：只认**自己**拿到的 token 与 pid —— 不用状态文件
        # 里的未知 pid 动手，也不会去关闭别人的监听者。
        os.environ["MIMO_USAGE_DATA_DIR"] = big_spawn_data
        own = e2e_live if isinstance(e2e_live, dict) else {}
        own_token, own_pid = own.get("token"), own.get("pid")
        st_big = U.read_server_state()
        if own_token and st_big and st_big.get("token") == own_token:
            try:
                req_sd_big = _ureq.Request(
                    f"http://{U.LOOPBACK_HOST}:{int(st_big['port'])}/"
                    f"{st_big['token']}/api/shutdown", method="POST", data=b"")
                with _ureq.urlopen(req_sd_big, timeout=5) as resp:
                    check("大库：服务接受 shutdown", resp.status == 200)
            except Exception as ex:
                check("大库：服务接受 shutdown", False, f"({ex})")
        if own_pid:
            for _ in range(25):
                if not U._pid_alive(int(own_pid)):
                    break
                time_module.sleep(0.2)
            if U._pid_alive(int(own_pid)):
                try:
                    os.kill(int(own_pid), 9)
                except Exception:
                    pass
        U.clear_server_state()
        if old_data_big is None:
            os.environ.pop("MIMO_USAGE_DATA_DIR", None)
        else:
            os.environ["MIMO_USAGE_DATA_DIR"] = old_data_big
        if old_db_big is None:
            os.environ.pop("MIMO_USAGE_DB", None)
        else:
            os.environ["MIMO_USAGE_DB"] = old_db_big
        # 尽早释放 20MB+ 的 fixture 与数据目录；删不掉就打印残留（不静默吞掉）
        cleanup_temp(big_dir, "大库 fixture 目录")
        cleanup_temp(big_data, "大库数据目录")
        cleanup_temp(big_spawn_data, "大库 spawn 数据目录")

    # ---- P0 回归：selfcheck 必须零副作用（不读库、不写文件、无库也能通过）----
    #  安装器用它验证"这份 usage_cli.py 能被目标解释器启动"，所以它绝不能有任何
    #  副作用：不能建数据目录、不能写报告、没有数据库时也必须成功。
    print("\n--- selfcheck（安装器自检用） ---")
    import contextlib as _cl_sc
    import io as _io_sc
    _sc_dir = os.path.join(tmpdir, "selfcheck-data")
    _old_dd = os.environ.get("MIMO_USAGE_DATA_DIR")
    _old_db_sc = os.environ.get("MIMO_USAGE_DB")
    os.environ["MIMO_USAGE_DATA_DIR"] = _sc_dir
    os.environ["MIMO_USAGE_DB"] = os.path.join(tmpdir, "definitely-missing-selfcheck.db")
    try:
        _out_sc, _err_sc = _io_sc.StringIO(), _io_sc.StringIO()
        with _cl_sc.redirect_stdout(_out_sc), _cl_sc.redirect_stderr(_err_sc):
            _sc_code = U.main(["selfcheck"])
        _sc = json.loads(_out_sc.getvalue())
        check("selfcheck 返回 0（即使数据库不存在）", _sc_code == 0, f"(code={_sc_code})")
        check("selfcheck 报告 supported 与 hasServer",
              _sc.get("supported") is True and _sc.get("hasServer") is True)
        check("selfcheck 如实报告数据库不存在", _sc.get("dbExists") is False)
        check("selfcheck 不创建数据目录、不写任何文件", not os.path.exists(_sc_dir),
              f"(dir_exists={os.path.exists(_sc_dir)})")

        # 无参调用仍应正常生成报告（这是产品正常用法，与上面的"零副作用自检"是两条路）
        _rep_dir = os.path.join(tmpdir, "selfcheck-report")
        os.environ["MIMO_USAGE_DATA_DIR"] = _rep_dir
        os.environ["MIMO_USAGE_DB"] = db
        _out_r, _err_r = _io_sc.StringIO(), _io_sc.StringIO()
        with _cl_sc.redirect_stdout(_out_r), _cl_sc.redirect_stderr(_err_r):
            _rep_code = U.main([])
        check("无参调用（产品正常用法）仍能生成报告", _rep_code == 0 and os.path.isdir(_rep_dir),
              f"(code={_rep_code})")
    finally:
        if _old_dd is None:
            os.environ.pop("MIMO_USAGE_DATA_DIR", None)
        else:
            os.environ["MIMO_USAGE_DATA_DIR"] = _old_dd
        if _old_db_sc is None:
            os.environ.pop("MIMO_USAGE_DB", None)
        else:
            os.environ["MIMO_USAGE_DB"] = _old_db_sc

    # ---- 收尾 ----
    # 大库 fixture 有 20MB+，后台线程还可能持有过 DB 句柄：用 cleanup_temp
    # （installer.force_rmtree：清只读位重试 + 返回真实存在性）替代
    # shutil.rmtree(..., ignore_errors=True) —— 后者删不掉时会把失败咽掉，
    # 留下几十 MB 残留却照样打印成功。清理失败只报告残留，不覆盖已有结论。
    cleanup_temp(tmpdir, "测试临时目录")
    cleanup_temp(reg_root, "注册测试目录")

    print(f"\n{'PYTHON TESTS OK' if FAILS == 0 else 'PYTHON TESTS FAILED: ' + str(FAILS)}")
    return 0 if FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
