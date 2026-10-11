#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""e2e_live.py — 对**已安装**的配置做真实端到端验证。

流程完全复刻引擎（见 docs/architecture.md）：
  1) 读 ~/.config/mimocode/mimocode.jsonc 里的 command.usage.template
  2) 替换 $ARGUMENTS（引擎先做这步）
  3) 用 SHELL_REGEX = /!`([^`]+)`/g 扫描出 shell 命令
  4) 本地执行（等价 process.text），stdout 插回文本
  5) 断言：stdout 是简短一行、HTML 已生成、注入被拦

用法：python test/e2e_live.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from register_command import load_jsonc  # noqa: E402

CFG = os.path.join(os.path.expanduser("~"), ".config", "mimocode", "mimocode.jsonc")
SHELL_REGEX = re.compile(r"!`([^`]+)`")

failures = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  {extra}" if extra else ""))
    if not ok:
        failures += 1


def render(template: str, arguments: str) -> str:
    """复刻引擎：先替 $1..$n，再替 $ARGUMENTS。"""
    tokens = re.findall(r'"[^"]*"|\'[^\']*\'|\S+', arguments)
    tokens = [t.strip("\"'") for t in tokens]
    t = re.sub(r"\$(\d+)", lambda m: tokens[int(m.group(1)) - 1]
               if int(m.group(1)) - 1 < len(tokens) else "", template)
    return t.replace("$ARGUMENTS", arguments)


def run_shell(command: str) -> str:
    """引擎在 Windows 上走 PowerShell（见 shell.preferred()）。"""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.stdout


print("=== 读取已安装配置 ===")
if not os.path.exists(CFG):
    print(f"  配置不存在：{CFG}")
    raise SystemExit(1)
cfg = load_jsonc(CFG)
tpl = (cfg.get("command") or {}).get("usage", {}).get("template")
check("配置含 command.usage.template", bool(tpl))
if not tpl:
    raise SystemExit(1)
check("配置无残留 plugin 引用", "usage-dashboard" not in str(cfg.get("plugin", [])))

print("\n=== 复刻引擎：$ARGUMENTS → shell 扫描 → 执行 ===")
out_dir = tempfile.mkdtemp(prefix="e2e-live-")
out_file = os.path.join(out_dir, "live.html")
args = f'token --days 7 --out "{out_file}"'
rendered = render(tpl, args)
matches = SHELL_REGEX.findall(rendered)
check("模板扫出且仅扫出 1 条 shell 命令", len(matches) == 1, f"({len(matches)})")

if matches:
    stdout = run_shell(matches[0])
    check("执行有 stdout", bool(stdout.strip()))
    check("stdout 为简短一行", len(stdout.strip().splitlines()) == 1, f"({len(stdout.strip())} 字符)")
    check("HTML 已生成", os.path.exists(out_file), out_file)
    if os.path.exists(out_file):
        html = open(out_file, encoding="utf-8").read()
        check("HTML 内联数据", "var DATA" in html)
        check("kind=token 生效", '"kind": "token"' in html or '"kind":"token"' in html)
        check("days=7 生效", '"days": 7' in html or '"days":7' in html)
        check("HTML 自包含（无 fetch）", not re.search(r"(^|[^a-zA-Z])fetch\(", html))

print("\n=== 注入防护（真实路径）===")
# 恶意输入含 `;` / `&&`。模板用 PowerShell 单引号包裹 $ARGUMENTS，
# 这些字符在 shell 层就是字面文本、不会被解释；脚本侧白名单再兜一层。
canary = os.path.join(out_dir, "PWNED.txt")
evil = f'token ; echo pwned > "{canary}"'
parts = SHELL_REGEX.findall(render(tpl, evil))
stdout = run_shell(parts[0]) if parts else ""
time.sleep(0.2)
check("恶意命令未执行（无金丝雀文件）", not os.path.exists(canary))
check("脚本仍正常运行（白名单剔除危险 token）", "已生成" in stdout or "用量" in stdout)

print(f"\n{'E2E LIVE OK' if failures == 0 else 'E2E LIVE FAILED: ' + str(failures)}")
raise SystemExit(0 if failures == 0 else 1)
