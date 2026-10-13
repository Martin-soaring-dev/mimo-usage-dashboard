#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# mimo-usage-dashboard:managed-file:v1
"""register_command.py — 把 /usage 命令写进 MiMo 的 mimocode.jsonc

MiMo 的命令来源之一是配置的 `command` 字段：

    "command": {
      "<name>": { "template": "...", "description": "...", "agent": "..." }
    }

template 里支持两类替换（引擎 src/config/markdown.ts）：
  * ``!`cmd` ``  ← SHELL_REGEX = /!`([^`]+)`/g，**先在本地执行**，stdout 插回文本
  * `$1` `$2` …  ← 位置参数；`$ARGUMENTS` 为全部参数

本脚本负责把 `command.usage` 以及可选的 `command.usage-open` 写进用户配置。

**就地修改（不改写整个文件）**：解析与写入都不经过 ``json.loads`` / ``json.dumps``
整文件往返。脚本先做一次"记录字节区间"的容错结构扫描，再只对目标成员的文本片段做
拼接，因此下列内容逐字节保留：
  * JSONC 注释（行注释与块注释）
  * 缩进风格（空格 / 制表符）与用户原有排版
  * 换行符（CRLF / LF 原样）
  * UTF-8 BOM
  * 其它命令条目、其它配置键、以及本条目上用户自行添加的字段（如 ``agent``）

因为做不到"无损"而拒绝猜测的形态（一律 fail closed，见退出码 6）：
  * 重复键（``command`` 重复、``usage`` 重复等）
  * 目标位置不是对象（``command`` / ``usage`` 不是对象）
  * ``description`` / ``template`` 是对象或数组
  * 无法定位区域（结构扫描失败、字符串未闭合、注释未闭合等）

用法：
    python register_command.py [--config <path>] [--python <exe>] [--script <path>]
    python register_command.py --uninstall
    python register_command.py --purge-legacy-plugin
    python register_command.py --preflight --config <path> --install-dir <dir>

行为约定：
  * 只改本项目拥有的 ``command.usage`` / ``command.usage-open``；其它命令条目、
    其它配置键、以及这两个条目上用户自行添加的字段都原样保留。
  * ``plugin`` 里指向旧版 ``usage-dashboard`` 的条目**默认保留**，只有显式传入
    ``--purge-legacy-plugin`` 才会移除。
  * 写入前先把候选内容回读校验（语义一致 + "改动只落在目标区间内"），再经同目录
    临时文件 ``os.replace`` 原子替换，不会中途截断原配置。
  * 备份文件名带时间戳且保证唯一（同秒重复时追加序号），绝不覆盖已有备份，
    且在任何写操作之前完成。
  * ``--journal <path>`` 会写一份机器可读的执行记录（备份路径、写前/写后哈希），
    供安装器在失败时精确回滚。

退出码：
    0 成功
    1 已存在（无变化）
    2 参数错误
    3 解析失败（含无法解码的字节）
    4 配置或脚本不存在
    5 写入失败（含备份失败、回读校验失败）
    6 无法安全就地修改（重复键 / 非对象 / 无法定位区域）
    7 --preflight：存在需要人工确认的既有状态（需显式 --force）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime

MARK = "mimo-usage-dashboard"
PRODUCT = "mimo-usage-dashboard"
OWNERSHIP_MARKER = "mimo-usage-dashboard:managed-file:v1"
MANAGED_FILES = ("usage_cli.py", "register_command.py")

EXIT_OK = 0
EXIT_NOCHANGE = 1
EXIT_ARGS = 2
EXIT_PARSE = 3
EXIT_NOTFOUND = 4
EXIT_WRITE = 5
EXIT_UNEDITABLE = 6
EXIT_NEEDS_FORCE = 7


# ---------------------------------------------------------------------------
# JSONC 支持（供测试与兼容保留的"剥离后解析"路径）
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
    """统计字符串字面量之外的 ``//`` 与 ``/* */`` 注释条数。"""
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
# 结构扫描：记录每个成员的原始字节区间
# ---------------------------------------------------------------------------


class JsoncStructureError(Exception):
    """配置结构无法安全就地修改。调用方必须 fail closed，不得猜测。"""


class _Node:
    __slots__ = ("kind", "start", "end", "members", "elements",
                 "value", "open_pos", "close_pos", "dupes", "comma_pos")

    def __init__(self, kind, start, end, **kw):
        self.kind = kind
        self.start = start
        self.end = end
        self.members = kw.get("members") or []
        self.elements = kw.get("elements") or []
        self.value = kw.get("value")
        self.open_pos = kw.get("open_pos")
        self.close_pos = kw.get("close_pos")
        self.dupes = kw.get("dupes") or {}
        self.comma_pos = kw.get("comma_pos")


class _Member:
    __slots__ = ("key", "key_pos", "value", "comma_pos")

    def __init__(self, key, key_pos, value):
        self.key = key
        self.key_pos = key_pos
        self.value = value
        self.comma_pos = None


_WS = " \t\r\n"
_LITERAL_RE = re.compile(r"(?:-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null)")


def _skip_trivia(text: str, i: int, n: int) -> int:
    """跳过空白、// 行注释与 /* */ 块注释。"""
    while i < n:
        c = text[i]
        if c in _WS:
            i += 1
            continue
        if c == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                j = i + 2
                while j < n and text[j] not in "\r\n":
                    j += 1
                i = j
                continue
            if nxt == "*":
                j = text.find("*/", i + 2)
                if j < 0:
                    raise JsoncStructureError("块注释未闭合")
                i = j + 2
                continue
        break
    return i


def _parse_string_token(text: str, i: int, n: int) -> _Node:
    j = i + 1
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == '"':
            break
        if c in "\r\n":
            raise JsoncStructureError("字符串中出现裸换行")
        j += 1
    if j >= n or text[j] != '"':
        raise JsoncStructureError("字符串未闭合")
    try:
        value = json.loads(text[i:j + 1])
    except ValueError as exc:
        raise JsoncStructureError(f"字符串无法解析：{exc}")
    return _Node("string", i, j + 1, value=value)


def _parse_value(text: str, i: int, n: int, path: str) -> _Node:
    i = _skip_trivia(text, i, n)
    if i >= n:
        raise JsoncStructureError(f"{path}：缺少值")
    c = text[i]
    if c == "{":
        return _parse_object(text, i, n, path)
    if c == "[":
        return _parse_array(text, i, n, path)
    if c == '"':
        return _parse_string_token(text, i, n)
    m = _LITERAL_RE.match(text, i)
    if not m:
        # JSONC 只有双引号字符串与 JSON 字面量；单引号等一律视为无法定位
        raise JsoncStructureError(f"{path}：无法识别的值（JSONC 只允许双引号字符串）")
    return _Node("scalar", i, m.end())


def _parse_object(text: str, i: int, n: int, path: str) -> _Node:
    open_pos = i
    i += 1
    members: list[_Member] = []
    dupes: dict[str, int] = {}
    while True:
        i = _skip_trivia(text, i, n)
        if i >= n:
            raise JsoncStructureError(f"{path}：对象未闭合")
        if text[i] == "}":
            return _Node("object", open_pos, i + 1, members=members,
                         open_pos=open_pos, close_pos=i, dupes=dupes)
        if text[i] != '"':
            raise JsoncStructureError(f"{path}：成员名必须是双引号字符串")
        key_node = _parse_string_token(text, i, n)
        i = _skip_trivia(text, key_node.end, n)
        if i >= n or text[i] != ":":
            raise JsoncStructureError(f"{path}：成员 “{key_node.value}” 缺少 ':'")
        value = _parse_value(text, i + 1, n, f"{path}.{key_node.value}")
        members.append(_Member(key_node.value, key_node.start, value))
        dupes[key_node.value] = dupes.get(key_node.value, 0) + 1
        i = _skip_trivia(text, value.end, n)
        if i < n and text[i] == ",":
            members[-1].comma_pos = i
            i += 1
            continue
        j = _skip_trivia(text, i, n)
        if j < n and text[j] == "}":
            return _Node("object", open_pos, j + 1, members=members,
                         open_pos=open_pos, close_pos=j, dupes=dupes)
        raise JsoncStructureError(f"{path}：成员之间缺少 ','")


def _parse_array(text: str, i: int, n: int, path: str) -> _Node:
    open_pos = i
    i += 1
    elements: list[_Node] = []
    while True:
        i = _skip_trivia(text, i, n)
        if i >= n:
            raise JsoncStructureError(f"{path}：数组未闭合")
        if text[i] == "]":
            return _Node("array", open_pos, i + 1, elements=elements,
                         open_pos=open_pos, close_pos=i)
        value = _parse_value(text, i, n, path)
        elements.append(value)
        i = _skip_trivia(text, value.end, n)
        if i < n and text[i] == ",":
            elements[-1].comma_pos = i
            i += 1
            continue
        j = _skip_trivia(text, i, n)
        if j < n and text[j] == "]":
            return _Node("array", open_pos, j + 1, elements=elements,
                         open_pos=open_pos, close_pos=j)
        raise JsoncStructureError(f"{path}：数组元素之间缺少 ','")


def parse_structure(text: str) -> _Node:
    n = len(text)
    i = _skip_trivia(text, 0, n)
    if i >= n:
        raise JsoncStructureError("配置为空")
    root = _parse_value(text, i, n, "$")
    j = _skip_trivia(text, root.end, n)
    if j != n:
        raise JsoncStructureError("根对象之后存在多余内容")
    if root.kind != "object":
        raise JsoncStructureError("顶层不是对象")
    return root


def unique_member(obj: _Node, key: str) -> _Member | None:
    hits = [m for m in obj.members if m.key == key]
    if len(hits) > 1:
        raise JsoncStructureError(
            f"重复的键 “{key}”（出现 {len(hits)} 次），无法确定引擎取值，拒绝修改")
    return hits[0] if hits else None


# ---------------------------------------------------------------------------
# 就地修改：只生成"文本拼接"计划，不重建整份文件
# ---------------------------------------------------------------------------


def _detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _detect_indent_unit(text: str) -> str:
    counts: dict[str, int] = {}
    for line in text.splitlines():
        stripped = line.lstrip(" \t")
        if not stripped.startswith('"'):
            continue
        ws = line[: len(line) - len(stripped)]
        if ws:
            counts[ws] = counts.get(ws, 0) + 1
    if not counts:
        return "  "
    return max(counts, key=lambda k: (counts[k], -len(k)))


def _member_line(key: str, literal: str, indent: str) -> str:
    return f"{indent}{json.dumps(key, ensure_ascii=False)}: {literal}"


def _object_literal(pairs, nl: str, inner_indent: str, close_indent: str) -> str:
    body = ("," + nl).join(_member_line(k, v, inner_indent) for k, v in pairs)
    return "{" + nl + body + nl + close_indent + "}"


def _entry_pairs(description: str, template: str):
    return [
        ("description", json.dumps(description, ensure_ascii=False)),
        ("template", json.dumps(template, ensure_ascii=False)),
    ]


def _insert_point(obj: _Node) -> int:
    return obj.members[-1].value.end if obj.members else obj.close_pos


def _line_indent(text: str, pos: int) -> str:
    """``pos`` 之前的行内空白；``pos`` 不在行首（前面还有其它内容）时返回空串。"""
    start = text.rfind("\n", 0, pos) + 1
    head = text[start:pos]
    return head if head.strip() == "" else ""


def _line_leading_indent(text: str, pos: int) -> str:
    """``pos`` 所在行的**行首缩进**（即使 ``pos`` 前面还有其它内容）。"""
    start = text.rfind("\n", 0, pos) + 1
    line = text[start:start + 512]
    stripped = line.lstrip(" \t")
    return line[: len(line) - len(stripped)]


def _insert_indent(text: str, obj: _Node) -> str:
    if obj.members:
        ind = _line_indent(text, obj.members[-1].key_pos)
        if ind:
            return ind
    return _line_leading_indent(text, obj.start) + _detect_indent_unit(text)


def _insert_member_src(text: str, obj: _Node, pairs) -> str:
    """构造"把若干成员插入 ``obj``"的文本片段，含必要的前导分隔逗号与换行。

    空对象要特别注意：第一个插入的成员不能带前导逗号，后续成员才带。
    """
    nl = _detect_newline(text)
    indent = _insert_indent(text, obj)
    if obj.members:
        return "".join("," + nl + _member_line(k, lit, indent) for k, lit in pairs)
    parts = [nl + _member_line(pairs[0][0], pairs[0][1], indent)]
    for key, literal in pairs[1:]:
        parts.append("," + nl + _member_line(key, literal, indent))
    parts.append(nl + _line_leading_indent(text, obj.close_pos))
    return "".join(parts)


def _build_command_literal(usage_desc, usage_tpl, open_desc, open_tpl,
                           text: str, member_indent: str, inner_indent: str) -> str:
    """构造一个完整的 command 对象字面量（含 usage 与 usage-open）。"""
    nl = _detect_newline(text)
    return _object_literal(
        [("usage", _object_literal(_entry_pairs(usage_desc, usage_tpl), nl, inner_indent, member_indent)),
         ("usage-open", _object_literal(_entry_pairs(open_desc, open_tpl), nl, inner_indent, member_indent))],
        nl, inner_indent, member_indent)


def _apply_splices(text: str, splices) -> str:
    if not splices:
        return text
    ordered = sorted(splices, key=lambda s: (s[0], s[1]))
    for prev, cur in zip(ordered, ordered[1:]):
        if cur[0] < prev[1]:
            raise JsoncStructureError("内部错误：修改区间重叠")
    out = text
    for start, end, new in sorted(splices, key=lambda s: (-s[0], -s[1])):
        out = out[:start] + new + out[end:]
    return out


def _plan_set_strings(text: str, obj: _Node, pairs) -> tuple[list, bool]:
    """在 ``obj`` 内更新/插入若干字符串成员。

    ``pairs`` 为 ``[(key, literal, comparable)]``：``comparable`` 用于判断"值其实没变"
    （避免因为转义形式不同而做无意义写盘）。
    """
    splices: list = []
    changed = False
    missing: list = []
    for key, literal, comparable in pairs:
        member = unique_member(obj, key)
        if member is None:
            missing.append((key, literal))
            continue
        value = member.value
        if value.kind in ("object", "array"):
            raise JsoncStructureError(f"成员 “{key}” 是 {value.kind}，拒绝覆盖用户内容")
        if value.kind == "string" and value.value == comparable:
            continue
        splices.append((value.start, value.end, literal))
        changed = True
    if missing:
        at = _insert_point(obj)
        splices.append((at, at, _insert_member_src(text, obj, missing)))
        changed = True
    return splices, changed


def _plan_member_removals(text: str, obj: _Node, indices) -> list:
    """移除对象成员；按连续区间整体切除，避免留下多余逗号。"""
    targets = sorted(set(indices))
    if not targets:
        return []
    runs = []
    run = [targets[0]]
    for idx in targets[1:]:
        if idx == run[-1] + 1:
            run.append(idx)
        else:
            runs.append(run)
            run = [idx]
    runs.append(run)

    members = obj.members
    total = len(members)
    splices = []
    for run in runs:
        first, last = run[0], run[-1]
        if first > 0:
            start = members[first - 1].comma_pos
            if start is None:
                raise JsoncStructureError("内部错误：无法定位分隔逗号")
        else:
            start = members[0].key_pos
            line_start = text.rfind("\n", 0, start) + 1
            if text[line_start:start].strip() == "":
                start = line_start
        if first == 0 and last < total - 1:
            comma = members[last].comma_pos
            if comma is None:
                raise JsoncStructureError("内部错误：无法定位分隔逗号")
            end = comma + 1
        else:
            end = members[last].value.end
        splices.append((start, end, ""))
    return splices


def _plan_element_removals(text: str, arr: _Node, indices) -> list:
    """移除数组元素；逻辑与对象成员一致。"""
    targets = sorted(set(indices))
    if not targets:
        return []
    runs = []
    run = [targets[0]]
    for idx in targets[1:]:
        if idx == run[-1] + 1:
            run.append(idx)
        else:
            runs.append(run)
            run = [idx]
    runs.append(run)

    elements = arr.elements
    total = len(elements)
    splices = []
    for run in runs:
        first, last = run[0], run[-1]
        if first > 0:
            start = elements[first - 1].comma_pos
            if start is None:
                raise JsoncStructureError("内部错误：无法定位分隔逗号")
        else:
            start = elements[0].start
            line_start = text.rfind("\n", 0, start) + 1
            if text[line_start:start].strip() == "":
                start = line_start
        if first == 0 and last < total - 1:
            comma = elements[last].comma_pos
            if comma is None:
                raise JsoncStructureError("内部错误：无法定位分隔逗号")
            end = comma + 1
        else:
            end = elements[last].end
        splices.append((start, end, ""))
    return splices


def _without_members(text: str, keys) -> str:
    """把指定顶层成员摘掉，用于"其余字节是否原样"的逐字节比较。

    摘除复用成员移除逻辑（连同相邻分隔逗号一起切除），因此"插入新成员"与
    "更新已有成员"两种情形在比较时都对齐到同一种形态。

    特殊情况：当该顶层对象**全部成员都是目标成员**（含空对象）时，整个对象的内容
    都在改动范围内，此时把对象整体折叠成哨兵——这种情况下文件里本来就没有别的
    东西需要保护。
    """
    root = parse_structure(text)
    idxs = [i for i, m in enumerate(root.members) if m.key in keys]
    if not root.members or len(idxs) == len(root.members):
        return text[:root.start] + "\x00" + text[root.end:]
    return _apply_splices(text, _plan_member_removals(text, root, idxs))


def _member_texts(text: str, path, skip) -> dict | None:
    """取某个对象下（``skip`` 之外）每个成员的原始文本，用于逐字节比较。"""
    obj = parse_structure(text)
    for name in path:
        member = unique_member(obj, name)
        if member is None or member.value.kind != "object":
            return None
        obj = member.value
    return {m.key: text[m.key_pos:m.value.end] for m in obj.members if m.key not in skip}


def _array_element_texts(text: str, name: str):
    root = parse_structure(text)
    member = unique_member(root, name)
    if member is None or member.value.kind != "array":
        return None
    return [text[e.start:e.end] for e in member.value.elements]


def verify_minimal_change(before: str, after: str, *,
                          ignore_top=(), ignore_in_command=(), purge_plugin=False) -> None:
    """断言"只改了目标区间"，否则抛错（绝不落盘）。

    这里独立于拼接计划重新解析两份文本并逐字节比较，因此能抓出"改动跑出目标
    区间"的实现 bug。三层检查：
      1. 除 ``command``（以及 purge 时的 ``plugin``）之外的顶层内容逐字节一致；
      2. ``command`` 里除本项目条目之外的成员逐字节一致（用户其它命令原样）；
      3. purge 时 ``plugin`` 数组里保留下来的元素逐字节一致。
    """
    if before == after:
        return
    ignore_top = set(ignore_top) | {"command"}
    if purge_plugin:
        ignore_top.add("plugin")
    if _without_members(before, ignore_top) != _without_members(after, ignore_top):
        raise JsoncStructureError("改动落在目标成员之外，拒绝落盘")
    before_cmds = _member_texts(before, ("command",), set(ignore_in_command))
    after_cmds = _member_texts(after, ("command",), set(ignore_in_command))
    if before_cmds is not None and after_cmds is not None and before_cmds != after_cmds:
        raise JsoncStructureError("command 下非目标成员被改动，拒绝落盘")
    if purge_plugin:
        before_elems = _array_element_texts(before, "plugin")
        after_elems = _array_element_texts(after, "plugin")
        if before_elems is not None and after_elems is not None:
            kept = [t for t in before_elems if "usage-dashboard.mjs" not in t]
            if kept != after_elems:
                raise JsoncStructureError("plugin 数组的非本项目元素被改动，拒绝落盘")

def plan_edit(text: str, *, usage_tpl: str, open_tpl: str,
              uninstall: bool = False, purge_plugin: bool = False):
    """生成就地修改计划，返回 ``(new_text, changed)``。

    所有拼接都只作用于 ``command.usage`` / ``command.usage-open`` 的文本片段
    （以及在 ``purge_plugin`` 时作用于 ``plugin`` 数组的对应元素）。
    """
    root = parse_structure(text)
    splices: list = []
    changed = False

    if purge_plugin:
        plugin = unique_member(root, "plugin")
        if plugin is not None:
            if plugin.value.kind != "array":
                raise JsoncStructureError("plugin 不是数组，拒绝就地修改")
            # 只清理**明确属于本项目旧版**的条目：必须同时命中旧插件文件名
            # usage-dashboard.mjs。仅凭 "usage-dashboard" 子串会把用户自己的
            # 插件（路径里恰好含这几个字）也误删，那不是本项目能确认归属的对象。
            victims = [i for i, e in enumerate(plugin.value.elements)
                       if e.kind == "string" and "usage-dashboard.mjs" in (e.value or "")]
            if victims:
                splices.extend(_plan_element_removals(text, plugin.value, victims))
                changed = True

    usage_desc = "MiMo Desktop 本机用量：生成 HTML 看板快照（Token / Session / 上下文）"
    open_desc = "MiMo Desktop 用量：打开可页内刷新实时看板（loopback 只读服务）"

    cmd_member = unique_member(root, "command")

    if cmd_member is None:
        if uninstall:
            return _apply_splices(text, splices), changed
        unit = _detect_indent_unit(text)
        indent = _insert_indent(text, root)
        literal = _build_command_literal(usage_desc, usage_tpl, open_desc, open_tpl,
                                         text, indent, indent + unit)
        at = _insert_point(root)
        splices.append((at, at, _insert_member_src(text, root, [("command", literal)])))
        return _apply_splices(text, splices), True

    if cmd_member.value.kind != "object":
        if uninstall:
            # 无对象可改：注销语义下这是"没有需要移除的键"
            return _apply_splices(text, splices), changed
        raise JsoncStructureError("command 不是对象，拒绝就地修改")

    cmd_obj = cmd_member.value
    pending: list = []
    for name, desc, tpl in (("usage", usage_desc, usage_tpl),
                            ("usage-open", open_desc, open_tpl)):
        member = unique_member(cmd_obj, name)
        if member is None:
            if uninstall:
                continue
            unit = _detect_indent_unit(text)
            indent = _insert_indent(text, cmd_obj)
            literal = _object_literal(_entry_pairs(desc, tpl), _detect_newline(text), indent + unit, indent)
            pending.append((name, literal))
            changed = True
            continue

        if member.value.kind != "object":
            if uninstall:
                splices.extend(_plan_member_removals(
                    text, cmd_obj, [cmd_obj.members.index(member)]))
                changed = True
                continue
            raise JsoncStructureError(f"command.{name} 不是对象，拒绝就地修改")

        if uninstall:
            splices.extend(_plan_member_removals(
                text, cmd_obj, [cmd_obj.members.index(member)]))
            changed = True
            continue

        sub_splices, sub_changed = _plan_set_strings(text, member.value, [
            ("description", json.dumps(desc, ensure_ascii=False), desc),
            ("template", json.dumps(tpl, ensure_ascii=False), tpl),
        ])
        splices.extend(sub_splices)
        changed = changed or sub_changed

    if pending:
        # 同一对象的多个插入必须合成一个拼接，否则相互不可见（会漏掉分隔逗号）
        at = _insert_point(cmd_obj)
        splices.append((at, at, _insert_member_src(text, cmd_obj, pending)))

    return _apply_splices(text, splices), changed


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
    """Build a live loopback dashboard and open it without interpolating untrusted args."""
    cmd = f"{q(py)} {q(script)} --open"
    return (
        "用户请求打开 MiMo Desktop 用量看板。下面一行是本地脚本的真实输出"
        "（脚本会复用/启动仅监听 127.0.0.1 的只读服务，写入 HTML 快照，并用默认浏览器打开）：\n\n"
        f"!`{cmd}`\n\n"
        "请把这一行原样告诉用户即可，不要臆造数字，不要调用其它工具。\n"
        "输出中的 http://127.0.0.1:... 地址就是可页内刷新的实时看板；不要改写成其它地址。"
    )


# ---------------------------------------------------------------------------
# 落盘：备份 + 原子写
# ---------------------------------------------------------------------------


def default_config_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".config", "mimocode", "mimocode.jsonc")


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


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_config_bytes(path: str) -> tuple[bytes, str]:
    """读原始字节并严格解码（不做 errors="replace"，避免静默损坏）。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        return raw, raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise JsoncStructureError(f"配置不是合法 UTF-8：{exc}")


def write_journal(path: str, payload: dict) -> None:
    try:
        directory = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(directory, exist_ok=True)
        write_atomically(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    except OSError:
        # 记录失败不应影响主流程；安装器会退化为"无 journal"的保守回滚
        pass


# ---------------------------------------------------------------------------
# Preflight：只读，绝不写盘
# ---------------------------------------------------------------------------


def classify_managed_file(path: str, manifest) -> str:
    """判断安装目录里的文件归属。

    返回 ``absent`` / ``owned`` / ``owned-modified`` / ``marker-only`` / ``unknown`` /
    ``unreadable``。**没有 manifest 时只能依靠文件内标记判断，绝不按文件名认领。**
    """
    if not os.path.exists(path):
        return "absent"
    try:
        digest = sha256_file(path)
    except OSError:
        return "unreadable"
    if isinstance(manifest, dict) and isinstance(manifest.get("files"), dict):
        rec = manifest["files"].get(os.path.basename(path))
        if isinstance(rec, dict) and rec.get("sha256"):
            return "owned" if rec["sha256"] == digest else "owned-modified"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(65536)
    except OSError:
        return "unreadable"
    return "marker-only" if OWNERSHIP_MARKER in head else "unknown"


def inspect_config(text: str) -> tuple[dict, int | None]:
    """检查配置能否安全就地修改。返回 ``(info, error_code)``。"""
    info = {
        "shape": "ok",
        "issues": [],
        "hasUsage": False,
        "hasUsageOpen": False,
        "usageLooksOurs": False,
        "usageOpenLooksOurs": False,
    }
    try:
        root = parse_structure(text)
    except JsoncStructureError as exc:
        return {**info, "shape": "unparseable", "issues": [str(exc)]}, EXIT_PARSE
    try:
        cmd = unique_member(root, "command")
    except JsoncStructureError as exc:
        return {**info, "shape": "duplicate-command", "issues": [str(exc)]}, EXIT_UNEDITABLE
    if cmd is None:
        return info, None
    if cmd.value.kind != "object":
        return {**info, "shape": "non-object-command",
                "issues": ["command 不是对象"]}, EXIT_UNEDITABLE
    for name, flag in (("usage", "hasUsage"), ("usage-open", "hasUsageOpen")):
        try:
            member = unique_member(cmd.value, name)
        except JsoncStructureError as exc:
            return {**info, "shape": f"duplicate-{name}", "issues": [str(exc)]}, EXIT_UNEDITABLE
        if member is None:
            continue
        if member.value.kind != "object":
            return {**info, "shape": f"non-object-{name}",
                    "issues": [f"command.{name} 不是对象"]}, EXIT_UNEDITABLE
        info[flag] = True
        template_value = None
        for sub in ("description", "template"):
            try:
                sub_member = unique_member(member.value, sub)
            except JsoncStructureError as exc:
                return {**info, "shape": f"duplicate-{name}-{sub}",
                        "issues": [str(exc)]}, EXIT_UNEDITABLE
            if sub_member is None:
                continue
            if sub_member.value.kind in ("object", "array"):
                return {**info, "shape": f"non-string-{name}-{sub}",
                        "issues": [f"command.{name}.{sub} 是 {sub_member.value.kind}"]}, EXIT_UNEDITABLE
            if sub == "template" and sub_member.value.kind == "string":
                template_value = sub_member.value.value or ""
        if template_value and "usage_cli.py" in template_value:
            info["usageLooksOurs" if name == "usage" else "usageOpenLooksOurs"] = True
    return info, None


def build_preflight_report(config_path: str, install_dir: str,
                           python_exe: str | None = None,
                           script_path: str | None = None) -> tuple[dict, int]:
    report = {
        "product": PRODUCT,
        "config": config_path,
        "installDir": install_dir,
        "upToDate": False,
        "configExists": os.path.exists(config_path),
        "configShape": "unknown",
        "configIssues": [],
        "hasCommandUsage": False,
        "hasCommandUsageOpen": False,
        "usageOwnedByUs": False,
        "usageOpenOwnedByUs": False,
        "manifestPresent": False,
        "manifest": None,
        "managedFiles": {},
        "requiresForce": False,
        "forceReasons": [],
        "mode": "fresh",
        "ownershipProven": False,
    }

    if not report["configExists"]:
        report["configShape"] = "missing"
        return report, EXIT_NOTFOUND

    try:
        _, text = read_config_bytes(config_path)
    except (OSError, JsoncStructureError) as exc:
        report["configShape"] = "unreadable"
        report["configIssues"] = [str(exc)]
        return report, EXIT_PARSE

    info, error = inspect_config(text)
    report["configShape"] = info["shape"]
    report["configIssues"] = info["issues"]
    report["hasCommandUsage"] = info["hasUsage"]
    report["hasCommandUsageOpen"] = info["hasUsageOpen"]
    report["usageOwnedByUs"] = info["usageLooksOurs"]
    report["usageOpenOwnedByUs"] = info["usageOpenLooksOurs"]
    if error is not None:
        return report, error

    # 命令是否已经就是本次要写的内容（用于让安装器跳过无意义的事务与备份增长）。
    # 复用真正的就地修改计划，而不是复制一份模板比较逻辑。
    if python_exe and script_path:
        try:
            usage_tpl = build_usage_template(os.path.abspath(python_exe), os.path.abspath(script_path))
            open_tpl = build_open_template(os.path.abspath(python_exe), os.path.abspath(script_path))
            _, would_change = plan_edit(text, usage_tpl=usage_tpl, open_tpl=open_tpl)
            report["upToDate"] = not would_change
            report["usageUpToDate"] = would_change is False
        except JsoncStructureError:
            report["upToDate"] = False

    manifest_path = os.path.join(install_dir, "install-manifest.json")
    manifest = None
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8-sig") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict) and loaded.get("product") == PRODUCT:
                manifest = loaded
                report["manifestPresent"] = True
                report["manifest"] = loaded
        except (OSError, ValueError):
            manifest = None

    statuses = {}
    for name in MANAGED_FILES:
        statuses[name] = classify_managed_file(os.path.join(install_dir, name), manifest)
    report["managedFiles"] = statuses

    reasons = []
    for name, status in statuses.items():
        if status in ("owned-modified",):
            reasons.append(f"{name} 已被修改（与 manifest 记录的 sha256 不一致）")
        elif status in ("unknown",):
            reasons.append(f"{name} 无 manifest 记录且不含本项目标记，归属不明")
        elif status in ("unreadable",):
            reasons.append(f"{name} 无法读取")
    if report["hasCommandUsage"] and not report["usageOwnedByUs"]:
        reasons.append("command.usage 已存在但不指向本项目脚本")
    if report["hasCommandUsageOpen"] and not report["usageOpenOwnedByUs"]:
        reasons.append("command.usage-open 已存在但不指向本项目脚本")

    # mode 只看"目录里有没有本项目命名的文件"，与"归属能否证明"分开表达：
    # 文件存在但归属未证明时，报 fresh 会让人以为目录是空的（真实误报过）。
    any_present = any(s != "absent" for s in statuses.values())
    report["ownershipProven"] = any(s in ("owned", "marker-only") for s in statuses.values())
    if any(s == "marker-only" for s in statuses.values()):
        report["mode"] = "adopt"
    elif any_present:
        report["mode"] = "upgrade"

    report["forceReasons"] = reasons
    report["requiresForce"] = bool(reasons)
    return report, (EXIT_NEEDS_FORCE if reasons else EXIT_OK)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


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
    parser.add_argument("--install-dir", default=None,
                        help="安装目录（仅 --preflight 用于判断文件归属）")
    parser.add_argument("--preflight", action="store_true",
                        help="只读检查既有安装与配置形态，输出 JSON，不写任何文件")
    parser.add_argument("--journal", default=None,
                        help="把本次执行记录（备份路径、写前/写后哈希）写入该 JSON 文件")
    parser.add_argument("--force", action="store_true",
                        help="--preflight：允许存在需要人工确认的既有状态")
    args = parser.parse_args(argv)

    if args.purge_legacy_plugin and args.uninstall:
        print("--purge-legacy-plugin 与 --uninstall 不能同时使用", file=sys.stderr)
        return EXIT_ARGS

    cfg_path = os.path.abspath(args.config)
    install_dir = os.path.abspath(args.install_dir) if args.install_dir \
        else os.path.dirname(cfg_path)

    if args.preflight:
        report, code = build_preflight_report(cfg_path, install_dir, args.python, args.script)
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        if code == EXIT_NEEDS_FORCE and args.force:
            code = EXIT_OK
        return code

    if not os.path.exists(cfg_path):
        print(f"CONFIG_NOT_FOUND {cfg_path}", file=sys.stderr)
        return EXIT_NOTFOUND

    try:
        raw, text = read_config_bytes(cfg_path)
    except OSError as exc:
        print(f"CONFIG_PARSE_FAILED: {exc}", file=sys.stderr)
        return EXIT_PARSE
    except JsoncStructureError as exc:
        print(f"CONFIG_PARSE_FAILED: {exc}", file=sys.stderr)
        return EXIT_PARSE

    try:
        stripped = json.loads(strip_jsonc(text))
    except ValueError as exc:
        print(f"CONFIG_PARSE_FAILED: {exc}", file=sys.stderr)
        return EXIT_PARSE
    if not isinstance(stripped, dict):
        print("CONFIG_PARSE_FAILED: 顶层不是对象", file=sys.stderr)
        return EXIT_PARSE

    py = os.path.abspath(args.python)
    script = os.path.abspath(args.script)
    if not args.uninstall and not os.path.exists(script):
        print(f"SCRIPT_NOT_FOUND {script}", file=sys.stderr)
        return EXIT_NOTFOUND

    usage_tpl = build_usage_template(py, script)
    open_tpl = build_open_template(py, script)

    try:
        candidate, changed = plan_edit(
            text, usage_tpl=usage_tpl, open_tpl=open_tpl,
            uninstall=args.uninstall, purge_plugin=args.purge_legacy_plugin)
        verify_minimal_change(
            text, candidate,
            ignore_top=({"plugin"} if args.purge_legacy_plugin else set()),
            ignore_in_command={"usage", "usage-open"},
            purge_plugin=args.purge_legacy_plugin)
    except JsoncStructureError as exc:
        print(f"CANNOT_EDIT_IN_PLACE: {exc}", file=sys.stderr)
        return EXIT_UNEDITABLE

    if not changed:
        if args.journal:
            write_journal(args.journal, {
                "product": PRODUCT,
                "config": cfg_path,
                "mode": "uninstall" if args.uninstall else "register",
                "changed": False,
                "inPlace": True,
                "preSha256": sha256_bytes(raw),
                "postSha256": sha256_bytes(raw),
                "backupPath": None,
            })
        print("already-unregistered" if args.uninstall else "already-registered")
        return EXIT_NOCHANGE

    # 写前回读校验：候选文本必须能解析，且 command 段与预期一致。
    try:
        reparsed = json.loads(strip_jsonc(candidate))
    except ValueError as exc:
        print(f"CANDIDATE_INVALID: {exc}", file=sys.stderr)
        return EXIT_WRITE
    if not isinstance(reparsed, dict):
        print("CANDIDATE_INVALID: 顶层不是对象", file=sys.stderr)
        return EXIT_WRITE
    if args.uninstall:
        cmds = reparsed.get("command")
        if isinstance(cmds, dict) and ("usage" in cmds or "usage-open" in cmds):
            print("CANDIDATE_INVALID: 注销后仍存在本项目命令", file=sys.stderr)
            return EXIT_WRITE
    else:
        cmds = reparsed.get("command")
        if not isinstance(cmds, dict):
            print("CANDIDATE_INVALID: 缺少 command 段", file=sys.stderr)
            return EXIT_WRITE
        for name, want in (("usage", usage_tpl), ("usage-open", open_tpl)):
            entry = cmds.get(name)
            if not isinstance(entry, dict) or entry.get("template") != want:
                print(f"CANDIDATE_INVALID: {name} 模板与预期不一致", file=sys.stderr)
                return EXIT_WRITE
        for key in stripped:
            if key == "command":
                continue
            if key == "plugin" and args.purge_legacy_plugin:
                continue
            if reparsed.get(key) != stripped.get(key):
                print(f"CANDIDATE_INVALID: 顶层键 “{key}” 被意外改动", file=sys.stderr)
                return EXIT_WRITE

    comment_count = jsonc_comment_count(text)

    backup_path = None
    if not args.no_backup:
        backup_path = unique_backup_path(cfg_path)
        try:
            copy_exclusive(cfg_path, backup_path)
            with open(backup_path, "rb") as fh:
                if fh.read() != raw:
                    print(f"BACKUP_VERIFY_FAILED {backup_path}", file=sys.stderr)
                    return EXIT_WRITE
        except OSError as exc:
            print(f"BACKUP_FAILED: {exc}", file=sys.stderr)
            return EXIT_WRITE

    if backup_path:
        print(f"backup    -> {backup_path}", file=sys.stderr)

    if comment_count:
        print(f"note      -> 原配置含 {comment_count} 处 JSONC 注释，"
              f"已就地保留（仅替换本项目的命令条目）", file=sys.stderr)

    # 原文件若带 UTF-8 BOM，则保持 BOM，避免给 PowerShell/编辑器带来意外变化。
    encoding = "utf-8-sig" if raw[:3] == b"\xef\xbb\xbf" else "utf-8"
    try:
        write_atomically(cfg_path, candidate, encoding=encoding)
    except OSError as exc:
        print(f"WRITE_FAILED: {exc}", file=sys.stderr)
        return EXIT_WRITE

    try:
        with open(cfg_path, "rb") as fh:
            post = fh.read()
    except OSError:
        post = candidate.encode("utf-8-sig" if encoding == "utf-8-sig" else "utf-8")

    if args.journal:
        write_journal(args.journal, {
            "product": PRODUCT,
            "config": cfg_path,
            "mode": "uninstall" if args.uninstall else "register",
            "changed": True,
            "inPlace": True,
            "preSha256": sha256_bytes(raw),
            "postSha256": sha256_bytes(post),
            "backupPath": backup_path,
        })

    print("unregistered" if args.uninstall else "registered")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
