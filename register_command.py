#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""register_command.py — 把 /usage 命令写进 MiMo 的 mimocode.jsonc

MiMo 的命令来源之一是配置的 `command` 字段：

    "command": {
      "<name>": { "template": "...", "description": "...", "agent": "..." }
    }

template 里支持两类替换（引擎 src/config/markdown.ts）：
  * ``!`cmd` ``  ← SHELL_REGEX = /!`([^`]+)`/g，**先在本地执行**，stdout 插回文本
  * `$1` `$2` …  ← 位置参数；`$ARGUMENTS` 为全部参数

本脚本负责把 `command.usage` 以及可选的 `command.usage-open` 写进用户配置。
与旧版 register-plugin.mjs 的区别：**JSONC 感知**（先剥离 // 与 /* */ 注释再解析），
避免用户按 README 写了注释后注册静默失败。

用法：
    python register_command.py [--config <path>] [--python <exe>] [--script <path>]
    python register_command.py --uninstall

退出码：0 成功 / 1 已存在（无变化） / 2 参数错误 / 3 解析失败 / 4 配置不存在
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime

MARK = "mimo-usage-dashboard"


# ---------------------------------------------------------------------------
# JSONC 支持
# ---------------------------------------------------------------------------


def strip_jsonc(text: str) -> str:
    """剥离 // 行注释与 /* */ 块注释（跳过字符串字面量内部）。"""
    out = []
    i, n = 0, len(text)
    in_str = False
    quote = ""
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                in_str = False
            i += 1
            continue
        if ch in ('"', "'"):
            in_str, quote = True, ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        # 容忍尾随逗号（JSONC 允许；JSON.parse 不允许）
        out.append(ch)
        i += 1
    cleaned = "".join(out)
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    return cleaned


def load_jsonc(path: str) -> dict:
    raw = open(path, encoding="utf-8-sig").read()
    return json.loads(strip_jsonc(raw))


# ---------------------------------------------------------------------------
# 命令模板
# ---------------------------------------------------------------------------


def q(s: str) -> str:
    """PowerShell / shell 单层引号包裹（路径含空格时必需）。"""
    return f'"{s}"' if (" " in s and not s.startswith('"')) else s


def build_usage_template(py: str, script: str) -> str:
    """Build a fixed command; never interpolate untrusted arguments into shell text."""
    cmd = f"{q(py)} {q(script)}"
    return (
        "用户请求查看 MiMo Desktop 本机用量。下面一行是本地脚本的真实输出"
        "（已生成好静态 HTML 页面，脚本零 AI 参与、纯离线）：\\n\\n"
        f"!`{cmd}`\\n\\n"
        "请把上面这一行输出**原样**告诉用户，并提醒他打开该 HTML 文件即可看到"
        "完整的用量看板（Token 用量 / Session 用量 / 上下文占用）。"
        "如需筛选某一类数据或指定日期，请直接运行本地 CLI。\\n"
        "不要臆造或改写任何数字，不要自己渲染图表，也不要额外调用其它工具。"
    )


def build_open_template(py: str, script: str) -> str:
    """Build and open the overview without interpolating untrusted shell arguments."""
    cmd = f"{q(py)} {q(script)} --open"
    return (
        "用户请求打开 MiMo Desktop 用量看板。下面一行是本地脚本的真实输出"
        "（脚本已生成静态 HTML 并用默认浏览器打开）：\\n\\n"
        f"!`{cmd}`\\n\\n"
        "请把这一行原样告诉用户即可，不要臆造数字，不要调用其它工具。"
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def default_config_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".config", "mimocode", "mimocode.jsonc")


def purge_legacy_plugins(cfg: dict) -> int:
    """清掉 ``plugin`` 数组中指向旧版插件的条目。

    旧设计是一个 ``usage-dashboard.mjs`` 插件（起本地服务 + 收口 HTTP 请求）。
    新设计完全不装插件，但配置文件里的 ``plugin`` 引用若残留，引擎启动时会尝试
    加载一个**已不存在的文件**并报错。这里把它摘掉。

    只动明显属于本项目的条目（路径含 ``usage-dashboard``）；``opencode-go-session.mjs``
    等其它插件原样保留。
    """
    plugins = cfg.get("plugin")
    if not isinstance(plugins, list):
        return 0
    kept = [
        p for p in plugins
        if not (isinstance(p, str) and "usage-dashboard" in p)
    ]
    removed = len(plugins) - len(kept)
    if removed:
        if kept:
            cfg["plugin"] = kept
        else:
            cfg.pop("plugin", None)
    return removed


def main(argv=None) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(prog="register_command.py",
                                     description="把 /usage 命令写进 MiMo 的 mimocode.jsonc")
    parser.add_argument("--config", default=default_config_path(), help="配置文件路径")
    parser.add_argument("--script", default=os.path.join(here, "usage_cli.py"),
                        help="usage_cli.py 的路径")
    parser.add_argument("--python", default=sys.executable, help="python 解释器路径")
    parser.add_argument("--uninstall", action="store_true", help="移除已注册的命令")
    parser.add_argument("--no-backup", action="store_true", help="不生成 .bak 备份")
    args = parser.parse_args(argv)

    cfg_path = os.path.abspath(args.config)
    if not os.path.exists(cfg_path):
        print(f"CONFIG_NOT_FOUND {cfg_path}", file=sys.stderr)
        return 4

    try:
        cfg = load_jsonc(cfg_path)
    except (ValueError, OSError) as exc:
        print(f"CONFIG_PARSE_FAILED: {exc}", file=sys.stderr)
        return 3

    if not isinstance(cfg, dict):
        print("CONFIG_PARSE_FAILED: 顶层不是对象", file=sys.stderr)
        return 3

    commands = cfg.get("command")
    if not isinstance(commands, dict):
        commands = {}

    changed = False

    # 无论安装还是卸载，都先摘掉旧插件引用（新设计不装插件）。
    if purge_legacy_plugins(cfg):
        changed = True

    if args.uninstall:
        for name in ("usage", "usage-open"):
            if name in commands:
                del commands[name]
                changed = True
    else:
        py = os.path.abspath(args.python)
        script = os.path.abspath(args.script)
        if not os.path.exists(script):
            print(f"SCRIPT_NOT_FOUND {script}", file=sys.stderr)
            return 4

        new_usage = {
            "description": "MiMo Desktop 本机用量：生成静态 HTML 看板（Token / Session / 上下文）",
            "template": build_usage_template(py, script),
        }
        new_open = {
            "description": "MiMo Desktop 用量：生成并直接用浏览器打开静态看板",
            "template": build_open_template(py, script),
        }
        if commands.get("usage") != new_usage:
            commands["usage"] = new_usage
            changed = True
        if commands.get("usage-open") != new_open:
            commands["usage-open"] = new_open
            changed = True

    if not changed:
        print("already-registered")
        return 1

    cfg["command"] = commands

    if not args.no_backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copyfile(cfg_path, f"{cfg_path}.bak-{MARK}")

    with open(cfg_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")

    print("unregistered" if args.uninstall else "registered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
