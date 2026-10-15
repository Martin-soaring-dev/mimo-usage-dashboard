#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# mimo-usage-dashboard:managed-file:v1
"""installer.py — 安装 / 升级 / 修复 的事务引擎（唯一事实来源）

为什么要有这个文件
------------------
安装的事务性逻辑原本写在 `install.ps1` 里（772 行），带来三个结构性问题：

1. **npm CLI 无法复用**：Node 侧要么拉一个 PowerShell 子进程，要么把事务逻辑再实现
   一遍——而"两套逻辑必然漂移"在这个项目里已经真实发生过（`test/helpers.mjs` 曾手抄
   一份命令模板，与真实模板漂移后断言仍然全绿）。
2. **核心逻辑无法单元测试**：只能端起 PowerShell 子进程做端到端验证，慢、脆，还踩过
   "无 BOM 的 .ps1 被 ANSI 解码、中文 UTF-8 字节混出伪引号导致解析失败"这类环境坑。
3. **错误分类与回滚决策写在脚本里**，难以精确表达。

现在编排只在这里实现一次：

    install.ps1（Windows 解释器探测 + 转发）
    bin/mimo-usage.js（npm 入口）        ──►  installer.py  ──►  register_command.py

阶段
----
    Preflight -> Backup -> Stage -> Validate -> Apply -> Verify

任何阶段失败都走**同一条**回滚路径，且回滚只还原"本次运行真正动过的东西"：内容与本次
写入值不一致的一律不覆盖（那说明是安装之后的外部改动）。

输出与退出码
------------
* 所有人类可读输出都走 **stdout**（不走 stderr）。这是刻意的：调用方可能是
  `$ErrorActionPreference = "Stop"` 的 PowerShell，原生子进程往 stderr 写字会被提升为
  终止性错误。（机器可读输出等到 npm CLI 落地时再加，见 docs 的 M2。）
* 退出码：0 成功 / 1 失败 / 2 参数 / 3 配置无法解析 / 4 路径缺失 / 5 写入失败 /
  6 配置无法安全就地修改 / 7 存在需人工确认的既有状态 / 9 拒绝降级。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import register_command as rc  # noqa: E402

PRODUCT = "mimo-usage-dashboard"
MANAGED_FILES = ("usage_cli.py", "register_command.py")
MANIFEST_NAME = "install-manifest.json"
BACKUP_DIRNAME = ".mimo-usage-backups"
STAGE_PREFIX = ".mimo-usage-stage-"
STAGE_MAX_AGE_SECONDS = 3600
LEGACY_PATHS = ("skills\\usage", "skills\\mimo-usage-dashboard",
                "usage-dashboard.config.json", "plugin\\usage-dashboard.mjs")

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ARGS = 2
EXIT_PARSE = 3
EXIT_NOTFOUND = 4
EXIT_WRITE = 5
EXIT_UNEDITABLE = 6
EXIT_NEEDS_FORCE = 7
EXIT_DOWNGRADE_REFUSED = 9

MINIMUM_PYTHON = (3, 10)

# 服务身份校验要用的"已知视图"列表。刻意在这里定义而不是从 usage_cli.py 导入：
# 引擎可能面对的是**已安装的另一个版本**的 usage_cli.py，导入源码模块会引入版本错配；
# 而这是引擎与运行期之间的协议常量，必须由引擎自己持有。
KNOWN_KINDS = ("all", "token", "session", "context")


class InstallerError(Exception):
    """致命错误：中止并（如已进入事务）回滚。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def out(line: str = "") -> None:
    print(line, flush=True)


def sha256_file(path: str) -> str | None:
    if not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_py(exe: str, args: list[str]) -> tuple[int, str, str]:
    """运行子进程并取回 (退出码, stdout, stderr)，全部按 UTF-8 解码。

    不合并两个流：诊断信息的来源要可区分（退出码 + stdout 才是判定依据）。
    """
    try:
        proc = subprocess.run([exe, *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except OSError as exc:
        return 127, "", f"无法启动 {exe}：{exc}"
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


def version_key(text: str) -> tuple[int, int, int]:
    """把版本字符串转成可比较的数值三段组（忽略预发布后缀）。

    必须按数值段比较而不是字符串：字符串下 "0.2.0" > "0.10.0"，会把合法升级
    误判成降级、又把真正的降级放行。
    """
    core = str(text or "").split("-")[0]
    parts: list[int] = []
    for chunk in core.split(".")[:3]:
        m = re.match(r"\d+", chunk.strip())
        parts.append(int(m.group(0)) if m else 0)
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def compare_versions(a: str, b: str) -> int:
    ka, kb = version_key(a), version_key(b)
    return (ka > kb) - (ka < kb)


def python_version(exe: str) -> tuple[int, int] | None:
    if not exe or not os.path.isfile(exe):
        return None
    code, sout, serr = run_py(exe, ["--version"])
    text = sout or serr
    m = re.search(r"Python\s+(\d+)\.(\d+)", text or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def resolve_python(explicit: str) -> str:
    """定位解释器。**fail closed**：显式指定的绝不回退到别的解释器。"""
    if explicit:
        # 允许传裸命令名（如 `python` / `python3`）：按 PATH 解析到绝对路径。
        # 解析不到时仍然 fail closed —— 绝不回退到别的解释器。
        resolved = explicit
        if not os.path.isfile(resolved):
            found = shutil.which(resolved)
            if not found:
                raise InstallerError(
                    f"指定的 -Python 不存在：{explicit}（不会自动回退到其它解释器）")
            resolved = found
        ver = python_version(resolved)
        if ver is None:
            raise InstallerError(
                f"指定的 -Python 不受支持：{explicit}（无法识别为 Python）")
        if ver < MINIMUM_PYTHON:
            raise InstallerError(
                f"指定的 -Python 不受支持：{explicit}（探测到 '{ver[0]}.{ver[1]}'，"
                f"需要 >= {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]}）")
        return os.path.abspath(resolved)

    candidates: list[str] = []
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates += [os.path.join(local, "Programs", "Python", f"Python3{m}", "python.exe")
                        for m in (13, 12, 11, 10)]
    candidates += [rf"C:\Python3{m}\python.exe" for m in (13, 12, 11, 10)]
    for cand in candidates:
        if not cand:
            continue
        ver = python_version(cand)
        if ver and ver >= MINIMUM_PYTHON:
            return os.path.abspath(cand)
        if ver:
            out(f"skip      -> {cand} ({ver[0]}.{ver[1]} < {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]})")
        elif os.path.isfile(cand):
            out(f"skip      -> {cand} (无法识别为 Python)")
    raise InstallerError(
        f"未找到满足 >= {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]} 的 python.exe。"
        f"请安装 Python 3.10+，或用 -Python <path> 显式指定。")


def dir_writable(path: str) -> bool:
    """可写探测。会创建目标目录本身（这是安装的既定动作）。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, f".mimo-write-probe-{os.getpid()}")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("probe")
        os.unlink(probe)
        return True
    except OSError:
        return False


def product_version() -> str:
    pkg = os.path.join(HERE, "package.json")
    try:
        with open(pkg, encoding="utf-8-sig") as fh:
            ver = json.load(fh).get("version")
        return str(ver) if ver else "0.0.0"
    except (OSError, ValueError):
        return "0.0.0"


def atomic_write_text(path: str, text: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".mimo-atomic-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_manifest(path: str, version: str, cfg: str, py: str, hashes: dict) -> None:
    files = {}
    for name in sorted(hashes):
        p = os.path.join(os.path.dirname(path), name)
        files[name] = {
            "sha256": hashes[name],
            "bytes": os.path.getsize(p) if os.path.isfile(p) else 0,
        }
    payload = {
        "product": PRODUCT,
        "version": version,
        "installedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "python": py,
        "config": cfg,
        "files": files,
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def load_manifest(install_dir: str) -> dict | None:
    path = os.path.join(install_dir, MANIFEST_NAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) and data.get("product") == PRODUCT else None
    except (OSError, ValueError):
        return None


def ts_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def allocate_backup_dir(install_dir: str) -> str:
    """独占创建本次安装的备份目录，返回其路径。

    时间戳只作可读前缀；唯一性由 ``os.makedirs``（不带 ``exist_ok``）的原子性
    保证：同一秒内的连续两次安装不会落进同一目录，目录已存在时按 ``-1``、
    ``-2``… 重新分配。已存在的目录绝不复用——把文件写进去会覆盖上一次的备份。
    """
    base = os.path.join(install_dir, BACKUP_DIRNAME, ts_stamp())
    path = base
    n = 0
    while True:
        try:
            os.makedirs(path)
            return path
        except FileExistsError:
            n += 1
            path = f"{base}-{n}"


def force_rmtree(path: str) -> bool:
    """删除目录树并返回**是否真的删掉了**。

    `shutil.rmtree(path, ignore_errors=True)` 在 Windows 上删不掉只读文件，而
    ignore_errors 又抑制了错误 —— 结果是"清不掉"和"清掉了"返回值一模一样，调用方
    会照着打印成功。这里显式清一次只读位再删，并把最终存在性作为返回值交给调用方，
    让"清理失败"这件事可以被如实报告出来。
    """
    if not os.path.isdir(path):
        return True

    def onerror(func, target, _exc_info):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    try:
        shutil.rmtree(path, onerror=onerror)
    except OSError:
        pass
    return not os.path.exists(path)


# ---------------------------------------------------------------------------
# 运行时（数据目录）文件只读查询：状态文件 / 服务身份
# ---------------------------------------------------------------------------


def data_dir() -> str:
    env = os.environ.get("MIMO_USAGE_DATA_DIR")
    if env:
        return env
    if os.name == "nt":
        base = (os.environ.get("LOCALAPPDATA")
                or os.path.join(os.path.expanduser("~"), "AppData", "Local"))
    else:
        base = (os.environ.get("XDG_DATA_HOME")
                or os.path.join(os.path.expanduser("~"), ".local", "share"))
    return os.path.join(base, PRODUCT)


def state_path() -> str:
    return os.path.join(data_dir(), "live-server.json")


def valid_state_fields(st: dict):
    token, port = st.get("token"), st.get("port")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token):
        return None
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        return None
    return port, token


def looks_like_our_payload(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("kind") not in KNOWN_KINDS:
        return False
    gen = payload.get("generatedAt")
    if isinstance(gen, bool) or not isinstance(gen, int):
        return False
    return any(isinstance(payload.get(k), dict) for k in ("token", "session", "context"))


def service_status() -> dict:
    """只读服务身份报告。安装器自己实现一份，避免依赖被安装脚本的版本。"""
    status = {"statePresent": False, "stateValid": False, "reachable": False,
              "owned": False, "port": None, "pid": None, "tokenPrefix": None,
              "reason": "no_state"}
    path = state_path()
    if not os.path.isfile(path):
        return status
    status["statePresent"] = True
    try:
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        status["reason"] = "state_unreadable"
        return status
    if not isinstance(st, dict):
        status["reason"] = "state_not_object"
        return status
    fields = valid_state_fields(st)
    if fields is None:
        status["reason"] = "token_or_port_invalid"
        return status
    port, token = fields
    status["stateValid"] = True
    status["port"] = port
    status["tokenPrefix"] = token[:6]
    pid = st.get("pid")
    status["pid"] = pid if (isinstance(pid, int) and not isinstance(pid, bool)) else None
    url = f"http://127.0.0.1:{port}/{token}/api/report"
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=1.2) as resp:
            body = resp.read(1 << 20) if resp.status == 200 else b""
    except urllib.error.HTTPError as exc:
        status["reason"] = f"http_{exc.code}"
        return status
    except Exception as exc:
        status["reason"] = f"unreachable_{exc.__class__.__name__}"
        return status
    status["reachable"] = True
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        status["reason"] = "payload_not_json"
        return status
    if looks_like_our_payload(payload):
        status["owned"] = True
        status["reason"] = "ok"
    else:
        status["reason"] = "payload_not_ours"
    return status


# ---------------------------------------------------------------------------
# 事务上下文 + 回滚
# ---------------------------------------------------------------------------


class Context:
    def __init__(self, cfg: str, install_dir: str, backup_dir: str, stage_dir: str):
        self.cfg = cfg
        self.install_dir = install_dir
        self.backup_dir = backup_dir
        self.stage_dir = stage_dir
        self.manifest_path = os.path.join(install_dir, MANIFEST_NAME)
        self.overwritten: list[str] = []
        self.created: list[str] = []
        self.applied: dict[str, str] = {}
        self.manifest_existed = False
        self.config_journal: str | None = None


def rollback(ctx: Context) -> None:
    """只还原本次运行真正动过的东西；内容已被改动过的一律不覆盖。"""
    out("")
    out("  [rollback] 只回滚本次安装动过的文件；若内容已被改动则不覆盖（保护你的新修改）")
    manual: list[str] = []

    # 1) 配置：仅当当前字节 == 本次写入后的字节时才还原
    journal = ctx.config_journal
    if journal and os.path.isfile(journal):
        try:
            with open(journal, encoding="utf-8-sig") as fh:
                cj = json.load(fh)
        except (OSError, ValueError):
            cj = None
        if isinstance(cj, dict) and cj.get("changed"):
            if sha256_file(ctx.cfg) == cj.get("postSha256"):
                backup = cj.get("backupPath")
                if backup and os.path.isfile(backup):
                    try:
                        shutil.copy2(backup, ctx.cfg)
                        out(f"  [rollback] 配置已逐字节还原：{ctx.cfg}")
                    except OSError:
                        manual.append(f"配置还原失败，请手工复制：{backup} -> {ctx.cfg}")
                else:
                    manual.append(f"找不到配置备份，请人工检查：{ctx.cfg}")
            else:
                out("  [rollback] 配置在本次安装后又被改动过，未自动还原（避免覆盖你的新修改）")
                manual.append(f"如需恢复安装前的配置：{cj.get('backupPath')} -> {ctx.cfg}")

    # 2) 程序文件
    for name in MANAGED_FILES:
        target = os.path.join(ctx.install_dir, name)
        backup = os.path.join(ctx.backup_dir, name)
        expected = ctx.applied.get(name)
        exists = os.path.isfile(target)
        if name in ctx.overwritten:
            if not os.path.isfile(backup):
                manual.append(f"{name} 缺少备份，请人工恢复")
                continue
            if exists and expected and sha256_file(target) == expected:
                try:
                    os.replace(backup, target)
                    out(f"  [rollback] {name} 已还原为安装前版本")
                except OSError:
                    manual.append(f"{name} 还原失败，备份在：{backup}")
            else:
                out(f"  [rollback] {name} 在安装后又被改动过，未自动还原")
                manual.append(f"如需恢复：{backup} -> {target}")
        elif name in ctx.created:
            if exists:
                if expected and sha256_file(target) == expected:
                    try:
                        os.unlink(target)
                        out(f"  [rollback] {name} 是本次新建的，已移除")
                    except OSError:
                        manual.append(f"请人工确认是否删除：{target}")
                else:
                    out(f"  [rollback] {name} 是本次新建但随后被改动，保留不删")
                    manual.append(f"请人工确认是否删除：{target}")

    # 3) manifest
    mbackup = os.path.join(ctx.backup_dir, MANIFEST_NAME)
    if ctx.manifest_existed:
        if os.path.isfile(mbackup):
            try:
                shutil.copy2(mbackup, ctx.manifest_path)
                out("  [rollback] manifest 已还原为安装前版本")
            except OSError:
                manual.append(f"manifest 还原失败，备份在：{mbackup}")
    elif os.path.isfile(ctx.manifest_path):
        try:
            os.unlink(ctx.manifest_path)
            out("  [rollback] manifest 是本次新建的，已移除")
        except OSError:
            pass

    # 4) 暂存目录（备份目录一律保留）
    if os.path.isdir(ctx.stage_dir):
        if force_rmtree(ctx.stage_dir):
            out("  [rollback] 已清理暂存目录")
        else:
            # 绝不谎报成功：删不掉就说清楚，并交给下面的手工处理清单。
            out(f"  [rollback] 暂存目录未能清理（只读或被占用）：{ctx.stage_dir}")
            manual.append(f"暂存目录未能清理，请手工删除：{ctx.stage_dir}")

    if manual:
        out("  [rollback] 以下项目无法自动恢复，需人工处理：")
        for item in manual:
            out(f"              - {item}")
    else:
        out("  [rollback] 本次改动已全部还原。")
    out(f"  [rollback] 安装前的文件副本保留在：{ctx.backup_dir}")


# ---------------------------------------------------------------------------
# 各阶段
# ---------------------------------------------------------------------------


def preflight(cfg: str, install_dir: str, py: str, force: bool) -> tuple[dict, str]:
    """返回 (preflight 报告, mode 文本)。

    允许的副作用只有三种，都是后续阶段的必要前置：创建目标目录、写权限探测、
    以及用本项目协议优雅关闭正在运行的本项目服务（身份校验不过就中止，绝不强杀）。
    不安装任何文件、不改配置。
    """
    out("")
    out("-- Preflight --")

    if not os.path.isfile(cfg):
        raise InstallerError(f"MiMo 配置文件不存在：{cfg}（请先创建配置，或用 -Config <path> 指定）")

    rc_src = os.path.join(HERE, "register_command.py")
    if not os.path.isfile(rc_src):
        raise InstallerError(f"缺少 register_command.py：{rc_src}")

    args = [rc_src, "--preflight", "--config", cfg, "--install-dir", install_dir,
            "--python", py, "--script", os.path.join(install_dir, "usage_cli.py")]
    if force:
        args.append("--force")
    code, sout, serr = run_py(py, args)
    try:
        report = json.loads(sout)
    except ValueError:
        report = None

    if code == EXIT_NOTFOUND:
        raise InstallerError(f"MiMo 配置文件不存在：{cfg}")
    if code == EXIT_PARSE:
        raise InstallerError(f"配置文件无法解析（JSONC 结构不可安全修改）：{first_line(serr)}")
    if code == EXIT_UNEDITABLE:
        raise InstallerError(
            f"配置文件无法安全就地修改，已 fail closed（未写入任何内容）：{first_line(serr)}")
    if code == EXIT_NEEDS_FORCE:
        out("  检测到需要人工确认的既有状态：")
        for reason in (report or {}).get("forceReasons", []):
            out(f"    - {reason}")
        raise InstallerError(
            "安装已中止。请先确认这些文件/命令确实属于本项目，再显式加 -Force 重跑。")
    if code != 0 or report is None:
        raise InstallerError(f"preflight 失败（退出码 {code}）：{first_line(serr)}")

    mode_text = {
        "adopt": "adopt（无 manifest 但有本项目标记：备份后接管）",
        "upgrade": "upgrade（检测到既有安装目录，将先备份再替换）",
    }.get(report["mode"], "fresh（全新安装）")
    out(f"  mode        : {mode_text}")
    proven = report.get("ownershipProven")
    suffix = ""
    if not proven and report["mode"] != "fresh":
        suffix = "  ← 既有文件无法证明归属，需 -Force"
    out(f"  归属已证明  : {proven}{suffix}")
    out(f"  配置形态    : {report['configShape']}")
    out(f"  既有命令    : usage={report['hasCommandUsage']} usage-open={report['hasCommandUsageOpen']}")
    managed = ", ".join(f"{k}={v}" for k, v in sorted(report["managedFiles"].items()))
    out(f"  管理文件    : {managed}")
    if report.get("manifestPresent"):
        out(f"  已安装版本  : {(report.get('manifest') or {}).get('version')}")
    else:
        out("  manifest    : 不存在（旧文件只能检测，不会被删除；归属不明者需 -Force）")

    if not dir_writable(install_dir):
        raise InstallerError(f"安装目录不可写：{install_dir}（请检查权限或以合适的身份重跑）")
    out(f"  目录可写    : {install_dir}")

    cleanup_stale_stages(install_dir)
    print_residue(residue_report(install_dir, cfg))

    files_current = all(
        sha256_file(os.path.join(install_dir, name)) == sha256_file(os.path.join(HERE, name))
        for name in MANAGED_FILES)
    out(f"  文件已是最新: {files_current} ; 命令已是最新: {report['upToDate']}")
    report["filesCurrent"] = files_current

    handle_running_service(py)
    return report, mode_text


def cleanup_stale_stages(install_dir: str) -> None:
    """只清"本项目命名 + 已失效（>1h）"的暂存目录；名称不合规的一律保留。"""
    if not os.path.isdir(install_dir):
        return
    for name in sorted(os.listdir(install_dir)):
        if not name.startswith(STAGE_PREFIX):
            continue
        full = os.path.join(install_dir, name)
        if not os.path.isdir(full):
            continue
        stamp = name[len(STAGE_PREFIX):]
        if not re.fullmatch(r"\d{8}-\d{6}", stamp):
            out(f"  cleanup     : 保留 {name}（名称不符合本项目暂存目录格式，归属不明）")
            continue
        try:
            when = datetime.strptime(stamp, "%Y%m%d-%H%M%S").timestamp()
        except ValueError:
            out(f"  cleanup     : 保留 {name}（时间戳无法解析，归属不明）")
            continue
        if time.time() - when > STAGE_MAX_AGE_SECONDS:
            if force_rmtree(full):
                out(f"  cleanup     : 已清理失效暂存目录 {name}")
            else:
                out(f"  cleanup     : 未能清理 {name}（只读或被占用），保留待下次重试")
        else:
            out(f"  cleanup     : 保留 {name}（时间很新，可能是并发安装在使用）")


def handle_running_service(py: str) -> None:
    """身份三态全过才用本项目协议优雅关闭；否则绝不碰。"""
    status = service_status()
    if not status["statePresent"]:
        out("  服务检测    : 无运行中的本项目服务")
        return
    if status["stateValid"] and status["reachable"] and status["owned"]:
        out(f"  服务检测    : 检测到身份确认的本项目服务（port={status['port']}，"
            f"token={status['tokenPrefix']}…），正在优雅关闭并等待退出…")
        code, sout, serr = run_py(py, [os.path.join(HERE, "usage_cli.py"),
                                       "--shutdown", "--wait-seconds", "20"])
        if code != 0:
            raise InstallerError(
                f"无法优雅关闭正在运行的本项目服务（退出码 {code}）：{first_line(serr)}\n"
                f"本安装器不会按端口或 PID 强制结束进程。请手动关闭后重跑，或稍候重试。")
        out(f"  服务检测    : 已确认退出（{sout}）")
        return
    if status["stateValid"] and status["reachable"] and not status["owned"]:
        raise InstallerError(
            f"live-server.json 指向的监听者（port={status['port']}）不是本项目服务"
            f"（原因：{status['reason']}）。已拒绝继续，也不会强制结束任何进程。请人工确认后处理。")
    out(f"  服务检测    : 状态文件存在但服务不可达/无效（{status['reason']}），"
        f"视为陈旧状态，继续安装")
    out("                该状态文件不会被删除；下次 --open 会覆盖它。")


def backup(ctx: Context) -> None:
    out("")
    out("-- Backup --")
    os.makedirs(ctx.backup_dir, exist_ok=True)
    if os.path.isfile(ctx.manifest_path):
        ctx.manifest_existed = True
        shutil.copy2(ctx.manifest_path, os.path.join(ctx.backup_dir, MANIFEST_NAME))
    for name in MANAGED_FILES:
        src = os.path.join(ctx.install_dir, name)
        if os.path.isfile(src):
            dst = os.path.join(ctx.backup_dir, name)
            shutil.copy2(src, dst)
            if sha256_file(src) != sha256_file(dst):
                raise InstallerError(
                    f"备份校验失败：{name}（复制结果与原文件哈希不一致）。已中止，未改动任何文件。")
            ctx.overwritten.append(name)
            out(f"  backup      : {name}")
        else:
            ctx.created.append(name)
    out(f"  backupDir   : {ctx.backup_dir}")


def stage_and_validate(ctx: Context, cfg: str, py: str, remove_legacy: bool,
                       force: bool = False) -> None:
    out("")
    out("-- Stage / Validate --")
    os.makedirs(ctx.stage_dir, exist_ok=True)
    for name in MANAGED_FILES:
        shutil.copy2(os.path.join(HERE, name), os.path.join(ctx.stage_dir, name))
        ctx.applied[name] = sha256_file(os.path.join(ctx.stage_dir, name))
        out(f"  staged      : {name}  {ctx.applied[name][:12]}…")

    # usage_cli.py 的可用性检查必须用 selfcheck：它不读数据库、不写任何文件。
    code, sout, serr = run_py(py, [os.path.join(ctx.stage_dir, "usage_cli.py"), "selfcheck"])
    if code != 0:
        raise InstallerError(
            f"暂存文件自检失败：usage_cli.py selfcheck（退出码 {code}）{first_line(serr)}")
    code, sout2, serr2 = run_py(py, [os.path.join(ctx.stage_dir, "register_command.py"), "--help"])
    if code != 0:
        raise InstallerError(
            f"暂存文件自检失败：register_command.py --help（退出码 {code}）{first_line(serr2)}")
    out("  自检        : usage_cli.py selfcheck + register_command.py --help 均通过（未读写任何数据）")
    try:
        self_info = json.loads(sout)
    except ValueError:
        self_info = None
    if isinstance(self_info, dict) and not self_info.get("dbExists"):
        out(f"  note        : MiMo 数据库尚未创建（{self_info.get('dbPath')}）——不影响安装；")
        out("                MiMo Desktop 至少成功用过一次之后，/usage 才会有数据。")

    # 注册预演：在配置**副本**上跑一遍真实流程，形态问题在动真文件之前就暴露。
    rehearsal = os.path.join(ctx.stage_dir, "rehearsal")
    os.makedirs(rehearsal, exist_ok=True)
    reh_cfg = os.path.join(rehearsal, "mimocode.jsonc")
    shutil.copy2(cfg, reh_cfg)
    # 预演只验证"配置形态能否被安全就地修改"，所以 --script 用**暂存路径**：
    # 此刻最终路径还不存在（文件要到 Apply 才移动），而 register_command.py 会校验
    # --script 指向的文件存在。真正的模板路径在 Apply 阶段用安装目录下的那个。
    reh_args = [os.path.join(ctx.stage_dir, "register_command.py"),
                "--config", reh_cfg, "--python", py,
                "--script", os.path.join(ctx.stage_dir, "usage_cli.py"),
                "--journal", os.path.join(rehearsal, "journal.json")]
    if remove_legacy:
        reh_args.append("--purge-legacy-plugin")
    if force:
        # 用户已显式 -Force：预演必须拿到同一个开关，否则预演会因归属冲突
        # 以退出码 7 中止，正式 Apply 永远走不到 —— 表现为"-Force 也没用"。
        reh_args.append("--force")
    code, sout, serr = run_py(py, reh_args)
    if code not in (0, 1):
        raise InstallerError(
            f"注册预演失败（退出码 {code}）：{first_line(serr)}\n真实配置未被改动。")
    out(f"  注册预演    : OK（退出码 {code}）")
    force_rmtree(rehearsal)


def apply(ctx: Context, cfg: str, py: str, version: str, remove_legacy: bool,
          force: bool = False) -> None:
    """顺序刻意是 程序文件 -> manifest -> 配置（理由见文件头注释与下面对应注释）。"""
    out("")
    out("-- Apply --")

    # 5a. 程序文件：先就位 —— 配置里的模板要指向 <installdir>\usage_cli.py，
    #     注册阶段会校验该文件存在，所以脚本必须先落地。
    for name in MANAGED_FILES:
        os.replace(os.path.join(ctx.stage_dir, name), os.path.join(ctx.install_dir, name))
        out(f"  installed   : {name}")

    # 5b. manifest 紧跟文件写入：中断时"文件 + manifest"保持一致，下次 preflight 仍判 owned。
    write_manifest(ctx.manifest_path, version, cfg, py, ctx.applied)
    out(f"  manifest    : {ctx.manifest_path} (v{version})")

    # 5c. 配置最后写：即使中断，固定命令仍然可用（路径没变）。
    journal = os.path.join(ctx.backup_dir, "config-journal.json")
    args = [os.path.join(ctx.install_dir, "register_command.py"),
            "--config", cfg, "--python", py,
            "--script", os.path.join(ctx.install_dir, "usage_cli.py"),
            "--journal", journal]
    if remove_legacy:
        args.append("--purge-legacy-plugin")
    if force:
        # 与预演一致：归属冲突时 register_command.py 自己也会 fail closed，
        # 只有把用户的 -Force 透传下去才允许覆盖归属不明的既有命令。
        args.append("--force")
    code, sout, serr = run_py(py, args)
    if code not in (0, 1):
        raise InstallerError(
            f"注册 /usage 命令失败（register_command.py 退出码 {code}）：{first_line(serr)}")
    ctx.config_journal = journal
    if code == 1:
        out(f"  command     -> {cfg}  [already-registered]")
    else:
        out(f"  command     -> {cfg}  [{sout}]")


def verify(ctx: Context, cfg: str, py: str) -> None:
    out("")
    out("-- Verify --")
    errors: list[str] = []

    for name in MANAGED_FILES:
        if sha256_file(os.path.join(ctx.install_dir, name)) != ctx.applied[name]:
            errors.append(f"{name} 的哈希与暂存内容不一致")
    if not errors:
        out("  哈希校验    : 两个脚本与暂存内容一致")

    manifest_now = load_manifest(ctx.install_dir)
    if manifest_now is None:
        errors.append(f"{MANIFEST_NAME} 缺失或内容异常")
    else:
        for name in MANAGED_FILES:
            rec = (manifest_now.get("files") or {}).get(name)
            if not isinstance(rec, dict) or rec.get("sha256") != ctx.applied[name]:
                errors.append(f"manifest 中的 {name} 记录与磁盘不一致")
        if not errors:
            out(f"  manifest    : 已写入并自洽（v{manifest_now.get('version')}）")

    code, sout, serr = run_py(py, [os.path.join(ctx.install_dir, "register_command.py"),
                                   "--preflight", "--config", cfg,
                                   "--install-dir", ctx.install_dir,
                                   "--python", py,
                                   "--script", os.path.join(ctx.install_dir, "usage_cli.py")])
    try:
        rep = json.loads(sout)
    except ValueError:
        rep = None
    if code != 0 or rep is None:
        errors.append(f"安装后 preflight 复查失败（退出码 {code}）：{first_line(serr)}")
    else:
        if not (rep.get("hasCommandUsage") and rep.get("hasCommandUsageOpen")):
            errors.append("配置中缺少 command.usage / command.usage-open")
        if not (rep.get("usageOwnedByUs") and rep.get("usageOpenOwnedByUs")):
            errors.append("命令模板未指向本次安装的脚本路径")
        bad = {k: v for k, v in rep.get("managedFiles", {}).items() if v != "owned"}
        if bad:
            errors.append("manifest 归属校验未通过：" +
                          ", ".join(f"{k}={v}" for k, v in sorted(bad.items())))
        if not errors:
            out(f"  配置复查    : 两个命令已注册且指向 {ctx.install_dir}\\usage_cli.py")
            out("  归属校验    : manifest 与磁盘一致（owned）")

    code, sout, serr = run_py(py, [os.path.join(ctx.install_dir, "usage_cli.py"), "selfcheck"])
    if code != 0:
        errors.append(f"安装后的 usage_cli.py selfcheck 失败（退出码 {code}）{first_line(serr)}")
    else:
        out(f"  可运行性    : {ctx.install_dir}\\usage_cli.py selfcheck 通过（未读写任何数据）")

    if errors:
        out("")
        out("Verify 阶段未通过：")
        for err in errors:
            out(f"    - {err}")
        raise InstallerError("安装验证未通过：" + " / ".join(errors))


def legacy_cleanup(cfg: str, remove_legacy: bool, force: bool) -> None:
    out("")
    if not remove_legacy:
        out("Legacy files preserved (pass -RemoveLegacy only after reviewing them).")
        return
    mimo = os.path.dirname(cfg)
    out("-- Legacy 清理（显式 -RemoveLegacy）--")
    for legacy in LEGACY_PATHS:
        path = os.path.join(mimo, legacy)
        if not os.path.exists(path):
            continue
        if os.path.isdir(path):
            entries = []
            for root, _dirs, files in os.walk(path):
                entries += [os.path.join(root, f) for f in files]
            if entries and not force:
                out(f"  保留        : {path}（非空目录，含 {len(entries)} 个条目）")
                out("                未确认这些文件是否属于本项目，拒绝递归删除。内容如下：")
                for item in entries[:20]:
                    out(f"                  {item}")
                out("                确认要删请同时加 -Force。")
                continue
            if force_rmtree(path):
                out(f"  removed     : {path}（目录）")
            else:
                # 与下面的文件分支保持同一种诚实：删不掉就说删不掉。
                out(f"  保留        : {path}（目录删除失败：只读或被占用）")
        else:
            try:
                os.unlink(path)
                out(f"  removed     : {path}（文件）")
            except OSError as exc:
                out(f"  保留        : {path}（删除失败：{exc}）")


# ---------------------------------------------------------------------------
# 运行期残留体检（只枚举与判定，绝不删除）
# ---------------------------------------------------------------------------


def pid_alive(pid: int) -> bool:
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


def lock_status() -> dict:
    """`live-server.lock` 的存在性与持有者情况。只读，不回收。"""
    path = os.path.join(data_dir(), "live-server.lock")
    info = {"path": path, "present": os.path.exists(path), "holder": None, "holderAlive": None}
    if not info["present"]:
        return info
    holder = None
    try:
        with open(path, encoding="ascii", errors="ignore") as fh:
            lines = (fh.read() or "").strip().splitlines()
        if lines:
            holder = int(lines[0])
    except (OSError, ValueError):
        holder = None
    info["holder"] = holder
    if holder is not None:
        info["holderAlive"] = pid_alive(holder)
    return info


def residue_report(install_dir: str, cfg: str) -> dict:
    """安装前残留体检。分类只用于**提示**：本项目的东西会自动处理，其余一律只报告。"""
    res = {"staleStageDirs": [], "keptStageDirs": [], "backupDirs": [],
           "configBackups": [], "reports": [], "lock": lock_status()}
    if os.path.isdir(install_dir):
        for name in sorted(os.listdir(install_dir)):
            if name.startswith(STAGE_PREFIX) and os.path.isdir(os.path.join(install_dir, name)):
                res["keptStageDirs"].append(name)
        bdir = os.path.join(install_dir, BACKUP_DIRNAME)
        if os.path.isdir(bdir):
            res["backupDirs"] = sorted(os.listdir(bdir))
    cfgdir = os.path.dirname(cfg)
    if os.path.isdir(cfgdir):
        prefix = os.path.basename(cfg) + ".bak-"
        res["configBackups"] = sorted(n for n in os.listdir(cfgdir) if n.startswith(prefix))
    ddir = data_dir()
    if os.path.isdir(ddir):
        res["reports"] = sorted(n for n in os.listdir(ddir)
                                if n.startswith("mimo-usage-") and n.endswith(".html"))
    return res


def print_residue(res: dict) -> None:
    lock = res["lock"]
    if not lock["present"]:
        out("  残留·锁     : 无 live-server.lock")
    elif lock["holder"] is None:
        out("  残留·锁     : live-server.lock 存在但内容无法解析（只提示，不删除）")
    elif lock["holderAlive"]:
        out(f"  残留·锁     : live-server.lock 由存活进程 {lock['holder']} 持有（不删除）")
    else:
        out(f"  残留·锁     : live-server.lock 的持有者 {lock['holder']} 已退出"
            f"（失效锁，下次 --open 会自动回收）")
    out(f"  残留·暂存   : {len(res['keptStageDirs'])} 个（名称不合规或时间很新 → 保留）")
    out(f"  残留·备份   : 程序备份 {len(res['backupDirs'])} 份 · "
        f"配置备份 {len(res['configBackups'])} 份（一律不自动删除）")
    out(f"  残留·报告   : {len(res['reports'])} 份 mimo-usage-*.html（用户数据，不删除）")


# ---------------------------------------------------------------------------
# doctor：只读体检
# ---------------------------------------------------------------------------


def _symbol(errors: list, warnings: list, ok: bool, bad: bool, text: str) -> None:
    if ok:
        out(f"  OK    {text}")
    elif bad:
        errors.append(text)
        out(f"  ERROR {text}")
    else:
        warnings.append(text)
        out(f"  WARN  {text}")


def cmd_doctor(args) -> int:
    out("== MiMo Desktop Usage Dashboard - doctor ==")
    out(f"config    : {args.config}")
    out(f"installDir: {args.install_dir}")
    errors: list[str] = []
    warnings: list[str] = []
    out("")

    # 1) Python
    try:
        py = resolve_python(args.python)
        ver = python_version(py)
        _symbol(errors, warnings, True, False,
                f"Python {ver[0]}.{ver[1]}（{py}）")
    except InstallerError as exc:
        errors.append(str(exc))
        out(f"  ERROR {exc}")
        py = None

    # 2) 配置
    if not os.path.isfile(args.config):
        _symbol(errors, warnings, False, True, f"配置文件不存在：{args.config}")
        report, code = None, EXIT_NOTFOUND
    elif py is None:
        out("  SKIP  配置检查（没有可用的解释器）")
        report, code = None, None
    else:
        report, code = rc.build_preflight_report(
            args.config, args.install_dir, py, os.path.join(args.install_dir, "usage_cli.py"))
        shape_ok = code in (0, EXIT_NEEDS_FORCE)
        _symbol(errors, warnings, shape_ok, code in (EXIT_PARSE, EXIT_UNEDITABLE),
                f"配置可解析（形态 {report.get('configShape')}）")
        if report.get("configIssues"):
            for issue in report["configIssues"]:
                out(f"        {issue}")
        both = bool(report.get("hasCommandUsage") and report.get("hasCommandUsageOpen"))
        _symbol(errors, warnings, both, False,
                "已注册 /usage 与 /usage-open" if both else "尚未注册 /usage 或 /usage-open")
        if both:
            ours = bool(report.get("usageOwnedByUs") and report.get("usageOpenOwnedByUs"))
            _symbol(errors, warnings, ours, False,
                    "两个命令的模板都指向本项目脚本" if ours
                    else "命令已存在但模板不指向本项目脚本（归属不明）")

    # 3) 安装状态
    manifest = load_manifest(args.install_dir)
    if manifest is None:
        _symbol(errors, warnings, False, False,
                "没有 install-manifest.json（无法确认文件归属；旧安装只提示不删除）")
    else:
        _symbol(errors, warnings, True, False,
                f"manifest 存在：v{manifest.get('version')}"
                f"（记录 {len(manifest.get('files') or {})} 个文件）")
    if report is not None:
        st = report.get("managedFiles") or {}
        out("        文件归属：" + ", ".join(f"{k}={v}" for k, v in sorted(st.items())))
        if any(v == "owned-modified" for v in st.values()):
            _symbol(errors, warnings, False, False, "有受管文件已被修改（升级时需 -Force）")
        if any(v == "unknown" for v in st.values()):
            _symbol(errors, warnings, False, False, "有同名文件无法确认归属（不会被覆盖）")

    # 4) 版本对比
    pkg_version = product_version()
    if manifest is not None:
        installed = str(manifest.get("version") or "?")
        if installed == pkg_version:
            _symbol(errors, warnings, True, False, f"已安装版本与当前包一致（{installed}）")
        else:
            _symbol(errors, warnings, False, False,
                    f"已安装 {installed}，当前包 {pkg_version} → 可执行 install/upgrade")

    # 5) 运行期
    status = service_status()
    if not status["statePresent"]:
        _symbol(errors, warnings, True, False, "没有正在运行的本项目服务")
    elif status["stateValid"] and status["reachable"] and status["owned"]:
        _symbol(errors, warnings, True, False,
                f"本项目服务正在运行（port={status['port']}，token={status['tokenPrefix']}…）")
    elif status["stateValid"] and status["reachable"]:
        _symbol(errors, warnings, False, False,
                f"状态文件指向的监听者不是本项目服务（port={status['port']}）")
    else:
        _symbol(errors, warnings, False, False,
                f"状态文件存在但服务不可达（{status['reason']}）→ 陈旧状态，不影响安装")

    out("")
    out("-- 残留 --")
    print_residue(residue_report(args.install_dir, args.config))

    out("")
    if errors:
        out(f"结论：发现 {len(errors)} 个错误、{len(warnings)} 个警告。")
        for e in errors:
            out(f"  - {e}")
        return 2
    if warnings:
        out(f"结论：健康，有 {len(warnings)} 个警告。")
        for w in warnings:
            out(f"  - {w}")
        return 1
    out("结论：一切正常。")
    return EXIT_OK


# ---------------------------------------------------------------------------
# uninstall：注销 + 只删除能证明归属的文件
# ---------------------------------------------------------------------------


def cmd_uninstall(args) -> int:
    out("== MiMo Desktop Usage Dashboard - uninstall ==")
    out(f"config    : {args.config}")
    out(f"installDir: {args.install_dir}")

    py = resolve_python(args.python)
    manifest = load_manifest(args.install_dir)

    # 1) 先决定"删什么、留什么"。证据标准与 install 完全一致：
    #    manifest 记录的 sha256 命中 → owned；无 manifest 但文件内含归属标记 → marker-only。
    #    两者都不是 → 归属不明 → 永不删除，只报告。
    removable: list[str] = []
    kept: list[tuple] = []
    for name in MANAGED_FILES:
        path = os.path.join(args.install_dir, name)
        state = rc.classify_managed_file(path, manifest)
        if state == "absent":
            continue
        if state in ("owned", "marker-only"):
            removable.append(name)
        elif state == "owned-modified" and args.force:
            removable.append(name)
            kept.append((name, "已被你修改过，按 --force 一并删除（删除前已列入下方清单）"))
        else:
            kept.append((name, "归属无法确认或已被你修改，保留"))

    out("")
    out("-- 计划 --")
    out(f"  将注销      : command.usage / command.usage-open（只动这两个键）")
    if args.keep_files:
        out("  将删除      : 无（--keep-files：只注销，保留所有文件）")
    else:
        out(f"  将删除      : {', '.join(removable) if removable else '无'}")
    out("  将保留      : HTML 报告、数据库、未知文件、以及配置里的其它全部内容")
    for name, why in kept:
        out(f"                - {name}：{why}")

    # 2) 运行中的服务：只用自己的协议停，绝不强杀
    status = service_status()
    if status["statePresent"] and status["stateValid"] and status["reachable"] and status["owned"]:
        out("")
        out("  服务        : 检测到本项目服务，正在优雅关闭…")
        code, sout, serr = run_py(py, [os.path.join(HERE, "usage_cli.py"),
                                       "--shutdown", "--wait-seconds", "20"])
        if code == 0:
            out(f"  服务        : 已确认退出（{sout}）")
        else:
            out(f"  服务        : 未能优雅关闭（退出码 {code}）：{first_line(serr)}")
            out("                不会强制结束进程；请手动处理后再重试。")

    # 3) 注销（必须在删除脚本之前做，否则没有可执行的注销工具）
    reg = os.path.join(args.install_dir, "register_command.py")
    if not os.path.isfile(reg):
        reg = os.path.join(HERE, "register_command.py")
    out("")
    out("-- 注销命令 --")
    if not os.path.isfile(args.config):
        out(f"  SKIP  配置文件不存在：{args.config}（无需注销）")
    else:
        reg_args = [reg, "--config", args.config, "--uninstall"]
        if args.force:
            # 归属不明的既有 command.usage 默认不注销（register_command.py 会
            # fail closed），只有用户显式 --force 才允许移除。
            reg_args.append("--force")
        code, sout, serr = run_py(py, reg_args)
        if code not in (0, 1, EXIT_UNEDITABLE):
            out(f"  注销失败（退出码 {code}）：{first_line(serr)}")
            out("  已中止，未删除任何文件。")
            return EXIT_FAIL
        if code == EXIT_UNEDITABLE:
            out(f"  配置无法安全就地修改，已 fail closed（未写入）：{first_line(serr)}")
            out("  已中止，未删除任何文件。")
            return EXIT_FAIL
        out(f"  结果        : {sout or 'unregistered'}")

    # 4) 删除文件（--keep-files 时跳过）
    removed: list[str] = []
    out("")
    out("-- 删除文件 --")
    if args.keep_files:
        out("  SKIP  --keep-files：保留全部文件")
    elif not removable:
        out("  无需删除（没有能证明归属的文件）")
    else:
        for name in removable:
            path = os.path.join(args.install_dir, name)
            want = None
            if isinstance(manifest, dict):
                rec = (manifest.get("files") or {}).get(name)
                if isinstance(rec, dict):
                    want = rec.get("sha256")
            # 删除前再核对一次：manifest 有记录就必须命中，否则说明内容已变，保留。
            if want and sha256_file(path) != want and not args.force:
                out(f"  保留        : {name}（内容与 manifest 记录不一致，未删除）")
                continue
            try:
                os.unlink(path)
                removed.append(name)
                out(f"  removed     : {name}")
            except OSError as exc:
                out(f"  保留        : {name}（删除失败：{exc}）")

    # 5) manifest：只有在受管文件确实都被删掉之后才删
    mpath = os.path.join(args.install_dir, MANIFEST_NAME)
    if removed and os.path.isfile(mpath):
        try:
            os.unlink(mpath)
            out(f"  removed     : {MANIFEST_NAME}")
        except OSError as exc:
            out(f"  保留        : {MANIFEST_NAME}（删除失败：{exc}）")

    # 6) 备份：只在显式 --purge-backups 时清理，且**先列出**
    bdir = os.path.join(args.install_dir, BACKUP_DIRNAME)
    if args.purge_backups:
        out("")
        out("-- 清理备份（--purge-backups）--")
        targets = []
        if os.path.isdir(bdir):
            targets.append(bdir)
        cfgdir = os.path.dirname(args.config)
        if os.path.isdir(cfgdir):
            prefix = os.path.basename(args.config) + ".bak-"
            targets += [os.path.join(cfgdir, n) for n in sorted(os.listdir(cfgdir))
                        if n.startswith(prefix)]
        if not targets:
            out("  没有可清理的备份")
        for t in targets:
            out(f"  purge       : {t}")
            if os.path.isdir(t):
                if not force_rmtree(t):
                    out(f"  保留        : {t}（删除失败：只读或被占用）")
            else:
                try:
                    os.unlink(t)
                except OSError as exc:
                    out(f"  保留        : {t}（删除失败：{exc}）")
    elif os.path.isdir(bdir):
        out("")
        out(f"  保留        : {bdir}（备份默认保留；如确认要删请加 --purge-backups）")

    # 7) 安装目录：空了才删（绝不递归删未知内容）
    if not args.keep_files and os.path.isdir(args.install_dir):
        leftover = os.listdir(args.install_dir)
        if not leftover:
            try:
                os.rmdir(args.install_dir)
                out(f"  removed     : {args.install_dir}（已空）")
            except OSError:
                pass
        else:
            out(f"  保留        : {args.install_dir}（仍含 {len(leftover)} 个条目，未删除）")

    out("")
    out("已完成卸载。")
    out("  保留的用户数据：%LOCALAPPDATA%\\mimo-usage-dashboard\\mimo-usage-*.html 与 mimocode.db（只读，从未改动）")
    if kept:
        out("  以下文件因归属无法确认或已被修改而保留：")
        for name, why in kept:
            out(f"    - {name}：{why}")
    out("  提示：/usage 与 /usage-open 会在 MiMo 新会话中消失。")
    return EXIT_OK


# ---------------------------------------------------------------------------
# where：打印所有关键路径
# ---------------------------------------------------------------------------


def cmd_where(args) -> int:
    out(f"config      : {args.config}")
    out(f"configDir   : {os.path.dirname(args.config)}")
    out(f"installDir  : {args.install_dir}")
    out(f"engine      : {os.path.abspath(__file__)}")
    out(f"manifest    : {os.path.join(args.install_dir, MANIFEST_NAME)}")
    out(f"backupDir   : {os.path.join(args.install_dir, BACKUP_DIRNAME)}")
    out(f"dataDir     : {data_dir()}")
    out(f"stateFile   : {state_path()}")
    out(f"lockFile    : {os.path.join(data_dir(), 'live-server.lock')}")
    out(f"packageVer  : {product_version()}")
    manifest = load_manifest(args.install_dir)
    out(f"installedVer: {(manifest or {}).get('version') or '(未安装)'}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def cmd_install(args) -> int:
    out("== MiMo Desktop Usage Dashboard - install ==")
    out(f"config    : {args.config}")
    out(f"installDir: {args.install_dir}")

    py = resolve_python(args.python)
    ver = python_version(py)
    out(f"Python    : {py} ({ver[0]}.{ver[1]})")
    version = product_version()

    # 降级护栏刻意放在引擎里，而不是放在调用方：npm CLI 与 install.ps1 都是薄壳，
    # 只有放在这里，两条入口才会拿到同一个判定和同一个退出码。
    existing = load_manifest(args.install_dir)
    if existing is not None:
        installed = str(existing.get("version") or "")
        if installed and compare_versions(installed, version) > 0 and not args.allow_downgrade:
            out("")
            out(f"拒绝降级：已安装 {installed}，本次要安装的是 {version}。")
            out("如确实要降级，请显式加 --allow-downgrade"
                "（install.ps1 用 -AllowDowngrade）；会先备份，且备份会保留。")
            return EXIT_DOWNGRADE_REFUSED

    report, mode_text = preflight(args.config, args.install_dir, py, args.force)

    if args.preflight_only:
        out("")
        out("PreflightOnly：检测完成，未安装任何文件（命令注册与配置均未改动）。")
        return EXIT_OK

    already_current = bool(report.get("filesCurrent") and report.get("upToDate"))
    # 备份目录只在确有事务要跑时才分配：allocate_backup_dir 会**独占创建**目录，
    # 同一秒内的连续安装靠它拿到不同的目录（时间戳只作前缀，不作唯一性依据）。
    backup_dir = "" if already_current else allocate_backup_dir(args.install_dir)
    ctx = Context(args.config, args.install_dir, backup_dir,
                  os.path.join(args.install_dir, STAGE_PREFIX + ts_stamp()))

    if already_current:
        out("")
        out("-- 无需改动 --")
        out("  已是最新版本：程序文件与命令注册都与本次安装内容一致。")
        out("  跳过 Backup / Stage / Apply / Verify（不重装任何文件，也不新增备份）。")
        # 纯版本号发布：文件与命令都没变，但 manifest 的 version 必须跟上本次包
        # 版本。否则 doctor / npm CLI 会永远提示"可升级"，而再次 upgrade 又会
        # 走进这里跳过一切 —— 版本号永远停在旧值。这里只重写 manifest 这一个
        # 元数据文件，不碰任何程序文件、配置或用户数据。
        recorded = str((load_manifest(args.install_dir) or {}).get("version") or "")
        if recorded != version:
            hashes = {name: sha256_file(os.path.join(args.install_dir, name))
                      for name in MANAGED_FILES}
            try:
                write_manifest(os.path.join(args.install_dir, MANIFEST_NAME),
                               version, args.config, py, hashes)
            except OSError as exc:
                raise InstallerError(f"manifest 版本对齐失败：{exc}")
            out(f"  manifest    : 版本已对齐 v{version}"
                f"（此前 {recorded or '缺失'}；文件内容未变，未重装任何文件）")
    else:
        try:
            backup(ctx)
            stage_and_validate(ctx, args.config, py, args.remove_legacy, args.force)
            apply(ctx, args.config, py, version, args.remove_legacy, args.force)
            verify(ctx, args.config, py)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                out("")
                out("安装被中断。")
                rollback(ctx)
                raise
            out("")
            out(f"安装失败：{exc}")
            rollback(ctx)
            out("")
            out("本次安装未完成。请按上面的 [rollback] 提示处理后再重试。")
            return EXIT_FAIL
        if force_rmtree(ctx.stage_dir):
            out(f"  stage 清理  : 已移除 {ctx.stage_dir}")
        else:
            out(f"  stage 清理  : 未能移除 {ctx.stage_dir}（只读或被占用），请手工删除")

    legacy_cleanup(args.config, args.remove_legacy, args.force)

    out("")
    out("Done.")
    out(f"  Version      : {version} ({mode_text})")
    out(f"  Try it now   : & \"{py}\" \"{os.path.join(args.install_dir, 'usage_cli.py')}\" --open")
    out("  In MiMo      : open a NEW session and type /usage   (or /usage-open)")
    out("  With args    : the registered command never forwards arguments; run the CLI directly,")
    out(f"                 e.g. & \"{py}\" \"{os.path.join(args.install_dir, 'usage_cli.py')}\" token --days 7")
    out(f"  Python       : {py}")
    out(f"  Config       : {args.config}")
    out(f"  Manifest     : {os.path.join(args.install_dir, MANIFEST_NAME)}")
    if not already_current:
        out(f"  Backup       : {ctx.backup_dir}  (本次安装前的文件副本；不含用户数据)")
    out(f"  Uninstall    : & \"{py}\" "
        f"\"{os.path.join(args.install_dir, 'register_command.py')}\" "
        f"--config \"{args.config}\" --uninstall")
    out("")
    out("Note: /usage takes effect in a NEW session (the engine builds the command table at session start).")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="installer.py",
        description="MiMo Desktop Usage Dashboard — 安装/升级/卸载事务引擎")
    parser.add_argument("command", nargs="?", default="install",
                        choices=["install", "doctor", "uninstall", "where"],
                        help="要执行的动作（默认 install）")
    parser.add_argument("--config", default=rc.default_config_path(), help="mimocode.jsonc 路径")
    parser.add_argument("--install-dir", default=None,
                        help="脚本安装目录（默认 <配置目录>/usage-dashboard）")
    parser.add_argument("--python", default=sys.executable, help="目标 python.exe（默认当前解释器）")
    parser.add_argument("--force", action="store_true",
                        help="允许覆盖/删除归属不明或已被用户修改的既有文件")
    parser.add_argument("--remove-legacy", action="store_true",
                        help="install：清理旧版遗留文件（非空目录还需同时 --force）")
    parser.add_argument("--allow-downgrade", action="store_true",
                        help="install：已安装版本比本次要装的更新时，允许降级（会先备份）")
    parser.add_argument("--preflight-only", action="store_true",
                        help="install：只做检测并打印结论，不安装任何文件"
                             "（会创建安装目录，并可能用本项目协议关闭正在运行的本项目服务）")
    parser.add_argument("--keep-files", action="store_true",
                        help="uninstall：只注销命令，保留所有文件")
    parser.add_argument("--purge-backups", action="store_true",
                        help="uninstall：额外清理备份（会先列出再删）")
    return parser


# 每个动作只接受属于自己的开关；传了不属于本动作的开关就报错，
# 而不是静默忽略（静默忽略是"看起来做了、其实没做"的温床）。
COMMAND_ONLY_FLAGS = {
    "--remove-legacy": {"install"},
    "--preflight-only": {"install"},
    "--allow-downgrade": {"install"},
    "--keep-files": {"uninstall"},
    "--purge-backups": {"uninstall"},
}


def rejected_flags(args, argv) -> list[str]:
    given = [a for a in (argv or []) if a in COMMAND_ONLY_FLAGS]
    return sorted(f for f in given if args.command not in COMMAND_ONLY_FLAGS[f])


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    args = build_parser().parse_args(argv)
    args.config = os.path.abspath(args.config)
    if not args.install_dir:
        args.install_dir = os.path.join(os.path.dirname(args.config), "usage-dashboard")
    args.install_dir = os.path.abspath(args.install_dir)

    bad = rejected_flags(args, argv)
    if bad:
        out(f"参数错误：{args.command} 不接受 {' '.join(bad)}。")
        return EXIT_ARGS

    handler = {"install": cmd_install, "doctor": cmd_doctor,
               "uninstall": cmd_uninstall, "where": cmd_where}[args.command]
    try:
        return handler(args)
    except InstallerError as exc:
        out("")
        label = "安装失败" if args.command == "install" else f"{args.command} 失败"
        out(f"{label}：{exc}")
        return EXIT_FAIL
    except KeyboardInterrupt:
        out("")
        out("已取消。")
        return EXIT_FAIL
    except Exception as exc:  # noqa: BLE001
        # 兜底：安装器对用户永远不该打印 traceback。
        out("")
        out(f"失败：未预期的错误（{exc.__class__.__name__}）：{exc}")
        out("这属于安装器自身的缺陷，请把上面这一行连同复现步骤一起报告。")
        return EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
