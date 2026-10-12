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
    python register_command.py --purge-legacy-plugin

行为约定：
  * 只改本项目拥有的 ``command.usage`` / ``command.usage-open``；其它命令条目、
    其它配置键、以及这两个条目上用户自行添加的字段（如 ``agent``）都原样保留。
  * ``plugin`` 里指向旧版 ``usage-dashboard`` 的条目**默认保留**，只有显式传入
    ``--purge-legacy-plugin`` 才会移除。
  * 写入前先序列化候选内容并回读校验，再经同目录临时文件 ``os.replace`` 原子替换，
    不会中途截断原配置。
  * 备份文件名带时间戳且保证唯一（同秒重复时追加序号），绝不覆盖已有备份。
  * JSONC 注释与原始排版**不会保留**：本脚本以规范化 JSON 重写文件。原文件在改动前
    会逐字节备份，且检测到注释时会在 stderr 明确告警。

退出码：0 成功 / 1 已存在（无变化） / 2 参数错误 / 3 解析失败 / 4 配置或脚本不存在 / 5 写入失败
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
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


def jsonc_comment_count(text: str) -> int:
    """统计字符串字面量之外的 ``//`` 与 ``/* */`` 注释条数。

    仅用于在重写前告诉用户注释不会被保留，不参与解析。
    """
    count = 0
    i, n = 0, len(text)
    in_str = False
    quote = ""
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                in_str = False
            i += 1
            continue
        if ch in ('"', "'"):
            in_str, quote = True, ch
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            count += 1
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            count += 1
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        i += 1
    return count


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
        "（已生成好静态 HTML 页面，脚本零 AI 参与、纯离线）：\n\n"
        f"!`{cmd}`\n\n"
        "请把上面这一行输出**原样**告诉用户，并提醒他打开该 HTML 文件即可看到"
        "完整的用量看板（Token 用量 / Session 用量 / 上下文占用）。"
        "如需筛选某一类数据或指定日期，请直接运行本地 CLI。\n"
        "不要臆造或改写任何数字，不要自己渲染图表，也不要额外调用其它工具。"
    )


def build_open_template(py: str, script: str) -> str:
    """Build and open the overview without interpolating untrusted shell arguments."""
    cmd = f"{q(py)} {q(script)} --open"
    return (
        "用户请求打开 MiMo Desktop 用量看板。下面一行是本地脚本的真实输出"
        "（脚本已生成静态 HTML 并用默认浏览器打开）：\n\n"
        f"!`{cmd}`\n\n"
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


def merge_command(existing, description: str, template: str) -> dict:
    """覆盖本项目的 description/template，保留用户在该条目上添加的其它字段。"""
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged["description"] = description
    merged["template"] = template
    return merged


def unique_backup_path(cfg_path: str) -> str:
    """带时间戳的备份路径；同秒重复时追加序号，绝不覆盖已有备份。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{cfg_path}.bak-{MARK}-{stamp}"
    if not os.path.exists(base):
        return base
    n = 1
    while True:
        cand = f"{base}-{n}"
        if not os.path.exists(cand):
            return cand
        n += 1


def copy_exclusive(src: str, dst: str) -> None:
    """复制到新文件；目标已存在则抛 FileExistsError，不覆盖任何已有备份。"""
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            with open(src, "rb") as inp:
                shutil.copyfileobj(inp, out)
    except BaseException:
        try:
            os.unlink(dst)
        except OSError:
            pass
        raise


def write_atomically(path: str, text: str, encoding: str = "utf-8") -> None:
    """同目录临时文件 + os.replace，避免失败时截断原文件。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".mimocode-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
    parser.add_argument("--purge-legacy-plugin", action="store_true",
                        help="额外移除 config 里指向旧版 usage-dashboard 插件的 plugin 条目（默认保留）")
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

    # 旧插件引用默认保留；只有显式传入 --purge-legacy-plugin 时才摘除。
    if args.purge_legacy_plugin and purge_legacy_plugins(cfg):
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

        new_usage = merge_command(
            commands.get("usage"),
            "MiMo Desktop 本机用量：生成静态 HTML 看板（Token / Session / 上下文）",
            build_usage_template(py, script),
        )
        new_open = merge_command(
            commands.get("usage-open"),
            "MiMo Desktop 用量：生成并直接用浏览器打开静态看板",
            build_open_template(py, script),
        )
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

    # 候选内容先序列化并回读校验，确认可解析且 command 段一致后才落盘。
    candidate = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    try:
        reparsed = json.loads(candidate)
    except ValueError as exc:
        print(f"CANDIDATE_INVALID: {exc}", file=sys.stderr)
        return 5
    if reparsed.get("command") != commands:
        print("CANDIDATE_INVALID: command 段与预期不一致", file=sys.stderr)
        return 5

    raw = open(cfg_path, "rb").read()
    comment_count = jsonc_comment_count(raw.decode("utf-8-sig", errors="replace"))

    backup_path = None
    if not args.no_backup:
        backup_path = unique_backup_path(cfg_path)
        try:
            copy_exclusive(cfg_path, backup_path)
            with open(backup_path, "rb") as fh:
                if fh.read() != raw:
                    print(f"BACKUP_VERIFY_FAILED {backup_path}", file=sys.stderr)
                    return 5
        except OSError as exc:
            print(f"BACKUP_FAILED: {exc}", file=sys.stderr)
            return 5

    if backup_path:
        print(f"backup    -> {backup_path}", file=sys.stderr)

    if comment_count:
        print(
            f"WARNING: 原配置含 {comment_count} 处 JSONC 注释，"
            f"重写后注释与原始排版不会保留（原始字节见上面的 backup 行）。",
            file=sys.stderr,
        )

    # 原文件若带 UTF-8 BOM，则保持 BOM，避免给 PowerShell/编辑器带来意外变化。
    encoding = "utf-8-sig" if raw[:3] == b"\xef\xbb\xbf" else "utf-8"
    try:
        write_atomically(cfg_path, candidate, encoding=encoding)
    except OSError as exc:
        print(f"WRITE_FAILED: {exc}", file=sys.stderr)
        return 5

    print("unregistered" if args.uninstall else "registered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
